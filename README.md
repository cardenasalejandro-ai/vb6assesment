# Scripts de evaluación VB6 + DB2 LUW

Estos scripts implementan la recolección automatizable de las fases A y B del documento
`ESP-vb6-db2-migration-assessment-prompts.docx`. Ambos escriben en `./assessment/` y
respetan los prefijos/nombres solicitados por el prompt pack.

## 1. Phase A: repositorio VB6

Requisito: Python 3.10+; no requiere paquetes externos.

```bash
python phase_a_vb6_assessment.py /ruta/al/repo-vb6 --output /ruta/al/repo-vb6/assessment
```

Genera los archivos `01-*` a `08-*` solicitados, además de
`00-source-scan-manifest.json` con hashes SHA-256 para trazabilidad.

El parser es conservador y léxico. Marca como `UNDETERMINED`, `UNKNOWN`,
`REQUIRES_SEMANTIC_PARSER` o `MANUAL_BINARY_REVIEW` lo que no puede afirmar sin
compilar, ejecutar o interpretar un binario. Esto es intencional: según el documento,
el compilador y la evidencia de runtime prevalecen sobre el análisis estático.

No automatiza R1-R4 porque exigen una máquina Windows con VB6, componentes COM
registrados, un entorno ejecutable y una ventana de observación del negocio. Los
resultados estáticos sí dejan preparada la reconciliación con esas actividades.

## 2. Phase B: DB2 LUW

Requisitos:

```bash
python -m pip install ibm_db
```

Primero genere y revise el SQL sin conectarse:

```bash
python phase_b_db2_extract.py --schemas MYSCHEMA --output assessment --dry-run
```

Conexión mediante variables de entorno (recomendado):

```bash
export DB2_HOST='<DB2_HOST>'
export DB2_PORT='50000'
export DB2_DATABASE='<DB2_DATABASE>'
export DB2_USER='<DB2_USER>'
export DB2_PASSWORD='<DB2_PASSWORD>'
export DB2_SCHEMAS='SCHEMA1,SCHEMA2'
python phase_b_db2_extract.py --output assessment
```

También acepta un DSN catalogado:

```bash
export DB2_DSN='<DB2_CATALOGED_DSN>'
export DB2_USER='<DB2_USER>'
export DB2_PASSWORD='<DB2_PASSWORD>'
python phase_b_db2_extract.py --schemas SCHEMA1,SCHEMA2 --output assessment
```

La contraseña no se escribe en los artefactos. Evite `--password` para que tampoco
quede en el historial del shell.

El script ejecuta E antes de P9-P14, guarda un CSV por result set y crea:

- `00-query-plan.sql`: SQL exacto ya expandido para los esquemas.
- `00-extraction-manifest.json`: estado, filas y errores por consulta.
- `00-environment.md`: perfil inicial con datos encontrados y vacíos explícitos.
- `E1-*` a `E5-*`, `09a-*` a `14-*`: extractos CSV con la nomenclatura del documento.

Las vistas con control de versión/licencia/autoridad se consideran opcionales. Un error
queda registrado como hallazgo, tal como pide el documento. Las consultas `13g` deben
ejecutarse varias veces durante picos reales; una sola ejecución no permite inferir la
concurrencia.

## Límites que requieren pasos adicionales

- P10-P14 piden análisis narrativo y cruces con los resultados de Phase A. El extractor
  produce la evidencia estructurada, pero no inventa conclusiones sin datos reales.
- P12 exige `COUNT(*)` para las 50 tablas principales. Se recomienda seleccionar esas
  tablas desde `09b-table-metrics.csv`, obtener aprobación por el costo de ejecución y
  ejecutar los conteos en una ventana acordada con el DBA.
- El estado de proveedores de OCX/DLL y sus reemplazos debe investigarse después de
  identificar productos/versiones concretos.
- P11 debe combinar los CSV de DB2 con `07-sql-inventory.csv` y los `.sql` del repo.
- P13/P14 requieren que el DBA identifique cuentas y consumidores; el script no deduce
  identidades a partir de nombres de usuario.

## Códigos de salida de Phase B

- `0`: consultas obligatorias completadas (puede haber opcionales fallidas).
- `1`: error de configuración/conexión.
- `2`: al menos una consulta obligatoria falló; revisar el manifiesto.
