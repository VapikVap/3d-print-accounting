"""План с принтера Bambu по FTPS. FTP-сервер подменён: раскладка папок и имена —
предположение (корень SD, /cache), на реальном P1S ещё не проверены."""
import io
import os
import unittest
import zipfile
from unittest import mock

from app import bambu, bambu_ftp, db, printers
from tests.helpers import fresh_db

SLICE_INFO_2_PLATES = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate><metadata key="index" value="1"/><metadata key="prediction" value="1200"/><metadata key="weight" value="5.00"/>
    <filament id="1" type="PLA" color="#FFFFFF" used_m="1.68" used_g="5.00"/></plate>
  <plate><metadata key="index" value="2"/><metadata key="prediction" value="3600"/><metadata key="weight" value="20.00"/>
    <filament id="1" type="PLA" color="#FFFFFF" used_m="6.70" used_g="20.00"/></plate>
</config>
"""


def make_3mf(slice_info=SLICE_INFO_2_PLATES):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/slice_info.config", slice_info)
    return buf.getvalue()


class FakeFTP:
    def __init__(self, tree):
        self.tree = tree          # {папка: {имя: bytes}}
        self.retrieved = []

    def nlst(self, d):
        if d not in self.tree:
            raise bambu_ftp.ftplib.error_perm("550 Failed to open directory")
        return [(d.rstrip("/") + "/" + n) for n in self.tree[d]]

    def retrbinary(self, cmd, cb):
        path = cmd.split(" ", 1)[1]
        d, name = os.path.split(path)
        self.retrieved.append(path)
        cb(self.tree[d or "/"][name])

    def quit(self):
        pass


PRINTER = {"id": 1, "name": "P1S", "host": "10.0.0.5", "serial": "01P00A000000001", "access_code": "12345678"}


class FetchPlanTest(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_finds_file_in_cache_and_picks_plate(self):
        ftp = FakeFTP({"/": {"other.gcode.3mf": b"x"}, "/cache": {"Benchy box.gcode.3mf": make_3mf()}})
        plan, local, msg = bambu_ftp.fetch_plan(PRINTER, "Benchy box", "/data/Metadata/plate_2.gcode",
                                                connect_fn=lambda h, c: ftp)
        self.assertEqual(ftp.retrieved, ["/cache/Benchy box.gcode.3mf"])
        self.assertEqual(plan["planned_g"], 20.0)
        self.assertEqual(plan["planned_s"], 3600)
        self.assertTrue(os.path.isfile(local))
        self.assertIn("print-tests-", local)  # не в data/ проекта
        self.assertIn("/cache/Benchy box.gcode.3mf", msg)

    def test_plate_suffix_and_no_plate_info(self):
        ftp = FakeFTP({"/": {"Benchy box.3mf": make_3mf()}})
        plan, _l, _m = bambu_ftp.fetch_plan(PRINTER, "Benchy box_plate_1", "", connect_fn=lambda h, c: ftp)
        self.assertEqual(plan["planned_g"], 25.0)  # пластина неизвестна — сумма по файлу

    def test_not_found_and_connect_error(self):
        ftp = FakeFTP({"/": {"a.gcode.3mf": b"x"}})
        plan, _l, msg = bambu_ftp.fetch_plan(PRINTER, "Benchy", "", connect_fn=lambda h, c: ftp)
        self.assertIsNone(plan)
        self.assertIn("не найден", msg)

        def boom(h, c):
            raise OSError("timed out")
        plan, _l, msg = bambu_ftp.fetch_plan(PRINTER, "Benchy", "", connect_fn=boom)
        self.assertIsNone(plan)
        self.assertIn("нет подключения", msg)

    def test_connection_check_lists_files(self):
        ftp = FakeFTP({"/": {"a.gcode.3mf": b"", "b.txt": b""}, "/cache": {"c.3mf": b""}})
        r = bambu_ftp.test(PRINTER, connect_fn=lambda h, c: ftp)
        self.assertTrue(r["ok"])
        self.assertEqual(r["listing"], {"/": ["a.gcode.3mf"], "/cache": ["c.3mf"]})
        self.assertIn("файлов .3mf — 2", r["message"])


class PollerFtpTest(unittest.TestCase):
    """Задание Bambu получает план с принтера, в т.ч. если файл скачался после конца печати."""

    def setUp(self):
        fresh_db()
        self.fil = db.insert("filaments", {"name": "PLA", "price_kg": 1000, "remaining_g": 1000})
        self.pid = db.insert("printers", dict({k: v for k, v in PRINTER.items() if k != "id"},
                                              kind="bambu", port=8883, filament_id=self.fil))
        self.poller = printers.Poller()
        self.client = mock.Mock()
        self.client.state = {}
        self.client.status = lambda: dict(bambu.interpret(self.client.state), connected=True, age_s=1, error="")
        p = db.get_row("printers", self.pid)
        self.poller._bambu[self.pid] = ((p["host"], p["serial"], p["access_code"], 8883, None), self.client)
        self.ftp = FakeFTP({"/cache": {"Benchy.gcode.3mf": make_3mf()}})
        self.poller.fetch_plan = lambda pr, name, gf: bambu_ftp.fetch_plan(pr, name, gf, connect_fn=lambda h, c: self.ftp)
        self.deferred = []

    def push(self, part, now):
        bambu.deep_merge(self.client.state, part)
        self.poller.tick(now=now)

    def test_plan_at_start(self):
        self.poller._schedule = lambda fn, *a: fn(*a)
        self.push({"gcode_state": "RUNNING", "subtask_name": "Benchy", "gcode_file": "/data/Metadata/plate_2.gcode",
                   "mc_percent": 5}, 1000)
        job = printers.active_job(self.pid)
        self.assertEqual(job["planned_g"], 20.0)
        self.assertTrue(job["slicer_file"].endswith("Benchy.gcode.3mf"))
        self.push({"gcode_state": "FINISH", "mc_percent": 100}, 4600)
        j = db.get_row("jobs", job["id"])
        self.assertEqual((j["filament_g"], j["filament_source"]), (20.0, "plan"))
        self.assertAlmostEqual(db.get_row("filaments", self.fil)["remaining_g"], 980.0)

    def test_plan_arrives_after_failed_print(self):
        self.poller._schedule = lambda fn, *a: self.deferred.append((fn, a))  # FTP «медленный»
        self.push({"gcode_state": "RUNNING", "subtask_name": "Benchy", "gcode_file": "/data/Metadata/plate_2.gcode",
                   "mc_percent": 30}, 1000)
        self.push({"gcode_state": "FAILED"}, 2000)
        jid = db.all_rows("jobs")[0]["id"]
        self.assertIsNone(db.get_row("jobs", jid)["filament_g"])
        for fn, a in self.deferred:
            fn(*a)
        j = db.get_row("jobs", jid)
        self.assertEqual((j["status"], j["filament_g"], j["filament_source"]), ("error", 6.0, "plan_progress"))
        self.assertAlmostEqual(db.get_row("filaments", self.fil)["remaining_g"], 994.0)

    def test_manual_grams_not_overwritten(self):
        self.poller._schedule = lambda fn, *a: self.deferred.append((fn, a))
        self.push({"gcode_state": "RUNNING", "subtask_name": "Benchy", "mc_percent": 50}, 1000)
        self.push({"gcode_state": "FINISH"}, 2000)
        jid = db.all_rows("jobs")[0]["id"]
        db.update("jobs", jid, {"filament_g": 7.5, "filament_source": "manual"})
        for fn, a in self.deferred:
            fn(*a)
        self.assertEqual(db.get_row("jobs", jid)["filament_g"], 7.5)

    def test_printer_test_includes_ftp(self):
        with mock.patch.object(bambu, "test_connection", return_value={"ok": True, "message": "Отчёт получен",
                                                                       "parsed": {}, "raw": {"print": {}}}), \
             mock.patch.object(bambu_ftp, "connect", return_value=self.ftp):
            r = printers.printer_test(db.get_row("printers", self.pid))
        self.assertIn("FTPS работает", r["message"])
        self.assertEqual(r["raw"]["ftp_files"], {"/cache": ["Benchy.gcode.3mf"]})


class ImplicitTlsTest(unittest.TestCase):
    def test_no_certificate_check_for_lan(self):
        ctx = bambu_ftp._context()
        self.assertFalse(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, bambu_ftp.ssl.CERT_NONE)


if __name__ == "__main__":
    unittest.main()
