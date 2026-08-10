# Carga_Datos_Arca

Descarga las ventas de ARCA (ex AFIP) y las carga en Xubio, para varias empresas.

Cada empresa elige de dónde salen sus ventas:

| Fuente | Cuándo usarla | Qué necesita |
|---|---|---|
| `wsfe` | El cliente emite todo por **Comprobantes en Línea** | Certificado digital (`.key` + `.crt`) |
| `mis_comprobantes` | El cliente tiene **controlador fiscal** u otros comprobantes que no pasan por CAE | Clave Fiscal |

`wsfe` es la preferida: trae el desglose por alícuota de IVA, el receptor y el CAE.
`mis_comprobantes` trae menos datos, así que **el desglose de IVA se infiere** a
partir del neto y el IVA total, y queda marcado como tal en la base
(`alicuotas_inferidas`).

## Puesta en marcha

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
python main.py generar-clave    # pegar el resultado en FERNET_KEY del .env

docker compose up -d            # PostgreSQL
python main.py init-db
```

Si venías de la base SQLite original:

```bash
python scripts/migrar_sqlite.py data/empresa_contable.db
```

## De dónde salen las credenciales

**AFIP, fuente `wsfe`** — por cada cliente hace falta un certificado digital:

1. Generar la clave y el pedido de certificado (CSR) con el CUIT del cliente:
   ```bash
   openssl genrsa -out .certs/acme.key 2048
   openssl req -new -key .certs/acme.key -subj "/C=AR/O=ACME SA/CN=carga-datos/serialNumber=CUIT 30712345671" -out .certs/acme.csr
   ```
2. En ARCA con Clave Fiscal: *Administración de Certificados Digitales* → subir
   el `.csr` → descargar el `.crt` a `.certs/`.
3. En *Administrador de Relaciones de Clave Fiscal*, asociar el certificado al
   servicio **Facturación Electrónica (wsfe)**. Sin este paso el certificado
   autentica pero no devuelve puntos de venta.
4. Si trabajás como contador, el cliente tiene que **delegarte** el servicio.

En homologación los certificados se sacan del portal de homologación, son
distintos de los de producción.

**AFIP, fuente `mis_comprobantes`** — solo el usuario (CUIT) y la Clave Fiscal
del cliente. No hace falta certificado.

**Xubio** — en el panel de *cada empresa*: Configuración → API/Integraciones →
crear una aplicación. Da un `client_id` y un `secret`. Son distintos por
empresa, por eso se guardan por cliente y no en el `.env`.

## Alta de clientes

```bash
# Cliente con facturación electrónica propia
python main.py alta-cliente \
  --cuit 30-71234567-1 --razon-social "ACME SA" \
  --fuente wsfe --key-path acme.key --crt-path acme.crt \
  --xubio-client-id XXX --xubio-secret YYY --producto-xubio-id 123

# Cliente con controlador fiscal
python main.py alta-cliente \
  --cuit 27-12345678-0 --razon-social "Kiosco Don José" \
  --fuente mis_comprobantes \
  --clave-fiscal-usuario 27123456780 --clave-fiscal-password '...' \
  --xubio-client-id ZZZ --xubio-secret WWW --producto-xubio-id 456
```

Las rutas de certificados relativas se resuelven contra `BASE_CERTS_PATH`.
La Clave Fiscal y el secret de Xubio se guardan cifrados con `FERNET_KEY`.

`--producto-xubio-id` es obligatorio en la práctica: Xubio exige un
producto/servicio existente en cada línea del comprobante, y ni AFIP ni Mis
Comprobantes devuelven el detalle de líneas. El ID se averigua con
`probar-xubio --listar productos` (ver abajo).

## Verificar antes de sincronizar

```bash
python main.py probar-afip  --cuit 30712345671   # WSAA + puntos de venta
python main.py probar-xubio --cuit 30712345671   # autenticación
python main.py probar-xubio --cuit 30712345671 --listar productos
```

`probar-afip` autentica contra WSAA y lista los puntos de venta habilitados sin
descargar comprobantes: es la forma barata de saber si el certificado quedó bien
asociado al servicio de Facturación Electrónica.

## Uso diario

```bash
python main.py sync                                  # todos los clientes activos
python main.py sync --cuit 30712345671               # uno solo
python main.py sync --desde 2026-03-01 --hasta 2026-03-31
python main.py sync --solo-descargar                 # bajar de ARCA sin subir
python main.py sync --solo-subir                     # reintentar lo que falló

