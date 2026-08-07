"""CLI de sincronización ARCA -> Xubio."""
from __future__ import annotations

import datetime as dt
import logging
import sys
from typing import Any

import click

from src import config
from src.database.connection import crear_tablas, get_session
from src.database.models import Ambiente, Cliente, Comprobante, EstadoSync, FuenteVentas
from src.security import crypto
from src.sync import clientes_activos, rango_por_defecto, sincronizar_cliente
from src.utils.helpers import cuit_valido, formatear_cuit, normalizar_cuit


def _configurar_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("zeep").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _cuit_opcion(valor: str) -> str:
    """Normaliza y valida un CUIT recibido por parámetro.

    Se acepta escrito como venga (con guiones, puntos o espacios) pero se guarda
    y se busca siempre en 11 dígitos.
    """
    cuit = normalizar_cuit(valor)
    if not cuit_valido(cuit):
        raise click.ClickException(
            f"El CUIT {valor!r} no es válido: no verifica el dígito verificador. "
            "Revisá que no falte o sobre un número."
        )
    return cuit


def _buscar_cliente(session, cuit: str) -> Cliente | None:
    return session.query(Cliente).filter_by(cuit=normalizar_cuit(cuit)).first()


# Campos de la planilla que se pueden actualizar sobre un cliente existente.
# Los secretos se cifran; el producto va adentro del JSON de configuración.
_CAMPOS_ACTUALIZABLES = (
    ("razon_social", "razon_social", None),
    ("fuente", "fuente_ventas", None),
    ("ambiente", "ambiente", None),
    ("key_path", "key_path", None),
    ("crt_path", "crt_path", None),
    ("clave_fiscal_usuario", "clave_fiscal_usuario", None),
    ("clave_fiscal_password", "clave_fiscal_password", crypto.cifrar),
    ("xubio_client_id", "xubio_client_id", None),
    ("xubio_secret", "xubio_secret_id", crypto.cifrar),
)


def _aplicar(session, operacion):
    """Corre la operación en un savepoint.

    Una fila con datos incoherentes no puede tirar abajo el resto del lote, así
    que cada una se confirma por separado. Devuelve el resultado, o el mensaje
    de error si la base la rechazó.
    """
    from sqlalchemy.exc import IntegrityError

    try:
        with session.begin_nested():
            return operacion()
    except IntegrityError as exc:
        return f"la base rechazó el cambio ({_causa(exc)})"


def _causa(exc: Exception) -> str:
    texto = str(getattr(exc, "orig", exc))
    return texto.split("\n")[0][:160]


def _validar_coherencia(cliente: Cliente | None, registro: dict, fuente: str) -> str | None:
    """Chequea que la fuente resultante tenga sus credenciales.

    Mira el estado que quedaría combinando lo que ya está cargado con lo que
    trae la planilla, así una actualización parcial no rompe un cliente válido.
    La base tiene el mismo CHECK, pero acá el mensaje es entendible.
    """
    def efectivo(columna: str, atributo: str) -> Any:
        return registro.get(columna) or (getattr(cliente, atributo, None) if cliente else None)

    if fuente == FuenteVentas.WSFE:
        # key_path/crt_path se deducen del CUIT si no vienen, así que siempre hay.
        return None

    if not efectivo("clave_fiscal_password", "clave_fiscal_password"):
        return (
            "la fuente 'mis_comprobantes' necesita clave_fiscal_password "
            "(no está en la planilla ni cargada)"
        )
    return None


def _completar_defaults(cliente: Cliente, fuente: str, dry_run: bool) -> list[str]:
    """Rellena lo que cada fuente da por sentado.

    Son los mismos defaults que aplica el alta: las rutas de certificados salen
    del CUIT y el usuario de Clave Fiscal es el CUIT. Sin esto, cambiar la
    fuente de un cliente ya cargado choca contra el CHECK de la base.
    """
    cambios = []
    if fuente == FuenteVentas.WSFE:
        faltantes = [
            (a, f"{cliente.cuit}{s}")
            for a, s in (("key_path", ".key"), ("crt_path", ".crt"))
            if not getattr(cliente, a)
        ]
    else:
        faltantes = [
            ("clave_fiscal_usuario", cliente.cuit)
        ] if not cliente.clave_fiscal_usuario else []

    for atributo, valor in faltantes:
        cambios.append(atributo)
        if not dry_run:
            setattr(cliente, atributo, valor)
    return cambios


