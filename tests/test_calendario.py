"""Calendario hábil colombiano (calidad.py)."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import calidad as q  # noqa: E402

D = dt.date


class TestCalendario(unittest.TestCase):
    def test_pascua(self):
        self.assertEqual(q.pascua(2026), D(2026, 4, 5))
        self.assertEqual(q.pascua(2027), D(2027, 3, 28))
        self.assertEqual(q.pascua(2024), D(2024, 3, 31))

    def test_festivos_trasladados_2026(self):
        # 1-nov-2026 es domingo -> lunes 2; 11-nov es miércoles -> lunes 16.
        self.assertFalse(q.es_habil(D(2026, 11, 2)))
        self.assertFalse(q.es_habil(D(2026, 11, 16)))
        self.assertTrue(q.es_habil(D(2026, 11, 11)))
        # Ley 2578 de 2026: 9-jul (jueves) -> lunes 13-jul.
        self.assertIn(D(2026, 7, 13), q.festivos(2026))
        self.assertTrue(q.es_habil(D(2026, 7, 9)))
        # 12-oct-2026 cae lunes: se queda.
        self.assertIn(D(2026, 10, 12), q.festivos(2026))

    def test_9_julio_solo_desde_2026(self):
        self.assertNotIn(D(2025, 7, 14), q.festivos(2025))

    def test_fijos(self):
        for d in (D(2026, 1, 1), D(2026, 5, 1), D(2026, 7, 20), D(2026, 8, 7),
                  D(2026, 12, 8), D(2026, 12, 25)):
            self.assertIn(d, q.festivos(2026), d)

    def test_festivos_2026_completos(self):
        esperados = {
            D(2026, 1, 1), D(2026, 1, 12), D(2026, 3, 23), D(2026, 4, 2), D(2026, 4, 3),
            D(2026, 5, 1), D(2026, 5, 18), D(2026, 6, 8), D(2026, 6, 15), D(2026, 6, 29),
            D(2026, 7, 13), D(2026, 7, 20), D(2026, 8, 7), D(2026, 8, 17), D(2026, 10, 12),
            D(2026, 11, 2), D(2026, 11, 16), D(2026, 12, 8), D(2026, 12, 25),
        }
        self.assertEqual(set(q.festivos(2026)), esperados)

    def test_semana_santa_y_abril_2027(self):
        self.assertFalse(q.es_habil(D(2027, 3, 25)))  # Jueves Santo
        self.assertFalse(q.es_habil(D(2027, 3, 26)))  # Viernes Santo
        self.assertFalse(q.es_habil(D(2027, 3, 22)))  # San José (19-mar viernes) -> lunes
        self.assertEqual(q.sumar_habiles(D(2027, 3, 31), 5), D(2027, 4, 7))
        # Ascensión, Corpus y Sagrado Corazón 2027 caen en lunes.
        for n in (43, 64, 71):
            d = q.pascua(2027) + dt.timedelta(days=n)
            self.assertEqual(d.weekday(), 0)
            self.assertIn(d, q.festivos(2027))

    def test_fin_de_semana(self):
        self.assertFalse(q.es_habil(D(2026, 11, 21)))
        self.assertFalse(q.es_habil(D(2026, 11, 22)))
        self.assertTrue(q.es_habil(D(2026, 11, 24)))

    def test_dias_habiles_entre(self):
        # Viernes 13-nov -> martes 17-nov: sáb, dom, lunes festivo 16 => 1 hábil.
        self.assertEqual(q.dias_habiles_entre(D(2026, 11, 13), D(2026, 11, 17)), 1)
        self.assertEqual(q.dias_habiles_entre(D(2026, 11, 17), D(2026, 11, 17)), 0)
        self.assertEqual(q.dias_habiles_entre(D(2026, 11, 17), D(2026, 11, 13)), -1)
        self.assertEqual(q.dias_habiles_entre(D(2026, 11, 1), D(2026, 11, 30)), 19)  # 21 días lun-vie menos 2 y 16

    def test_sumar_habiles(self):
        self.assertEqual(q.sumar_habiles(D(2026, 11, 13), 1), D(2026, 11, 17))
        self.assertEqual(q.sumar_habiles(D(2026, 11, 17), -1), D(2026, 11, 13))
        self.assertEqual(q.sumar_habiles(D(2026, 11, 17), 0), D(2026, 11, 17))
        d = D(2026, 12, 18)
        self.assertEqual(q.dias_habiles_entre(d, q.sumar_habiles(d, 7)), 7)


if __name__ == "__main__":
    unittest.main()