python main.py pendientes                            # qué no entró todavía en Xubio
python main.py listar-clientes
```

Sin `--desde`, arranca desde el último sync menos un día (solape deliberado).
Correr dos veces el mismo período **no duplica**: la clave natural
`(cliente, tipo, punto de venta, número)` es única.

## Mis Comprobantes: flujo asistido

El portal no tiene API y el login con Clave Fiscal puede pedir captcha o segundo
factor. `sync` intenta entrar con un perfil de navegador persistente por CUIT; si
no puede, avisa con la ruta exacta donde dejar el CSV bajado a mano:

```
data/descargas/<CUIT>/ventas_<AAAAMMDD>_<AAAAMMDD>.csv
```

La corrida siguiente lo levanta solo. También se puede importar directo:

```bash
python main.py importar-csv --cuit 27123456780 ventas_marzo.csv
```

## Estructura

```
main.py                      CLI
src/config.py                configuración desde .env
src/security/crypto.py       cifrado de secretos (Fernet)
src/database/                modelos y conexión (SQLAlchemy)
src/afip/
  base.py                    interfaz FuenteVentas + ComprobanteNormalizado
  wsaa.py                    autenticación AFIP y cache del Ticket de Acceso
  wsfe.py                    fuente 'wsfe' (Comprobantes en Línea / WSFEv1)
  mis_comprobantes.py        fuente 'mis_comprobantes' (portal + CSV)
  inferencia.py              reconstrucción del desglose de IVA
  tablas.py                  tipos de comprobante y alícuotas de AFIP
src/xubio/
  client.py                  OAuth2 + HTTP + alta de comprobantes
  mapeo.py                   traducción AFIP -> Xubio
src/sync.py                  orquestador
```

Para agregar una fuente nueva alcanza con heredar de `FuenteVentas`, decorarla
con `@registrar` y darle un `nombre`.

## Tests

```bash
pytest -q
```

Cubren la inferencia de alícuotas, el parseo del CSV, el mapeo a Xubio, la firma
CMS del WSAA y la idempotencia. Nada de eso toca la red.

## Pendiente de verificar contra las APIs reales

- **Esquema de la API de Xubio** (`src/xubio/client.py`, `src/xubio/mapeo.py`):
  los nombres de rutas y campos están escritos según la documentación pública de
  la v1.1 y hay que contrastarlos con una cuenta real antes de operar. Está todo
  concentrado en esos dos archivos.
- **Navegación dentro de Mis Comprobantes** (`descargar_csv`): el login está
  automatizado, la exportación del CSV todavía no.
- Probar `wsfe` primero con `AFIP_AMBIENTE=homologacion`.

## Alta masiva desde planilla

Para varios clientes conviene una planilla (`.xlsx` o `.csv`) en vez de un
`alta-cliente` por cada uno.

Columnas mínimas: **RAZON SOCIAL** y **CUIT**. Opcionales: `fuente`, `ambiente`,
`key_path`, `crt_path`, `clave_fiscal_usuario`, `clave_fiscal_password`,
`xubio_client_id`, `xubio_secret`, `producto_xubio_id`. El orden, las mayúsculas
y las tildes no importan.

```bash
# 1. Generar la clave y el CSR de cada cliente con certificado
python scripts/generar_csr.py .certs/clientes.xlsx --dry-run
python scripts/generar_csr.py .certs/clientes.xlsx

# 2. Subir cada .csr a ARCA y bajar el .crt como <CUIT>.crt en .certs/

# 3. Dar de alta todos
python main.py alta-masiva .certs/clientes.xlsx --dry-run
python main.py alta-masiva .certs/clientes.xlsx

