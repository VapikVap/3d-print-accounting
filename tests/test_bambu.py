"""Bambu: разбор отчётов и переходы gcode_state. Отчёты — заглушки по формату
LAN MQTT, не сняты с вашего принтера."""
import unittest

from app import bambu, db, printers
from tests.helpers import fresh_db


class FakeClient:
    """Подменяет BambuClient: состояние задаётся тестом."""
    def __init__(self):
        self.state = {}
        self.connected = True

    def push(self, part):
        bambu.deep_merge(self.state, part)

    def status(self):
        st = bambu.interpret(self.state)
        st.update(connected=True, age_s=1, error="")
        return st

    def request_full(self):
        pass

    def stop(self):
        pass


class BambuParseTest(unittest.TestCase):
    def test_interpret(self):
        st = bambu.interpret({"gcode_state": "RUNNING", "mc_percent": 42, "mc_remaining_time": 30,
                              "subtask_name": "Benchy", "layer_num": 10, "total_layer_num": 100})
        self.assertEqual(st["state"], "printing")
        self.assertAlmostEqual(st["progress"], 0.42)
        self.assertEqual(st["remaining_s"], 1800)
        self.assertEqual(st["filename"], "Benchy")

    def test_partial_reports_merge(self):
        s = {}
        bambu.deep_merge(s, {"gcode_state": "RUNNING", "mc_percent": 1, "subtask_name": "A"})
        bambu.deep_merge(s, {"mc_percent": 5})  # P1/A1 шлют только изменившееся
        self.assertEqual(bambu.interpret(s)["state"], "printing")
        self.assertEqual(s["mc_percent"], 5)


class PushallThrottleTest(unittest.TestCase):
    def test_pushall_not_more_often_than_interval(self):
        sent = []

        class Pub:
            def publish(self, topic, payload):
                sent.append((topic, payload))

        c = bambu.BambuClient("10.0.0.5", "01P00A000000001", "x")
        c._client, c.connected = Pub(), True
        self.assertTrue(c.request_full())
        self.assertFalse(c.request_full())          # сразу повторно — нельзя
        c._last_pushall -= bambu.PUSHALL_MIN_INTERVAL + 1
        self.assertTrue(c.request_full())           # через 5 мин — можно
        self.assertTrue(c.request_full(force=True))  # после переподключения — всегда
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[0][0], "device/01P00A000000001/request")
        self.assertIn('"pushall"', sent[0][1])

    def test_idle_printer_does_not_spam_pushall(self):
        fresh_db()
        pid = db.insert("printers", {"name": "P1S", "kind": "bambu", "host": "h", "serial": "s",
                                     "access_code": "c", "port": 8883})
        calls = []
        fake = FakeClient()
        fake.push({"gcode_state": "IDLE"})
        fake.status = lambda: dict(bambu.interpret(fake.state), connected=True, age_s=600, error="")
        fake.request_full = lambda force=False: calls.append(force)
        poller = printers.Poller()
        poller._bambu[pid] = (("h", "s", "c", 8883), fake)
        for i in range(5):
            poller.tick(now=1000 + i * 5)
        # опрос может просить, но решает клиент; главное — не force
        self.assertTrue(all(f is False for f in calls))


class BambuJobFlowTest(unittest.TestCase):
    def setUp(self):
        fresh_db()
        self.fil = db.insert("filaments", {"name": "Basic", "price_kg": 2000, "remaining_g": 1000})
        self.pid = db.insert("printers", {"name": "P1S", "kind": "bambu", "host": "10.0.0.5", "serial": "01P00A000000001",
                                          "access_code": "12345678", "port": 8883, "filament_id": self.fil,
                                          "avg_power_w": 200})
        self.poller = printers.Poller()
        self.poller.fetch_plan = lambda *a: (None, None, "FTP отключён в тесте")
        self.poller._schedule = lambda fn, *a: fn(*a)
        self.client = FakeClient()
        p = db.get_row("printers", self.pid)
        sig = (p["host"], p["serial"], p["access_code"], 8883)
        self.poller._bambu[self.pid] = (sig, self.client)

    def _run(self, part, now):
        self.client.push(part)
        self.poller.tick(now=now)

    def _start(self, planned_g=20.0):
        self._run({"gcode_state": "PREPARE", "subtask_name": "Benchy", "mc_percent": 0}, 1000)
        job = printers.active_job(self.pid)
        db.update("jobs", job["id"], {"planned_g": planned_g})  # как будто нашли файл нарезки
        self._run({"gcode_state": "RUNNING", "mc_percent": 10}, 1100)
        return job["id"]

    def test_finish_uses_full_plan(self):
        jid = self._start()
        self._run({"mc_percent": 100, "gcode_state": "FINISH"}, 4600)
        j = db.get_row("jobs", jid)
        self.assertEqual(j["status"], "complete")
        self.assertEqual(j["filament_g"], 20.0)
        self.assertEqual(j["filament_source"], "plan")
        self.assertEqual(j["duration_s"], 3600)
        self.assertEqual(j["energy_source"], "estimate")
        self.assertAlmostEqual(j["energy_kwh"], 0.2)
        self.assertEqual(j["cost_filament"], 40.0)
        self.assertAlmostEqual(db.get_row("filaments", self.fil)["remaining_g"], 980.0)
        # FINISH висит и дальше — нового задания быть не должно
        self.poller.tick(now=4700)
        self.assertEqual(len(db.all_rows("jobs")), 1)

    def test_failed_scales_by_progress(self):
        jid = self._start()
        self._run({"mc_percent": 40}, 2000)
        self._run({"gcode_state": "FAILED"}, 2100)
        j = db.get_row("jobs", jid)
        self.assertEqual(j["status"], "error")
        self.assertAlmostEqual(j["filament_g"], 8.0)
        self.assertEqual(j["filament_source"], "plan_progress")

    def test_idle_after_running_is_cancelled(self):
        jid = self._start()
        self._run({"mc_percent": 25}, 2000)
        self._run({"gcode_state": "IDLE"}, 2100)
        j = db.get_row("jobs", jid)
        self.assertEqual(j["status"], "cancelled")
        self.assertAlmostEqual(j["filament_g"], 5.0)

    def test_no_job_on_startup_with_finish(self):
        self._run({"gcode_state": "FINISH", "subtask_name": "Old", "mc_percent": 100}, 1000)
        self.assertEqual(db.all_rows("jobs"), [])


if __name__ == "__main__":
    unittest.main()
