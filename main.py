"""CLI de sincronización ARCA -> Xubio."""
from __future__ import annotations

import datetime as dt
import logging
import sys

import click

from src import config
from src.database.connection import crear_tablas, get_session
from src.database.models import Ambiente, Cliente, Comprobante, EstadoSync, FuenteVentas
from src.security import crypto
from src.sync import clientes_activos, rango_por_defecto, sincronizar_cliente


def _configurar_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("zeep").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


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
    cuit = cuit.replace("-", "").strip()

    with get_session() as session:
        if session.query(Cliente).filter_by(cuit=cuit).first():
            raise click.ClickException(f"Ya existe un cliente con CUIT {cuit}")

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

    click.echo(f"Cliente {razon_social} ({cuit}) dado de alta con fuente '{fuente}'.")


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
        objetivo = clientes_activos(session, cuit)
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
        cliente = session.query(Cliente).filter_by(cuit=cuit.replace("-", "")).first()
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
        cliente = session.query(Cliente).filter_by(cuit=cuit.replace("-", "")).first()
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
@click.option("--cuit", required=True)
@click.option("--listar", type=click.Choice(["productos", "clientes"]), help="Muestra maestros.")
def probar_xubio(cuit: str, listar: str | None) -> None:
    """Verifica las credenciales de Xubio y ayuda a encontrar los IDs de maestros.

    Con --listar productos se obtiene el ID que va en --producto-xubio-id.
    """
    import json

    from src.xubio.client import RUTA_CLIENTES, RUTA_PRODUCTOS, XubioClient

    with get_session() as session:
        cliente = session.query(Cliente).filter_by(cuit=cuit.replace("-", "")).first()
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


@cli.command("pendientes")
@click.option("--cuit")
def ver_pendientes(cuit: str | None) -> None:
    """Lista los comprobantes que todavía no entraron en Xubio."""
    with get_session() as session:
        consulta = session.query(Comprobante).filter(
            Comprobante.estado_sync.in_((EstadoSync.PENDIENTE, EstadoSync.ERROR))
        )
        if cuit:
            consulta = consulta.join(Cliente).filter(Cliente.cuit == cuit.replace("-", ""))

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
