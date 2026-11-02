"""Автопоиск Bambu в сети и определение системы подачи (AMS / AMS 2 Pro / AMS HT).
Формат объявления — по разбору трафика P1S сообществом; типы модулей — по ha-bambulab.
На реальном P1S Combo с AMS 2 Pro ещё не проверено."""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import mock_bambu  # noqa: E402

from app import bambu, bambu_models, db, discovery, printers, server  # noqa: E402
from tests.helpers import fresh_db  # noqa: E402

# объявление P1S как в разборе трафика (Location — IP, USN — серийный, C12 — P1S)
CAPTURED = (b"NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nServer: UPnP/1.0\r\nLocation: 192.168.3.8\r\n"
            b"NT: urn:bambulab-com:device:3dprinter:1\r\nUSN: 01P00A391800123\r\nCache-Control: max-age=1800\r\n"
            b"DevModel.bambu.com: C12\r\nDevName.bambu.com: Drucker 001\r\nDevSignal.bambu.com: -61\r\n"
            b"DevConnect.bambu.com: lan\r\nDevBind.bambu.com: free\r\nDevseclink.bambu.com: secure\r\n"
            b"DevVersion.bambu.com: 01.07.00.00\r\nDevCap.bambu.com: 1\r\n\r\n")


class ParseTest(unittest.TestCase):
    def test_captured_p1s(self):
        i = discovery.parse_ssdp(CAPTURED, "192.168.3.8")
        self.assertEqual((i["serial"], i["ip"], i["model_code"], i["model"], i["name"], i["connect"], i["version"]),
                         ("01P00A391800123", "192.168.3.8", "C12", "Bambu Lab P1S", "Drucker 001", "lan", "01.07.00.00"))

    def test_foreign_ssdp_ignored(self):
        router = b"NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nNT: upnp:rootdevice\r\nUSN: uuid:abc::upnp:rootdevice\r\n\r\n"
        self.assertIsNone(discovery.parse_ssdp(router, "192.168.1.1"))
        self.assertIsNone(discovery.parse_ssdp(b"garbage", "1.2.3.4"))

    def test_model_codes(self):
        self.assertEqual(bambu_models.model_name("C12"), "Bambu Lab P1S")
        self.assertEqual(bambu_models.model_name("N2S"), "Bambu Lab A1")
        self.assertEqual(bambu_models.model_name("ZZZ"), "Bambu Lab (код ZZZ)")


class RegisterTest(unittest.TestCase):
    def setUp(self):
        fresh_db()
        server.app.testing = True
        self.c = server.app.test_client()
        self.info = discovery.parse_ssdp(CAPTURED, "192.168.3.8")

    def test_new_printer_added_without_code(self):
        pid = discovery.register(self.info)
        p = db.get_row("printers", pid)
        self.assertEqual((p["kind"], p["model"], p["host"], p["serial"], p["port"], p["access_code"], p["name"]),
                         ("bambu", "Bambu Lab P1S", "192.168.3.8", "01P00A391800123", 8883, "", "Drucker 001"))
        self.assertIn("Введите код доступа", db.all_rows("events")[-1]["message"])

    def test_ip_change_updates_known_printer(self):
        pid = discovery.register(self.info)
        discovery.register(dict(self.info, ip="192.168.3.42"))
        self.assertEqual(db.get_row("printers", pid)["host"], "192.168.3.42")
        self.assertEqual(len(db.all_rows("printers")), 1)

    def test_deleted_printer_not_readded_but_can_be_added_by_button(self):
        pid = discovery.register(self.info)
        self.c.delete("/api/printers/%d" % pid)
        self.assertIsNone(discovery.register(self.info))
        self.assertEqual(db.all_rows("printers"), [])
        server.finder.found[self.info["serial"]] = dict(self.info, last_seen=time.time())
        try:
            items = self.c.get("/api/discovered").get_json()["items"]
            self.assertTrue(items[0]["ignored"])
            r = self.c.post("/api/discovered/%s/add" % self.info["serial"]).get_json()
            self.assertTrue(r["printer_id"])
            self.assertNotIn(self.info["serial"], discovery.ignored())
        finally:
            server.finder.found.clear()

    def test_autoadd_off(self):
        db.set_settings({"discovery_autoadd": "0"})
        self.assertIsNone(discovery.register(self.info))

    def test_real_udp_packet(self):
        d = discovery.Discovery(ports=(0,)).start()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(mock_bambu.ssdp_notify("127.0.0.1"), ("127.0.0.1", d.port))
            s.close()
            t0 = time.time()
            while time.time() - t0 < 3 and not d.found:
                time.sleep(0.05)
        finally:
            d.stop()
        self.assertIn(mock_bambu.SERIAL, d.found)
        p = db.all_rows("printers")[0]
        self.assertEqual((p["model"], p["name"], p["host"]), ("Bambu Lab P1S", "P1S Combo", "127.0.0.1"))

    def test_printer_without_code_waits_quietly(self):
        discovery.register(self.info)
        poller = printers.Poller()
        for i in range(5):
            poller.tick(now=1000 + i * 5)
        live = list(poller.live.values())[0]
        self.assertTrue(live["need_code"])
        self.assertIsNone(live["online"])
        self.assertFalse(any("Нет связи" in e["message"] for e in db.all_rows("events")))
        self.assertEqual(poller._bambu, {})  # к принтеру без кода даже не подключаемся


class AmsTest(unittest.TestCase):
    MODULES = [{"name": "ota", "product_name": "Bambu Lab P1S", "sw_ver": "01.08.02.00"},
               {"name": "n3f/0", "sw_ver": "00.00.06.40"}, {"name": "n3s/128", "sw_ver": "00.00.01.10"},
               {"name": "ams_f1/1", "product_name": "AMS lite"}]

    def test_modules(self):
        m = bambu.parse_modules(self.MODULES)
        self.assertEqual(m["firmware"], "01.08.02.00")
        self.assertEqual({k: v["type"] for k, v in m["ams"].items()}, {0: "AMS 2 Pro", 128: "AMS HT", 1: "AMS lite"})

    def test_units_and_trays(self):
        p = {"ams": {"tray_now": "128", "ams": [
            {"id": "0", "humidity": "5", "humidity_raw": "18", "temp": "26.5", "dry_time": 95,
             "tray": [{"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF"}, {"id": "1"}]},
            {"id": "128", "humidity_raw": "9", "temp": "55.0", "dry_time": 0,
             "tray": [{"id": "0", "tray_type": "PA-CF", "tray_color": "000000FF"}]}]}}
        trays, active, units = bambu.parse_ams(p, self.MODULES)
        self.assertEqual([t["tray"] for t in trays], ["A1", "A2", "HT1"])
        self.assertEqual(active, "HT1")
        a, ht = units
        self.assertEqual((a["label"], a["type"], a["humidity_pct"], a["temp"], a["drying_min"]),
                         ("A", "AMS 2 Pro", 18.0, 26.5, 95.0))
        self.assertEqual((ht["label"], ht["type"], ht["drying_min"]), ("HT1", "AMS HT", None))

    def test_unknown_type_without_get_version(self):
        _t, _a, units = bambu.parse_ams({"ams": {"ams": [{"id": "0", "tray": []}]}})
        self.assertEqual((units[0]["type"], units[0]["detected"]), ("AMS", False))


if __name__ == "__main__":
    unittest.main()
