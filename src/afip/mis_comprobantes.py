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
import logging
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

from src import config
from src.afip.base import ComprobanteNormalizado, FuenteVentas, registrar
from src.afip.inferencia import inferir_alicuotas
from src.database.models import Cliente
from src.security import crypto

log = logging.getLogger(__name__)

URL_LOGIN = "https://auth.afip.gob.ar/contribuyente_/login.xhtml"
URL_MIS_COMPROBANTES = "https://serviciosjava2.afip.gob.ar/mis-comprobantes/"


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


# Cada campo interno con los fragmentos de encabezado que lo identifican.
# El portal cambia los títulos cada tanto, así que se busca por subcadena.
COLUMNAS = {
    "fecha": ("fecha_de_emision", "fecha_emision", "fecha"),
    "tipo_cbte": ("tipo_de_comprobante", "tipo_comprobante", "tipo"),
    "pto_vta": ("punto_de_venta", "punto_venta"),
    "nro_desde": ("numero_desde", "nro_desde", "numero"),
    "nro_hasta": ("numero_hasta", "nro_hasta"),
    "cae": ("cod_autorizacion", "codigo_de_autorizacion", "cae"),
    "doc_tipo": ("tipo_doc_receptor", "tipo_doc"),
    "doc_nro": ("nro_doc_receptor", "nro_doc", "numero_doc"),
    "receptor": ("denominacion_receptor", "denominacion"),
    "cotizacion": ("tipo_cambio", "tipo_de_cambio"),
    "moneda": ("moneda",),
    "neto": ("imp_neto_gravado", "neto_gravado"),
    "no_gravado": ("imp_neto_no_gravado", "neto_no_gravado"),
    "exento": ("imp_op_exentas", "op_exentas", "exento"),
    "iva": ("iva",),
    "tributos": ("otros_tributos", "imp_otros_tributos"),
    "total": ("imp_total", "total"),
}


