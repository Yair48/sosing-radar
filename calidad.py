#!/usr/bin/env python3
"""
Controles de calidad de la ingesta SECOP II — módulo PURO (sin red, sin disco).

Lo usa `fetch_secop.py` para decidir si una corrida se publica o no. Todo lo
que necesita llega como argumentos, así que se prueba entero con `unittest`.

Contiene:
    1. Calendario hábil colombiano (festivos Ley 51 de 1983 + Ley 2578 de 2026).
    2. Anomalía robusta de volumen (z modificado de Iglewicz–Hoaglin).
    3. `evaluar()`: combina todos los controles en un estado único:

        OK                 corrida normal, se publica.
        OK_SIN_RESULTADOS  cero procesos, pero es legítimo (la fuente responde,
                           el filtro base trae filas y el histórico es bajo).
        DEGRADADA          se publica, pero con advertencia explícita
                           (fuente atrasada, cambio de esquema, republicación,
                           caída de volumen).
        FALLIDA            NO se publica: se conserva el último resultado válido.
"""
from __future__ import annotations

import datetime as dt
import statistics
from functools import lru_cache

# ---------------------------------------------------------------------------
# Estados y resultados de control
# ---------------------------------------------------------------------------
OK = "OK"
OK_SIN_RESULTADOS = "OK_SIN_RESULTADOS"
DEGRADADA = "DEGRADADA"
FALLIDA = "FALLIDA"

# Resultado de cada control individual. NOTA y OMITIDO no afectan el estado.
R_OK, R_NOTA, R_OMITIDO = "OK", "NOTA", "OMITIDO"
R_DEGRADADA, R_FALLIDA = DEGRADADA, FALLIDA

# Estados cuyo resultado se publicó y por tanto sirven como línea base.
ESTADOS_VALIDOS = (OK, OK_SIN_RESULTADOS, DEGRADADA)

# Parámetros (documentados en el README, sección "Confiabilidad de la ingesta").
VENTANA_BASE = 28           # corridas válidas que forman la línea base
MIN_BASE = 7                # menos de esto: el control de volumen se omite
UMBRAL_M = 3.5              # |M| > 3.5 = atípico (Iglewicz y Hoaglin, 1993)
FRACCION_COLAPSO = 0.25     # x < 25 % de la mediana = colapso -> FALLIDA
MEDIANA_BAJA = 20           # mediana < 20: cero resultados puede ser legítimo
MAX_HABILES_DEGRADADA = 2   # fuente con > 2 días hábiles sin actualizar
MAX_HABILES_FALLIDA = 5     # fuente con > 5 días hábiles sin actualizar
HORA_PROGRAMADA = dt.time(10, 30)   # cron "30 10 * * *" (UTC)
MAX_RETRASO_H = 8.0         # retraso del cron que se deja anotado
UMBRAL_FILTRO_IGNORADO = 50  # control mínimo para sospechar LIKE ignorados

TZ_COLOMBIA = dt.timezone(dt.timedelta(hours=-5), "COT")  # sin horario de verano


