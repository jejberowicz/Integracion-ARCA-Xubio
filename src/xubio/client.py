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
        self._cache_clientes: dict[str, Any] = {}
        self._puntos_venta: dict[int, Any] | None = None

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

            # Token vencido: se fuerza la renovación y se reintenta.
            # Un token malformado da 401, pero la documentación describe además
            # un cuerpo {"error":"invalid_token","error_description":"token
            # died"} sin garantizar el código de estado, así que se contemplan
            # las dos formas.
            if respuesta.status_code == 401 or _token_muerto(respuesta):
                self._token = None
                ultimo_error = ErrorAutenticacionXubio(respuesta.text[:300])
                continue

            if respuesta.status_code == 429 or respuesta.status_code >= 500:
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

    def buscar_cliente(self, cuit: str | None, nombre: str | None) -> Any | None:
        """Busca el receptor en Xubio por CUIT y, si no hay, por nombre exacto."""
        clave = cuit or (nombre or "").strip().lower()
        if clave in self._cache_clientes:
            return self._cache_clientes[clave]

        resultado = None
        if cuit:
            candidatos = self.get(RUTA_CLIENTES, params={"identificacionTributaria": cuit})
            resultado = _primer_elemento(candidatos)

        if resultado is None and nombre:
            candidatos = self.get(RUTA_CLIENTES, params={"nombre": nombre})
            resultado = _primer_elemento(candidatos)

        self._cache_clientes[clave] = resultado
        return resultado

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

        existente = self.buscar_cliente(datos.get("CUIT"), datos.get("nombre"))
        if existente is not None:
            return _id_de(existente)

        log.info("Creando cliente en Xubio: %s", datos.get("nombre"))
        creado = self.post(RUTA_CLIENTES, datos)
        nuevo_id = _id_de(creado)
        clave = datos.get("CUIT") or (datos.get("nombre") or "").strip().lower()
        self._cache_clientes[clave] = creado
        return nuevo_id

    # --- Comprobantes ---

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
