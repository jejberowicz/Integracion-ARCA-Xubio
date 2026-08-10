# Carga_Datos_Arca — estado del proyecto

Sincroniza las ventas de ARCA (ex AFIP) hacia Xubio para las ~20 empresas de un
estudio contable. Corre desatendido por cron.

Español rioplatense en código, comentarios, commits y CLI.

---

## Cómo está armado

```
main.py                   CLI (click). Único punto de entrada
src/config.py             lee el .env
src/security/crypto.py    cifrado Fernet de secretos guardados en la base
src/database/models.py    Cliente, Comprobante, TicketAcceso, LogSync
src/importador.py         lectura de planillas .xlsx/.csv
src/afip/
  base.py                 FuenteVentas (interfaz) + ComprobanteNormalizado
  wsaa.py                 autenticación AFIP, cache del Ticket de Acceso (12 h)
  wsfe.py                 fuente 'wsfe'  (WSFEv1, con certificado)
  mis_comprobantes.py     fuente 'mis_comprobantes' (scraping + CSV)
  inferencia.py           reconstrucción de alícuotas (solo respaldo)
  tablas.py               tipos de comprobante y alícuotas de AFIP
  transporte.py           TLS especial para AFIP
src/xubio/client.py       OAuth2, HTTP, maestros, alta de comprobantes
src/xubio/mapeo.py        traducción AFIP -> Xubio (todo el esquema vive acá)
src/sync.py               orquestador
scripts/sincronizar.sh    entrada para cron
```

Flujo: ARCA → `ComprobanteNormalizado` → tabla `comprobantes_venta` → Xubio.
La fuente se elige por cliente (`clientes_afip.fuente_ventas`); el orquestador
no sabe cuál es.

Base: SQLite por defecto (`DATABASE_URL` en el `.env`). El modelo también
soporta Postgres (hay un `docker-compose.yml`), pero hoy se usa SQLite.

---

## Hechos verificados contra las APIs reales

Todo esto se comprobó con credenciales de producción. **No son suposiciones y
contradice lo que dice la documentación o lo que parecería razonable.** Si algo
acá parece raro, es porque la realidad es rara.

### AFIP / WSFEv1

- **WSFEv1 NO puede leer los comprobantes hechos desde Comprobantes en Línea.**
  Solo ve puntos de venta habilitados para web service. Un punto de venta del
  portal devuelve `11002` en `FECompUltimoAutorizado` y `602` en
  `FECompConsultar`. Por eso `wsfe` sirve únicamente para clientes que facturan
  desde un sistema propio integrado por API.
- Los servidores de AFIP negocian Diffie-Hellman con clave de 1024 bits y
  OpenSSL 3 los rechaza (`DH_KEY_TOO_SMALL`). Hace falta `SECLEVEL=1`, y al
  construir el contexto SSL a mano hay que cargar las CA raíz explícitamente
  (ver `src/afip/transporte.py`).
- El servicio se asocia en *Administrador de Relaciones → Nueva Relación →
  Buscar → **WebServices** → Facturación Electrónica*. **No** está en "Servicios
  Interactivos".
- Sin esa asociación, WSAA responde "Computador no autorizado a acceder al
  servicio".
- El TA dura 12 h y AFIP rechaza pedir otro mientras haya uno vigente: por eso
  se cachea en `tickets_acceso`.

### Portal Mis Comprobantes

- La Clave Fiscal es **siempre de una persona física**. El CUIT de una empresa
  da "Número de CUIL/CUIT incorrecto". Para un cliente persona jurídica hay que
  usar el CUIT de su representante.
- Una credencial suele cubrir **varias empresas** (ver comando `representados`).
- `https://serviciosjava2.afip.gob.ar/mis-comprobantes/` **está muerta** (404).
  Al servicio (`https://fes.afip.gob.ar/mcmp/jsp/index.do`) se llega escribiendo
  "Mis Comprobantes" en `#buscadorInput` del portal y clickeando
  `a.dropdown-item`; **abre una pestaña nueva**.
