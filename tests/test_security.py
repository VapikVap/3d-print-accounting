"""Защита от злоумышленника в локальной сети (0.0.3).

Атаки разыгрываются на двух эмуляторах P1S с разными сертификатами и настоящим TLS:
«настоящий» принтер A и подставное устройство B. Главное, что проверяется, —
код доступа не уходит устройству B (эмулятор считает полученные логины)."""
import os
import sqlite3
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import mock_bambu  # noqa: E402

from app import bambu, bambu_ftp, db, discovery, printers, security, server  # noqa: E402
from tests.helpers import fresh_db  # noqa: E402
from tests.test_bambu_ftp import FakeFTP, make_3mf  # noqa: E402


def wait(cond, timeout=8):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.1)
    return False


class PinningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = mock_bambu.start(mqtt_port=0, ftp_port=0)   # настоящий принтер
        cls.b = mock_bambu.start(mqtt_port=0, ftp_port=0)   # подставное устройство: другой сертификат
        cls.fp_a = security.probe("127.0.0.1", cls.a[1].port)
        cls.fp_b = security.probe("127.0.0.1", cls.b[1].port)

    @classmethod
    def tearDownClass(cls):
        for _p, m, f in (cls.a, cls.b):
            m.close()
            f.close()

    def setUp(self):
        fresh_db()
        for _p, m, f in (self.a, self.b):
            m.credentials_seen = f.credentials_seen = 0

    def test_two_devices_have_different_certificates(self):
        self.assertNotEqual(self.fp_a, self.fp_b)
        self.assertEqual(security.probe("127.0.0.1", self.a[2].port), self.fp_a)  # MQTT и FTPS — один сертификат

    def test_first_login_remembers_certificate(self):
        pins = []
        c = bambu.BambuClient("127.0.0.1", mock_bambu.SERIAL, mock_bambu.ACCESS_CODE, self.a[1].port, on_pin=pins.append)
        c.start()
        try:
            self.assertTrue(wait(lambda: c.connected))
        finally:
            c.stop()
        self.assertEqual(pins, [self.fp_a])
        self.assertEqual(c.pin, self.fp_a)

    def test_mqtt_code_not_sent_to_impostor(self):
        c = bambu.BambuClient("127.0.0.1", mock_bambu.SERIAL, mock_bambu.ACCESS_CODE, self.b[1].port, pin=self.fp_a)
        c.start()
        try:
            self.assertTrue(wait(lambda: c.pin_mismatch))
            time.sleep(0.5)
        finally:
            c.stop()
        self.assertFalse(c.connected)
        self.assertEqual(c.error, security.PIN_MISMATCH)
        self.assertEqual(self.b[1].credentials_seen, 0)  # пакет CONNECT с паролем не отправлялся

    def test_ftps_code_not_sent_to_impostor(self):
        with mock.patch.object(bambu_ftp, "PORT", self.b[2].port):
            with self.assertRaises(security.PinMismatch):
                bambu_ftp.connect("127.0.0.1", mock_bambu.ACCESS_CODE, pin=self.fp_a)
            plan, _l, msg = bambu_ftp.fetch_plan({"host": "127.0.0.1", "access_code": mock_bambu.ACCESS_CODE,
                                                  "ftp_pin": self.fp_a}, "Деталь")
        self.assertIsNone(plan)
        self.assertIn("подмена", msg)
        self.assertEqual(self.b[2].credentials_seen, 0)

    def test_connection_check_pins_then_detects_swap(self):
        pid = db.insert("printers", {"name": "P1S", "kind": "bambu", "host": "127.0.0.1", "port": self.a[1].port,
                                     "serial": mock_bambu.SERIAL, "access_code": mock_bambu.ACCESS_CODE})
        with mock.patch.object(bambu_ftp, "PORT", self.a[2].port):
            r = printers.printer_test(db.get_row("printers", pid))
        self.assertTrue(r["ok"], r["message"])
        self.assertIn("Сертификат принтера запомнен", r["message"])
        p = db.get_row("printers", pid)
        self.assertEqual((p["mqtt_pin"], p["ftp_pin"]), (self.fp_a, self.fp_a))

        # в сети «появился» другой принтер на том же адресе (порт эмулятора B)
        db.update("printers", pid, {"port": self.b[1].port})
        with mock.patch.object(bambu_ftp, "PORT", self.b[2].port):
            r = printers.printer_test(db.get_row("printers", pid))
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("pin_mismatch"))
        self.assertIn("НЕ отправлен", r["message"])
        self.assertEqual((self.b[1].credentials_seen, self.b[2].credentials_seen), (0, 0))

    def test_poller_alerts_once_and_trust_resets(self):
        pid = db.insert("printers", {"name": "P1S", "kind": "bambu", "host": "127.0.0.1", "port": self.b[1].port,
                                     "serial": mock_bambu.SERIAL, "access_code": mock_bambu.ACCESS_CODE,
                                     "mqtt_pin": self.fp_a})
        poller = printers.Poller()
        try:
            self.assertTrue(wait(lambda: (poller.tick() or True) and (poller.live.get(pid) or {}).get("pin_mismatch"), 10))
            for _ in range(3):
                poller.tick()
            alerts = [e for e in db.all_rows("events") if e["level"] == "error" and "подмена" in e["message"]]
            self.assertEqual(len(alerts), 1)
            self.assertEqual(self.b[1].credentials_seen, 0)
            server.app.testing = True
            with mock.patch.object(server, "poller", poller):
                server.app.test_client().post("/api/printers/%d/trust" % pid)
            self.assertEqual(db.get_row("printers", pid)["mqtt_pin"], "")
            self.assertNotIn(pid, poller._bambu)
        finally:
            poller.stop()


