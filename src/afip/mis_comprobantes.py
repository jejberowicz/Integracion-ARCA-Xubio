"""Fuente 'mis_comprobantes': portal ARCA "Mis Comprobantes" (Clave Fiscal).

Es la fuente para clientes que **no** emiten todo por Comprobantes en Línea
(típicamente los que tienen controlador fiscal): el CSV del portal es lo único
que los incluye.

Contrapartidas frente a WSFEv1:

* No hay API oficial. Se automatiza con Playwright sobre el portal, y el login
  con Clave Fiscal puede pedir captcha o segundo factor. Por eso el modo normal
  es **asistido**: si el login no puede completarse solo, se avisa y se puede
  importar el CSV bajado a mano con ``importar_csv``.
* El CSV no trae el desglose por alícuota de IVA: se infiere
  (ver :mod:`src.afip.inferencia`) y queda marcado como tal.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
import unicodedata
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

from src import config
from src.afip.base import ComprobanteNormalizado, FuenteVentas, registrar
from src.afip.inferencia import inferir_alicuotas
from src.database.models import Cliente
from src.security import crypto
from src.utils.helpers import formatear_cuit

log = logging.getLogger(__name__)

URL_LOGIN = "https://auth.afip.gob.ar/contribuyente_/login.xhtml"

# A Mis Comprobantes no se entra por URL: el servicio vive en fes.afip.gob.ar
# pero exige venir del portal con la sesión armada, así que se llega buscándolo
# en el buscador del portal. La URL directa devuelve 404.
BUSCADOR_PORTAL = "#buscadorInput"
NOMBRE_SERVICIO = "Mis Comprobantes"


class ErrorMisComprobantes(RuntimeError):
    pass


class LoginAsistidoRequerido(ErrorMisComprobantes):
    """El portal pidió captcha o 2FA: hace falta que intervenga una persona."""


# --- Normalización de encabezados del CSV ---

def _slug(texto: str) -> str:
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[^a-z0-9]+", "_", sin_tildes.lower()).strip("_")


# Encabezados reales del CSV de Mis Comprobantes. Se listan exactos porque
# varios se pisan por prefijo: "Imp. Neto Gravado Total" contra
# "Imp. Neto Gravado IVA 0%", o "Total IVA" contra "IVA 21%".
COLUMNAS = {
    "fecha": ("fecha_de_emision", "fecha_emision", "fecha"),
    "tipo_cbte": ("tipo_de_comprobante", "tipo_comprobante"),
    "pto_vta": ("punto_de_venta", "punto_venta"),
    "nro_desde": ("numero_desde", "nro_desde"),
    "nro_hasta": ("numero_hasta", "nro_hasta"),
    "cae": ("cod_autorizacion", "codigo_de_autorizacion", "cae"),
    "doc_tipo": ("tipo_doc_receptor", "tipo_doc"),
    "doc_nro": ("nro_doc_receptor", "nro_doc"),
    "receptor": ("denominacion_receptor", "denominacion"),
    "cotizacion": ("tipo_cambio", "tipo_de_cambio"),
    "moneda": ("moneda",),
    "neto": ("imp_neto_gravado_total", "imp_neto_gravado"),
    "no_gravado": ("imp_neto_no_gravado",),
    "exento": ("imp_op_exentas",),
    "tributos": ("otros_tributos", "imp_otros_tributos"),
    "iva": ("total_iva", "iva"),
    "total": ("imp_total",),
}

# El CSV trae el IVA discriminado en pares de columnas por alícuota:
#   "IVA 21%"  ->  importe    |  "Imp. Neto Gravado IVA 21%"  ->  base
# El porcentaje viene con coma decimal ("IVA 2,5%"), que el slug deja como
# "iva_2_5".
_COL_IVA = re.compile(r"^iva_(\d+(?:_\d+)?)$")
_COL_BASE = re.compile(r"^imp_neto_gravado_iva_(\d+(?:_\d+)?)$")


def _porcentaje_desde_slug(texto: str) -> Decimal:
    return Decimal(texto.replace("_", "."))


def _mapear_alicuotas(encabezados: list[str]) -> dict[Decimal, dict[str, int]]:
    """Ubica las columnas de importe y base de cada alícuota."""
    columnas: dict[Decimal, dict[str, int]] = {}
    for indice, encabezado in enumerate(encabezados):
        slug = _slug(encabezado or "")
        if (m := _COL_IVA.match(slug)):
            columnas.setdefault(_porcentaje_desde_slug(m.group(1)), {})["iva"] = indice
        elif (m := _COL_BASE.match(slug)):
            columnas.setdefault(_porcentaje_desde_slug(m.group(1)), {})["base"] = indice
    return columnas


def _mapear_encabezados(encabezados: list[str]) -> dict[str, int]:
    slugs = [_slug(h or "") for h in encabezados]
    mapa: dict[str, int] = {}

    # Primero todas las coincidencias exactas: si se mezclara con la búsqueda
    # por prefijo, "imp_neto_gravado" se quedaría con la columna del 0 %.
    for campo, alias in COLUMNAS.items():
        for a in alias:
            if a in slugs:
                mapa[campo] = slugs.index(a)
                break

    for campo, alias in COLUMNAS.items():
        if campo in mapa:
            continue
        for a in alias:
            candidatos = [i for i, s in enumerate(slugs) if s.startswith(a)]
            if candidatos:
                mapa[campo] = candidatos[0]
                break

    faltantes = {"fecha", "tipo_cbte", "pto_vta", "nro_desde", "total"} - mapa.keys()
    if faltantes:
        raise ErrorMisComprobantes(
            f"El CSV no tiene las columnas esperadas: faltan {sorted(faltantes)}. "
            f"Encabezados leídos: {encabezados}"
        )
    return mapa


def _detectar_delimitador(lineas: list[str]) -> str:
    """El portal alterna entre ';' y ',' según el navegador y el idioma.

    Se decide por el encabezado: es la única línea sin importes, así que el
    separador es simplemente el carácter que más aparece. ``csv.Sniffer`` no
    sirve acá porque los importes con coma decimal lo confunden.
    """
    encabezado = next((l for l in lineas if l.strip()), "")
    conteos = {sep: encabezado.count(sep) for sep in (";", "\t", ",")}
    mejor = max(conteos, key=lambda s: conteos[s])
    if conteos[mejor] == 0:
        raise ErrorMisComprobantes(
            f"No se pudo determinar el separador del CSV. Encabezado: {encabezado[:200]!r}"
        )
    return mejor


def _num(valor: str | None) -> Decimal:
    """Parsea importes del portal, que vienen con coma decimal."""
    if valor is None:
        return Decimal("0")
    texto = valor.strip().replace("$", "").replace(" ", "")
    if not texto:
        return Decimal("0")
    if "," in texto:  # formato 1.234,56
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return Decimal(texto)
    except InvalidOperation:
        log.warning("Importe ilegible en el CSV: %r", valor)
        return Decimal("0")


def _entero(valor: str | None) -> int | None:
    """Extrae el número inicial de celdas como '1 - Factura A' o '00003'."""
    if not valor:
        return None
    m = re.search(r"\d+", valor)
    return int(m.group()) if m else None


def _leer_texto(ruta: Path) -> str:
    """Devuelve el contenido del CSV, venga suelto o dentro de un ZIP.

    Mis Comprobantes exporta un .zip con el CSV adentro, aunque el navegador lo
    guarde con otro nombre.
    """
    crudo = ruta.read_bytes()
    if not crudo.startswith(b"PK"):
        return crudo.decode("utf-8-sig", errors="replace")

    with zipfile.ZipFile(io.BytesIO(crudo)) as zip_:
        internos = [n for n in zip_.namelist() if n.lower().endswith(".csv")] or zip_.namelist()
        if not internos:
            raise ErrorMisComprobantes(f"El ZIP descargado está vacío: {ruta}")
        log.debug("CSV dentro del ZIP: %s", internos[0])
        return zip_.read(internos[0]).decode("utf-8-sig", errors="replace")


# El portal escribe la moneda como símbolo, no con el código de AFIP.
MONEDAS = {"$": "PES", "ARS": "PES", "U$S": "DOL", "USD": "DOL", "US$": "DOL"}


def _moneda(valor: str | None) -> str:
    texto = (valor or "").strip()
    if not texto:
        return "PES"
    return MONEDAS.get(texto.upper(), MONEDAS.get(texto, texto[:3].upper()))


def _alicuotas_del_csv(fila: list[str], columnas: dict) -> list:
    """Arma el desglose de IVA a partir de las columnas por alícuota."""
    from src.afip.base import AlicuotaIVA
    from src.afip.tablas import ALICUOTAS_IVA

    por_porcentaje = {v: k for k, v in ALICUOTAS_IVA.items()}
    resultado = []

    def valor(indice: int | None) -> Decimal:
        # El 0 % trae columna de base pero no de importe, así que cada par
        # puede venir incompleto.
        if indice is None or indice >= len(fila):
            return Decimal("0")
        return _num(fila[indice])

    for porcentaje, indices in sorted(columnas.items()):
        base = valor(indices.get("base"))
        importe = valor(indices.get("iva"))
        if base == 0 and importe == 0:
            continue  # la alícuota no se usó en este comprobante

        alicuota_id = por_porcentaje.get(porcentaje)
        if alicuota_id is None:
            log.warning("Alícuota %s%% del CSV sin equivalente en AFIP", porcentaje)
            continue
        resultado.append(AlicuotaIVA(id=alicuota_id, base_imp=base, importe=importe))

    return resultado


def _fecha(valor: str) -> dt.date:
    texto = valor.strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d"):
        try:
            return dt.datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    raise ErrorMisComprobantes(f"Fecha con formato desconocido: {valor!r}")


@registrar
class MisComprobantes(FuenteVentas):
    nombre = "mis_comprobantes"

    def validar_cliente(self, cliente: Cliente) -> None:
        if not (cliente.clave_fiscal_usuario and cliente.clave_fiscal_password):
            raise ErrorMisComprobantes(
                f"{cliente.cuit}: la fuente 'mis_comprobantes' requiere usuario y "
                "password de Clave Fiscal."
            )

    def obtener_ventas(
        self,
        cliente: Cliente,
        desde: dt.date,
        hasta: dt.date,
    ) -> Iterator[ComprobanteNormalizado]:
        csv_path = self._csv_existente(cliente, desde, hasta)
        if csv_path is None:
            self.validar_cliente(cliente)
            csv_path = self.descargar_csv(cliente, desde, hasta)
        log.info("Leyendo comprobantes desde %s", csv_path)
        yield from self.importar_csv(csv_path, desde, hasta)

    # --- Lectura del CSV (independiente de cómo se haya bajado) ---

    def importar_csv(
        self,
        ruta: Path | str,
        desde: dt.date | None = None,
        hasta: dt.date | None = None,
    ) -> Iterator[ComprobanteNormalizado]:
        """Parsea un CSV de Mis Comprobantes, bajado por el bot o a mano."""
        ruta = Path(ruta)
        contenido = _leer_texto(ruta)
        lineas = contenido.splitlines()
        lector = csv.reader(lineas, delimiter=_detectar_delimitador(lineas))

        try:
            encabezados = next(lector)
        except StopIteration:
            raise ErrorMisComprobantes(f"El CSV está vacío: {ruta}") from None

        mapa = _mapear_encabezados(encabezados)
        alicuotas = _mapear_alicuotas(encabezados)

        for numero_linea, fila in enumerate(lector, start=2):
            if not any(celda.strip() for celda in fila):
                continue
            try:
                cbte = self._normalizar(fila, mapa, alicuotas)
            except ErrorMisComprobantes as exc:
                log.warning("Línea %s de %s ignorada: %s", numero_linea, ruta.name, exc)
                continue
            if desde and cbte.fecha < desde:
                continue
            if hasta and cbte.fecha > hasta:
                continue
            yield cbte

    def _normalizar(
        self,
        fila: list[str],
        mapa: dict[str, int],
        columnas_alicuota: dict | None = None,
    ) -> ComprobanteNormalizado:
        def celda(campo: str) -> str | None:
            idx = mapa.get(campo)
            if idx is None or idx >= len(fila):
                return None
            return fila[idx]

        tipo_cbte = _entero(celda("tipo_cbte"))
        pto_vta = _entero(celda("pto_vta"))
        nro = _entero(celda("nro_desde"))
        if tipo_cbte is None or pto_vta is None or nro is None:
            raise ErrorMisComprobantes("faltan tipo/punto de venta/número")

        neto = _num(celda("neto"))
        iva = _num(celda("iva"))

        # El CSV trae el IVA discriminado en columnas por alícuota, así que ese
        # dato es el bueno. Solo se infiere si el archivo no las tiene (formatos
        # viejos del portal). Que no haya ninguna alícuota con importe no es
        # falta de datos: en una Factura C no hay IVA que discriminar.
        columnas_alicuota = columnas_alicuota or {}
        inferidas = not columnas_alicuota
        if inferidas:
            alicuotas = inferir_alicuotas(neto, iva)
        else:
            alicuotas = _alicuotas_del_csv(fila, columnas_alicuota)

        cotizacion = _num(celda("cotizacion")) or Decimal("1")

        return ComprobanteNormalizado(
            tipo_cbte=tipo_cbte,
            pto_vta=pto_vta,
            nro=nro,
            fecha=_fecha(celda("fecha") or ""),
            imp_total=_num(celda("total")),
            imp_neto=neto,
            imp_iva=iva,
            imp_op_ex=_num(celda("exento")),
            imp_tot_conc=_num(celda("no_gravado")),
            imp_trib=_num(celda("tributos")),
            doc_tipo=_entero(celda("doc_tipo")),
            doc_nro=(celda("doc_nro") or "").strip() or None,
            receptor_nombre=(celda("receptor") or "").strip() or None,
            moneda=_moneda(celda("moneda")),
            cotizacion=cotizacion,
            cae=(celda("cae") or "").strip() or None,
            alicuotas_iva=alicuotas,
            alicuotas_inferidas=inferidas,
            fuente=self.nombre,
            raw={"csv": fila},
        )


    # --- Descarga automatizada del portal ---

    def _carpeta(self, cliente: Cliente) -> Path:
        carpeta = config.DOWNLOADS_PATH / cliente.cuit
        carpeta.mkdir(parents=True, exist_ok=True)
        return carpeta

    def ruta_csv(self, cliente: Cliente, desde: dt.date, hasta: dt.date) -> Path:
        return self._carpeta(cliente) / f"ventas_{desde:%Y%m%d}_{hasta:%Y%m%d}.csv"

    def _csv_existente(self, cliente: Cliente, desde: dt.date, hasta: dt.date) -> Path | None:
        """Reusa un CSV ya bajado para ese período (permite el flujo manual)."""
        esperado = self.ruta_csv(cliente, desde, hasta)
        return esperado if esperado.exists() else None

    def descargar_csv(
        self,
        cliente: Cliente,
        desde: dt.date,
        hasta: dt.date,
        headless: bool = True,
        explorar: bool = False,
    ) -> Path:
        """Baja el CSV de ventas del portal de ARCA con Playwright.

        La Clave Fiscal es siempre de una **persona física**: el CUIT de la
        empresa no sirve como usuario. Por eso ``clave_fiscal_usuario`` es el
        CUIT de quien entra (el contador, típicamente) y la empresa se elige
        después como representado. Un mismo usuario sirve para todos los
        clientes que lo hayan delegado.

        Con ``explorar=True`` no intenta descargar: recorre hasta Mis
        Comprobantes y guarda capturas y el HTML de cada paso, que es la forma
        de ajustar los selectores cuando el portal cambia.
        """
        try:
            from playwright.sync_api import TimeoutError as PWTimeout
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise ErrorMisComprobantes(
                "Falta Playwright. Instalar con:\n"
                "  pip install playwright && playwright install chromium"
            ) from exc

        usuario = cliente.clave_fiscal_usuario
        password = crypto.descifrar(cliente.clave_fiscal_password)
        if not (usuario and password):
            raise ErrorMisComprobantes(
                f"{cliente.cuit}: faltan usuario y clave fiscal. Recordá que el "
                "usuario es el CUIT de la persona que entra al portal, no el de "
                "la empresa."
            )

        destino = self.ruta_csv(cliente, desde, hasta)
        # El perfil es por usuario de login, no por cliente: la sesión sirve
        # para todas las empresas que esa persona representa.
        perfil = config.DOWNLOADS_PATH / ".perfiles" / str(usuario)
        perfil.mkdir(parents=True, exist_ok=True)
        diagnostico = Diagnostico(config.DOWNLOADS_PATH / ".debug" / str(cliente.cuit))

        with sync_playwright() as p:
            contexto = p.chromium.launch_persistent_context(
                user_data_dir=str(perfil),
                headless=headless,
                accept_downloads=True,
                downloads_path=str(self._carpeta(cliente)),
                viewport={"width": 1440, "height": 900},
            )
            pagina = contexto.pages[0] if contexto.pages else contexto.new_page()
            pagina.set_default_timeout(30_000)

            try:
                self._ir_al_portal(pagina, usuario, password, diagnostico)
                servicio = self._abrir_mis_comprobantes(pagina, diagnostico)
                self._elegir_empresa(servicio, cliente, diagnostico)

                if explorar:
                    diagnostico.volcar(servicio, "mis-comprobantes")
                    raise ExploracionTerminada(
                        "Exploración completa. Revisá los archivos en:\n"
                        f"  {diagnostico.carpeta}\n"
                        "Con eso se pueden ajustar los selectores de la descarga."
                    )

                return self._exportar(servicio, cliente, desde, hasta, destino, diagnostico)

            except (ExploracionTerminada, ErrorMisComprobantes):
                raise
            except PWTimeout as exc:
                diagnostico.volcar(pagina, "timeout")
                raise LoginAsistidoRequerido(
                    f"El portal no respondió a tiempo. Capturas en {diagnostico.carpeta}.\n"
                    f"Como alternativa, bajá el CSV a mano y guardalo en: {destino}"
                ) from exc
            except Exception as exc:
                diagnostico.volcar(pagina, "error")
                raise ErrorMisComprobantes(
                    f"Falló la descarga: {exc}\nCapturas en {diagnostico.carpeta}"
                ) from exc
            finally:
                contexto.close()

    # --- pasos ---

    def _ir_al_portal(self, pagina, usuario: str, password: str, diag: "Diagnostico") -> None:
        pagina.goto(URL_LOGIN, wait_until="domcontentloaded")

        # Con la sesión del perfil todavía viva, ARCA redirige solo al portal.
        if "login" not in pagina.url:
            log.info("Sesión reutilizada del perfil, no hace falta loguearse")
            return

        log.info("Ingresando con Clave Fiscal del usuario %s", usuario)
        pagina.fill("#F1\\:username", str(usuario))
        pagina.click("#F1\\:btnSiguiente")
        pagina.wait_for_load_state("networkidle")

        if pagina.query_selector("#F1\\:password") is None:
            diag.volcar(pagina, "login-sin-password")
            texto = pagina.inner_text("body")[:200].replace("\n", " ")
            raise ErrorMisComprobantes(
                f"El portal no pidió la contraseña. Respondió: {texto!r}. "
                "Verificá que el usuario sea el CUIT de una persona física."
            )

        pagina.fill("#F1\\:password", password)
        pagina.click("#F1\\:btnIngresar")
        pagina.wait_for_load_state("networkidle")

        if "login" in pagina.url:
            diag.volcar(pagina, "login-fallido")
            raise LoginAsistidoRequerido(
                "El login no se completó: puede ser la clave equivocada, un "
                "captcha o el segundo factor.\n"
                f"Mirá {diag.carpeta} y, si hace falta, corré con --headed para "
                "resolverlo a mano una vez (la sesión queda guardada en el perfil)."
            )
        log.info("Login OK")

    def _abrir_mis_comprobantes(self, pagina, diag: "Diagnostico"):
        """Abre el servicio desde el buscador del portal y devuelve su pestaña."""
        log.info("Abriendo %s desde el portal", NOMBRE_SERVICIO)
        pagina.fill(BUSCADOR_PORTAL, NOMBRE_SERVICIO)
        pagina.wait_for_timeout(2500)  # el buscador filtra por JS mientras se tipea

        enlace = pagina.locator("a.dropdown-item", has_text=NOMBRE_SERVICIO).first
        if enlace.count() == 0:
            diag.volcar(pagina, "sin-servicio")
            raise ErrorMisComprobantes(
                f"El buscador del portal no encontró '{NOMBRE_SERVICIO}'. "
                "Probablemente el usuario no tenga adherido el servicio: se "
                "agrega desde el Administrador de Relaciones."
            )

        # El servicio se abre en una pestaña nueva.
        try:
            with pagina.context.expect_page(timeout=30_000) as nueva:
                enlace.click()
            servicio = nueva.value
        except Exception:
            servicio = pagina  # por si algún día deja de abrir pestaña

        servicio.wait_for_load_state("networkidle")
        servicio.set_default_timeout(30_000)
        log.info("Servicio abierto en %s", servicio.url)
        return servicio

    def _elegir_empresa(self, servicio, cliente: Cliente, diag: "Diagnostico") -> None:
        """Elige el contribuyente entre los que representa el usuario.

        La primera pantalla del servicio lista un panel por representado. Se
        busca por CUIT formateado, que es unívoco, y no por razón social, que
        puede estar escrita distinto en ARCA que en nuestra base.
        """
        cuit_formateado = formatear_cuit(cliente.cuit)
        panel = servicio.locator("a.panel", has_text=cuit_formateado).first

        if panel.count() == 0:
            diag.volcar(servicio, "sin-representado")
            disponibles = [
                t.strip().replace("\n", " ")
                for t in servicio.locator("a.panel").all_inner_texts()
            ]
            raise ErrorMisComprobantes(
                f"El usuario {cliente.clave_fiscal_usuario} no representa a "
                f"{cuit_formateado} en Mis Comprobantes.\n"
                f"Representa a: {disponibles}\n"
                "La empresa tiene que delegarle el servicio en su Administrador "
                "de Relaciones."
            )

        log.info("Entrando como %s", cuit_formateado)
        panel.click()
        servicio.wait_for_load_state("networkidle")

    def _exportar(
        self, servicio, cliente: Cliente, desde: dt.date, hasta: dt.date,
        destino: Path, diag: "Diagnostico",
    ) -> Path:
        """Filtra los comprobantes emitidos del período y baja el CSV."""
        log.info("Consultando emitidos del %s al %s", desde, hasta)
        servicio.click("#btnEmitidos")
        servicio.wait_for_load_state("networkidle")

        # El rango va en un solo campo (daterangepicker), no en dos.
        rango = f"{desde:%d/%m/%Y} - {hasta:%d/%m/%Y}"
        servicio.fill("#fechaEmision", rango)
        servicio.keyboard.press("Escape")  # cierra el calendario emergente

        servicio.click("#buscarComprobantes")
        servicio.wait_for_load_state("networkidle")
        servicio.wait_for_timeout(2000)

        boton = _boton_csv(servicio)
        if boton is None:
            diag.volcar(servicio, "sin-csv")
            texto = servicio.inner_text("body")[:200].replace("\n", " ")
            raise ErrorMisComprobantes(
                "No apareció el botón de exportar a CSV. Puede que no haya "
                f"comprobantes en el período. La página dice: {texto!r}. "
                f"Diagnóstico en {diag.carpeta}"
            )

        with servicio.expect_download(timeout=120_000) as espera:
            boton.click()
        espera.value.save_as(str(destino))
        log.info("CSV guardado en %s", destino)
        return destino


def _boton_csv(pagina):
    """Encuentra el botón de exportar a CSV entre las variantes del portal."""
    for selector in ("#btnCsv", "#exportarCsv", "a[href*='csv']", "button[id*='sv']"):
        elemento = pagina.locator(selector).first
        if elemento.count() > 0:
            return elemento
    porTexto = pagina.get_by_text(re.compile(r"\bcsv\b", re.I)).first
    return porTexto if porTexto.count() > 0 else None


class ExploracionTerminada(ErrorMisComprobantes):
    """Se llegó al final del modo --explorar; no es un fallo."""


class Diagnostico:
    """Guarda capturas y HTML para poder ajustar los selectores."""

    def __init__(self, carpeta: Path):
        self.carpeta = carpeta

    def volcar(self, pagina, etiqueta: str, con_html: bool = False) -> None:
        """Guarda el inventario de elementos y, opcionalmente, el HTML crudo.

        El ``.txt`` es lo único pensado para compartir: describe la estructura
        de la página sin valores. El HTML completo queda afuera salvo que se
        pida expresamente, porque después del login contiene tokens de sesión y
        datos fiscales del contribuyente.
        """
        try:
            self.carpeta.mkdir(parents=True, exist_ok=True)
            marca = dt.datetime.now().strftime("%H%M%S")
            base = self.carpeta / f"{marca}_{etiqueta}"

            (base.with_suffix(".txt")).write_text(
                f"URL: {_url_sin_sesion(pagina.url)}\n\n{_inventario(pagina)}",
                encoding="utf-8",
            )
            pagina.screenshot(path=f"{base}.png", full_page=True)
            if con_html:
                (base.with_suffix(".html")).write_text(pagina.content(), encoding="utf-8")

            log.info("Diagnóstico guardado en %s.txt (y .png)", base)
        except Exception as exc:  # pragma: no cover - el diagnóstico no debe romper
            log.warning("No se pudo guardar el diagnóstico: %s", exc)


def _url_sin_sesion(url: str) -> str:
    """Saca el jsessionid y cualquier token del querystring."""
    url = re.sub(r";jsessionid=[^?#]*", ";jsessionid=<oculto>", url)
    return re.sub(r"([?&](token|sign|password|clave)=)[^&]*", r"\1<oculto>", url, flags=re.I)


# Atributos que pueden traer datos del contribuyente o de la sesión.
_ATRIBUTOS_SENSIBLES = ("value",)


def _inventario(pagina) -> str:
    """Describe los elementos con los que se puede interactuar en la página.

    Pensado para compartir: se listan identificadores y tipos, que es lo que
    hace falta para escribir selectores, pero **no** los valores, que en un
    portal fiscal son datos del contribuyente. Los campos de contraseña se
    marcan sin tocar su contenido.
    """
    lineas = []
    for selector in ("input", "select", "button", "a"):
        for el in pagina.query_selector_all(selector)[:80]:
            datos = {
                k: el.get_attribute(k)
                for k in ("id", "name", "type", "href", "class", "placeholder")
            }
            datos = {k: v for k, v in datos.items() if v}

            # Se informa si el campo tiene contenido, no cuál es.
            if selector == "input":
                valor = el.get_attribute("value")
                if valor:
                    datos["value"] = "<con contenido>"
                if (datos.get("type") or "").lower() == "password":
                    datos["value"] = "<contraseña>"

            texto = (el.inner_text() or "").strip()[:60] if selector != "input" else ""
            if datos or texto:
                lineas.append(f"<{selector}> {texto!r} {datos}")
    return "\n".join(lineas)


def _completar_fecha(pagina, nombres: tuple[str, ...], fecha: dt.date) -> None:
    """Completa un campo de fecha probando varios nombres y formatos."""
    for nombre in nombres:
        for selector in (f"#{nombre}", f"[name='{nombre}']", f"[id*='{nombre}']"):
            campo = pagina.query_selector(selector)
            if campo is None:
                continue
            for formato in ("%d/%m/%Y", "%Y-%m-%d"):
                try:
                    campo.fill(fecha.strftime(formato))
                    return
                except Exception:
                    continue
    log.warning("No se encontró el campo de fecha %s", nombres[0])