def _actualizar_cliente(
    cliente: Cliente, registro: dict, fuente: str, dry_run: bool
) -> list[str]:
    """Aplica los valores no vacíos de la planilla. Devuelve los campos tocados.

    Las celdas vacías no borran lo que ya está cargado: la planilla completa
    datos, no los pisa con nada.
    """
    cambios = []

    for columna, atributo, transformar in _CAMPOS_ACTUALIZABLES:
        valor = registro.get(columna)
        if valor in (None, ""):
            continue
        valor = str(valor).strip()
        # Los secretos se comparan descifrados: si no, cada corrida los
        # reescribiría y reportaría un cambio que no existe.
        actual = getattr(cliente, atributo)
        if transformar is not None and actual:
            try:
                actual = crypto.descifrar(actual)
            except crypto.ClaveNoConfigurada:
                actual = None  # cifrado con otra clave: conviene reescribirlo
        if actual == valor:
            continue
        cambios.append(columna)
        if not dry_run:
            setattr(cliente, atributo, transformar(valor) if transformar else valor)

    producto = registro.get("producto_xubio_id")
    if producto not in (None, ""):
        conf = dict(cliente.config_xubio or {})
        if str(conf.get("producto_default_id")) != str(producto):
            cambios.append("producto_xubio_id")
            if not dry_run:
                conf["producto_default_id"] = str(producto)
                cliente.config_xubio = conf

    cambios += _completar_defaults(cliente, fuente, dry_run)
    return cambios


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Log detallado.")
def cli(verbose: bool) -> None:
    """Descarga ventas de ARCA y las carga en Xubio."""
    _configurar_logging(verbose)


@cli.command("init-db")
def init_db() -> None:
    """Crea las tablas en PostgreSQL."""
    crear_tablas()
    click.echo(f"Esquema creado en {config.DATABASE_URL.split('@')[-1]}")


@cli.command("generar-clave")
def generar_clave() -> None:
    """Genera una FERNET_KEY para cifrar los secretos del .env."""
    click.echo(crypto.generar_clave())
    click.echo("\nGuardala en el .env como FERNET_KEY=... y no la pierdas:", err=True)
    click.echo("sin ella no se pueden descifrar los secretos ya guardados.", err=True)


@cli.command("alta-cliente")
@click.option("--cuit", required=True)
@click.option("--razon-social", required=True)
@click.option(
    "--fuente",
    type=click.Choice(FuenteVentas.TODAS),
    default=FuenteVentas.WSFE,
    help="wsfe = Comprobantes en Línea (certificado); mis_comprobantes = portal ARCA.",
)
@click.option("--ambiente", type=click.Choice(Ambiente.TODOS), default=Ambiente.PRODUCCION)
@click.option("--key-path", help="Ruta al .key (solo fuente wsfe).")
@click.option("--crt-path", help="Ruta al .crt (solo fuente wsfe).")
@click.option("--clave-fiscal-usuario", help="CUIT/usuario de Clave Fiscal (solo mis_comprobantes).")
@click.option("--clave-fiscal-password", help="Clave Fiscal (se guarda cifrada).")
@click.option("--xubio-client-id")
@click.option("--xubio-secret")
@click.option("--producto-xubio-id", help="ID del producto/servicio genérico en Xubio.")
def alta_cliente(
    cuit: str,
    razon_social: str,
    fuente: str,
    ambiente: str,
    key_path: str | None,
    crt_path: str | None,
    clave_fiscal_usuario: str | None,
    clave_fiscal_password: str | None,
    xubio_client_id: str | None,
    xubio_secret: str | None,
    producto_xubio_id: str | None,
) -> None:
    """Da de alta una empresa a sincronizar."""
    cuit = _cuit_opcion(cuit)

    with get_session() as session:
        existente = session.query(Cliente).filter_by(cuit=cuit).first()
        if existente:
            raise click.ClickException(
                f"Ya existe un cliente con CUIT {formatear_cuit(cuit)}: "
                f"{existente.razon_social}"
            )

        cliente = Cliente(
            cuit=cuit,
            razon_social=razon_social,
            fuente_ventas=fuente,
            ambiente=ambiente,
            key_path=key_path,
            crt_path=crt_path,
            clave_fiscal_usuario=clave_fiscal_usuario,
            clave_fiscal_password=crypto.cifrar(clave_fiscal_password),
            xubio_client_id=xubio_client_id,
            xubio_secret_id=crypto.cifrar(xubio_secret),
            config_xubio={"producto_default_id": producto_xubio_id} if producto_xubio_id else {},
        )
        session.add(cliente)

    click.echo(
        f"Cliente {razon_social} ({formatear_cuit(cuit)}) dado de alta "
        f"con fuente '{fuente}'."
    )