# 4. Completar datos que se averiguan después (IDs de producto, credenciales):
#    agregar las columnas a la misma planilla y volver a correr
python main.py alta-masiva .certs/clientes.xlsx --actualizar --dry-run
python main.py alta-masiva .certs/clientes.xlsx --actualizar
```

`--actualizar` aplica solo las celdas **no vacías**: la planilla completa datos,
nunca los borra. Es idempotente, así que se puede correr las veces que haga
falta a medida que vas consiguiendo credenciales. Sin `--actualizar`, los
clientes que ya existen se saltean.

El `producto_xubio_id` no se puede saber antes de tener las credenciales
cargadas, así que el orden natural es: alta con lo que tengas, después
`probar-xubio --listar productos`, y después `--actualizar`. Para consultar los
maestros sin haber dado de alta nada todavía:

```bash
python main.py probar-xubio --client-id XXX --secret YYY --listar productos
```

Los archivos se nombran por CUIT (`30712345671.key` / `.crt`), así el alta
deduce las rutas sola y no hay que cargarlas en la planilla.

`generar_csr.py` nunca pisa una clave existente: si ya hay un certificado
emitido contra ella, regenerarla lo invalidaría.

## Productos de Xubio por alícuota

En Xubio la tasa de IVA es un atributo del producto, así que un producto
"Servicio al 21%" no sirve para una línea al 10,5 % ni para una exenta. Hay que
crear un producto por cada alícuota que el cliente facture:

| Producto en Xubio | Tasa IVA | Para |
|---|---|---|
| Servicio al 21% | 21 % | la mayoría de las ventas |
| Servicio al 10,5% | 10,5 % | alícuota reducida |
| Servicio exento / no gravado | 0 % | exentos y no gravados |

El ID interno (numérico) no es el Código/SKU: se obtiene con
`probar-xubio --listar productos`. Después:

```bash
python main.py mapear-producto --cuit 30701234568 --alicuota 21   --producto-id 101
python main.py mapear-producto --cuit 30701234568 --alicuota 10.5 --producto-id 102
python main.py mapear-producto --cuit 30701234568 --alicuota 0    --producto-id 103

python main.py ver-config --cuit 30701234568
```

Las alícuotas que no estén mapeadas caen en `producto_default_id`. Si el cliente
factura todo al 21 %, alcanza con ese default y no hace falta mapear nada.

### Cuidado con Excel y los identificadores largos

El `client_id` de Xubio tiene ~99 dígitos. Si la celda no está formateada como
**Texto**, Excel lo interpreta como número y lo guarda en notación científica
(`5,51249281159823E+98`), perdiendo todo menos los primeros quince dígitos. El
valor original no se puede recuperar.

Antes de pegar credenciales: seleccionar la columna → Formato de celdas →
**Texto**. `alta-masiva` detecta el patrón y rechaza la fila en vez de guardar
una credencial rota que después falla con un error de autenticación difícil de
rastrear.

## Trabajar con un subconjunto de clientes

`sync` y `analizar-xubio` procesan solo los clientes activos, así que se puede
poner en marcha de a poco sin borrar nada:

```bash
# Dejar activos únicamente estos dos
python main.py activar --cuit 27123456780 --cuit 20123456786 --solo

python main.py desactivar --cuit 30701234568
```

Los inactivos quedan en la base con toda su configuración; volver a activarlos
es un comando.

## El programa no emite comprobantes

Asociar el servicio **Facturación Electrónica (WSFE)** en ARCA es lo que da
acceso a WSFEv1, y ese servicio permite tanto consultar como emitir: AFIP no
ofrece una variante de solo lectura. La garantía tiene que estar del lado del
código.

De WSFEv1 se invocan únicamente tres métodos, los tres de consulta:

| Método | Para qué |
|---|---|
| `FEParamGetPtosVenta` | listar los puntos de venta habilitados |
| `FECompUltimoAutorizado` | saber hasta qué número llegó cada uno |
| `FECompConsultar` | leer un comprobante ya emitido |

Nunca se llama `FECAESolicitar`, que es el método que emite. `tests/
test_solo_lectura_afip.py` lo verifica sobre el código fuente y falla si
alguien agrega una llamada de escritura, así que la garantía no depende de
acordarse.

Los comprobantes hechos desde **Comprobantes en Línea** se leen igual por
WSFEv1: es la misma información, consultada por otra vía.
