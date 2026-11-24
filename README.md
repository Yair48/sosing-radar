# sosing-radar

Puente de datos para el **radar de licitaciones de SOSING S.A.S.**
(`SOSING_BID_INTELLIGENCE/00_SISTEMA/RADAR`).

Todos los días a las 5:30 a.m. (Colombia) una acción de GitHub consulta el
dataset público *SECOP II – Procesos de Contratación* (datos.gov.co,
`p6dx-8zbt`) con la misma consulta del radar y deja el resultado en:

- `data/secop_ultimos.json` — procesos abiertos, modalidad competitiva, últimos 10 días
- `data/meta.json` — fecha de corrida, ventana, conteo, hash y estado
- `data/ultima_ejecucion.json` — estado y controles de la última corrida (siempre)
- `data/ejecuciones.jsonl` — auditoría: una línea por corrida, incluidas las fallidas

El radar (lunes y jueves 7:00 a.m.) descarga ese archivo y lo evalúa con
`python radar.py --archivo ...`.

Solo contiene datos públicos. No hay credenciales.

**Mantenimiento:** si en `radar.py` se amplía `TERMINOS_SERVIDOR` o en
`config/modalidades.json` cambian las modalidades, replicar el cambio en
`fetch_secop.py`.

## Confiabilidad de la ingesta

Origen: el 2026-11-15 la corrida devolvió 0 procesos, terminó "en verde" y
**sobrescribió el último archivo válido con una lista vacía**. Ese mismo día el
dataset fue republicado en datos.gov.co (`viewLastModified` 07:05 UTC). Desde
entonces cada corrida se evalúa antes de publicar (`calidad.py`, sin red).

### Estados

| Estado | ¿Se publica? | Significado |
|---|---|---|
| `OK` | sí | Corrida normal. |
| `OK_SIN_RESULTADOS` | sí | Cero procesos, pero legítimo: la consulta de control trae filas y la mediana histórica es baja (< 20). |
| `DEGRADADA` | sí, con advertencia | Fuente atrasada, cambio de esquema, republicación del dataset o caída atípica de volumen. |
| `FALLIDA` | **no** | Se conserva el último resultado válido y el job de GitHub falla (llega correo al dueño). |

`fetch_secop.py` escribe el resultado nuevo en temporales y solo los promueve
a `data/secop_ultimos.json` + `data/meta.json` si el estado no es `FALLIDA`.
El script sale con código 0 incluso en `FALLIDA` para que el workflow guarde la
auditoría; el último paso del workflow es el que marca el job como fallido.

### Controles

| Control | Regla |
|---|---|
| `conexion` | Error HTTP/red tras 5 intentos con backoff exponencial (2, 4, 8, 16 s; respeta `Retry-After` en 429), o un objeto de error SODA (`error`/`code`/`errorCode`) en lugar de filas → `FALLIDA`. Los 4xx distintos de 429 no se reintentan. |
| `paginacion` | La descarga debe terminar en una página corta. Si llega al tope `RADAR_LIMITE` con páginas llenas → `FALLIDA`. |
| `consulta_control` | `count(*)` con el mismo filtro de fecha/estado/modalidad **sin** los términos LIKE. Si da 0 → `FALLIDA` (fuente o filtro base roto). |
| `filtro_terminos` | Si el filtro con términos trae tantas filas como el control (≥ 50) → `FALLIDA`: Socrata ignoró los LIKE. |
| `frescura` | `rowsUpdatedAt` del dataset, en días hábiles colombianos (hora de Colombia) entre la última actualización y la corrida, sin contar el día de la actualización. Más de 2 hábiles (≈ 48 horas hábiles) → `DEGRADADA`; más de 5 → `FALLIDA`. |
| `esquema` | sha256 de las columnas `fieldName:dataTypeName` (ordenadas). Si cambia frente a la corrida anterior → `DEGRADADA`. |
| `republicacion` | `viewLastModified` distinto al de la corrida anterior → `DEGRADADA` (con nota explícita). Republicación o cambio de esquema **con 0 resultados** → `FALLIDA`. |
| `volumen` | z modificado M = 0,6745·(x − mediana)/MAD (Iglewicz–Hoaglin, NIST) sobre las últimas ≤ 28 corridas válidas de 10 días (se omite con menos de 7; si MAD = 0 se usa la desviación media). x < 25 % de la mediana (o x = 0 con mediana ≥ 20) → `FALLIDA`; M < −3,5 → `DEGRADADA`; M > +3,5 → solo nota. |
| `ejecucion_tardia` | Solo nota: el cron corrió más de 8 h después de las 10:30 UTC. |
| `ejecucion_omitida` | Solo nota: turnos diarios del cron que no tuvieron ninguna corrida. |

Calendario hábil: festivos fijos, Ley 51 de 1983 (Emiliani), Semana Santa,
Ascensión/Corpus/Sagrado Corazón y Ley 2578 de 2026 (9 de julio al lunes).

El volumen alto no degrada a propósito: en la historia los picos (24-sep,
8-oct, 9-oct) fueron publicaciones reales; un filtro roto hacia arriba lo
atrapan `paginacion` y `filtro_terminos`. Tampoco se usa "más de 26 h desde la
corrida anterior" para detectar omisiones: con el retraso normal del cron
(3–12 h) eso se dispararía casi todos los lunes.

Aceptación (`tests/test_historia.py`): reproduciendo los 67 días reales
(2026-09-18 a 2026-11-23), el 2026-11-15 queda `FALLIDA`, el 2026-10-01 (283)
queda `DEGRADADA` y los otros 65 días quedan `OK`.

### Auditoría

- `data/ultima_ejecucion.json`: estado, cada control (`nombre`, `resultado`,
  `detalle`), conteos, ventana, hash, metadatos de la fuente, tiempos y
  `ultimo_valido_utc` (el resultado que sigue publicado).
- `data/ejecuciones.jsonl`: una línea por corrida. Es la línea base del
  control de volumen. Arranca sembrada con la historia de 67 días
  (`origen: "semilla_historia"`, con el 2026-11-15 como `FALLIDA`); si el
  archivo no existe se vuelve a sembrar desde `tests/datos/historia_67.json`.
- `data/meta.json` conserva sus claves (`corrida_utc`, `ventana_desde`,
  `dias`, `registros`, `sha256`, `fuente`) y agrega `estado`,
  `ultimo_valido_utc`, `ventana_hasta`, `sin_cambios` y `controles`.
- Idempotencia: si el sha256 del resultado es igual al último válido,
  `sin_cambios: true` (el archivo de procesos queda idéntico byte a byte).

### Reprocesar una ventana

En GitHub: *Actions → Descarga SECOP II → Run workflow*, con `dias` y
opcionalmente `hasta` (AAAA-MM-DD). En local:

```bash
RADAR_DIAS=10 RADAR_HASTA=2026-11-15 python fetch_secop.py
```

La ventana es (`hasta` − `dias`, `hasta` + 1 día) sobre
`fecha_de_publicacion_del`, con los procesos que **hoy** siguen abiertos. Un
reproceso queda marcado `reproceso: true`, se promueve si no es `FALLIDA` y no
entra en la línea base de volumen. Opcional: secreto `SOCRATA_APP_TOKEN`
(se envía como cabecera `X-App-Token`).

Pruebas: `python -m unittest discover -s tests` (también corren en cada push
y pull request, workflow `pruebas.yml`).