@cli.command("alta-masiva")
@click.argument("planilla", type=click.Path(exists=True, dir_okay=False))
@click.option("--dry-run", is_flag=True, help="Mostrar qué haría, sin escribir.")
@click.option(
    "--actualizar",
    is_flag=True,
    help="Actualizar los clientes que ya existan con los datos no vacíos de la planilla.",
)
@click.option(
    "--fuente-por-defecto",
    type=click.Choice(FuenteVentas.TODAS),
    default=FuenteVentas.WSFE,
    help="Fuente para las filas que no traigan la columna 'fuente'.",
)
def alta_masiva(
    planilla: str, dry_run: bool, actualizar: bool, fuente_por_defecto: str
) -> None:
    """Da de alta varios clientes desde un .xlsx o .csv.

    Columnas mínimas: RAZON SOCIAL y CUIT. Opcionales: fuente, ambiente,
    key_path, crt_path, clave_fiscal_usuario, clave_fiscal_password,
    xubio_client_id, xubio_secret, producto_xubio_id.

    Si no se indican key_path/crt_path, se asumen <CUIT>.key y <CUIT>.crt, que
    es lo que genera scripts/generar_csr.py.
    """
    from src.importador import leer_planilla

    registros = leer_planilla(planilla)
    if not registros:
        raise click.ClickException("La planilla no tiene filas con razón social y CUIT.")

    altas, omitidos, actualizados, errores = 0, 0, 0, []

    with get_session() as session:
        for registro in registros:
            razon = str(registro["razon_social"])
            cuit = normalizar_cuit(registro["cuit"])

            if not cuit_valido(cuit):
                errores.append(f"{razon}: CUIT inválido ({registro['cuit']})")
                continue

            existente = _buscar_cliente(session, cuit)

            fuente = (
                registro.get("fuente")
                or (existente.fuente_ventas if existente else None)
                or fuente_por_defecto
            ).strip().lower()
            if fuente not in FuenteVentas.TODAS:
                errores.append(f"{razon}: fuente desconocida {fuente!r}")
                continue

            problema = _validar_coherencia(existente, registro, fuente)
            if problema:
                errores.append(f"{razon}: {problema}")
                continue

            if existente:
                if not actualizar:
                    omitidos += 1
                    continue
                cambios = _aplicar(
                    session, lambda: _actualizar_cliente(existente, registro, fuente, dry_run)
                )
                if isinstance(cambios, str):
                    errores.append(f"{razon}: {cambios}")
                elif cambios:
                    actualizados += 1
                    click.echo(
                        f"  {'[dry-run] ' if dry_run else ''}{formatear_cuit(cuit)}  "
                        f"{razon[:38]:<38} actualiza: {', '.join(cambios)}"
                    )
                continue

            if fuente == FuenteVentas.WSFE:
                key_path = registro.get("key_path") or f"{cuit}.key"
                crt_path = registro.get("crt_path") or f"{cuit}.crt"
            else:
                key_path = crt_path = None

            producto = registro.get("producto_xubio_id")
            click.echo(
                f"  {'[dry-run] ' if dry_run else ''}{formatear_cuit(cuit)}  "
                f"{razon[:38]:<38} {fuente}"
            )

            if dry_run:
                altas += 1
                continue

            session.add(
                Cliente(
                    cuit=cuit,
                    razon_social=razon,
                    fuente_ventas=fuente,
                    ambiente=(registro.get("ambiente") or config.AFIP_AMBIENTE),
                    key_path=key_path,
                    crt_path=crt_path,
                    clave_fiscal_usuario=(
                        registro.get("clave_fiscal_usuario")
                        or (cuit if fuente == FuenteVentas.MIS_COMPROBANTES else None)
                    ),
                    clave_fiscal_password=crypto.cifrar(
                        registro.get("clave_fiscal_password")
                    ),
                    xubio_client_id=registro.get("xubio_client_id"),
                    xubio_secret_id=crypto.cifrar(registro.get("xubio_secret")),
                    config_xubio={"producto_default_id": producto} if producto else {},
                )
            )
            altas += 1

        if dry_run:
            session.rollback()

    resumen = (
        f"\n{'Se darían de alta' if dry_run else 'Dados de alta'}: {altas}. "
        f"Actualizados: {actualizados}. "
        f"Sin cambios: {omitidos}. Con problemas: {len(errores)}."
    )
    click.echo(resumen)
    if omitidos and not actualizar:
        click.echo("Usá --actualizar para completar los datos de los que ya existen.")
    for e in errores:
        click.echo(f"  !! {e}")

    sin_xubio = [r for r in registros if not r.get("xubio_client_id")]
    if sin_xubio and not errores:
        click.echo(
            f"\nAviso: {len(sin_xubio)} clientes quedaron sin credenciales de Xubio. "
            "Van a fallar al subir hasta que las cargues."
        )


