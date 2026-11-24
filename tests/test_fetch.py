"""fetch_secop.py con un Socrata simulado (sin red)."""
import datetime as dt
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
import calidad as q  # noqa: E402
import fetch_secop as f  # noqa: E402

UTC = dt.timezone.utc
AHORA = dt.datetime(2026, 11, 24, 16, 0, tzinfo=UTC)
SEMILLA = os.path.join(RAIZ, "tests", "datos", "historia_67.json")


class Respuesta(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def http_error(url, code, cuerpo=b"{}", cabeceras=None):
    return urllib.error.HTTPError(url, code, "err", cabeceras or {}, io.BytesIO(cuerpo))


class SocrataFalso:
    """Imita /resource (filas y count(*)) y /api/views (metadatos)."""

    def __init__(self, n=600, control=9000, columnas=None, vlm=1794726340,
                 rua=None, fallas=None, respuesta_filas=None):
        self.filas = [{"id_del_proceso": f"CO1.REQ.{i}", "entidad": "E", "precio_base": "1"}
                      for i in range(n)]
        self.control = control
        self.columnas = columnas or [{"fieldName": c, "dataTypeName": "text"}
                                     for c in f.CAMPOS.split(",")]
        self.vlm = vlm
        self.rua = rua if rua is not None else int((AHORA - dt.timedelta(hours=5)).timestamp())
        self.fallas = list(fallas or [])     # excepciones a lanzar antes de responder
        self.respuesta_filas = respuesta_filas
        self.urls, self.cabeceras = [], []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        self.cabeceras.append(dict(req.header_items()))
        if self.fallas:
            e = self.fallas.pop(0)
            raise e(url) if callable(e) and not isinstance(e, Exception) else e
        if url.startswith(f.META_URL):
            cuerpo = {"rowsUpdatedAt": self.rua, "viewLastModified": self.vlm,
                      "columns": self.columnas}
        else:
            p = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            if p.get("$select") == "count(*)":
                cuerpo = [{"count": str(self.control)}]
            elif self.respuesta_filas is not None:
                cuerpo = self.respuesta_filas
            else:
                off, lim = int(p["$offset"]), int(p["$limit"])
                cuerpo = self.filas[off:off + lim]
        return Respuesta(json.dumps(cuerpo).encode("utf-8"))


class BaseFetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="radar-test-")
        self.data = os.path.join(self.tmp, "data")
        self.esperas = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def correr(self, socrata, ahora=AHORA, **entorno):
        ent = {"GITHUB_EVENT_NAME": "schedule"}
        ent.update(entorno)
        return f.ejecutar(self.data, ahora=ahora, entorno=ent, abrir=socrata,
                          dormir=self.esperas.append, ruta_semilla=SEMILLA)

    def leer(self, nombre, binario=False):
        with open(os.path.join(self.data, nombre), "rb" if binario else "r") as fh:
            return fh.read() if binario else fh.read()

    def jsonl(self):
        return [json.loads(x) for x in self.leer("ejecuciones.jsonl").splitlines() if x]


class TestCliente(BaseFetch):
    def test_dict_de_error_soda_no_son_filas(self):
        cli = f.Cliente(abrir=SocrataFalso(respuesta_filas={
            "code": "query.compiler.malformed", "error": True, "message": "x"}))
        with self.assertRaises(f.ErrorFuente) as cm:
            f.consultar(cli, {"$where": "1=1", "$limit": "10", "$offset": "0"})
        self.assertIn("query.compiler.malformed", str(cm.exception))
        cli = f.Cliente(abrir=SocrataFalso(respuesta_filas={"errorCode": "x", "message": "y"}))
        with self.assertRaises(f.ErrorFuente):
            f.consultar(cli, {"$where": "1=1"})

    def test_reintenta_429_y_5xx_con_backoff(self):
        s = SocrataFalso(fallas=[http_error("u", 429), http_error("u", 503),
                                 urllib.error.URLError("dns")])
        cli = f.Cliente(abrir=s, dormir=self.esperas.append)
        self.assertEqual(f.contar(cli, "1=1"), 9000)
        self.assertEqual(self.esperas, [2.0, 4.0, 8.0])
        self.assertEqual(cli.reintentos, 3)

    def test_respeta_retry_after(self):
        s = SocrataFalso(fallas=[http_error("u", 429, cabeceras={"Retry-After": "7"})])
        cli = f.Cliente(abrir=s, dormir=self.esperas.append)
        f.contar(cli, "1=1")
        self.assertEqual(self.esperas, [7.0])

    def test_4xx_no_se_reintenta(self):
        cuerpo = json.dumps({"errorCode": "query.soql.no-such-column",
                             "message": "No such column"}).encode()
        s = SocrataFalso(fallas=[http_error("u", 400, cuerpo)])
        cli = f.Cliente(abrir=s, dormir=self.esperas.append)
        with self.assertRaises(f.ErrorFuente) as cm:
            f.contar(cli, "1=1")
        self.assertIn("no-such-column", str(cm.exception))
        self.assertEqual(self.esperas, [])

    def test_agota_reintentos(self):
        s = SocrataFalso(fallas=[urllib.error.URLError("caída")] * 10)
        cli = f.Cliente(abrir=s, dormir=self.esperas.append)
        with self.assertRaises(f.ErrorFuente):
            f.contar(cli, "1=1")
        self.assertEqual(len(s.urls), f.INTENTOS)
        self.assertEqual(self.esperas, [2.0, 4.0, 8.0, 16.0])

    def test_token_en_cabecera(self):
        s = SocrataFalso()
        f.contar(f.Cliente("tok-prueba", abrir=s), "1=1")
        self.assertEqual(s.cabeceras[0].get("X-app-token"), "tok-prueba")
        self.assertNotIn("app_token", s.urls[0])
        s = SocrataFalso()
        f.contar(f.Cliente("", abrir=s), "1=1")
        self.assertNotIn("X-app-token", s.cabeceras[0])

    def test_where_codifica_like_y_reproceso(self):
        w = f.where(dt.date(2026, 11, 1), dt.date(2026, 11, 10))
        self.assertIn("fecha_de_publicacion_del < '2026-11-11T00:00:00.000'", w)
        self.assertIn("like '%AMBIENTAL%'", w)
        self.assertNotIn("like", f.where_base(dt.date(2026, 11, 1)))
        self.assertIn("%25AMBIENTAL%25", urllib.parse.urlencode({"$where": w}))