- La primera pantalla del servicio lista un panel por empresa representada
  (`a.panel`); el propio usuario aparece **dos veces**.
- Después: `#btnEmitidos` → `#fechaEmision` (un solo campo, daterangepicker,
  formato `dd/mm/aaaa - dd/mm/aaaa`) → `#buscarComprobantes`.
- **La descarga es un ZIP** con el CSV adentro.
- La sesión expira rápido: el perfil persistente **no** la mantiene entre
  corridas. Cada corrida se loguea.
- El CSV **sí trae el IVA discriminado**, con un par de columnas por alícuota
  (`IVA 21%` / `Imp. Neto Gravado IVA 21%`, etc.). La inferencia quedó solo como
  respaldo.
- Cuidado al mapear columnas: `Imp. Neto Gravado Total` se pisa por prefijo con
  `Imp. Neto Gravado IVA 0%`, y `Total IVA` con `IVA 21%`. **La coincidencia
  exacta va primero.**

### Xubio

- Token: `POST` al TokenEndpoint con `grant_type` como form-data y credenciales
  en Basic auth. Con `GET` devuelve **200 y cuerpo vacío**, que es la peor forma
  posible de fallar.
- **Los errores de validación llegan con estado 401** y el motivo en
  `description`, enterrado bajo un stack trace de Java. No confundirlos con
  token vencido: reintentarlos en un POST puede duplicar el comprobante.
- **La API ignora los filtros por CUIT** (`?identificacionTributaria=`,
  `?CUIT=`, `?codigo=` devuelven la lista completa). Solo funciona `?nombre=`,
  que no sirve porque Xubio recorta los nombres a 30 caracteres. El CUIT figura
  únicamente en el **detalle** de cada cliente, así que hay que indexar
  pidiéndolos uno por uno.
- No existe recurso de productos: `/productoBean` y variantes dan 500. El
  catálogo se deduce de los renglones de los comprobantes ya cargados.
- Esquema de `comprobanteVentaBean`, verificado leyendo registros reales:
  - `tipo` es un **entero** (1 factura, 2 nota de débito, 3 nota de crédito).
  - La letra fiscal **no es un campo**: va como prefijo de `numeroDocumento`
    (`"C-00001-00000764"`).
  - Los maestros se referencian como objetos `{"ID": n}` (moneda, cliente,
    puntoVenta). La moneda es un ID negativo fijo: −2 pesos, −3 dólares.
  - Importes: `importeGravado`, `importeImpuestos`, `importetotal` (esta última
    **toda en minúscula**).
  - `condicionDePago` es obligatorio.
  - En los renglones, **`iva` es el importe en pesos, no el porcentaje.** La
    tasa se deduce dividiendo por `importe`.
  - **`cotizacion` debe ser el entero `1`**: con `1.0` rechaza con "La
    cotización para moneda principal no puede ser distinta de 1".
- En Xubio la tasa de IVA es atributo del **producto**, así que hace falta un
  producto por alícuota (`config_xubio.producto_por_alicuota`).

---

## Estado actual

| Pieza | Estado |
|---|---|
| Descarga por `mis_comprobantes` (scraping) | Funciona end-to-end |
| Descarga por `wsfe` | Funciona, pero casi ningún cliente la puede usar |
| Parseo del CSV con alícuotas reales | Funciona |
| Alta de comprobante en Xubio | **Verificado con uno real** (`transaccionid 9000001`) |
| Protección contra duplicados en Xubio | Funciona (17 detectados como ya cargados) |
| Corrida por cron | Lista, sin usar todavía |
| 138 tests | Pasan, sin tocar la red |

### Clientes

20 cargados. Los tres primeros son los únicos configurados:

