"""Секреты не должны попадать в API, журнал и Excel."""
import io
import unittest
import zipfile

from app import db, server
from tests.helpers import fresh_db

CODE, KEY, TOKEN = "AC-87654321", "tuyaLocalKey0123", "y0_AgAAAA-secret-token"


class SecretsTest(unittest.TestCase):
    def setUp(self):
        fresh_db()
        server.app.testing = True
        self.c = server.app.test_client()
        r = self.c.post("/api/printers", json={"name": "X1C", "kind": "bambu", "host": "10.0.0.2",
                                               "serial": "00M00A000000000", "access_code": CODE})
        self.assertEqual(r.status_code, 201)
        self.pid = r.get_json()["id"]
        self.c.post("/api/plugs", json={"name": "T", "kind": "tuya", "host": "10.0.0.3", "device_id": "d1",
                                        "plug_key": KEY})
        self.c.post("/api/plugs", json={"name": "Y", "kind": "yandex", "device_id": "y1", "token": TOKEN})
        db.insert("jobs", {"printer_id": self.pid, "printer_name": "X1C", "filename": "a", "status": "complete",
                           "started_at": 1, "notes": ""})

    def _assert_clean(self, text):
        for s in (CODE, KEY, TOKEN):
            self.assertNotIn(s, text)

    def test_api_responses(self):
        for url in ("/api/printers", "/api/plugs", "/api/state", "/api/jobs", "/api/events", "/api/settings"):
            self._assert_clean(self.c.get(url).get_data(as_text=True))
        p = self.c.get("/api/printers").get_json()[0]
        self.assertTrue(p["has_access_code"])
        self.assertNotIn("access_code", p)

    def test_empty_secret_keeps_old(self):
        self.c.put("/api/printers/%d" % self.pid, json={"name": "X1C-2", "access_code": ""})
        self.assertEqual(db.get_row("printers", self.pid)["access_code"], CODE)
        self.c.put("/api/printers/%d" % self.pid, json={"clear_access_code": True})
        self.assertEqual(db.get_row("printers", self.pid)["access_code"], "")

    def test_log_redaction(self):
        db.log_event("warn", "test", "ответ с ключом %s и токеном %s" % (KEY, TOKEN))
        self._assert_clean(self.c.get("/api/events").get_data(as_text=True))

    def test_excel(self):
        data = self.c.get("/api/export.xlsx").data
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            text = "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())
        self._assert_clean(text)
        self.assertIn("X1C", text)

    def test_server_binds_localhost(self):
        self.assertEqual(server.HOST, "127.0.0.1")


class GuardTest(unittest.TestCase):
    """Чужая страница в браузере не должна менять данные."""

    def setUp(self):
        fresh_db()
        server.app.testing = False
        self.c = server.app.test_client()
        self.own = "127.0.0.1:%d" % server.PORT

    def tearDown(self):
        server.app.testing = True

    def test_own_origin_allowed(self):
        r = self.c.post("/api/filaments", json={"name": "A"}, base_url="http://" + self.own,
                        headers={"Origin": "http://" + self.own, "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(r.status_code, 201)

    def test_foreign_origin_rejected(self):
        r = self.c.post("/api/filaments", data='{"name":"evil"}', content_type="text/plain",
                        base_url="http://" + self.own,
                        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(db.all_rows("filaments"), [])

    def test_dns_rebinding_host_rejected(self):
        r = self.c.get("/api/printers", base_url="http://evil.example:%d" % server.PORT)
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