def _mapear_encabezados(encabezados: list[str]) -> dict[str, int]:
    slugs = [_slug(h) for h in encabezados]
    mapa: dict[str, int] = {}
    for campo, alias in COLUMNAS.items():
        for alias_slug in alias:
            # Coincidencia exacta primero, después por prefijo.
            if alias_slug in slugs:
                mapa[campo] = slugs.index(alias_slug)
                break
            coincidencias = [i for i, s in enumerate(slugs) if s.startswith(alias_slug)]
            if coincidencias:
                mapa[campo] = coincidencias[0]
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
        contenido = ruta.read_text(encoding="utf-8-sig", errors="replace")
        lineas = contenido.splitlines()
        lector = csv.reader(lineas, delimiter=_detectar_delimitador(lineas))

        try:
            encabezados = next(lector)
        except StopIteration:
            raise ErrorMisComprobantes(f"El CSV está vacío: {ruta}") from None

        mapa = _mapear_encabezados(encabezados)

        for numero_linea, fila in enumerate(lector, start=2):
            if not any(celda.strip() for celda in fila):
                continue
            try:
                cbte = self._normalizar(fila, mapa)
            except ErrorMisComprobantes as exc:
                log.warning("Línea %s de %s ignorada: %s", numero_linea, ruta.name, exc)
                continue
            if desde and cbte.fecha < desde:
                continue
            if hasta and cbte.fecha > hasta:
                continue
            yield cbte

    def _normalizar(self, fila: list[str], mapa: dict[str, int]) -> ComprobanteNormalizado:
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
        alicuotas = inferir_alicuotas(neto, iva)

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
            moneda=(celda("moneda") or "PES").strip()[:3].upper() or "PES",
            cotizacion=cotizacion,
            cae=(celda("cae") or "").strip() or None,
            alicuotas_iva=alicuotas,
            alicuotas_inferidas=True,
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
                self._elegir_representado(pagina, cliente, diagnostico)
                self._abrir_mis_comprobantes(pagina, diagnostico)

                if explorar:
                    diagnostico.volcar(pagina, "mis-comprobantes")
                    raise ExploracionTerminada(
                        "Exploración completa. Revisá los archivos en:\n"
                        f"  {diagnostico.carpeta}\n"
                        "Con eso se pueden ajustar los selectores de la descarga."
                    )

                return self._exportar(pagina, cliente, desde, hasta, destino, diagnostico)

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

    def _elegir_representado(self, pagina, cliente: Cliente, diag: "Diagnostico") -> None:
        """Cambia al CUIT de la empresa si el usuario entra como persona física."""
        if str(cliente.clave_fiscal_usuario) == cliente.cuit:
            return  # entra directamente como el contribuyente

        log.info("Seleccionando representado %s", cliente.cuit)
        try:
            pagina.click("#represented-selector, .representado, [id*='representado']", timeout=5000)
            pagina.fill("input[type='search'], #buscadorRepresentado", cliente.cuit, timeout=5000)
            pagina.keyboard.press("Enter")
            pagina.wait_for_load_state("networkidle")
        except Exception:
            # El selector de representado cambia seguido; no es fatal, porque
            # muchas veces el servicio permite elegir la empresa más adelante.
            diag.volcar(pagina, "representado")
            log.warning(
                "No se pudo cambiar de representado automáticamente. "
                "Capturas en %s", diag.carpeta
            )

    def _abrir_mis_comprobantes(self, pagina, diag: "Diagnostico") -> None:
        log.info("Abriendo Mis Comprobantes")
        pagina.goto(URL_MIS_COMPROBANTES, wait_until="domcontentloaded")
        pagina.wait_for_load_state("networkidle")

    def _exportar(
        self, pagina, cliente: Cliente, desde: dt.date, hasta: dt.date,
        destino: Path, diag: "Diagnostico",
    ) -> Path:
        """Filtra por 'Emitidos' y el rango de fechas, y baja el CSV."""
        log.info("Filtrando emitidos del %s al %s", desde, hasta)

        for texto in ("Emitidos", "Ventas", "Comprobantes emitidos"):
            enlace = pagina.get_by_text(texto, exact=False).first
            if enlace.count() > 0:
                enlace.click()
                pagina.wait_for_load_state("networkidle")
                break

        _completar_fecha(pagina, ("fechaEmisionDesde", "fechaDesde", "desde"), desde)
        _completar_fecha(pagina, ("fechaEmisionHasta", "fechaHasta", "hasta"), hasta)

        boton_buscar = pagina.get_by_role("button", name=re.compile("buscar", re.I)).first
        if boton_buscar.count() > 0:
            boton_buscar.click()
            pagina.wait_for_load_state("networkidle")

        with pagina.expect_download(timeout=120_000) as espera:
            exportar = pagina.get_by_text(re.compile("csv", re.I)).first
            if exportar.count() == 0:
                diag.volcar(pagina, "sin-boton-csv")
                raise ErrorMisComprobantes(
                    "No se encontró el botón de exportar a CSV. "
                    f"Capturas en {diag.carpeta}"
                )
            exportar.click()

        espera.value.save_as(str(destino))
        log.info("CSV guardado en %s", destino)
        return destino


class ExploracionTerminada(ErrorMisComprobantes):
    """Se llegó al final del modo --explorar; no es un fallo."""


class Diagnostico:
    """Guarda capturas y HTML para poder ajustar los selectores."""

    def __init__(self, carpeta: Path):
        self.carpeta = carpeta

    def volcar(self, pagina, etiqueta: str) -> None:
        try:
            self.carpeta.mkdir(parents=True, exist_ok=True)
            marca = dt.datetime.now().strftime("%H%M%S")
            base = self.carpeta / f"{marca}_{etiqueta}"
            pagina.screenshot(path=f"{base}.png", full_page=True)
            (base.with_suffix(".html")).write_text(pagina.content(), encoding="utf-8")
            (base.with_suffix(".txt")).write_text(
                f"URL: {pagina.url}\n\n{_inventario(pagina)}", encoding="utf-8"
            )
            log.info("Diagnóstico guardado en %s.*", base)
        except Exception as exc:  # pragma: no cover - el diagnóstico no debe romper
            log.warning("No se pudo guardar el diagnóstico: %s", exc)


def _inventario(pagina) -> str:
    """Lista los elementos con los que se puede interactuar en la página."""
    lineas = []
    for selector in ("input", "select", "button", "a"):
        for el in pagina.query_selector_all(selector)[:80]:
            datos = {
                k: el.get_attribute(k)
                for k in ("id", "name", "type", "value", "href", "class", "placeholder")
            }
            datos = {k: v for k, v in datos.items() if v}
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
