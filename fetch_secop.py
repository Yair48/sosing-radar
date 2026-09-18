#!/usr/bin/env python3
"""
Descarga de SECOP II para el radar SOSING — corre en GitHub Actions.

Replica EXACTAMENTE la consulta de `connectors/secop.py` del radar
(SOSING_BID_INTELLIGENCE/00_SISTEMA/RADAR): misma ventana, mismos campos,
mismas modalidades competitivas y mismos términos de servidor.

REGLA: `TERMINOS_SERVIDOR` y `MODALIDADES` deben mantenerse iguales a los de
radar.py / config/modalidades.json. Si allá se amplían, aquí también.

Salida:
    data/secop_ultimos.json   filas crudas tal como las devuelve Socrata
    data/meta.json            fecha de corrida, ventana, conteo, hash
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE_URL = "https://www.datos.gov.co/resource/p6dx-8zbt.json"
DIAS = int(os.environ.get("RADAR_DIAS", "10"))
LIMITE_TOTAL = int(os.environ.get("RADAR_LIMITE", "3000"))
PAGINA = 1000
APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN", "")

CAMPOS = ",".join([
    "id_del_proceso", "referencia_del_proceso", "entidad", "departamento_entidad",
    "ciudad_entidad", "ordenentidad", "nombre_del_procedimiento",
    "descripci_n_del_procedimiento", "modalidad_de_contratacion",
    "estado_del_procedimiento", "estado_de_apertura_del_proceso", "precio_base",
    "fecha_de_publicacion_del", "fecha_de_recepcion_de",
    "codigo_principal_de_categoria", "tipo_de_contrato", "urlproceso",
])

MODALIDADES = [
    "Concurso de méritos abierto", "Licitación pública",
    "Licitación pública Obra Publica",
    "Licitación Pública Acuerdo Marco de Precios",
    "Selección Abreviada de Menor Cuantía",
    "Seleccion Abreviada Menor Cuantia Sin Manifestacion Interes",
    "Selección abreviada subasta inversa", "Mínima cuantía",
    "Contratación Directa (con ofertas)",
    "Contratación régimen especial (con ofertas)",
]

TERMINOS_SERVIDOR = [
    # --- Agua, saneamiento y ambiente ---
    "AMBIENTAL", "FORESTAL", "REFOREST", "ARBOL",
    "ACUEDUCTO", "ALCANTARILLADO", "SANEAMIENTO", "AGUA POTABLE", "RESIDUAL",
    "RESIDUOS", "PGIRS",
    "HIDROLOG", "HIDRAULIC", "RIESGO", "MITIGACION", "INUNDA", "GAVION",
    "CUENCA", "POMCA", "VERTIMIENTO", "SANITARIA",
    # --- Consultoría y estudios ---
    "INTERVENTOR", "SUPERVISION",
    "CONSULTORIA", "ESTUDIOS Y DISE", "FORMULACION DE PROYECTO",
    "ORDENAMIENTO TERRITORIAL", "CARTOGRAF", "CATASTR", "TOPOGRAF",
    "GERENCIA DE PROYECTO", "GESTION DE PROYECTO", "UNIDAD EJECUTORA",
    # --- Obra civil y edificación ---
    "CERRAMIENTO", "INSTITUCION EDUCATIVA", "INFRAESTRUCTURA EDUCATIVA",
    "ESPACIO PUBLICO", "PARQUE", "EQUIPAMIENTO",
    "PAVIMENTO", "PLACA HUELLA", "PUENTE", "BOX COULVERT",
    "MURO DE CONTENCION", "OBRAS CIVILES", "SENALIZACION",
    "VIA TERCIARIA", "ADECUACION",
]


def _esc(v: str) -> str:
    return v.replace("'", "''")


def where(desde: dt.date) -> str:
    cond = [
        f"fecha_de_publicacion_del > '{desde.isoformat()}T00:00:00.000'",
        "estado_de_apertura_del_proceso = 'Abierto'",
        "modalidad_de_contratacion in(" + ",".join(f"'{_esc(m)}'" for m in MODALIDADES) + ")",
    ]
    ors = []
    for t in TERMINOS_SERVIDOR:
        t = _esc(t.upper())
        ors.append(f"upper(nombre_del_procedimiento) like '%{t}%'")
        ors.append(f"upper(descripci_n_del_procedimiento) like '%{t}%'")
    cond.append("(" + " OR ".join(ors) + ")")
    return " AND ".join(cond)


def get_json(params: dict, intentos: int = 4) -> list:
    # urlencode escapa el '%' de los LIKE como %25: sin eso Socrata descarta
    # el filtro en silencio (trampa documentada en el radar).
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    ultimo = None
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                       "User-Agent": "sosing-radar/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            ultimo = e
            time.sleep(5 * (i + 1))
    raise SystemExit(f"SECOP II no respondió tras {intentos} intentos: {ultimo}")


def main() -> int:
    hoy = dt.date.today()
    desde = hoy - dt.timedelta(days=DIAS)
    w = where(desde)
    filas: list[dict] = []
    offset = 0
    while len(filas) < LIMITE_TOTAL:
        params = {
            "$select": CAMPOS, "$where": w,
            "$order": "fecha_de_publicacion_del DESC",
            "$limit": str(min(PAGINA, LIMITE_TOTAL - len(filas))),
            "$offset": str(offset),
        }
        if APP_TOKEN:
            params["$$app_token"] = APP_TOKEN
        lote = get_json(params)
        if not lote:
            break
        filas.extend(lote)
        if len(lote) < int(params["$limit"]):
            break
        offset += len(lote)

    # Dedup por id dentro de la descarga (paginación con datos en movimiento)
    vistos, unicas = set(), []
    for f in filas:
        pid = f.get("id_del_proceso")
        if pid and pid not in vistos:
            vistos.add(pid)
            unicas.append(f)

    os.makedirs("data", exist_ok=True)
    cuerpo = json.dumps(unicas, ensure_ascii=False, indent=1)
    with open("data/secop_ultimos.json", "w", encoding="utf-8") as fh:
        fh.write(cuerpo)
    meta = {
        "corrida_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "ventana_desde": desde.isoformat(),
        "dias": DIAS,
        "registros": len(unicas),
        "sha256": hashlib.sha256(cuerpo.encode("utf-8")).hexdigest()[:16],
        "fuente": BASE_URL,
    }
    with open("data/meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print(json.dumps(meta, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
