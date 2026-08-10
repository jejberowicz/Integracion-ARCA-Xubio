"""Cliente HTTP de la API de Xubio, con credenciales por cliente contable.

Cada empresa de Xubio tiene su propio par client_id/secret, así que hay una
instancia (y un token) por cliente, nunca uno global.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from src import config
from src.database.models import Cliente, Comprobante
from src.security import crypto
from src.xubio import mapeo

log = logging.getLogger(__name__)

# Rutas de la API. Verificar contra la documentación de la cuenta antes de
# operar en producción: Xubio cambió los nombres entre versiones.
RUTA_COMPROBANTE_VENTA = "/comprobanteVentaBean"
RUTA_CLIENTES = "/clienteBean"
RUTA_PUNTOS_VENTA = "/puntoVentaBean"
# Xubio no expone un recurso de productos: el catálogo se deduce de los
# comprobantes ya cargados (ver XubioClient.productos_conocidos).
RUTA_PRODUCTOS = RUTA_COMPROBANTE_VENTA

REINTENTOS = 3
ESPERA_REINTENTO = 2  # segundos, se duplica en cada intento


class ErrorXubio(RuntimeError):
    pass


class ErrorAutenticacionXubio(ErrorXubio):
    pass


class XubioClient:
    def __init__(self, cliente: Cliente):
        if not (cliente.xubio_client_id and cliente.xubio_secret_id):
            raise ErrorXubio(
                f"{cliente.cuit}: faltan las credenciales de Xubio "
                "(xubio_client_id / xubio_secret_id)."
            )
        self.cliente = cliente
        self.client_id = cliente.xubio_client_id
        self.secret = crypto.descifrar(cliente.xubio_secret_id)
        self.base = config.XUBIO_API_BASE
        self.sesion = requests.Session()

        self._token: str | None = None
        self._token_expira: dt.datetime | None = None
        self._por_cuit: dict[str, Any] | None = None
        self._puntos_venta: dict[int, Any] | None = None
        self._existentes: dict[str, Any] | None = None
        self._desde_cuando: str | None = None

    # --- Autenticación ---

    def _token_vigente(self) -> bool:
        return bool(
            self._token
            and self._token_expira
            and dt.datetime.now(dt.timezone.utc) < self._token_expira
        )

    def autenticar(self) -> str:
        if self._token_vigente():
            return self._token  # type: ignore[return-value]

        log.info("Autenticando en Xubio para %s", self.cliente.cuit)
        # POST con el grant_type como form-data y las credenciales en el header
        # Basic. Con GET el endpoint devuelve 200 y cuerpo vacío en vez de un
        # error, y mandar las credenciales en el cuerpo da 400.
        respuesta = self.sesion.post(
            config.XUBIO_TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=HTTPBasicAuth(self.client_id, self.secret),
            timeout=config.XUBIO_TIMEOUT,
        )
        if respuesta.status_code != 200:
            raise ErrorAutenticacionXubio(
                f"Xubio rechazó las credenciales de {self.cliente.cuit} "
                f"(HTTP {respuesta.status_code}): {respuesta.text[:300]}"
            )
        try:
            datos = respuesta.json()
        except ValueError as exc:
            raise ErrorAutenticacionXubio(
                "Xubio respondió algo que no es JSON al pedir el token. "
                f"Revisá XUBIO_TOKEN_URL ({config.XUBIO_TOKEN_URL}). "
                f"Respuesta: {respuesta.text[:200]!r}"
            ) from exc
        token = datos.get("access_token")
        if not token:
            raise ErrorAutenticacionXubio(f"Respuesta de token inesperada: {datos}")

        # Se renueva un minuto antes de vencer.
        expira_en = int(datos.get("expires_in", 3600))
        self._token = token
        self._token_expira = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            seconds=max(expira_en - 60, 60)
        )
        return token

    # --- HTTP ---

    def _request(self, metodo: str, ruta: str, **kwargs) -> Any:
        url = f"{self.base}{ruta}"
        ultimo_error: Exception | None = None

        for intento in range(1, REINTENTOS + 1):
            headers = {
                "Authorization": f"Bearer {self.autenticar()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            try:
                respuesta = self.sesion.request(
                    metodo, url, headers=headers, timeout=config.XUBIO_TIMEOUT, **kwargs
                )
            except requests.RequestException as exc:
                ultimo_error = exc
                log.warning("Error de red contra Xubio (intento %s): %s", intento, exc)
                time.sleep(ESPERA_REINTENTO * intento)
                continue

            # Xubio devuelve los errores de validación con estado 401 y el
            # motivo en 'description'. Reintentarlos no sirve, y en un POST es
            # peligroso: si la operación llegó a impactar, el reintento
            # duplicaría el comprobante.
            motivo = _motivo_funcional(respuesta)
            if motivo:
                raise ErrorXubio(f"{metodo} {ruta} -> {motivo}")

            if respuesta.status_code == 401 or _token_muerto(respuesta):
                self._token = None
                ultimo_error = ErrorAutenticacionXubio(respuesta.text[:300])
                continue

            if respuesta.status_code == 429 or respuesta.status_code >= 500:
                # Un POST no se reintenta a ciegas: no hay forma de saber si el
                # comprobante se creó antes del error.
                if metodo == "POST":
                    raise ErrorXubio(
                        f"POST {ruta} -> HTTP {respuesta.status_code}. No se "
                        "reintenta para no arriesgar un duplicado; revisá en "
                        f"Xubio si quedó creado. {respuesta.text[:200]}"
                    )
                espera = ESPERA_REINTENTO * intento
                log.warning(
                    "Xubio respondió %s, reintentando en %ss", respuesta.status_code, espera
                )
                ultimo_error = ErrorXubio(f"HTTP {respuesta.status_code}: {respuesta.text[:300]}")
                time.sleep(espera)
                continue

            if respuesta.status_code >= 400:
                raise ErrorXubio(
                    f"{metodo} {ruta} -> HTTP {respuesta.status_code}: {respuesta.text[:500]}"
                )

            if not respuesta.content:
                return None
            try:
                return respuesta.json()
            except ValueError:
                return respuesta.text

        raise ErrorXubio(f"{metodo} {ruta} falló tras {REINTENTOS} intentos: {ultimo_error}")

    def get(self, ruta: str, params: dict | None = None) -> Any:
        return self._request("GET", ruta, params=params)

    def post(self, ruta: str, datos: dict) -> Any:
        return self._request("POST", ruta, json=datos)

    # --- Maestros ---

    def _indice_por_cuit(self) -> dict[str, Any]:
        """Índice CUIT -> id de cliente en Xubio.

        La API **ignora** los filtros por CUIT (``?identificacionTributaria=``,
        ``?CUIT=`` y ``?codigo=`` devuelven la lista completa), y el listado no
        trae el CUIT: solo id y nombre. El único filtro que funciona es
        ``?nombre=``, que no sirve porque Xubio recorta los nombres a 30
        caracteres y no coinciden con los de AFIP.

        Así que el índice se arma pidiendo el detalle de cada cliente una vez
        por corrida. Es lento, pero es la única forma de identificar al receptor
        con certeza, y equivocarse acá significa facturarle a otro.
        """
        if self._por_cuit is None:
            from src.utils.helpers import normalizar_cuit

            self._por_cuit = {}
            listado = self.get(RUTA_CLIENTES) or []
            log.info("Indexando %s clientes de Xubio por CUIT", len(listado))
            for resumen in listado:
                identificador = _id_de(resumen)
                if identificador is None:
                    continue
                detalle = self.get(f"{RUTA_CLIENTES}/{identificador}") or {}
                cuit = normalizar_cuit(detalle.get("CUIT") or detalle.get("cuit"))
                if cuit:
                    self._por_cuit[cuit] = identificador
        return self._por_cuit

    def buscar_cliente_por_cuit(self, cuit: str | None) -> Any | None:
        """Id del cliente en Xubio para ese CUIT, o None si no está."""
        from src.utils.helpers import normalizar_cuit

        normalizado = normalizar_cuit(cuit)
        if not normalizado:
            return None
        return self._indice_por_cuit().get(normalizado)

    def productos_conocidos(self) -> list[dict]:
        """Catálogo de productos deducido de los comprobantes ya cargados.

        La API no expone un recurso de productos (``/productoBean`` y sus
        variantes devuelven 500), pero cada renglón de un comprobante trae el
        producto embebido con su ID, así que se juntan de ahí.
        """
        vistos: dict[Any, dict] = {}
        for resumen in self.get(RUTA_COMPROBANTE_VENTA) or []:
            transaccion = _id_de(resumen)
            if transaccion is None:
                continue
            detalle = self.get(f"{RUTA_COMPROBANTE_VENTA}/{transaccion}")
            for item in (detalle or {}).get("transaccionProductoItems") or []:
                producto = item.get("producto") or {}
                pid = _id_de(producto)
                if pid is not None and pid not in vistos:
                    vistos[pid] = {
                        "ID": pid,
                        "nombre": producto.get("nombre"),
                        "codigo": producto.get("codigo"),
                        "iva_visto": item.get("iva"),
                    }
        return sorted(vistos.values(), key=lambda p: str(p.get("nombre") or ""))

    def radiografia(self, limite: int = 100) -> dict:
        """Resume cómo viene facturando la empresa en Xubio.

        Sirve para decidir la configuración sin adivinar: qué productos usa de
        verdad (no los que tiene dados de alta), con qué alícuotas y con qué
        letras de comprobante.
        """
        from collections import Counter

        resumen = {
            "comprobantes": 0,
            "letras": Counter(),
            "alicuotas": Counter(),
            "productos": {},
            "desde": None,
            "hasta": None,
        }

        listado = (self.get(RUTA_COMPROBANTE_VENTA) or [])[:limite]
        resumen["comprobantes"] = len(listado)
        if not listado:
            return resumen

        fechas = [v.get("fecha") for v in listado if v.get("fecha")]
        resumen["desde"], resumen["hasta"] = min(fechas, default=None), max(fechas, default=None)

        for vista in listado:
            resumen["letras"][(vista.get("numeroDocumento") or "?")[:1]] += 1
            transaccion = _id_de(vista)
            if transaccion is None:
                continue
            detalle = self.get(f"{RUTA_COMPROBANTE_VENTA}/{transaccion}")
            for item in (detalle or {}).get("transaccionProductoItems") or []:
                producto = item.get("producto") or {}
                pid = _id_de(producto)
                if pid is None:
                    continue
                # 'iva' es el importe en pesos: la tasa sale de dividir.
                base = float(item.get("importe") or 0)
                monto_iva = float(item.get("iva") or 0)
                tasa = round(monto_iva / base * 100, 2) if base else 0.0
                resumen["alicuotas"][tasa] += 1
                ficha = resumen["productos"].setdefault(
                    pid, {"ID": pid, "nombre": producto.get("nombre"), "usos": 0, "ivas": set()}
                )
                ficha["usos"] += 1
                ficha["ivas"].add(tasa)

        return resumen

    def punto_venta_id(self, pto_vta: int) -> Any | None:
        """Traduce el número de punto de venta de AFIP al ID interno de Xubio."""
        if self._puntos_venta is None:
            self._puntos_venta = {}
            for pv in self.get(RUTA_PUNTOS_VENTA) or []:
                numero = str(pv.get("puntoVenta") or pv.get("codigo") or "").strip()
                if numero.isdigit():
                    self._puntos_venta[int(numero)] = _id_de(pv)
        return self._puntos_venta.get(int(pto_vta))

    def asegurar_cliente(self, cbte: Comprobante) -> Any:
        """Devuelve el ID del receptor en Xubio, creándolo si no existe."""
        datos = mapeo.datos_cliente_xubio(cbte)

        # Consumidor final: se usa uno único configurado, para no crear miles.
        if datos.get("categoriaFiscal") == "CONSUMIDOR_FINAL":
            configurado = (self.cliente.config_xubio or {}).get("cliente_consumidor_final_id")
            if configurado:
                return configurado

        from src.utils.helpers import normalizar_cuit

        existente = self.buscar_cliente_por_cuit(cbte.doc_nro)
        if existente is not None:
            return existente

        log.info("Creando cliente en Xubio: %s (%s)", datos.get("nombre"), cbte.doc_nro)
        creado = self.post(RUTA_CLIENTES, datos)
        nuevo_id = _id_de(creado)
        if nuevo_id is None:
            raise ErrorXubio(f"Xubio no devolvió el id del cliente creado: {creado}")

        cuit = normalizar_cuit(cbte.doc_nro)
        if cuit and self._por_cuit is not None:
            self._por_cuit[cuit] = nuevo_id
        return nuevo_id

    # --- Comprobantes ---

    def _indice_existentes(self) -> tuple[dict[str, Any], dt.date | None]:
        """Comprobantes ya cargados en Xubio, indexados por número.

        Devuelve también la fecha más vieja del listado: más allá de esa fecha
        el índice no dice nada, porque la API devuelve una tanda y no todo el
        histórico.
        """
        if self._existentes is None:
            self._existentes = {}
            fechas = []
            for vista in self.get(RUTA_COMPROBANTE_VENTA) or []:
                numero = vista.get("numeroDocumento")
                if numero:
                    self._existentes[numero.strip()] = _id_de(vista)
                if vista.get("fecha"):
                    fechas.append(vista["fecha"])
            self._desde_cuando = min(fechas) if fechas else None
            log.info(
                "Xubio ya tiene %s comprobantes cargados (desde %s)",
                len(self._existentes), self._desde_cuando,
            )
        limite = (
            dt.date.fromisoformat(self._desde_cuando) if self._desde_cuando else None
        )
        return self._existentes, limite

    def existente_en_xubio(self, cbte: Comprobante) -> tuple[str, Any]:
        """Dice si el comprobante ya está en Xubio.

        Los comprobantes pueden haberse cargado a mano o desde otro sistema, así
        que la unicidad de nuestra base no alcanza para evitar duplicados en la
        contabilidad del cliente.

        Devuelve ('existe', id), ('no_existe', None) o ('sin_certeza', None),
        este último cuando el comprobante es más viejo que lo que la API
        devuelve y no se puede afirmar nada.
        """
        indice, mas_antiguo = self._indice_existentes()
        numero = mapeo.numero_formateado(cbte)

        if numero in indice:
            return "existe", indice[numero]
        if mas_antiguo is not None and cbte.fecha < mas_antiguo:
            return "sin_certeza", None
        return "no_existe", None

    def subir_comprobante(self, cbte: Comprobante) -> str:
        """Sube un comprobante de venta y devuelve su ID en Xubio."""
        conf = self.cliente.config_xubio or {}
        cliente_xubio_id = self.asegurar_cliente(cbte)
        punto_venta_id = (conf.get("punto_venta_ids") or {}).get(str(cbte.pto_vta))
        if punto_venta_id is None:
            punto_venta_id = self.punto_venta_id(cbte.pto_vta)

        payload = mapeo.armar_payload(
            cbte,
            cliente_xubio_id=cliente_xubio_id,
            producto_id=conf.get("producto_default_id"),
            punto_venta_id=punto_venta_id,
            productos_por_alicuota=conf.get("producto_por_alicuota"),
        )

        log.debug("POST %s payload=%s", RUTA_COMPROBANTE_VENTA, payload)
        respuesta = self.post(RUTA_COMPROBANTE_VENTA, payload)
        xubio_id = _id_de(respuesta)
        if xubio_id is None:
            raise ErrorXubio(f"Xubio no devolvió un ID para {cbte}: {respuesta}")
        return str(xubio_id)


def _motivo_funcional(respuesta: requests.Response) -> str | None:
    """Extrae el motivo de un rechazo de negocio de Xubio.

    Los errores de validación llegan con estado 401 y un cuerpo que trae la
    explicación en 'description', enterrada bajo un stack trace de Java. Sin
    esto el usuario ve el stack trace y no el motivo.
    """
    if b"description" not in respuesta.content:
        return None
    try:
        cuerpo = respuesta.json()
    except ValueError:
        return None
    descripcion = cuerpo.get("description")
    if not descripcion:
        return None
    codigo = cuerpo.get("codeResponse") or respuesta.status_code
    return f"Xubio rechazó la operación ({codigo}): {descripcion}"


def _token_muerto(respuesta: requests.Response) -> bool:
    """Detecta el ``invalid_token`` que Xubio devuelve cuando el token expiró."""
    if b"invalid_token" not in respuesta.content:
        return False
    try:
        return respuesta.json().get("error") == "invalid_token"
    except ValueError:
        return False


def _primer_elemento(respuesta: Any) -> Any | None:
    if not respuesta:
        return None
    if isinstance(respuesta, list):
        return respuesta[0] if respuesta else None
    return respuesta


def _id_de(objeto: Any) -> Any:
    if objeto is None:
        return None
    if isinstance(objeto, (str, int)):
        return objeto
    if isinstance(objeto, dict):
        for clave in (
            "ID", "id", "transaccionid", "cliente_id", "puntoVentaId",
            "moneda_id", "productoid", "clienteid",
        ):
            if objeto.get(clave) is not None:
                return objeto[clave]
    return None