@cli.command("listar-clientes")
def listar_clientes() -> None:
    """Muestra los clientes configurados y su última sincronización."""
    with get_session() as session:
        clientes = session.query(Cliente).order_by(Cliente.razon_social).all()
        if not clientes:
            click.echo("No hay clientes cargados. Usá 'alta-cliente'.")
            return
        for c in clientes:
            estado = "activo" if c.activo else "inactivo"
            ultimo = c.last_sync.strftime("%Y-%m-%d %H:%M") if c.last_sync else "nunca"
            click.echo(
                f"{c.cuit}  {c.razon_social[:35]:<35} {c.fuente_ventas:<17} "
                f"{estado:<9} último sync: {ultimo}"
            )


@cli.command("sync")
@click.option("--cuit", help="Sincronizar solo este CUIT. Por defecto, todos los activos.")
@click.option("--desde", type=click.DateTime(["%Y-%m-%d"]), help="Fecha inicial.")
@click.option("--hasta", type=click.DateTime(["%Y-%m-%d"]), help="Fecha final.")
@click.option("--solo-descargar", is_flag=True, help="Baja de ARCA sin subir a Xubio.")
@click.option("--solo-subir", is_flag=True, help="Sube lo pendiente sin bajar de ARCA.")
def sync(
    cuit: str | None,
    desde: dt.datetime | None,
    hasta: dt.datetime | None,
    solo_descargar: bool,
    solo_subir: bool,
) -> None:
    """Corre la sincronización ARCA -> Xubio."""
    if solo_descargar and solo_subir:
        raise click.ClickException("--solo-descargar y --solo-subir son excluyentes.")

    resultados = []
    with get_session() as session:
        objetivo = clientes_activos(session, normalizar_cuit(cuit) if cuit else None)
        if not objetivo:
            raise click.ClickException(
                f"No hay clientes activos{f' con CUIT {cuit}' if cuit else ''}."
            )

        for cliente in objetivo:
            d, h = rango_por_defecto(cliente)
            resultados.append(
                sincronizar_cliente(
                    session,
                    cliente,
                    desde=desde.date() if desde else d,
                    hasta=hasta.date() if hasta else h,
                    solo_descargar=solo_descargar,
                    solo_subir=solo_subir,
                )
            )

    click.echo("\n--- Resumen ---")
    for r in resultados:
        click.echo(("OK  " if r.ok else "FALLA ") + r.resumen())
        for error in r.errores:
            click.echo(f"      {error}")

    if any(not r.ok for r in resultados):
        sys.exit(1)


