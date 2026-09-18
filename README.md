# sosing-radar

Puente de datos para el **radar de licitaciones de SOSING S.A.S.**
(`SOSING_BID_INTELLIGENCE/00_SISTEMA/RADAR`).

Todos los días a las 5:30 a.m. (Colombia) una acción de GitHub consulta el
dataset público *SECOP II – Procesos de Contratación* (datos.gov.co,
`p6dx-8zbt`) con la misma consulta del radar y deja el resultado en:

- `data/secop_ultimos.json` — procesos abiertos, modalidad competitiva, últimos 10 días
- `data/meta.json` — fecha de corrida, ventana y conteo

El radar (lunes y jueves 7:00 a.m.) descarga ese archivo y lo evalúa con
`python radar.py --archivo ...`.

Solo contiene datos públicos. No hay credenciales.

**Mantenimiento:** si en `radar.py` se amplía `TERMINOS_SERVIDOR` o en
`config/modalidades.json` cambian las modalidades, replicar el cambio en
`fetch_secop.py`.
