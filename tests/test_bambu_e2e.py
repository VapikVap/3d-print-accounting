"""Сквозной прогон Bambu на эмуляторе P1S (tools/mock_bambu.py): настоящий TLS-MQTT
(paho) и неявный FTPS — те же пути кода, что пойдут к живому принтеру.
Формат отчётов в эмуляторе — по открытым описаниям, не снят с реального P1S."""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import mock_bambu  # noqa: E402

from app import bambu_ftp, db, parts, printers  # noqa: E402
from tests.helpers import fresh_db  # noqa: E402


class BambuE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.printer, cls.mqtt, cls.ftp = mock_bambu.start(mqtt_port=0, ftp_port=0, print_secs=4)
        cls.ftp_patch = mock.patch.object(bambu_ftp, "PORT", cls.ftp.port)
        cls.ftp_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.ftp_patch.stop()
        cls.mqtt.close()
        cls.ftp.close()

    def setUp(self):
        fresh_db()
        self.white = db.insert("filaments", {"name": "Basic", "material": "PLA", "color": "белый",
                                             "color_hex": "#FFFFFF", "price_kg": 2000, "remaining_g": 1000})
        self.black = db.insert("filaments", {"name": "Basic", "material": "PLA", "color": "чёрный",
                                             "color_hex": "#101010", "price_kg": 1500, "remaining_g": 500})
        self.petg = db.insert("filaments", {"name": "HF", "material": "PETG", "color": "красный",
                                            "color_hex": "#C0301F", "price_kg": 1800, "remaining_g": 1000})
        self.pid = db.insert("printers", {"name": "P1S", "model": "Bambu Lab P1S", "kind": "bambu",
                                          "host": "127.0.0.1", "port": self.mqtt.port, "serial": mock_bambu.SERIAL,
                                          "access_code": mock_bambu.ACCESS_CODE, "avg_power_w": 120})
        parts.set_slot(self.pid, "A1", self.white)  # белый привязан к слоту, чёрный найдётся по цвету
        self.poller = printers.Poller()

    def tearDown(self):
        self.poller.stop()

    def _tick_until(self, cond, timeout=15):
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.poller.tick()
            if cond():
                return True
            time.sleep(0.3)
        return False

    def test_connection_check(self):
        r = printers.printer_test(db.get_row("printers", self.pid))
        self.assertTrue(r["ok"], r["message"])
        self.assertIn("gcode_state=IDLE", r["message"])
        self.assertIn("FTPS работает", r["message"])
        self.assertIn("A — AMS 2 Pro", r["message"])
        self.assertIn("прошивка 01.08.02.00", r["message"])
        self.assertEqual(len(r["parsed"]["trays"]), 5)  # 4 слота AMS + внешняя
        unit = r["parsed"]["ams_units"][0]
        self.assertEqual((unit["type"], unit["detected"], unit["humidity_pct"]), ("AMS 2 Pro", True, 18.0))

    def test_wrong_access_code(self):
        db.update("printers", self.pid, {"access_code": "00000000"})
        r = printers.printer_test(db.get_row("printers", self.pid))
        self.assertFalse(r["ok"])
        self.assertIn("код доступа", r["message"])

    def test_two_color_print_full_cycle(self):
        live = lambda: self.poller.live.get(self.pid) or {}
        self.assertTrue(self._tick_until(lambda: live().get("state") == "standby"), live())
        self.assertEqual([t["tray"] for t in live()["trays"]], ["A1", "A2", "A3", "A4", "EXT"])

        self.printer.start_print(secs=4)
        self.assertTrue(self._tick_until(lambda: printers.active_job(self.pid) is not None))
        jid = printers.active_job(self.pid)["id"]
        self.assertTrue(self._tick_until(lambda: db.get_row("jobs", jid)["status"] == "complete"))
        self.poller._pool.shutdown(wait=True)  # дождаться фоновой загрузки файла по FTPS

        j = db.get_row("jobs", jid)
        ps = parts.list_parts(jid)
        self.assertEqual(self.ftp.downloads[-1], "/cache/Два цвета.gcode.3mf")
        self.assertEqual([(p["material"], p["color_hex"], p["grams"]) for p in ps],
                         [("PLA", "#FFFFFF", 12.5), ("PLA", "#000000", 3.2)])
        self.assertEqual([(p["filament_id"], p["tray"], p["match"]) for p in ps],
                         [(self.white, "A1", "tray"), (self.black, "A2", "color")])
        self.assertEqual(j["filament_g"], 15.7)
        self.assertEqual(j["filament_source"], "plan")
        self.assertEqual(j["cost_filament"], 29.8)  # 12.5 г × 2000 + 3.2 г × 1500 за кг
        self.assertEqual(j["filament_id"], self.white)  # основная — где больше граммов
        self.assertGreater(j["duration_s"], 2)
        self.assertEqual(j["energy_source"], "estimate")
        self.assertAlmostEqual(db.get_row("filaments", self.white)["remaining_g"], 987.5)
        self.assertAlmostEqual(db.get_row("filaments", self.black)["remaining_g"], 496.8)
        self.assertEqual(db.get_row("filaments", self.petg)["remaining_g"], 1000)
        self.assertGreaterEqual(self.mqtt.connections, 1)


class StartTimeEstimateTest(unittest.TestCase):
    def test_print_seen_midway(self):
        fresh_db()
        pid = db.insert("printers", {"name": "P1S", "kind": "bambu", "host": "h", "serial": "s"})
        poller = printers.Poller()
        status = {"state": "printing", "filename": "Деталь", "progress": 0.5, "remaining_s": 1800, "trays": []}
        job = poller._create_job(db.get_row("printers", pid), status, now=10000)
        self.assertAlmostEqual(job["started_at"], 10000 - 1800)  # половина пройдена — прошло столько же
        self.assertIn("оценено", job["notes"])


if __name__ == "__main__":
    unittest.main()
