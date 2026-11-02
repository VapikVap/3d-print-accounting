"""Сквозной прогон: эмулятор Moonraker + Shelly Gen2 -> фоновый опрос -> задание."""
import os
import sys
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import mock_moonraker as mock  # noqa: E402

from app import db, printers  # noqa: E402
from tests.helpers import fresh_db  # noqa: E402


class MockE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mr = ThreadingHTTPServer(("127.0.0.1", 0), mock.make_handler("moonraker"))
        cls.pl = ThreadingHTTPServer(("127.0.0.1", 0), mock.make_handler("plug"))
        for s in (cls.mr, cls.pl):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        cls.mr_port, cls.pl_port = cls.mr.server_address[1], cls.pl.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.mr.shutdown()
        cls.pl.shutdown()

    def setUp(self):
        fresh_db()
        mock.SIM.reset()
        mock.SIM.history.clear()
        self.fil = db.insert("filaments", {"name": "PLA", "price_kg": 1000, "remaining_g": 1000})
        self.plug = db.insert("plugs", {"name": "Shelly", "kind": "shelly2", "host": "127.0.0.1:%d" % self.pl_port})
        self.pid = db.insert("printers", {"name": "K1 Max", "kind": "moonraker", "host": "127.0.0.1",
                                          "port": self.mr_port, "plug_id": self.plug, "filament_id": self.fil})
        self.poller = printers.Poller()
        self.base = "http://127.0.0.1:%d" % self.mr_port

    def test_connection_check(self):
        res = printers.printer_test(db.get_row("printers", self.pid))
        self.assertTrue(res["ok"], res["message"])
        res = printers.plug_test(db.get_row("plugs", self.plug))
        self.assertTrue(res["ok"], res["message"])
        self.assertEqual(res["parsed"]["power_w"], mock.IDLE_W)

    def test_unreachable(self):
        db.update("printers", self.pid, {"port": 1})
        res = printers.printer_test(db.get_row("printers", self.pid))
        self.assertFalse(res["ok"])
        self.assertIn("порт", res["message"])

    def _print(self, end):
        self.poller.tick()
        requests.get(self.base + "/mock/start", params={"filename": "cube.gcode", "duration": 3, "mm": 1000})
        for _ in range(3):
            time.sleep(0.6)
            self.poller.tick()
        job = printers.active_job(self.pid)
        self.assertIsNotNone(job)
        self.assertGreater(job["filament_mm"], 0)
        if end == "complete":
            time.sleep(1.5)
        else:
            requests.get(self.base + "/mock/" + end)
        self.poller.tick()
        return db.get_row("jobs", job["id"])

    def test_same_file_twice_uses_own_history(self):
        first = self._print("complete")
        time.sleep(0.2)
        second = self._print("cancel")
        self.assertEqual(first["status"], "complete")
        self.assertEqual(second["status"], "cancelled")
        self.assertNotEqual(first["external_id"], second["external_id"])

    def test_full_print_cycle(self):
        j = self._print("complete")
        self.assertEqual(j["status"], "complete")
        self.assertEqual(j["external_id"], "%06X" % mock.SIM.job_seq)
        self.assertAlmostEqual(j["filament_mm"], 1000, delta=1)
        # граммы по соотношению г/мм из метаданных слайсера
        self.assertAlmostEqual(j["filament_g"], 2.98, delta=0.02)
        self.assertEqual(j["filament_source"], "printer")
        self.assertEqual(j["planned_g"], 2.98)
        self.assertEqual(j["energy_source"], "counter")
        self.assertGreater(j["energy_kwh"], 0)
        self.assertGreater(j["cost_total"], 0)
        self.assertAlmostEqual(db.get_row("filaments", self.fil)["remaining_g"], 1000 - j["filament_g"], places=2)
        self.poller.tick()
        self.assertEqual(len(db.all_rows("jobs")), 1)

    def test_cancel(self):
        j = self._print("cancel")
        self.assertEqual(j["status"], "cancelled")
        self.assertLess(j["filament_mm"], 1000)

    def test_reset_to_standby_keeps_filament(self):
        j = self._print("reset")
        self.assertEqual(j["status"], "cancelled")
        self.assertGreater(j["filament_mm"], 0)


if __name__ == "__main__":
    unittest.main()