class DiscoverySpoofTest(unittest.TestCase):
    def setUp(self):
        fresh_db()
        self.pid = db.insert("printers", {"name": "P1S", "kind": "bambu", "host": "192.168.1.20", "port": 8883,
                                          "serial": "01P00A391800123", "access_code": "12345678", "mqtt_pin": "aa" * 32})
        self.info = {"serial": "01P00A391800123", "ip": "192.168.1.66", "model": "Bambu Lab P1S", "name": "P1S"}

    def test_ip_not_changed_when_certificate_differs(self):
        with mock.patch.object(security, "probe", return_value="bb" * 32):
            discovery.register(self.info)
        self.assertEqual(db.get_row("printers", self.pid)["host"], "192.168.1.20")
        self.assertTrue(any("Подозрительное объявление" in e["message"] for e in db.all_rows("events")))

    def test_ip_not_changed_when_unreachable(self):
        with mock.patch.object(security, "probe", side_effect=OSError("timed out")):
            discovery.register(self.info)
        self.assertEqual(db.get_row("printers", self.pid)["host"], "192.168.1.20")

    def test_ip_changed_when_same_certificate(self):
        with mock.patch.object(security, "probe", return_value="aa" * 32):
            discovery.register(self.info)
        self.assertEqual(db.get_row("printers", self.pid)["host"], "192.168.1.66")

    def test_flood_of_fake_printers_limited(self):
        for i in range(10):
            discovery.register({"serial": "FAKE%d" % i, "ip": "10.0.0.%d" % i, "model": "Bambu Lab P1S", "name": "x"})
        self.assertEqual(len(db.all_rows("printers")), 1 + discovery.AUTOADD_LIMIT[0])
        self.assertTrue(discovery.register({"serial": "MINE", "ip": "10.0.0.99", "model": "", "name": ""}, force=True))


class SecretsAtRestTest(unittest.TestCase):
    @unittest.skipUnless(security.available(), "DPAPI есть только в Windows")
    def test_codes_encrypted_in_database_file(self):
        fresh_db()
        pid = db.insert("printers", {"name": "P1S", "kind": "bambu", "access_code": "87654321"})
        db.insert("plugs", {"name": "T", "kind": "tuya", "plug_key": "tuyaLocalKey0123", "token": "y0_token_xyz"})
        self.assertEqual(db.get_row("printers", pid)["access_code"], "87654321")  # приложение видит как обычно
        db.update("printers", pid, {"access_code": "11112222"})
        raw = sqlite3.connect(db.DB_PATH)
        try:
            rows = raw.execute("SELECT access_code FROM printers").fetchall() + raw.execute(
                "SELECT plug_key, token FROM plugs").fetchall()
        finally:
            raw.close()
        flat = " ".join(v for r in rows for v in r)
        for secret in ("11112222", "87654321", "tuyaLocalKey0123", "y0_token_xyz"):
            self.assertNotIn(secret, flat)
        self.assertTrue(all(v.startswith("dpapi:") for r in rows for v in r))
        self.assertEqual(db.secret_values(), {"11112222", "tuyaLocalKey0123", "y0_token_xyz"})

    @unittest.skipUnless(security.available(), "DPAPI есть только в Windows")
    def test_old_plaintext_codes_get_encrypted(self):
        fresh_db()
        raw = sqlite3.connect(db.DB_PATH)
        raw.execute("INSERT INTO printers(name, kind, access_code) VALUES ('старый', 'bambu', '55556666')")
        raw.commit()
        raw.close()
        db.init()  # как запуск новой версии
        raw = sqlite3.connect(db.DB_PATH)
        stored = raw.execute("SELECT access_code FROM printers").fetchone()[0]
        raw.close()
        self.assertTrue(stored.startswith("dpapi:"))
        self.assertEqual(db.all_rows("printers")[0]["access_code"], "55556666")

    def test_unreadable_secret_becomes_empty(self):
        self.assertEqual(security.decrypt("dpapi:bm90LWEtZHBhcGktYmxvYg=="), "")


class LimitsTest(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_huge_download_aborted(self):
        ftp = FakeFTP({"/cache": {"Деталь.gcode.3mf": make_3mf() * 50}})
        with mock.patch.object(bambu_ftp, "MAX_DOWNLOAD", 1000):
            plan, local, msg = bambu_ftp.fetch_plan({"host": "h", "access_code": "c", "serial": "S"}, "Деталь",
                                                    connect_fn=lambda *a: ftp)
        self.assertIsNone(plan)
        self.assertIn("больше", msg)
        folder = os.path.join(bambu_ftp.CACHE_DIR, "S")
        self.assertEqual([f for f in os.listdir(folder) if f.endswith(".part")] if os.path.isdir(folder) else [], [])

    def test_moonraker_api_key_header(self):
        with mock.patch.object(printers.requests, "get") as get:
            get.return_value.json.return_value = {"result": {}}
            printers.mr_get({"host": "10.0.0.7", "port": 7125, "access_code": "KEY123"}, "/server/info")
            self.assertEqual(get.call_args.kwargs["headers"], {"X-Api-Key": "KEY123"})
            printers.mr_get({"host": "10.0.0.7", "port": 7125, "access_code": ""}, "/server/info")
            self.assertIsNone(get.call_args.kwargs["headers"])


if __name__ == "__main__":
    unittest.main()
