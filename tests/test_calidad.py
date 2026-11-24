"""Volumen robusto y evaluación de estado (calidad.py), con casos sintéticos."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import calidad as q  # noqa: E402

UTC = dt.timezone.utc
# Martes 24-nov-2026, 16:00 UTC (5:30 h de retraso sobre el turno de 10:30).
AHORA = dt.datetime(2026, 11, 24, 16, 0, tzinfo=UTC)
VLM = "2026-11-15T07:05:40+00:00"


def historial(n=28, valor=500, inicio=AHORA - dt.timedelta(days=28), **campos):
    """n corridas OK diarias alrededor de `valor` (con algo de dispersión)."""
    out = []
    for i in range(n):
        t = inicio + dt.timedelta(days=i)
        e = {"corrida_utc": t.isoformat(), "dias": 10, "estado": q.OK,
             "registros": valor + (i % 7 - 3) * 10,
             "firma_esquema": "f1", "view_last_modified": VLM}
        e.update(campos)
        out.append(e)
    return out


def obs(**cambios):
    o = {"corrida_utc": AHORA, "dias": 10, "disparo": "schedule", "reproceso": False,
         "error": None, "paginacion_completa": True, "registros": 510, "control": 9000,
         "rows_updated_at": AHORA - dt.timedelta(hours=3), "view_last_modified": VLM,
         "firma_esquema": "f1"}
    o.update(cambios)
    return o


def control(ev, nombre):
    return next(c for c in ev["controles"] if c["nombre"] == nombre)


class TestVolumen(unittest.TestCase):
    def test_z_modificado_nist(self):
        m, med, mad = q.z_modificado(10, [1, 2, 3, 4, 5])
        self.assertEqual((med, mad), (3, 1))
        self.assertAlmostEqual(m, 0.6745 * 7)

    def test_mad_cero_usa_desviacion_media(self):
        base = [10, 10, 10, 10, 10, 10, 12, 14]
        m, med, mad = q.z_modificado(20, base)
        self.assertEqual(mad, 0)
        mean_ad = sum(abs(v - 10) for v in base) / len(base)
        self.assertAlmostEqual(m, 10 / (1.253314 * mean_ad))

    def test_base_constante_no_explota(self):
        c = q.control_volumen(7, [7] * 10)
        self.assertEqual(c["resultado"], q.R_OK)
        self.assertIsNone(c["m"])

    def test_menos_de_7_se_omite(self):
        self.assertEqual(q.control_volumen(10, [500] * 6)["resultado"], q.R_OMITIDO)

    def test_colapso(self):
        base = [500, 510, 490, 505, 495, 500, 520]
        self.assertEqual(q.control_volumen(100, base)["resultado"], q.R_FALLIDA)
        self.assertEqual(q.control_volumen(0, base)["resultado"], q.R_FALLIDA)

    def test_caida_degrada_y_alza_solo_anota(self):
        base = [500, 510, 490, 505, 495, 500, 520, 480]
        self.assertEqual(q.control_volumen(400, base)["resultado"], q.R_DEGRADADA)
        self.assertEqual(q.control_volumen(650, base)["resultado"], q.R_NOTA)

    def test_linea_base_solo_validas_y_comparables(self):
        h = [{"estado": q.OK, "registros": 1, "dias": 10},
             {"estado": q.FALLIDA, "registros": 0, "dias": 10},
             {"estado": q.DEGRADADA, "registros": 2, "dias": 10},
             {"estado": q.OK, "registros": 3, "dias": 30},
             {"estado": q.OK, "registros": 4, "dias": 10, "reproceso": True},
             {"estado": q.OK_SIN_RESULTADOS, "registros": 0, "dias": 10}]
        self.assertEqual(q.linea_base(h, 10), [1, 2, 0])
        self.assertEqual(len(q.linea_base(historial(40), 10)), 28)


class TestEvaluar(unittest.TestCase):
    def test_corrida_normal_ok(self):
        ev = q.evaluar(obs(), historial())
        self.assertEqual(ev["estado"], q.OK, ev)

    def test_error_soda_es_fallida(self):
        ev = q.evaluar(obs(error="HTTP 400: query.soql.no-such-column"), historial())
        self.assertEqual(ev["estado"], q.FALLIDA)
        self.assertEqual(control(ev, "conexion")["resultado"], q.R_FALLIDA)

    def test_paginacion_incompleta_es_fallida(self):
        self.assertEqual(q.evaluar(obs(paginacion_completa=False), historial())["estado"],
                         q.FALLIDA)

    def test_control_cero_es_fallida(self):
        ev = q.evaluar(obs(registros=0, control=0), historial(valor=5))
        self.assertEqual(ev["estado"], q.FALLIDA)
        self.assertEqual(control(ev, "consulta_control")["resultado"], q.R_FALLIDA)

    def test_ok_sin_resultados_legitimo(self):
        h = historial(valor=40)
        for e in h:
            e["registros"] = [0, 1, 2, 3, 5][int(e["corrida_utc"][8:10]) % 5]
        ev = q.evaluar(obs(registros=0, control=1200), h)
        self.assertEqual(ev["estado"], q.OK_SIN_RESULTADOS, ev)

    def test_cero_con_mediana_alta_es_fallida(self):
        self.assertEqual(q.evaluar(obs(registros=0), historial())["estado"], q.FALLIDA)

    def test_cero_sin_linea_base_degrada(self):
        ev = q.evaluar(obs(registros=0), historial(n=3))
        self.assertEqual(ev["estado"], q.DEGRADADA)

    def test_filtro_ignorado(self):
        ev = q.evaluar(obs(registros=9000, control=9000), historial())
        self.assertEqual(control(ev, "filtro_terminos")["resultado"], q.R_FALLIDA)

    def test_cambio_de_esquema_degrada(self):
        ev = q.evaluar(obs(firma_esquema="f2"), historial())
        self.assertEqual(ev["estado"], q.DEGRADADA)
        self.assertIn("firma de columnas", control(ev, "esquema")["detalle"])

    def test_republicacion_degrada_y_con_cero_falla(self):
        nueva = "2026-11-24T07:00:00+00:00"
        ev = q.evaluar(obs(view_last_modified=nueva), historial())
        self.assertEqual(ev["estado"], q.DEGRADADA)
        self.assertIn("republicado", control(ev, "republicacion")["detalle"])
        # Republicación + cero, aun con histórico bajo (donde cero sería legítimo).
        h = historial()
        for e in h:
            e["registros"] = 3
        ev = q.evaluar(obs(view_last_modified=nueva, registros=0), h)
        self.assertEqual(ev["estado"], q.FALLIDA)
        self.assertEqual(control(ev, "volumen")["resultado"], q.R_FALLIDA)

    def test_comparacion_contra_corrida_anterior_aunque_fallida(self):
        h = historial()
        h.append({"corrida_utc": (AHORA - dt.timedelta(hours=20)).isoformat(),
                  "estado": q.FALLIDA, "registros": None, "dias": 10,
                  "firma_esquema": "f1", "view_last_modified": "2026-11-23T07:00:00+00:00"})
        ev = q.evaluar(obs(view_last_modified="2026-11-23T07:00:00+00:00"), h)
        self.assertEqual(control(ev, "republicacion")["resultado"], q.R_OK)

    def test_fuente_atrasada(self):
        # Martes 24-nov. Última actualización miércoles 18-nov: 4 hábiles (19, 20, 23, 24).
        ev = q.evaluar(obs(rows_updated_at=dt.datetime(2026, 11, 18, 20, tzinfo=UTC)),
                       historial())
        self.assertEqual(ev["estado"], q.DEGRADADA)
        self.assertEqual(control(ev, "frescura")["dias_habiles"], 4)
        # Viernes 13-nov: 6 hábiles (17..20, 23, 24; el 16 es festivo) -> FALLIDA.
        ev = q.evaluar(obs(rows_updated_at=dt.datetime(2026, 11, 13, 20, tzinfo=UTC)),
                       historial())
        self.assertEqual(ev["estado"], q.FALLIDA)
        # Viernes 20-nov visto el martes 24: 2 hábiles -> OK (fin de semana no cuenta).
        ev = q.evaluar(obs(rows_updated_at=dt.datetime(2026, 11, 20, 20, tzinfo=UTC)),
                       historial())
        self.assertEqual(control(ev, "frescura")["resultado"], q.R_OK)

    def test_frescura_en_hora_colombia(self):
        # 2026-11-24 03:00 UTC = 23-nov 22:00 en Colombia.
        t = dt.datetime(2026, 11, 24, 3, tzinfo=UTC)
        ev = q.evaluar(obs(rows_updated_at=t), historial())
        self.assertEqual(control(ev, "frescura")["dias_habiles"], 1)

    def test_ejecucion_omitida_es_nota(self):
        h = historial(inicio=AHORA - dt.timedelta(days=31))   # última corrida hace 4 días
        ev = q.evaluar(obs(), h)
        c = control(ev, "ejecucion_omitida")
        self.assertEqual((c["resultado"], c["turnos_omitidos"]), (q.R_NOTA, 3))
        self.assertEqual(ev["estado"], q.OK)

    def test_lunes_con_27h_no_es_omitida(self):
        h = historial()
        h[-1]["corrida_utc"] = dt.datetime(2026, 11, 23, 12, 0, tzinfo=UTC).isoformat()
        tarde = dt.datetime(2026, 11, 24, 15, 30, tzinfo=UTC)          # 27.5 h después
        ev = q.evaluar(obs(corrida_utc=tarde), h)
        self.assertNotIn("ejecucion_omitida", q.resumen_controles(ev["controles"]))

    def test_retraso_es_nota(self):
        tarde = dt.datetime(2026, 11, 24, 21, 0, tzinfo=UTC)   # 10.5 h tarde
        ev = q.evaluar(obs(corrida_utc=tarde), historial())
        c = control(ev, "ejecucion_tardia")
        self.assertEqual(c["resultado"], q.R_NOTA)
        self.assertAlmostEqual(c["retraso_h"], 10.5)
        self.assertEqual(ev["estado"], q.OK)

    def test_manual_no_evalua_puntualidad(self):
        ev = q.evaluar(obs(disparo="workflow_dispatch",
                           corrida_utc=dt.datetime(2026, 11, 24, 23, tzinfo=UTC)), historial())
        self.assertNotIn("ejecucion_tardia", q.resumen_controles(ev["controles"]))


if __name__ == "__main__":
    unittest.main()