| Cliente | Estado |
|---|---|
| EMPRESA DEMO S.A. (30712345671) | Completo y probado. `mis_comprobantes`, producto 1000001 |
| PEREZ MARIA LAURA (27123456780) | Xubio OK. Falta fuente correcta. Usa 4 productos, mezcla 0 % y 21 % |
| GOMEZ JUAN CARLOS (20123456786) | Xubio OK, producto 1000002. Monotributista, todo Factura C |
| Los otros 17 | Inactivos, sin credenciales |

Una misma Clave Fiscal suele cubrir varias empresas: la del tercer cliente
representa a otras tres que **no están** en la planilla. Conviene correr
`representados` antes de salir a pedir credenciales cliente por cliente.

> Los nombres y CUIT de este archivo son ficticios. El repositorio no lleva
> datos reales de clientes: son de un estudio contable y están alcanzados por el
> secreto profesional.

---

## Trampas conocidas

1. **La columna `fuente` de la planilla dice `wsfe` para los 20.** Cada
   `alta-masiva --actualizar` revierte a `wsfe` los clientes que se hayan
   pasado a `mis_comprobantes`. Hay que corregir la planilla.
2. **Excel rompe los identificadores largos.** El `client_id` de Xubio tiene 99
   dígitos; si la celda no está en formato Texto, Excel lo guarda como
   `5,51E+98` y el valor **se pierde**. `alta-masiva` lo detecta y rechaza la
   fila.
3. **Los clientes pueden tener comprobantes ya cargados a mano en Xubio.** Antes
   de subir se consulta si el número existe. Si no se puede confirmar (más viejo
   que lo que devuelve la API), **no se sube**.
4. **Un POST fallido no se reintenta**: no hay forma de saber si el comprobante
   se creó igual.
5. Los CUIT de ejemplo tienen que ser válidos: la CLI valida el dígito
   verificador.

---

## Comandos

```bash
# Puesta a punto
python main.py init-db
python main.py generar-clave                    # FERNET_KEY para el .env

# Alta y configuración
python main.py alta-masiva planilla.xlsx [--actualizar] [--dry-run]
python main.py credencial-fiscal --cuit A --cuit B --usuario 20XXXXXXXXX
python main.py cambiar-fuente --cuit A --fuente mis_comprobantes
python main.py mapear-producto --cuit A --alicuota 21 --producto-id N
python main.py mapear-punto-venta --cuit A --pto-vta 1
python main.py activar --cuit A [--solo]

# Diagnóstico (todo de solo lectura)
python main.py ver-config --cuit A
python main.py probar-afip --cuit A
python main.py probar-xubio --cuit A [--listar productos]
python main.py analizar-xubio [--cuit A]        # qué productos usa cada uno
python main.py representados --usuario 20XXXXXXXXX
python main.py scrapear --cuit A --explorar     # vuelca el DOM del portal

# Operación
python main.py sync [--cuit A] [--solo-descargar|--solo-subir]
python main.py pendientes [--cuit A]
python main.py scrapear --cuit A --desde 2026-07-01 --hasta 2026-07-31
python main.py importar-csv --cuit A archivo.csv
```

---

## Reglas de trabajo

- **Verificar contra la API real antes de escribir el mapeo.** Cada vez que se
  asumió algo, estaba mal. Los modos `--explorar` y `analizar-xubio` existen
  para eso.
- **Nunca emitir comprobantes en AFIP.** Solo se usan `FECompConsultar`,
  `FECompUltimoAutorizado` y `FEParamGetPtosVenta`.
  `tests/test_solo_lectura_afip.py` falla si alguien agrega una escritura.
- **Antes de escribir en Xubio, preguntar si ya existe.** Duplicar en la
  contabilidad de un cliente es más caro que dejar algo pendiente.
- **No pedir la Clave Fiscal por chat.** Se carga cifrada con
  `credencial-fiscal`. El diagnóstico del scraping está saneado para poder
  compartirse (`tests/test_diagnostico_no_filtra.py`).
- Nada de certificados, planillas, bases ni perfiles de navegador en git: ver
  `.gitignore`.