# ---------------------------------------------------------------------------
# 1. Calendario hábil colombiano
# ---------------------------------------------------------------------------
def pascua(anio: int) -> dt.date:
    """Domingo de Pascua (algoritmo gregoriano anónimo de Meeus/Jones/Butcher)."""
    a = anio % 19
    b, c = divmod(anio, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(anio, mes, dia)


def _lunes_siguiente(d: dt.date) -> dt.date:
    """Ley Emiliani: si no cae lunes, se traslada al lunes siguiente."""
    return d + dt.timedelta(days=(7 - d.weekday()) % 7)


@lru_cache(maxsize=None)
def festivos(anio: int) -> frozenset[dt.date]:
    """Festivos nacionales de Colombia para `anio`.

    - Fijos: 1 ene, 1 may, 20 jul, 7 ago, 8 dic, 25 dic.
    - Ley 51 de 1983 (trasladables al lunes): 6 ene, 19 mar, 29 jun, 15 ago,
      12 oct, 1 nov, 11 nov.
    - Ley 2578 de 2026: 9 jul trasladable al lunes desde 2026.
    - Según Pascua: Jueves y Viernes Santo; Ascensión (+43), Corpus Christi
      (+64) y Sagrado Corazón (+71), contados desde el domingo de Pascua y que
      ya caen en lunes.
    """
    fijos = [(1, 1), (5, 1), (7, 20), (8, 7), (12, 8), (12, 25)]
    trasladables = [(1, 6), (3, 19), (6, 29), (8, 15), (10, 12), (11, 1), (11, 11)]
    if anio >= 2026:
        trasladables.append((7, 9))
    dias = {dt.date(anio, m, d) for m, d in fijos}
    dias |= {_lunes_siguiente(dt.date(anio, m, d)) for m, d in trasladables}
    p = pascua(anio)
    dias |= {p - dt.timedelta(days=3), p - dt.timedelta(days=2)}
    dias |= {p + dt.timedelta(days=n) for n in (43, 64, 71)}
    return frozenset(dias)


def es_habil(d: dt.date) -> bool:
    """Lunes a viernes que no sea festivo nacional."""
    if isinstance(d, dt.datetime):
        d = d.date()
    return d.weekday() < 5 and d not in festivos(d.year)


def dias_habiles_entre(desde: dt.date, hasta: dt.date) -> int:
    """Días hábiles en el intervalo (desde, hasta]: excluye `desde`, incluye `hasta`.

    Es la "edad" en días hábiles: algo actualizado el viernes y revisado el
    lunes hábil siguiente tiene 1. Si hasta < desde, el resultado es negativo.
    """
    if hasta < desde:
        return -dias_habiles_entre(hasta, desde)
    n, d = 0, desde
    while d < hasta:
        d += dt.timedelta(days=1)
        n += es_habil(d)
    return n


def sumar_habiles(d: dt.date, n: int) -> dt.date:
    """Fecha que está `n` días hábiles después de `d` (antes si n < 0).

    `d` no cuenta: sumar_habiles(31-mar-2027, 5) es el 5.º hábil de abril.
    """
    paso = dt.timedelta(days=1 if n >= 0 else -1)
    restantes = abs(n)
    while restantes:
        d += paso
        if es_habil(d):
            restantes -= 1
    return d


# ---------------------------------------------------------------------------
# 2. Anomalía robusta de volumen
# ---------------------------------------------------------------------------
def z_modificado(x: float, base: list[float]) -> tuple[float | None, float, float]:
    """z modificado de Iglewicz–Hoaglin (NIST e-Handbook 1.3.5.17).

        M = 0.6745 · (x − mediana) / MAD

    Si MAD = 0 (más de la mitad de la base es idéntica) se usa la variante con
    la desviación absoluta MEDIA: M = (x − mediana) / (1.253314 · MeanAD).
    Si además MeanAD = 0 (base constante) no hay dispersión y M es None.
    Devuelve (M, mediana, MAD).
    """
    med = statistics.median(base)
    desv = [abs(v - med) for v in base]
    mad = statistics.median(desv)
    if mad > 0:
        return 0.6745 * (x - med) / mad, med, mad
    mean_ad = sum(desv) / len(desv)
    if mean_ad > 0:
        return (x - med) / (1.253314 * mean_ad), med, mad
    return None, med, mad


def linea_base(historial: list[dict], dias: int) -> list[int]:
    """Registros de las últimas `VENTANA_BASE` corridas válidas comparables.

    Válida = su resultado se publicó (OK, OK_SIN_RESULTADOS, DEGRADADA).
    Comparable = misma ventana en días y no es un reproceso manual.
    """
    valores = [
        e["registros"] for e in historial
        if e.get("estado") in ESTADOS_VALIDOS
        and e.get("registros") is not None
        and int(e.get("dias", dias)) == dias
        and not e.get("reproceso")
    ]
    return valores[-VENTANA_BASE:]


def control_volumen(x: int, base: list[int]) -> dict:
    """Clasifica el volumen `x` contra la línea base.

    - Menos de `MIN_BASE` corridas: OMITIDO.
    - Mediana >= 20 y (x < 25 % de la mediana): FALLIDA (colapso; incluye x = 0).
    - Mediana < 20 y x = 0: OK (cero puede ser legítimo; lo decide evaluar()).
    - M < −3.5: DEGRADADA (caída atípica).
    - M > +3.5: NOTA (volumen alto). Es deliberadamente asimétrico: el modo de
      falla que nos importa es PERDER datos. En los 67 días de historia las
      alzas atípicas (24-sep, 8-oct, 9-oct) fueron picos reales de publicación;
      un filtro roto hacia arriba lo atrapan los controles de paginación y de
      filtro ignorado, no este.
    """
    nombre = "volumen"
    if len(base) < MIN_BASE:
        return _c(nombre, R_OMITIDO,
                  f"{x} registros; línea base con {len(base)} corridas válidas "
                  f"(mínimo {MIN_BASE})", m=None, mediana=None, mad=None, n_base=len(base))
    m, med, mad = z_modificado(x, base)
    extra = dict(m=None if m is None else round(m, 2), mediana=med, mad=mad, n_base=len(base))
    txt_m = "n/d" if m is None else f"{m:+.2f}"
    resumen = f"{x} registros; mediana {med:g}, MAD {mad:g}, M={txt_m} (n={len(base)})"
    if med >= MEDIANA_BAJA and x < FRACCION_COLAPSO * med:
        return _c(nombre, R_FALLIDA, f"colapso de volumen: {resumen}; "
                  f"< {int(FRACCION_COLAPSO * 100)} % de la mediana", **extra)
    if x == 0:
        return _c(nombre, R_OK, f"cero registros con mediana baja (< {MEDIANA_BAJA}): {resumen}",
                  **extra)
    if m is not None and m < -UMBRAL_M:
        return _c(nombre, R_DEGRADADA, f"caída atípica de volumen: {resumen}", **extra)
    if m is not None and m > UMBRAL_M:
        return _c(nombre, R_NOTA, f"volumen alto atípico (no degrada): {resumen}", **extra)
    return _c(nombre, R_OK, resumen, **extra)


# ---------------------------------------------------------------------------
# 3. Evaluación del estado de la corrida
# ---------------------------------------------------------------------------
def _c(nombre: str, resultado: str, detalle: str, **extra) -> dict:
    d = {"nombre": nombre, "resultado": resultado, "detalle": detalle}
    d.update(extra)
    return d


def _ultimo_valor(historial: list[dict], campo: str):
    """Último valor no nulo de `campo` en corridas anteriores (cualquier estado)."""
    for e in reversed(historial):
        if e.get(campo) is not None:
            return e[campo]
    return None


def _a_datetime(v) -> dt.datetime | None:
    if v is None or isinstance(v, dt.datetime):
        return v
    return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def turno_programado(t: dt.datetime) -> dt.datetime:
    """Turno del cron (10:30 UTC) al que corresponde una corrida: el último
    10:30 UTC anterior o igual a `t`. GitHub retrasa el cron horas, pero nunca
    lo adelanta."""
    t = t.astimezone(dt.timezone.utc)
    turno = dt.datetime.combine(t.date(), HORA_PROGRAMADA, tzinfo=dt.timezone.utc)
    return turno if t >= turno else turno - dt.timedelta(days=1)


def controles_ejecucion(corrida: dt.datetime, historial: list[dict]) -> list[dict]:
    """Notas de puntualidad (nunca degradan): retraso y turnos omitidos.

    Solo se calculan para corridas del cron (disparo == "schedule").
    `ejecucion_omitida` cuenta turnos diarios del cron sin ninguna corrida entre
    la anterior y esta. No se usa "más de 26 h desde la corrida anterior"
    porque con el retraso normal del cron (3–12 h) eso se dispara casi todos
    los lunes sin que falte ninguna corrida (10 de 67 días en la historia).
    """
    out = []
    turno = turno_programado(corrida)
    retraso_h = (corrida - turno).total_seconds() / 3600
    if retraso_h > MAX_RETRASO_H:
        out.append(_c("ejecucion_tardia", R_NOTA,
                      f"corrió {retraso_h:.1f} h después del turno de "
                      f"{turno:%Y-%m-%d %H:%M} UTC (umbral {MAX_RETRASO_H:g} h)",
                      retraso_h=round(retraso_h, 2)))
    else:
        out.append(_c("ejecucion_tardia", R_OK, f"retraso {retraso_h:.1f} h",
                      retraso_h=round(retraso_h, 2)))
    previas = [e for e in historial if not e.get("reproceso") and e.get("corrida_utc")]
    if previas:
        ant = _a_datetime(previas[-1]["corrida_utc"])
        omitidos = (turno.date() - turno_programado(ant).date()).days - 1
        if omitidos > 0:
            out.append(_c("ejecucion_omitida", R_NOTA,
                          f"{omitidos} turno(s) del cron sin corrida desde "
                          f"{ant:%Y-%m-%d %H:%M} UTC", turnos_omitidos=omitidos))
    return out


def evaluar(obs: dict, historial: list[dict]) -> dict:
    """Combina los controles de una corrida en un estado.

    `obs` (observación de la corrida actual):
        corrida_utc          datetime aware o ISO
        dias                 tamaño de la ventana (int)
        reproceso            bool, ventana manual (RADAR_HASTA / RADAR_DIAS)
        disparo              "schedule", "workflow_dispatch", "local", ...
        error                str si hubo error HTTP/SODA (o None)
        paginacion_completa  bool
        registros            filas únicas del filtro completo
        control              conteo SIN los términos LIKE (o None)
        rows_updated_at      datetime/ISO de rowsUpdatedAt (o None)
        view_last_modified   datetime/ISO de viewLastModified (o None)
        firma_esquema        sha256 de las columnas (o None)
    `historial`: corridas anteriores (líneas de data/ejecuciones.jsonl), en orden.

    Devuelve {"estado": ..., "controles": [{nombre, resultado, detalle, ...}]}.
    """
    corrida = _a_datetime(obs["corrida_utc"])
    dias = int(obs.get("dias", 10))
    controles: list[dict] = []

    # --- Puntualidad (solo notas, solo para el cron) ---
    if obs.get("disparo") == "schedule" and not obs.get("reproceso"):
        controles += controles_ejecucion(corrida, historial)

    # --- Conexión / respuesta SODA ---
    if obs.get("error"):
        controles.append(_c("conexion", R_FALLIDA, f"error HTTP/SODA: {obs['error']}"))
        return _resolver(controles, obs)
    controles.append(_c("conexion", R_OK, "fuente respondió sin errores"))

    # --- Paginación ---
    if not obs.get("paginacion_completa", False):
        controles.append(_c("paginacion", R_FALLIDA,
                            "la paginación no terminó en una página corta (posible "
                            "truncamiento por RADAR_LIMITE o corte de la fuente)"))
    else:
        controles.append(_c("paginacion", R_OK, "paginación completa"))

    registros = obs.get("registros")
    control = obs.get("control")

    # --- Consulta de control: mismo filtro sin los LIKE ---
    if control is None:
        controles.append(_c("consulta_control", R_FALLIDA, "no se obtuvo el conteo de control"))
    elif control == 0:
        controles.append(_c("consulta_control", R_FALLIDA,
                            "0 procesos abiertos en la ventana SIN términos: la fuente "
                            "o el filtro base (fecha/estado/modalidad) está roto"))
    else:
        controles.append(_c("consulta_control", R_OK,
                            f"{control} procesos en la ventana sin términos", control=control))
        if (control >= UMBRAL_FILTRO_IGNORADO and registros is not None
                and registros >= control):
            controles.append(_c("filtro_terminos", R_FALLIDA,
                                f"el filtro con términos devolvió {registros} ≥ control "
                                f"{control}: la fuente ignoró los LIKE"))

    # --- Frescura de la fuente (rowsUpdatedAt) en días hábiles colombianos ---
    rua = _a_datetime(obs.get("rows_updated_at"))
    if rua is None:
        controles.append(_c("frescura", R_OMITIDO, "sin rowsUpdatedAt en los metadatos"))
    else:
        f_fuente = rua.astimezone(TZ_COLOMBIA).date()
        f_corrida = corrida.astimezone(TZ_COLOMBIA).date()
        edad = dias_habiles_entre(f_fuente, f_corrida)
        txt = (f"filas actualizadas {rua.astimezone(dt.timezone.utc):%Y-%m-%d %H:%M} UTC; "
               f"{edad} día(s) hábil(es) de antigüedad")
        if edad > MAX_HABILES_FALLIDA:
            r = R_FALLIDA
        elif edad > MAX_HABILES_DEGRADADA:
            r = R_DEGRADADA
        else:
            r = R_OK
        controles.append(_c("frescura", r, txt, dias_habiles=edad))

    # --- Esquema y republicación (contra la corrida anterior con dato) ---
    cambio_fuente = False
    firma, firma_ant = obs.get("firma_esquema"), _ultimo_valor(historial, "firma_esquema")
    if firma is None or firma_ant is None:
        controles.append(_c("esquema", R_OMITIDO, "sin firma previa o actual para comparar"))
    elif firma != firma_ant:
        cambio_fuente = True
        controles.append(_c("esquema", R_DEGRADADA,
                            f"cambió la firma de columnas del dataset ({firma_ant[:12]} → "
                            f"{firma[:12]}): revisar campos usados"))
    else:
        controles.append(_c("esquema", R_OK, "misma firma de columnas"))

    vlm = _a_datetime(obs.get("view_last_modified"))
    vlm_ant = _a_datetime(_ultimo_valor(historial, "view_last_modified"))
    if vlm is None or vlm_ant is None:
        controles.append(_c("republicacion", R_OMITIDO, "sin viewLastModified previo o actual"))
    elif vlm != vlm_ant:
        cambio_fuente = True
        controles.append(_c("republicacion", R_DEGRADADA,
                            f"el dataset fue republicado (viewLastModified "
                            f"{vlm_ant:%Y-%m-%d %H:%M} → {vlm:%Y-%m-%d %H:%M} UTC)"))
    else:
        controles.append(_c("republicacion", R_OK, "sin republicación desde la corrida anterior"))

    # --- Volumen ---
    if registros is not None:
        base = linea_base(historial, dias)
        cv = control_volumen(registros, base)
        if registros == 0 and cambio_fuente and cv["resultado"] != R_FALLIDA:
            cv = dict(cv, resultado=R_FALLIDA,
                      detalle="cero registros justo tras republicación o cambio de esquema: "
                              + cv["detalle"])
        if registros == 0 and cv["resultado"] == R_OMITIDO:
            # Sin línea base no se puede confirmar que cero sea legítimo.
            cv = dict(cv, resultado=R_DEGRADADA,
                      detalle="cero registros sin línea base para confirmarlo: " + cv["detalle"])
        controles.append(cv)

    return _resolver(controles, obs)


def _resolver(controles: list[dict], obs: dict) -> dict:
    resultados = {c["resultado"] for c in controles}
    if R_FALLIDA in resultados:
        estado = FALLIDA
    elif R_DEGRADADA in resultados:
        estado = DEGRADADA
    elif obs.get("registros") == 0:
        estado = OK_SIN_RESULTADOS
    else:
        estado = OK
    return {"estado": estado, "controles": controles}


def resumen_controles(controles: list[dict]) -> dict:
    """{nombre: resultado} compacto, para meta.json."""
    return {c["nombre"]: c["resultado"] for c in controles}


# ---------------------------------------------------------------------------
# 4. Reproducción de la historia (semilla de la línea base y prueba de aceptación)
# ---------------------------------------------------------------------------
CONTROL_SUPUESTO = 20000   # la historia no guardó el conteo de control: se asume > 0


def reproducir_historia(historia: dict) -> list[dict]:
    """Pasa una historia de conteos por `evaluar()` en orden cronológico.

    `historia` = {"dias": 10, "republicaciones": [ISO, ...],
                  "corridas": [{"corrida_utc": ISO, "registros": int}, ...]}

    Supuestos (la historia solo tiene fecha y conteo): consulta de control > 0,
    fuente fresca (rowsUpdatedAt 1 h antes de la corrida), esquema constante y
    viewLastModified constante salvo en las republicaciones listadas.
    Devuelve las entradas de auditoría resultantes, en el mismo orden.
    """
    dias = int(historia.get("dias", 10))
    repub = sorted(_a_datetime(r) for r in historia.get("republicaciones", []))
    vlm_inicial = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    entradas: list[dict] = []
    for c in historia["corridas"]:
        t = _a_datetime(c["corrida_utc"])
        vlm = max([r for r in repub if r <= t], default=vlm_inicial)
        obs = {
            "corrida_utc": t, "dias": dias, "disparo": "schedule", "reproceso": False,
            "error": None, "paginacion_completa": True, "registros": int(c["registros"]),
            "control": CONTROL_SUPUESTO, "rows_updated_at": t - dt.timedelta(hours=1),
            "view_last_modified": vlm, "firma_esquema": "historia",
        }
        ev = evaluar(obs, entradas)
        entradas.append({
            "corrida_utc": t.isoformat(timespec="seconds"), "dias": dias,
            "estado": ev["estado"], "registros": obs["registros"],
            "view_last_modified": vlm.isoformat(timespec="seconds"),
            "firma_esquema": "historia", "controles": ev["controles"],
        })
    return entradas


def semilla_desde_historia(historia: dict) -> list[dict]:
    """Líneas compactas para sembrar data/ejecuciones.jsonl a partir de la historia.

    No se copian firma ni viewLastModified (eran supuestos de la reproducción):
    la primera corrida real compara contra sí misma desde la siguiente.
    """
    out = []
    for e in reproducir_historia(historia):
        linea = {
            "corrida_utc": e["corrida_utc"], "dias": e["dias"], "estado": e["estado"],
            "registros": e["registros"], "origen": "semilla_historia",
        }
        malos = []
        for c in e["controles"]:
            if c["resultado"] not in (R_DEGRADADA, R_FALLIDA):
                continue
            detalle = c["detalle"]
            if c["nombre"] == "republicacion":
                # El valor previo de la reproducción es ficticio: solo se anota el real.
                detalle = f"dataset republicado (viewLastModified {e['view_last_modified']})"
            malos.append(f"{c['nombre']}: {detalle}")
        if malos:
            linea["nota"] = "; ".join(malos)
        out.append(linea)
    return out
