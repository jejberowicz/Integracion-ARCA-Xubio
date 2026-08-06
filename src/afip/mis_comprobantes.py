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

    # --- Descarga automatizada ---

    def _carpeta(self, cliente: Cliente) -> Path:
        carpeta = config.DOWNLOADS_PATH / cliente.cuit
        carpeta.mkdir(parents=True, exist_ok=True)
        return carpeta

    def _csv_existente(self, cliente: Cliente, desde: dt.date, hasta: dt.date) -> Path | None:
        """Reusa un CSV ya bajado para ese período (permite el flujo manual)."""
        esperado = self._carpeta(cliente) / f"ventas_{desde:%Y%m%d}_{hasta:%Y%m%d}.csv"
        return esperado if esperado.exists() else None

    def descargar_csv(self, cliente: Cliente, desde: dt.date, hasta: dt.date) -> Path:
        """Baja el CSV de ventas del portal con Playwright.

        El login con Clave Fiscal no es determinístico (captcha, 2FA), así que se
        usa un perfil de navegador persistente por CUIT: una vez que la sesión se
        estableció a mano, las corridas siguientes suelen entrar solas.
        """
        try:
            from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise ErrorMisComprobantes(
                "Falta Playwright. Instalar con:\n"
                "  pip install playwright && playwright install chromium"
            ) from exc

        destino = self._carpeta(cliente) / f"ventas_{desde:%Y%m%d}_{hasta:%Y%m%d}.csv"
        perfil = config.DOWNLOADS_PATH / ".perfiles" / cliente.cuit
        perfil.mkdir(parents=True, exist_ok=True)

        password = crypto.descifrar(cliente.clave_fiscal_password)

        with sync_playwright() as p:
            contexto = p.chromium.launch_persistent_context(
                user_data_dir=str(perfil),
                headless=False,  # el login con Clave Fiscal necesita ser visible
                accept_downloads=True,
            )
            pagina = contexto.pages[0] if contexto.pages else contexto.new_page()
            try:
                pagina.goto(URL_LOGIN, wait_until="domcontentloaded")

                # Si la sesión persistente sigue viva, el portal redirige solo.
                if "login" in pagina.url:
                    self._login(pagina, cliente.clave_fiscal_usuario, password)

                pagina.goto(URL_MIS_COMPROBANTES, wait_until="domcontentloaded")

                raise LoginAsistidoRequerido(
                    "El login quedó resuelto, pero la navegación dentro de Mis "
                    "Comprobantes (elegir 'Emitidos', el rango de fechas y "
                    "'Exportar CSV') depende del layout actual del portal y no está "
                    "automatizada.\n"
                    "Bajá el CSV a mano y guardalo como:\n"
                    f"  {destino}\n"
                    "La próxima corrida lo va a levantar automáticamente."
                )
            except PWTimeout as exc:
                raise LoginAsistidoRequerido(
                    f"El portal no respondió a tiempo para {cliente.cuit}. "
                    f"Bajá el CSV a mano y guardalo como: {destino}"
                ) from exc
            finally:
                contexto.close()

    @staticmethod
    def _login(pagina, usuario: str, password: str) -> None:
        """Completa el formulario de Clave Fiscal en dos pasos (CUIT y clave)."""
        pagina.fill("#F1\\:username", usuario)
        pagina.click("#F1\\:btnSiguiente")
        pagina.wait_for_selector("#F1\\:password", timeout=15_000)
        pagina.fill("#F1\\:password", password)
        pagina.click("#F1\\:btnIngresar")
        pagina.wait_for_load_state("networkidle", timeout=30_000)