@cli.command("importar-csv")
@click.option("--cuit", required=True)
@click.argument("archivo", type=click.Path(exists=True, dir_okay=False))
def importar_csv(cuit: str, archivo: str) -> None:
    """Importa un CSV de Mis Comprobantes bajado a mano."""
    from src.afip.mis_comprobantes import MisComprobantes
    from src.sync import Resultado, _persistir

    with get_session() as session:
        cliente = _buscar_cliente(session, cuit)
        if cliente is None:
            raise click.ClickException(f"No existe el cliente {cuit}")

        resultado = Resultado(cuit=cuit, fuente="mis_comprobantes")
        for cbte in MisComprobantes().importar_csv(archivo):
            resultado.descargados += 1
            if _persistir(session, cliente, cbte):
                resultado.nuevos += 1

    click.echo(resultado.resumen())


@cli.command("probar-afip")
@click.option("--cuit", required=True)
def probar_afip(cuit: str) -> None:
    """Verifica las credenciales de AFIP sin descargar nada.

    Para la fuente 'wsfe' autentica contra WSAA y lista los puntos de venta.
    """
    from src.afip.wsaa import obtener_credenciales
    from src.afip.wsfe import ComprobantesEnLinea

    with get_session() as session:
        cliente = _buscar_cliente(session, cuit)
        if cliente is None:
            raise click.ClickException(f"No existe el cliente {cuit}")

        click.echo(f"Cliente : {cliente.razon_social}")
        click.echo(f"Fuente  : {cliente.fuente_ventas}")
        click.echo(f"Ambiente: {cliente.ambiente}")

        if cliente.fuente_ventas != FuenteVentas.WSFE:
            click.echo(
                "\nEste cliente usa Mis Comprobantes: no hay certificado que probar.\n"
                "Verificá que la Clave Fiscal esté cargada y usá 'sync' o "
                "'importar-csv'."
            )
            return

        try:
            creds = obtener_credenciales(session, cliente, servicio="wsfe")
        except Exception as exc:
            raise click.ClickException(f"Falló la autenticación con AFIP: {exc}") from exc

        click.echo(f"\nTicket de Acceso OK, válido hasta {creds.expira_en:%Y-%m-%d %H:%M} UTC")

        fuente = ComprobantesEnLinea()
        soap = fuente._cliente_soap(cliente.ambiente)
        puntos = fuente.puntos_de_venta(soap, fuente._auth(creds))
        if puntos:
            click.echo(f"Puntos de venta habilitados: {', '.join(map(str, puntos))}")
        else:
            click.echo(
                "Sin puntos de venta habilitados. Revisá que el certificado tenga "
                "asociado el servicio 'Facturación Electrónica' en ARCA."
            )


@cli.command("probar-xubio")
@click.option("--cuit", help="Cliente ya cargado en la base.")
@click.option("--client-id", help="Probar credenciales sueltas, sin dar de alta el cliente.")
@click.option("--secret", help="Secret que acompaña a --client-id.")
@click.option("--listar", type=click.Choice(["productos", "clientes"]), help="Muestra maestros.")
def probar_xubio(
    cuit: str | None, client_id: str | None, secret: str | None, listar: str | None
) -> None:
    """Verifica las credenciales de Xubio y ayuda a encontrar los IDs de maestros.

    Con --listar productos se obtiene el ID que va en --producto-xubio-id.

    Acepta --client-id/--secret sueltos para consultar los maestros antes de
    tener el cliente cargado, que es el orden natural cuando todavía no se sabe
    qué producto usar.
    """
    import json

    from src.xubio.client import RUTA_CLIENTES, RUTA_PRODUCTOS, XubioClient

    if not cuit and not (client_id and secret):
        raise click.ClickException(
            "Indicá --cuit de un cliente cargado, o --client-id y --secret."
        )

    with get_session() as session:
        if client_id:
            # Cliente en memoria, no se guarda: solo sirve para esta consulta.
            cliente = Cliente(
                cuit=normalizar_cuit(cuit) if cuit else "0",
                razon_social="(credenciales sueltas)",
                xubio_client_id=client_id,
                xubio_secret_id=crypto.cifrar(secret),
                config_xubio={},
            )
        else:
            cliente = _buscar_cliente(session, cuit)
            if cliente is None:
                raise click.ClickException(f"No existe el cliente {cuit}")

        xubio = XubioClient(cliente)
        try:
            xubio.autenticar()
        except Exception as exc:
            raise click.ClickException(f"Falló la autenticación con Xubio: {exc}") from exc
        click.echo("Autenticación con Xubio OK")

        if not listar:
            return

        ruta = RUTA_PRODUCTOS if listar == "productos" else RUTA_CLIENTES
        datos = xubio.get(ruta)
        if isinstance(datos, list):
            click.echo(f"\n{len(datos)} {listar}:")
            for item in datos[:50]:
                click.echo(json.dumps(item, ensure_ascii=False)[:200])
        else:
            click.echo(json.dumps(datos, ensure_ascii=False, indent=2)[:4000])


