"""CLI de sincronización ARCA -> Xubio."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sys
from typing import Any

import click

from src import config
from src.database.connection import crear_tablas, get_session
from src.database.models import (
    Ambiente,
    Cliente,
    Comprobante,
    EstadoSync,
    FuenteCompras,
    FuenteVentas,
)
from src.security import crypto
from src.sync import (
    clientes_activos,
    corresponde_sincronizar,
    rango_por_defecto,
    sincronizar_cliente,
)
from src.utils.helpers import (
    cuit_valido,
    es_persona_fisica,
    formatear_cuit,
    normalizar_cuit,
)

log = logging.getLogger(__name__)


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
    ("fuente_compras", "fuente_compras", None),
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


# Excel convierte los identificadores largos a número y los guarda en notación
# científica, perdiendo todos los dígitos menos los primeros quince. El valor
# es irrecuperable, así que hay que frenarlo antes de que entre a la base.
_NOTACION_CIENTIFICA = re.compile(r"^-?\d+([.,]\d+)?[Ee][+-]?\d+$")

_COLUMNAS_SENSIBLES = ("xubio_client_id", "xubio_secret", "producto_xubio_id")


def _detectar_corrupcion_excel(registro: dict) -> str | None:
    for columna in _COLUMNAS_SENSIBLES:
        valor = registro.get(columna)
        if valor is None:
            continue
        if _NOTACION_CIENTIFICA.match(str(valor).strip()):
            return (
                f"la columna '{columna}' tiene {str(valor).strip()!r}, que es lo que "
                "queda cuando Excel convierte un identificador largo a número. "
                "El valor original se perdió: copialo de nuevo desde Xubio con la "
                "columna formateada como Texto."
            )
    return None


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

    usuario = efectivo("clave_fiscal_usuario", "clave_fiscal_usuario")
    cuit_cliente = getattr(cliente, "cuit", None) if cliente else None
    if not usuario and not es_persona_fisica(cuit_cliente):
        return (
            "falta clave_fiscal_usuario. Es una empresa, así que su propio CUIT "
            "no sirve para entrar al portal: hay que cargar el CUIT de la "
            "persona física que la representa"
        )
    if usuario and not es_persona_fisica(usuario):
        return (
            f"clave_fiscal_usuario {usuario} es un CUIT de empresa. El portal de "
            "ARCA solo acepta el de una persona física (el representante)"
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
    elif not cliente.clave_fiscal_usuario:
        # El usuario del portal es una persona física. Si el cliente es una
        # empresa, su propio CUIT no sirve y hay que cargar el del
        # representante a mano: se deja vacío para que la validación avise.
        faltantes = (
            [("clave_fiscal_usuario", cliente.cuit)]
            if es_persona_fisica(cliente.cuit)
            else []
        )
    else:
        faltantes = []

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

            problema = _detectar_corrupcion_excel(registro) or _validar_coherencia(
                existente, registro, fuente
            )
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
@click.option(
    "--igualmente",
    is_flag=True,
    help="Sincronizar aunque hoy no le toque según su día del mes.",
)
def sync(
    cuit: str | None,
    desde: dt.datetime | None,
    hasta: dt.datetime | None,
    solo_descargar: bool,
    solo_subir: bool,
    igualmente: bool,
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

        # Pedir un cliente explícitamente equivale a decir "hacelo ahora".
        if not (cuit or igualmente):
            postergados = [c for c in objetivo if not corresponde_sincronizar(c)]
            objetivo = [c for c in objetivo if corresponde_sincronizar(c)]
            for c in postergados:
                click.echo(
                    f"  (se saltea {c.razon_social[:34]}: le toca el día "
                    f"{c.dia_sincronizacion} de cada mes)"
                )
            if not objetivo:
                click.echo("Hoy no le toca a ningún cliente.")
                return

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
        if r.ok:
            marca = "OK       "
        elif r.requiere_atencion:
            marca = "REQUIERE ATENCIÓN "
        else:
            marca = "FALLA    "
        click.echo(marca + r.resumen())
        for error in r.errores:
            click.echo(f"      {error}")

    atencion = [r for r in resultados if r.requiere_atencion]
    if atencion:
        click.echo(
            f"\n{len(atencion)} cliente(s) necesitan que resuelvas algo a mano; "
            "reintentar solo no los va a destrabar:"
        )
        for r in atencion:
            click.echo(f"  {r.cuit}")

    sys.exit(_codigo_de_salida(resultados))


#: El orquestador (Airflow) lee el código de salida para decidir qué hacer:
#: 1 se reintenta, 2 no, porque hace falta que alguien vaya a resolver algo.
SALIDA_OK = 0
SALIDA_FALLA = 1
SALIDA_REQUIERE_ATENCION = 2


def _codigo_de_salida(resultados: list) -> int:
    """Traduce el resumen de una corrida a un código de salida.

    Una falla transitoria (la red, un 500 de Xubio) pesa más que una que
    requiere atención: si hay aunque sea una que se puede recuperar sola,
    conviene que el orquestador reintente.
    """
    fallados = [r for r in resultados if not r.ok]
    if not fallados:
        return SALIDA_OK
    if all(r.requiere_atencion for r in fallados):
        return SALIDA_REQUIERE_ATENCION
    return SALIDA_FALLA


@cli.command("clientes-hoy")
@click.option("--json", "como_json", is_flag=True, help="Salida JSON, para el DAG de Airflow.")
@click.option(
    "--igualmente",
    is_flag=True,
    help="Listar todos los activos, sin mirar el calendario de cada uno.",
)
def clientes_hoy(como_json: bool, igualmente: bool) -> None:
    """Lista los clientes que hoy corresponde sincronizar.

    Es lo que separa "todos los activos" de "los que hoy les toca": el
    calendario vive en la base, un cliente por vez, y no en el cron ni en el
    schedule del DAG. Con --json devuelve una lista de CUIT en una sola línea,
    que es como la consume Airflow para armar una tarea por cliente.
    """
    with get_session() as session:
        clientes = clientes_activos(session)
        if not igualmente:
            clientes = [c for c in clientes if corresponde_sincronizar(c)]

        if como_json:
            # Una sola línea y nada más: del otro lado esto se parsea.
            click.echo(json.dumps([c.cuit for c in clientes]))
            return

        if not clientes:
            click.echo("Hoy no le toca a ningún cliente.")
            return
        for c in clientes:
            cuando = (
                f"día {c.dia_sincronizacion} de cada mes"
                if c.dia_sincronizacion
                else "todos los días"
            )
            click.echo(f"{c.cuit}  {c.razon_social[:35]:<35} {cuando}")


@cli.command("scrapear")
@click.option("--cuit", required=True)
@click.option("--desde", type=click.DateTime(["%Y-%m-%d"]))
@click.option("--hasta", type=click.DateTime(["%Y-%m-%d"]))
@click.option("--headed", is_flag=True, help="Navegador visible (para resolver captcha o 2FA).")
@click.option(
    "--explorar",
    is_flag=True,
    help="No descarga: entra al portal y guarda capturas y HTML de cada paso.",
)
def scrapear(
    cuit: str,
    desde: dt.datetime | None,
    hasta: dt.datetime | None,
    headed: bool,
    explorar: bool,
) -> None:
    """Baja el CSV de Mis Comprobantes del portal de ARCA.

    La primera vez conviene correrlo con --headed: si el portal pide captcha o
    segundo factor se resuelve a mano una sola vez y la sesión queda guardada
    en el perfil del navegador para las corridas siguientes.
    """
    from src.afip.mis_comprobantes import ExploracionTerminada, MisComprobantes
    from src.sync import rango_por_defecto

    with get_session() as session:
        cliente = _buscar_cliente(session, cuit)
        if cliente is None:
            raise click.ClickException(f"No existe el cliente {cuit}")

        d, h = rango_por_defecto(cliente)
        desde_f = desde.date() if desde else d
        hasta_f = hasta.date() if hasta else h

        fuente = MisComprobantes()
        click.echo(
            f"{cliente.razon_social}: {'explorando' if explorar else 'descargando'} "
            f"{desde_f} a {hasta_f}"
        )
        try:
            ruta = fuente.descargar_csv(
                cliente, desde_f, hasta_f, headless=not headed, explorar=explorar
            )
        except ExploracionTerminada as fin:
            click.echo(f"\n{fin}")
            return
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc

    click.echo(f"\nCSV en {ruta}")
    click.echo(f"Importalo con: main.py importar-csv --cuit {normalizar_cuit(cuit)} {ruta}")


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
        configurados = (cliente.config_xubio or {}).get("puntos_venta")
        puntos = fuente.puntos_de_venta(soap, fuente._auth(creds), configurados)

        if puntos and not configurados:
            click.echo(f"Puntos de venta informados por AFIP: {', '.join(map(str, puntos))}")
            return

        if puntos:
            # Los configurados a mano hay que probarlos: que estén en la config
            # no significa que WSFEv1 los acepte.
            click.echo(f"Puntos de venta configurados a mano: {', '.join(map(str, puntos))}")
            click.echo("Verificando que respondan...")
            algunos_andan = False
            for pto in puntos:
                try:
                    ultimo = fuente._ultimo_autorizado(soap, fuente._auth(creds), pto, 11)
                    click.echo(f"  PtoVta {pto}: responde (último Factura C: {ultimo})")
                    algunos_andan = True
                except Exception as exc:
                    click.echo(f"  PtoVta {pto}: NO responde -> {str(exc)[:110]}")
            if not algunos_andan:
                click.echo(
                    "\nNinguno responde. Son puntos de venta que no están habilitados\n"
                    "para web service, así que este cliente va por 'mis_comprobantes'."
                )
            return

        click.echo(
            "\nAFIP no informa ningún punto de venta habilitado para web service.\n"
            "Esto NO es un problema del certificado: la autenticación funcionó.\n"
            "\n"
            "Pasa cuando el contribuyente factura por Comprobantes en Línea o por\n"
            "controlador fiscal, porque esos puntos de venta son de otro tipo y\n"
            "FEParamGetPtosVenta no los lista.\n"
            "\n"
            "Verificado contra AFIP: un punto de venta de Comprobantes en Línea\n"
            "devuelve 11002 en FECompUltimoAutorizado y 602 en FECompConsultar.\n"
            "WSFEv1 no puede leer esos comprobantes.\n"
            "\n"
            "Este cliente va por la otra fuente:\n"
            f"  main.py cambiar-fuente --cuit {cliente.cuit} --fuente mis_comprobantes\n"
            "\n"
            "La fuente 'wsfe' sirve solo si el contribuyente factura desde un sistema\n"
            "integrado por web service, que es cuando el punto de venta figura como\n"
            "'Factura Electrónica - Web Service' en ARCA."
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

    from src.xubio.client import RUTA_CLIENTES, XubioClient

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

        if listar == "productos":
            datos = xubio.productos_conocidos()
        else:
            datos = xubio.get(RUTA_CLIENTES)
        if isinstance(datos, list):
            click.echo(f"\n{len(datos)} {listar}:")
            for item in datos[:50]:
                click.echo(json.dumps(item, ensure_ascii=False)[:200])
        else:
            click.echo(json.dumps(datos, ensure_ascii=False, indent=2)[:4000])


@cli.command("activar")
@click.option("--cuit", "cuits", multiple=True, required=True, help="Repetible.")
@click.option(
    "--solo",
    is_flag=True,
    help="Además, desactivar todos los demás. Útil para probar con unos pocos.",
)
def activar(cuits: tuple[str, ...], solo: bool) -> None:
    """Marca clientes como activos. 'sync' solo procesa los activos."""
    objetivo = {normalizar_cuit(c) for c in cuits}

    with get_session() as session:
        faltantes = objetivo - {
            c.cuit for c in session.query(Cliente).filter(Cliente.cuit.in_(objetivo))
        }
        if faltantes:
            raise click.ClickException(f"No existen estos CUIT: {sorted(faltantes)}")

        activados, desactivados = [], []
        for cliente in session.query(Cliente).all():
            deberia = cliente.cuit in objetivo
            if deberia and not cliente.activo:
                cliente.activo = True
                activados.append(cliente.razon_social)
            elif solo and not deberia and cliente.activo:
                cliente.activo = False
                desactivados.append(cliente.razon_social)

    for nombre in activados:
        click.echo(f"  activado    {nombre}")
    for nombre in desactivados:
        click.echo(f"  desactivado {nombre}")
    click.echo(f"\nActivados: {len(activados)}. Desactivados: {len(desactivados)}.")


@cli.command("representados")
@click.option("--usuario", required=True, help="CUIT de la persona que entra al portal.")
@click.option("--password", help="Si se omite, se usa la que ya esté cargada.")
@click.option("--headed", is_flag=True, help="Navegador visible.")
def representados(usuario: str, password: str | None, headed: bool) -> None:
    """Lista qué empresas puede consultar una Clave Fiscal.

    Una sola credencial suele cubrir varias empresas, así que esto evita salir a
    pedir una por cliente. Cruza el resultado con los clientes cargados.
    """
    from src.afip.mis_comprobantes import MisComprobantes

    usuario = normalizar_cuit(usuario)

    with get_session() as session:
        if not password:
            fila = (
                session.query(Cliente)
                .filter_by(clave_fiscal_usuario=usuario)
                .filter(Cliente.clave_fiscal_password.isnot(None))
                .first()
            )
            if fila is None:
                raise click.ClickException(
                    f"No hay ninguna clave cargada para {formatear_cuit(usuario)}. "
                    "Pasala con --password o cargala con 'credencial-fiscal'."
                )
            password = crypto.descifrar(fila.clave_fiscal_password)

        try:
            empresas = MisComprobantes().listar_representados(
                usuario, password, headless=not headed
            )
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc

        nuestros = {c.cuit: c for c in session.query(Cliente).all()}

    click.echo(f"\n{formatear_cuit(usuario)} puede consultar {len(empresas)} empresas:\n")
    coinciden = []
    for cuit, nombre in empresas:
        cliente = nuestros.get(cuit)
        if cliente:
            coinciden.append(cuit)
            falta = "" if cliente.clave_fiscal_password else "  <- sin credencial cargada"
            click.echo(f"  [cargado]    {formatear_cuit(cuit)}  {nombre[:34]:<34}{falta}")
        else:
            click.echo(f"  [no está]    {formatear_cuit(cuit)}  {nombre[:34]}")

    if coinciden:
        argumentos = " ".join(f"--cuit {c}" for c in coinciden)
        click.echo(
            f"\nPara cargarles la credencial a todos de una:\n"
            f"  main.py credencial-fiscal {argumentos} --usuario {usuario}"
        )


@cli.command("credencial-fiscal")
@click.option("--cuit", "cuits", multiple=True, required=True,
              help="Cliente al que corresponde. Repetible si comparten credencial.")
@click.option("--usuario", required=True,
              help="CUIT de la PERSONA FÍSICA que entra al portal (no el de la empresa).")
@click.option("--password", prompt=True, hide_input=True, help="Clave Fiscal.")
def credencial_fiscal(cuits: tuple[str, ...], usuario: str, password: str) -> None:
    """Carga la Clave Fiscal de un cliente. Se guarda cifrada y no se vuelve a pedir.

    El portal de ARCA se abre siempre con el CUIT de una persona física: el de
    una empresa da "Número de CUIL/CUIT incorrecto". Así que --usuario es el
    CUIT del titular o representante, que normalmente **no** coincide con el
    CUIT del cliente.

    Si varias empresas comparten representante, se pasan varios --cuit y se
    carga una sola vez para todas.
    """
    usuario = normalizar_cuit(usuario)
    if not cuit_valido(usuario):
        raise click.ClickException(f"{usuario!r} no es un CUIT válido.")

    cifrada = crypto.cifrar(password)
    cargados, faltantes = [], []

    with get_session() as session:
        for cuit in cuits:
            cliente = _buscar_cliente(session, cuit)
            if cliente is None:
                faltantes.append(cuit)
                continue
            cliente.clave_fiscal_usuario = usuario
            cliente.clave_fiscal_password = cifrada
            cargados.append(cliente.razon_social)

    for nombre in cargados:
        click.echo(f"  {nombre}: credencial de {formatear_cuit(usuario)} guardada")
    for cuit in faltantes:
        click.echo(f"  !! no existe el cliente {cuit}")

    if usuario in {normalizar_cuit(c) for c in cuits}:
        click.echo(
            "\nOjo: el usuario coincide con el CUIT de un cliente. Si ese cliente\n"
            "es una empresa, el login va a fallar: tiene que ser el CUIT de la\n"
            "persona física que la representa."
        )


@cli.command("cambiar-fuente")
@click.option("--cuit", "cuits", multiple=True, required=True, help="Repetible.")
@click.option("--fuente", type=click.Choice(FuenteVentas.TODAS), required=True)
@click.option("--clave-fiscal-password", help="Necesaria al pasar a mis_comprobantes.")
def cambiar_fuente(
    cuits: tuple[str, ...], fuente: str, clave_fiscal_password: str | None
) -> None:
    """Cambia de dónde se bajan las ventas de un cliente."""
    with get_session() as session:
        for cuit in cuits:
            cliente = _buscar_cliente(session, cuit)
            if cliente is None:
                click.echo(f"  !! no existe {cuit}")
                continue

            if clave_fiscal_password:
                cliente.clave_fiscal_password = crypto.cifrar(clave_fiscal_password)

            problema = _validar_coherencia(cliente, {}, fuente)
            if problema:
                click.echo(f"  !! {cliente.razon_social}: {problema}")
                continue

            anterior = cliente.fuente_ventas
            cliente.fuente_ventas = fuente
            _completar_defaults(cliente, fuente, dry_run=False)
            click.echo(f"  {cliente.razon_social[:38]:<38} {anterior} -> {fuente}")


@cli.command("cambiar-ambiente")
@click.option("--cuit", "cuits", multiple=True, help="Repetible. Vacío = todos los activos.")
@click.option("--ambiente", type=click.Choice(Ambiente.TODOS), required=True)
def cambiar_ambiente(cuits: tuple[str, ...], ambiente: str) -> None:
    """Cambia entre homologación y producción.

    Son entornos separados con certificados distintos: el de uno no sirve en el
    otro. El Ticket de Acceso se cachea por ambiente, así que no hay que
    limpiarlo a mano.
    """
    with get_session() as session:
        if cuits:
            objetivo = [c for c in (_buscar_cliente(session, x) for x in cuits) if c]
        else:
            objetivo = clientes_activos(session)

        if not objetivo:
            raise click.ClickException("No se encontró ningún cliente.")

        for cliente in objetivo:
            anterior = cliente.ambiente
            cliente.ambiente = ambiente
            marca = "=" if anterior == ambiente else "->"
            click.echo(f"  {cliente.razon_social[:38]:<38} {anterior} {marca} {ambiente}")

    if ambiente == Ambiente.PRODUCCION:
        click.echo(
            "\nOjo: los certificados de homologación no sirven en producción. "
            "Hay que emitirlos de nuevo desde 'Administración de Certificados "
            "Digitales' y asociar el servicio de Facturación Electrónica."
        )


@cli.command("desactivar")
@click.option("--cuit", "cuits", multiple=True, required=True, help="Repetible.")
def desactivar(cuits: tuple[str, ...]) -> None:
    """Saca clientes de la sincronización sin borrarlos."""
    objetivo = {normalizar_cuit(c) for c in cuits}
    tocados = []

    with get_session() as session:
        for cliente in session.query(Cliente).filter(Cliente.cuit.in_(objetivo)):
            if cliente.activo:
                cliente.activo = False
                tocados.append(cliente.razon_social)

    for nombre in tocados:
        click.echo(f"  desactivado {nombre}")
    click.echo(f"Desactivados: {len(tocados)}.")


@cli.command("analizar-xubio")
@click.option("--cuit", help="Un cliente. Por defecto, todos los que tengan credenciales.")
def analizar_xubio(cuit: str | None) -> None:
    """Muestra cómo viene facturando cada empresa en Xubio.

    Dice qué productos usa realmente, con qué alícuotas y con qué letras, que es
    lo que hace falta para decidir el mapeo sin adivinar.
    """
    from src.xubio.client import ErrorXubio, XubioClient

    with get_session() as session:
        if cuit:
            objetivo = [c for c in [_buscar_cliente(session, cuit)] if c]
            if not objetivo:
                raise click.ClickException(f"No existe el cliente {cuit}")
        else:
            objetivo = [
                c for c in clientes_activos(session)
                if c.xubio_client_id and c.xubio_secret_id
            ]

        if not objetivo:
            raise click.ClickException(
                "Ningún cliente tiene credenciales de Xubio cargadas todavía."
            )

        for cliente in objetivo:
            click.echo(f"\n{cliente.razon_social}  ({formatear_cuit(cliente.cuit)})")
            try:
                datos = XubioClient(cliente).radiografia()
            except ErrorXubio as exc:
                click.echo(f"  ERROR: {exc}")
                continue

            if not datos["comprobantes"]:
                click.echo("  Sin comprobantes cargados en Xubio todavía.")
                continue

            compras = XubioClient(cliente).resumen_compras()
            letras = ", ".join(f"{l} x{n}" for l, n in datos["letras"].most_common())
            click.echo(
                f"  {datos['comprobantes']} comprobantes "
                f"({datos['desde']} a {datos['hasta']}) | letras: {letras}"
            )
            for p in sorted(datos["productos"].values(), key=lambda x: -x["usos"]):
                ivas = ", ".join(f"{i:g}%" for i in sorted(p["ivas"]))
                click.echo(
                    f"    producto {p['ID']}  {str(p['nombre'])[:32]:<32} "
                    f"{p['usos']:>4} renglones  IVA: {ivas}"
                )

            _informar_compras(cliente, compras)

            # Sugerencia concreta, lista para copiar. Se elige, por cada
            # alícuota, el producto que más se usó con ella.
            por_tasa: dict[float, dict] = {}
            for p in datos["productos"].values():
                for tasa in p["ivas"]:
                    mejor = por_tasa.get(tasa)
                    if mejor is None or p["usos"] > mejor["usos"]:
                        por_tasa[tasa] = p

            if len(por_tasa) == 1:
                unico = next(iter(por_tasa.values()))
                click.echo(
                    f"  -> Una sola alícuota: alcanza con "
                    f"producto_xubio_id = {unico['ID']}"
                )
            else:
                click.echo("  -> Varias alícuotas: conviene mapear producto por alícuota:")
                for tasa in sorted(por_tasa):
                    click.echo(
                        f"       main.py mapear-producto --cuit {cliente.cuit} "
                        f"--alicuota {tasa:g} --producto-id {por_tasa[tasa]['ID']}"
                    )


def resumir_compras(compras: dict) -> str:
    """Describe el estado de las compras sin sacar conclusiones.

    Se intentó deducir el origen mirando si había compras recientes, pero no
    sirve: unas compras cargadas ayer pueden venir del módulo de Portal IVA de
    Xubio o de alguien cargándolas a mano, y el registro de Xubio no guarda de
    dónde salieron. La decisión la tiene que tomar quien conoce al cliente.
    """
    if not compras.get("cantidad"):
        return "no hay compras cargadas"
    return (
        f"{compras['cantidad']} compras cargadas, la última del {compras['hasta']}. "
        "De dónde salieron no se puede saber desde acá"
    )


def _informar_compras(cliente: Cliente, compras: dict) -> None:
    click.echo(
        f"  compras: {resumir_compras(compras)} | configurado: {cliente.fuente_compras}"
    )
    if cliente.fuente_compras == FuenteCompras.NINGUNA:
        click.echo(
            "    -> definí quién las carga: 'portal_iva_xubio' si las trae el módulo\n"
            "       de Xubio, 'mis_comprobantes' si querés que las baje este programa\n"
            f"       main.py cambiar-fuente-compras --cuit {cliente.cuit} --fuente ..."
        )


@cli.command("calendario")
@click.option("--cuit", "cuits", multiple=True, required=True, help="Repetible.")
@click.option(
    "--dia",
    type=click.IntRange(1, 28),
    help="Día del mes en que sincronizar. Sin este parámetro, vuelve a diario.",
)
def calendario(cuits: tuple[str, ...], dia: int | None) -> None:
    """Define cada cuánto se sincroniza un cliente.

    Con --dia se sincroniza una vez por mes, a partir de ese día. Si la corrida
    de ese día se pierde, se recupera en la siguiente en vez de esperar al mes
    que viene. Sin --dia, se sincroniza todos los días.

    El tope es 28 para que el día exista también en febrero.
    """
    with get_session() as session:
        for cuit in cuits:
            cliente = _buscar_cliente(session, cuit)
            if cliente is None:
                click.echo(f"  !! no existe {cuit}")
                continue
            cliente.dia_sincronizacion = dia
            cuando = f"el día {dia} de cada mes" if dia else "todos los días"
            click.echo(f"  {cliente.razon_social[:38]:<38} {cuando}")


@cli.command("cambiar-fuente-compras")
@click.option("--cuit", "cuits", multiple=True, required=True, help="Repetible.")
@click.option("--fuente", type=click.Choice(FuenteCompras.TODAS), required=True)
def cambiar_fuente_compras(cuits: tuple[str, ...], fuente: str) -> None:
    """Define quién carga las compras de un cliente.

    'portal_iva_xubio' significa que las trae el módulo de Xubio y este programa
    no las toca: cargarlas también las duplicaría.
    """
    with get_session() as session:
        for cuit in cuits:
            cliente = _buscar_cliente(session, cuit)
            if cliente is None:
                click.echo(f"  !! no existe {cuit}")
                continue
            anterior = cliente.fuente_compras
            cliente.fuente_compras = fuente
            click.echo(f"  {cliente.razon_social[:38]:<38} {anterior} -> {fuente}")


@cli.command("mapear-punto-venta")
@click.option("--cuit", required=True)
@click.option("--pto-vta", "puntos", multiple=True, type=int, required=True, help="Repetible.")
@click.option("--reemplazar", is_flag=True, help="Pisar la lista en vez de agregar.")
def mapear_punto_venta(cuit: str, puntos: tuple[int, ...], reemplazar: bool) -> None:
    """Carga a mano los puntos de venta a recorrer.

    Hace falta cuando FEParamGetPtosVenta no devuelve nada, que es lo que pasa
    si el contribuyente factura por Comprobantes en Línea o por controlador
    fiscal: esos puntos de venta existen pero no están habilitados para web
    service, y AFIP no los lista.
    """
    with get_session() as session:
        cliente = _buscar_cliente(session, cuit)
        if cliente is None:
            raise click.ClickException(f"No existe el cliente {cuit}")

        conf = dict(cliente.config_xubio or {})
        actuales = [] if reemplazar else list(conf.get("puntos_venta") or [])
        conf["puntos_venta"] = sorted({*actuales, *puntos})
        cliente.config_xubio = conf
        resultado = conf["puntos_venta"]

    click.echo(f"{cliente.razon_social}: puntos de venta = {resultado}")
    click.echo(f"Probalo con: main.py probar-afip --cuit {normalizar_cuit(cuit)}")


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
        click.echo(f"  ventas   : {cliente.fuente_ventas}")
        click.echo(f"  compras  : {cliente.fuente_compras}")
        click.echo(f"  ambiente : {cliente.ambiente}")
        click.echo(
            "  cuándo   : "
            + (f"día {cliente.dia_sincronizacion} de cada mes"
               if cliente.dia_sincronizacion else "todos los días")
        )
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
