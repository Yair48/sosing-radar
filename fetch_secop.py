#!/usr/bin/env python3
"""
Descarga de SECOP II para el radar SOSING — corre en GitHub Actions.

Replica EXACTAMENTE la consulta de `connectors/secop.py` del radar
(SOSING_BID_INTELLIGENCE/00_SISTEMA/RADAR): misma ventana, mismos campos,
mismas modalidades competitivas y mismos términos de servidor.

REGLA: `TERMINOS_SERVIDOR` y `MODALIDADES` deben mantenerse iguales a los de
radar.py / config/modalidades.json. Si allá se amplían, aquí también.

Confiabilidad (ver `calidad.py` y el README):
    - Cada corrida se evalúa: OK, OK_SIN_RESULTADOS, DEGRADADA o FALLIDA.
    - El resultado nuevo se escribe en temporales y SOLO se promueve a
      `data/secop_ultimos.json` + `data/meta.json` si el estado no es FALLIDA.
      Una corrida fallida NUNCA pisa el último resultado válido.
    - Siempre se escribe `data/ultima_ejecucion.json` y se agrega una línea a
      `data/ejecuciones.jsonl` (auditoría de todas las corridas).
    - El proceso sale con código 0 incluso en FALLIDA para que el workflow pueda
      guardar la auditoría; el paso final del workflow es el que falla.

Salida:
    data/secop_ultimos.json   filas crudas tal como las devuelve Socrata (si se promueve)
    data/meta.json            fecha de corrida, ventana, conteo, hash, estado (si se promueve)
    data/ultima_ejecucion.json  estado y controles de la última corrida (siempre)
    data/ejecuciones.jsonl    una línea por corrida (siempre)

Variables de entorno:
    RADAR_DIAS         tamaño de la ventana en días (10 por defecto)
    RADAR_HASTA        fecha final AAAA-MM-DD para reprocesar una ventana pasada
    RADAR_LIMITE       tope de filas (3000)
    SOCRATA_APP_TOKEN  token de aplicación opcional (se envía como X-App-Token)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import calidad

BASE_URL = "https://www.datos.gov.co/resource/p6dx-8zbt.json"
META_URL = "https://www.datos.gov.co/api/views/p6dx-8zbt.json"
DIAS_ESTANDAR = 10
PAGINA = 1000
USER_AGENT = "sosing-radar/1.1"
INTENTOS = 5
ESPERA_BASE_S = 2.0        # backoff exponencial: 2, 4, 8, 16 s
ESPERA_MAX_S = 60.0
TIMEOUT_S = 120

RUTA_SEMILLA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "tests", "datos", "historia_67.json")

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


class ErrorFuente(Exception):
    """Error HTTP, de red o un objeto de error SODA en lugar de filas."""


def _esc(v: str) -> str:
    return v.replace("'", "''")


def where_base(desde: dt.date, hasta: dt.date | None = None) -> str:
    """Filtro de fecha, estado y modalidad (sin términos). Es la consulta de control."""
    cond = [
        f"fecha_de_publicacion_del > '{desde.isoformat()}T00:00:00.000'",
        "estado_de_apertura_del_proceso = 'Abierto'",
        "modalidad_de_contratacion in(" + ",".join(f"'{_esc(m)}'" for m in MODALIDADES) + ")",
    ]
    if hasta is not None:
        # Reproceso: ventana cerrada (desde, hasta + 1 día).
        fin = hasta + dt.timedelta(days=1)
        cond.append(f"fecha_de_publicacion_del < '{fin.isoformat()}T00:00:00.000'")
    return " AND ".join(cond)


def where(desde: dt.date, hasta: dt.date | None = None) -> str:
    ors = []
    for t in TERMINOS_SERVIDOR:
        t = _esc(t.upper())
        ors.append(f"upper(nombre_del_procedimiento) like '%{t}%'")
        ors.append(f"upper(descripci_n_del_procedimiento) like '%{t}%'")
    return where_base(desde, hasta) + " AND (" + " OR ".join(ors) + ")"


# ---------------------------------------------------------------------------
# HTTP con reintentos
# ---------------------------------------------------------------------------
class Cliente:
    """Cliente HTTP mínimo. `abrir` y `dormir` se inyectan en las pruebas."""

    def __init__(self, token: str = "", abrir=None, dormir=None):
        self.token = token
        self.abrir = abrir or urllib.request.urlopen
        self.dormir = dormir or time.sleep
        self.peticiones = 0
        self.reintentos = 0

    def _cabeceras(self) -> dict:
        h = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self.token:
            h["X-App-Token"] = self.token
        return h

    def json(self, url: str):
        ultimo = None
        for i in range(INTENTOS):
            espera = min(ESPERA_BASE_S * (2 ** i), ESPERA_MAX_S)
            try:
                self.peticiones += 1
                req = urllib.request.Request(url, headers=self._cabeceras())
                with self.abrir(req, timeout=TIMEOUT_S) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detalle = _mensaje_soda(e)
                ultimo = f"HTTP {e.code}: {detalle}"
                if e.code != 429 and e.code < 500:
                    # 4xx (consulta inválida, columna inexistente...): reintentar no sirve.
                    raise ErrorFuente(ultimo) from e
                ra = (e.headers or {}).get("Retry-After") if hasattr(e, "headers") else None
                if ra and str(ra).isdigit():
                    espera = min(float(ra), ESPERA_MAX_S)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
                    json.JSONDecodeError, UnicodeDecodeError) as e:
                ultimo = f"{type(e).__name__}: {e}"
            if i < INTENTOS - 1:
                self.reintentos += 1
                self.dormir(espera)
        raise ErrorFuente(f"sin respuesta válida tras {INTENTOS} intentos: {ultimo}")


def _mensaje_soda(e: urllib.error.HTTPError) -> str:
    try:
        cuerpo = json.loads(e.read().decode("utf-8"))
        if isinstance(cuerpo, dict):
            cod = cuerpo.get("errorCode") or cuerpo.get("code") or ""
            msg = str(cuerpo.get("message", ""))[:200]
            return f"{cod} {msg}".strip()
    except Exception:  # noqa: BLE001
        pass
    return str(e.reason)


def es_error_soda(obj) -> bool:
    return isinstance(obj, dict) and any(k in obj for k in ("error", "code", "errorCode"))


def consultar(cliente: Cliente, params: dict) -> list:
    # urlencode escapa el '%' de los LIKE como %25: sin eso Socrata descarta
    # el filtro en silencio (trampa documentada en el radar).
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    r = cliente.json(url)
    if es_error_soda(r):
        raise ErrorFuente(f"error SODA: {r.get('errorCode') or r.get('code')} "
                          f"{str(r.get('message', ''))[:200]}".strip())
    if not isinstance(r, list):
        raise ErrorFuente(f"respuesta inesperada de SODA (se esperaba lista): {str(r)[:200]}")
    return r


def contar(cliente: Cliente, w: str) -> int:
    filas = consultar(cliente, {"$select": "count(*)", "$where": w})
    if len(filas) != 1 or not isinstance(filas[0], dict) or len(filas[0]) != 1:
        raise ErrorFuente(f"conteo con forma inesperada: {str(filas)[:200]}")
    return int(next(iter(filas[0].values())))


def leer_metadatos(cliente: Cliente) -> dict:
    v = cliente.json(META_URL)
    if not isinstance(v, dict) or es_error_soda(v) or "columns" not in v:
        raise ErrorFuente(f"metadatos del dataset inválidos: {str(v)[:200]}")
    cols = sorted(f"{c.get('fieldName')}:{c.get('dataTypeName')}" for c in v["columns"])
    nombres = {c.get("fieldName") for c in v["columns"]}

    def ts(k):
        return (dt.datetime.fromtimestamp(v[k], dt.timezone.utc).isoformat(timespec="seconds")
                if isinstance(v.get(k), (int, float)) else None)

    return {
        "rows_updated_at": ts("rowsUpdatedAt"),
        "view_last_modified": ts("viewLastModified"),
        "firma_esquema": hashlib.sha256("\n".join(cols).encode("utf-8")).hexdigest(),
        "columnas": len(cols),
        "campos_faltantes": [c for c in CAMPOS.split(",") if c not in nombres],
    }


def descargar(cliente: Cliente, w: str, limite: int) -> tuple[list[dict], bool, int]:
    """Pagina hasta una página corta. Devuelve (filas, paginación_completa, páginas)."""
    filas: list[dict] = []
    offset, paginas = 0, 0
    while len(filas) < limite:
        params = {
            "$select": CAMPOS, "$where": w,
            "$order": "fecha_de_publicacion_del DESC",
            "$limit": str(min(PAGINA, limite - len(filas))),
            "$offset": str(offset),
        }
        lote = consultar(cliente, params)
        paginas += 1
        filas.extend(lote)
        if len(lote) < int(params["$limit"]):
            return filas, True, paginas
        offset += len(lote)
    # Se llegó al tope con páginas llenas: puede haber más filas sin descargar.
    return filas, False, paginas


def dedup(filas: list[dict]) -> list[dict]:
    """Dedup por id dentro de la descarga (paginación con datos en movimiento)."""
    vistos, unicas = set(), []
    for f in filas:
        pid = f.get("id_del_proceso")
        if pid and pid not in vistos:
            vistos.add(pid)
            unicas.append(f)
    return unicas


# ---------------------------------------------------------------------------
# Archivos
# ---------------------------------------------------------------------------
def _leer_json(ruta: str):
    try:
        with open(ruta, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _escribir_atomico(ruta: str, texto: str) -> None:
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(texto)
    os.replace(tmp, ruta)


def cargar_historial(ruta_jsonl: str, ruta_semilla: str = RUTA_SEMILLA) -> list[dict]:
    """Lee data/ejecuciones.jsonl. Si no existe, lo siembra desde la historia."""
    if not os.path.exists(ruta_jsonl):
        historia = _leer_json(ruta_semilla)
        if not historia:
            return []
        semilla = calidad.semilla_desde_historia(historia)
        with open(ruta_jsonl, "w", encoding="utf-8") as fh:
            for e in semilla:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        return semilla
    out = []
    with open(ruta_jsonl, encoding="utf-8") as fh:
        for linea in fh:
            linea = linea.strip()
            if linea:
                try:
                    out.append(json.loads(linea))
                except ValueError:
                    continue  # una línea corrupta no debe tumbar la ingesta
    return out


def _parametros(entorno) -> tuple[int, dt.date | None, int]:
    dias = int(entorno.get("RADAR_DIAS") or DIAS_ESTANDAR)
    if not 1 <= dias <= 90:
        raise ValueError(f"RADAR_DIAS fuera de rango (1–90): {dias}")
    h = (entorno.get("RADAR_HASTA") or "").strip()
    hasta = dt.date.fromisoformat(h) if h else None
    limite = int(entorno.get("RADAR_LIMITE") or 3000)
    return dias, hasta, limite


# ---------------------------------------------------------------------------
# Corrida
# ---------------------------------------------------------------------------
def ejecutar(dir_datos: str = "data", *, ahora: dt.datetime | None = None,
             entorno=None, abrir=None, dormir=None,
             ruta_semilla: str = RUTA_SEMILLA) -> dict:
    """Una corrida completa. Devuelve la entrada de auditoría escrita."""
    entorno = os.environ if entorno is None else entorno
    t0 = time.monotonic()
    ahora = (ahora or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    os.makedirs(dir_datos, exist_ok=True)
    r_filas = os.path.join(dir_datos, "secop_ultimos.json")
    r_meta = os.path.join(dir_datos, "meta.json")
    r_ultima = os.path.join(dir_datos, "ultima_ejecucion.json")
    r_jsonl = os.path.join(dir_datos, "ejecuciones.jsonl")

    historial = cargar_historial(r_jsonl, ruta_semilla)
    meta_ant = _leer_json(r_meta) or {}
    cliente = Cliente(entorno.get("SOCRATA_APP_TOKEN", ""), abrir=abrir, dormir=dormir)
    tiempos: dict[str, float] = {}

    obs: dict = {
        "corrida_utc": ahora, "disparo": entorno.get("GITHUB_EVENT_NAME") or "local",
        "error": None, "paginacion_completa": False, "registros": None, "control": None,
        "rows_updated_at": None, "view_last_modified": None, "firma_esquema": None,
    }
    unicas: list[dict] | None = None
    extra: dict = {}
    try:
        dias, hasta, limite = _parametros(entorno)
        fin = hasta or ahora.date()
        desde = fin - dt.timedelta(days=dias)
        obs.update(dias=dias, reproceso=bool(hasta) or dias != DIAS_ESTANDAR)
        extra.update(ventana_desde=desde.isoformat(),
                     ventana_hasta=hasta.isoformat() if hasta else None)

        t = time.monotonic()
        md = leer_metadatos(cliente)
        tiempos["metadatos_s"] = round(time.monotonic() - t, 2)
        obs.update({k: md[k] for k in ("rows_updated_at", "view_last_modified", "firma_esquema")})
        extra.update(columnas=md["columnas"], campos_faltantes=md["campos_faltantes"])

        t = time.monotonic()
        obs["control"] = contar(cliente, where_base(desde, hasta))
        tiempos["control_s"] = round(time.monotonic() - t, 2)

        t = time.monotonic()
        filas, completa, paginas = descargar(cliente, where(desde, hasta), limite)
        tiempos["descarga_s"] = round(time.monotonic() - t, 2)
        unicas = dedup(filas)
        obs.update(paginacion_completa=completa, registros=len(unicas))
        extra.update(filas_crudas=len(filas), paginas=paginas)
    except ErrorFuente as e:
        obs["error"] = str(e)
    except Exception as e:  # noqa: BLE001 — cualquier sorpresa queda auditada como FALLIDA
        obs["error"] = f"{type(e).__name__}: {e}"
    obs.setdefault("dias", DIAS_ESTANDAR)
    obs.setdefault("reproceso", False)

    ev = calidad.evaluar(obs, historial)
    estado = ev["estado"]
    corrida_iso = ahora.isoformat(timespec="seconds")

    cuerpo = sha = None
    if unicas is not None:
        cuerpo = json.dumps(unicas, ensure_ascii=False, indent=1)
        sha = hashlib.sha256(cuerpo.encode("utf-8")).hexdigest()[:16]
    sin_cambios = sha is not None and sha == meta_ant.get("sha256")
    promovido = estado != calidad.FALLIDA and cuerpo is not None
    ultimo_valido = (corrida_iso if promovido
                     else meta_ant.get("ultimo_valido_utc") or meta_ant.get("corrida_utc"))

    if promovido:
        meta = {
            # Claves históricas: el radar las lee, no cambiar nombres ni formato.
            "corrida_utc": corrida_iso,
            "ventana_desde": extra["ventana_desde"],
            "dias": obs["dias"],
            "registros": obs["registros"],
            "sha256": sha,
            "fuente": BASE_URL,
            # Claves nuevas (confiabilidad).
            "estado": estado,
            "ultimo_valido_utc": corrida_iso,
            "ventana_hasta": extra.get("ventana_hasta"),
            "sin_cambios": sin_cambios,
            "controles": calidad.resumen_controles(ev["controles"]),
        }
        # Temporales primero; os.replace es atómico dentro del mismo directorio.
        tmp_f, tmp_m = r_filas + ".tmp", r_meta + ".tmp"
        with open(tmp_f, "w", encoding="utf-8") as fh:
            fh.write(cuerpo)
        with open(tmp_m, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_f, r_filas)
        os.replace(tmp_m, r_meta)

    tiempos["total_s"] = round(time.monotonic() - t0, 2)
    auditoria = {
        "corrida_utc": corrida_iso,
        "disparo": obs["disparo"],
        "estado": estado,
        "promovido": promovido,
        "sin_cambios": sin_cambios,
        "ultimo_valido_utc": ultimo_valido,
        "dias": obs["dias"],
        "reproceso": obs["reproceso"],
        **extra,
        "registros": obs["registros"],
        "control": obs["control"],
        "paginacion_completa": obs["paginacion_completa"],
        "sha256": sha,
        "rows_updated_at": obs["rows_updated_at"],
        "view_last_modified": obs["view_last_modified"],
        "firma_esquema": obs["firma_esquema"],
        "error": obs["error"],
        "peticiones": cliente.peticiones,
        "reintentos": cliente.reintentos,
        "tiempos": tiempos,
        "controles": ev["controles"],
    }
    _escribir_atomico(r_ultima, json.dumps(auditoria, ensure_ascii=False, indent=2) + "\n")
    with open(r_jsonl, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(auditoria, ensure_ascii=False) + "\n")
    return auditoria


def main() -> int:
    a = ejecutar()
    print(json.dumps({k: a[k] for k in ("corrida_utc", "estado", "promovido", "sin_cambios",
                                        "registros", "control", "error")}, ensure_ascii=False))
    for c in a["controles"]:
        print(f"  [{c['resultado']:>9}] {c['nombre']}: {c['detalle']}")
    # Código 0 incluso en FALLIDA: el workflow guarda la auditoría y falla después.
    return 0


if __name__ == "__main__":
    sys.exit(main())