@cli.command("mapear-producto")
@click.option("--cuit", required=True)
@click.option(
    "--alicuota",
    required=True,
    help="Porcentaje de IVA ('21', '10.5', '0') o Id de AFIP ('5', '4', '3').",
)
@click.option("--producto-id", required=True, help="ID interno del producto en Xubio.")
def mapear_producto(cuit: str, alicuota: str, producto_id: str) -> None:
    """Asocia un producto de Xubio a una alícuota de IVA.

    En Xubio la tasa de IVA es un atributo del producto, así que un producto
    "Servicio al 21%" no sirve para las líneas al 10,5 % ni para las exentas.
    Hace falta uno por alícuota que el cliente facture.
    """
    with get_session() as session:
        cliente = _buscar_cliente(session, cuit)
        if cliente is None:
            raise click.ClickException(f"No existe el cliente {cuit}")

        conf = dict(cliente.config_xubio or {})
        mapa = dict(conf.get("producto_por_alicuota") or {})
        mapa[alicuota.strip()] = producto_id
        conf["producto_por_alicuota"] = mapa
        # Reasignar entero: SQLAlchemy no detecta mutaciones dentro del JSON.
        cliente.config_xubio = conf

    click.echo(f"{cliente.razon_social}: IVA {alicuota}% -> producto {producto_id}")
    click.echo(f"Mapeo actual: {mapa}")


@cli.command("ver-config")
@click.option("--cuit", required=True)
def ver_config(cuit: str) -> None:
    """Muestra la configuración de Xubio de un cliente."""
    import json

    with get_session() as session:
        cliente = _buscar_cliente(session, cuit)
        if cliente is None:
            raise click.ClickException(f"No existe el cliente {cuit}")

        click.echo(f"{cliente.razon_social} ({formatear_cuit(cliente.cuit)})")
        click.echo(f"  fuente   : {cliente.fuente_ventas}")
        click.echo(f"  ambiente : {cliente.ambiente}")
        click.echo(f"  cert     : {cliente.key_path or '-'} / {cliente.crt_path or '-'}")
        click.echo(f"  xubio id : {cliente.xubio_client_id or '(sin cargar)'}")
        click.echo(f"  secretos : {'cargados' if cliente.xubio_secret_id else 'faltan'}")
        click.echo(f"  config   : {json.dumps(cliente.config_xubio or {}, indent=13)}")


@cli.command("pendientes")
@click.option("--cuit")
def ver_pendientes(cuit: str | None) -> None:
    """Lista los comprobantes que todavía no entraron en Xubio."""
    with get_session() as session:
        consulta = session.query(Comprobante).filter(
            Comprobante.estado_sync.in_((EstadoSync.PENDIENTE, EstadoSync.ERROR))
        )
        if cuit:
            consulta = consulta.join(Cliente).filter(Cliente.cuit == normalizar_cuit(cuit))

        filas = consulta.order_by(Comprobante.fecha).limit(200).all()
        if not filas:
            click.echo("No hay comprobantes pendientes.")
            return
        for c in filas:
            marca = " (alícuotas inferidas)" if c.alicuotas_inferidas else ""
            click.echo(
                f"{c.fecha}  {c.tipo_cbte:>3}-{c.pto_vta:05d}-{c.nro:08d}  "
                f"{c.imp_total:>12}  {c.estado_sync}{marca}"
            )
            if c.error_sync:
                click.echo(f"     -> {c.error_sync[:180]}")


if __name__ == "__main__":
    cli()
