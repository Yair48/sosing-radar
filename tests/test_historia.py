"""Prueba de aceptación: los 67 días reales (2026-09-18 .. 2026-11-23).

Supuestos de la reproducción (ver calidad.reproducir_historia): control > 0,
fuente fresca, esquema constante y viewLastModified que cambia solo el
2026-11-15 (republicación real del dataset, 07:05 UTC).
"""
import json
import os
import sys
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
import calidad as q  # noqa: E402

with open(os.path.join(RAIZ, "tests", "datos", "historia_67.json"), encoding="utf-8") as fh:
    HISTORIA = json.load(fh)


class TestHistoria67(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entradas = q.reproducir_historia(HISTORIA)
        cls.por_dia = {}
        for e in cls.entradas:
            cls.por_dia.setdefault(e["corrida_utc"][:10], []).append(e["estado"])

    def test_tamano(self):
        self.assertEqual(len(self.por_dia), 67)
        self.assertEqual(len(self.entradas), 68)

    def test_incidente_15_nov_fallida(self):
        self.assertEqual(self.por_dia["2026-11-15"], [q.FALLIDA])

    def test_1_oct_degradada(self):
        self.assertEqual(self.por_dia["2026-10-01"], [q.DEGRADADA])

    def test_cero_falsos_positivos(self):
        otros = {d: s for d, s in self.por_dia.items() if d not in ("2026-11-15", "2026-10-01")}
        self.assertEqual(len(otros), 65)
        malos = {d: s for d, s in otros.items() if set(s) != {q.OK}}
        self.assertEqual(malos, {})

    def test_16_nov_no_hereda_la_republicacion(self):
        # La corrida fallida del 15 registró el viewLastModified nuevo.
        self.assertEqual(self.por_dia["2026-11-16"], [q.OK])

    def test_semilla_versionada_coincide(self):
        """data/ejecuciones.jsonl arranca con la semilla de esta misma historia."""
        ruta = os.path.join(RAIZ, "data", "ejecuciones.jsonl")
        with open(ruta, encoding="utf-8") as fh:
            lineas = [json.loads(x) for x in fh if x.strip()]
        semilla = [e for e in lineas if e.get("origen") == "semilla_historia"]
        esperado = q.semilla_desde_historia(HISTORIA)
        self.assertEqual([(e["corrida_utc"], e["estado"], e["registros"]) for e in semilla],
                         [(e["corrida_utc"], e["estado"], e["registros"]) for e in esperado])


if __name__ == "__main__":
    unittest.main()