class TestEjecucion(BaseFetch):
    def test_corrida_ok_promueve_y_audita(self):
        a = self.correr(SocrataFalso(n=1500))
        self.assertEqual(a["estado"], q.OK, a["controles"])
        self.assertTrue(a["promovido"])
        filas = json.loads(self.leer("secop_ultimos.json"))
        self.assertEqual(len(filas), 1500)
        meta = json.loads(self.leer("meta.json"))
        for k in ("corrida_utc", "ventana_desde", "dias", "registros", "sha256", "fuente"):
            self.assertIn(k, meta)  # compatibilidad con el radar
        self.assertEqual(meta["ventana_desde"], "2026-11-14")
        self.assertEqual(meta["estado"], q.OK)
        self.assertEqual(meta["ultimo_valido_utc"], meta["corrida_utc"])
        self.assertEqual(meta["controles"]["volumen"], q.R_NOTA)  # 1500 >> mediana: solo nota
        self.assertEqual(len(meta["sha256"]), 16)
        self.assertEqual(a["paginas"], 2)
        # La semilla (68 líneas) + esta corrida.
        lineas = self.jsonl()
        self.assertEqual(len(lineas), 69)
        self.assertEqual(lineas[-1]["estado"], q.OK)
        self.assertEqual(json.loads(self.leer("ultima_ejecucion.json"))["sha256"], meta["sha256"])
        self.assertFalse(any(n.endswith(".tmp") for n in os.listdir(self.data)))

    def test_falla_de_red_conserva_archivo_previo(self):
        self.correr(SocrataFalso(n=600))
        antes_filas = self.leer("secop_ultimos.json", binario=True)
        antes_meta = self.leer("meta.json", binario=True)
        a = self.correr(SocrataFalso(fallas=[urllib.error.URLError("sin red")] * 20),
                        ahora=AHORA + dt.timedelta(days=1))
        self.assertEqual(a["estado"], q.FALLIDA)
        self.assertFalse(a["promovido"])
        self.assertIn("sin red", a["error"])
        self.assertEqual(self.leer("secop_ultimos.json", binario=True), antes_filas)
        self.assertEqual(self.leer("meta.json", binario=True), antes_meta)
        ultima = json.loads(self.leer("ultima_ejecucion.json"))
        self.assertEqual(ultima["estado"], q.FALLIDA)
        self.assertEqual(ultima["ultimo_valido_utc"], AHORA.isoformat(timespec="seconds"))
        self.assertEqual([e["estado"] for e in self.jsonl()[-2:]], [q.OK, q.FALLIDA])

    def test_incidente_15_nov_reproducido(self):
        """Cero filas + republicación: FALLIDA y el archivo previo intacto."""
        self.correr(SocrataFalso(n=574, vlm=1794000000))
        antes = self.leer("secop_ultimos.json", binario=True)
        a = self.correr(SocrataFalso(n=0, vlm=1794726340), ahora=AHORA + dt.timedelta(days=1))
        self.assertEqual(a["estado"], q.FALLIDA)
        r = q.resumen_controles(a["controles"])
        self.assertEqual((r["republicacion"], r["volumen"]), (q.R_DEGRADADA, q.R_FALLIDA))
        self.assertEqual(self.leer("secop_ultimos.json", binario=True), antes)

    def test_error_soda_en_corrida(self):
        self.correr(SocrataFalso(n=600))
        antes = self.leer("secop_ultimos.json", binario=True)
        a = self.correr(SocrataFalso(respuesta_filas={"error": True, "code": "x",
                                                      "message": "boom"}),
                        ahora=AHORA + dt.timedelta(days=1))
        self.assertEqual(a["estado"], q.FALLIDA)
        self.assertIn("error SODA", a["error"])
        self.assertEqual(self.leer("secop_ultimos.json", binario=True), antes)

    def test_control_cero(self):
        a = self.correr(SocrataFalso(n=0, control=0))
        self.assertEqual(a["estado"], q.FALLIDA)
        self.assertFalse(os.path.exists(os.path.join(self.data, "secop_ultimos.json")))

    def test_paginacion_truncada(self):
        a = self.correr(SocrataFalso(n=2500), RADAR_LIMITE="2000")
        self.assertEqual(a["estado"], q.FALLIDA)
        self.assertFalse(a["paginacion_completa"])

    def test_cambio_de_esquema_degrada_pero_publica(self):
        self.correr(SocrataFalso(n=600))
        cols = [{"fieldName": c, "dataTypeName": "text"} for c in f.CAMPOS.split(",")]
        cols.append({"fieldName": "columna_nueva", "dataTypeName": "number"})
        a = self.correr(SocrataFalso(n=600, columnas=cols), ahora=AHORA + dt.timedelta(days=1))
        self.assertEqual(a["estado"], q.DEGRADADA)
        self.assertTrue(a["promovido"])
        self.assertEqual(json.loads(self.leer("meta.json"))["estado"], q.DEGRADADA)

    def test_fuente_atrasada(self):
        viejo = int(dt.datetime(2026, 11, 13, 20, tzinfo=UTC).timestamp())
        a = self.correr(SocrataFalso(n=600, rua=viejo))
        self.assertEqual(a["estado"], q.FALLIDA)
        self.assertEqual(q.resumen_controles(a["controles"])["frescura"], q.R_FALLIDA)

    def test_reejecucion_idempotente(self):
        a1 = self.correr(SocrataFalso(n=600))
        antes = self.leer("secop_ultimos.json", binario=True)
        a2 = self.correr(SocrataFalso(n=600), ahora=AHORA + dt.timedelta(hours=2))
        self.assertFalse(a1["sin_cambios"])
        self.assertTrue(a2["sin_cambios"])
        self.assertEqual(a1["sha256"], a2["sha256"])
        self.assertEqual(self.leer("secop_ultimos.json", binario=True), antes)
        self.assertTrue(json.loads(self.leer("meta.json"))["sin_cambios"])

    def test_reproceso_de_ventana(self):
        s = SocrataFalso(n=600)
        a = self.correr(s, RADAR_DIAS="5", RADAR_HASTA="2026-11-10",
                        GITHUB_EVENT_NAME="workflow_dispatch")
        self.assertTrue(a["reproceso"])
        self.assertEqual((a["ventana_desde"], a["ventana_hasta"]), ("2026-11-05", "2026-11-10"))
        self.assertIn("2026-11-11T00%3A00%3A00.000", s.urls[-1])
        # La línea base solo usa ventanas de 10 días: con 5 días el volumen se omite.
        self.assertEqual(q.resumen_controles(a["controles"])["volumen"], q.R_OMITIDO)
        self.assertTrue(a["promovido"])
        self.assertEqual(json.loads(self.leer("meta.json"))["dias"], 5)

    def test_parametros_invalidos_fallan_auditados(self):
        a = self.correr(SocrataFalso(), RADAR_HASTA="no-es-fecha")
        self.assertEqual(a["estado"], q.FALLIDA)
        self.assertIn("ValueError", a["error"])

    def test_sin_semilla_ni_historial(self):
        a = f.ejecutar(self.data, ahora=AHORA, entorno={}, abrir=SocrataFalso(n=600),
                       dormir=self.esperas.append, ruta_semilla=os.path.join(self.tmp, "no"))
        self.assertEqual(a["estado"], q.OK)   # volumen OMITIDO no degrada
        self.assertEqual(q.resumen_controles(a["controles"])["volumen"], q.R_OMITIDO)

    def test_meta_antigua_da_ultimo_valido(self):
        os.makedirs(self.data)
        vieja = {"corrida_utc": "2026-11-23T22:07:47+00:00", "ventana_desde": "2026-11-13",
                 "dias": 10, "registros": 620, "sha256": "e5800b6d11d8dfac", "fuente": f.BASE_URL}
        with open(os.path.join(self.data, "meta.json"), "w") as fh:
            json.dump(vieja, fh)
        a = self.correr(SocrataFalso(fallas=[urllib.error.URLError("x")] * 20))
        self.assertEqual(a["ultimo_valido_utc"], "2026-11-23T22:07:47+00:00")


if __name__ == "__main__":
    unittest.main()
