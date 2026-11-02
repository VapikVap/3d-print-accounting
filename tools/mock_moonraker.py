"""Эмулятор Moonraker (порт 7125) и розеток Shelly Gen1/Gen2 + Tasmota (порт 7126).

Только для проверки приложения без железа. Форматы ответов взяты из
документации Moonraker/Shelly/Tasmota, а не сняты с реального K1 Max.

Запуск:
    python tools/mock_moonraker.py            # ждёт команды
    python tools/mock_moonraker.py --auto 30  # печать по 30 с, пауза 10 с, по кругу

Управление (POST или GET):
    /mock/start?filename=cube.gcode&duration=30&mm=1500
    /mock/cancel
    /mock/error
    /mock/reset         (состояние standby, как после сброса на экране)
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

IDLE_W, PRINT_W = 8.0, 260.0


class Sim:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "standby"
        self.filename = ""
        self.duration = 30.0
        self.total_mm = 1500.0
        self.started = 0.0
        self.frozen = None       # (total_duration, mm) после окончания
        self.history = []
        self.job_seq = 0
        self.energy_wh = 1234.5  # «счётчик» розетки
        self._last_e = time.time()

    # --- печать ---
    def start(self, filename, duration, mm):
        with self.lock:
            self.state, self.filename = "printing", filename
            self.duration, self.total_mm = float(duration), float(mm)
            self.started, self.frozen = time.time(), None
            self.job_seq += 1

    def _progress(self):
        if self.state != "printing":
            return None
        return min(1.0, (time.time() - self.started) / self.duration)

    def update(self):
        with self.lock:
            now = time.time()
            self.energy_wh += self.power() * (now - self._last_e) / 3600.0
            self._last_e = now
            p = self._progress()
            if p is not None and p >= 1.0:
                self._finish("complete")

    def _finish(self, state):
        dur = time.time() - self.started
        mm = self.total_mm * min(1.0, dur / self.duration)
        self.frozen = (dur, mm)
        self.state = state
        self.history.insert(0, {
            "job_id": "%06X" % self.job_seq, "filename": self.filename, "exists": True,
            "status": {"complete": "completed"}.get(state, state),
            "start_time": self.started, "end_time": time.time(),
            "print_duration": dur, "total_duration": dur, "filament_used": mm,
            "metadata": self.metadata(self.filename),
        })

    def stop(self, state):
        with self.lock:
            if self.state == "printing":
                self._finish(state)

    def reset(self):
        with self.lock:
            self.state, self.filename, self.frozen = "standby", "", None

    def power(self):
        return PRINT_W if self.state == "printing" else IDLE_W

    def print_stats(self):
        with self.lock:
            if self.state == "printing":
                dur = time.time() - self.started
                mm = self.total_mm * min(1.0, dur / self.duration)
            elif self.frozen and self.state != "standby":
                dur, mm = self.frozen
            else:
                dur, mm = 0.0, 0.0
            prog = (mm / self.total_mm) if self.total_mm and self.state != "standby" else 0.0
            return {
                "print_stats": {"filename": self.filename if self.state != "standby" else "",
                                "total_duration": dur, "print_duration": dur, "filament_used": mm,
                                "state": self.state, "message": "", "info": {}},
                "virtual_sdcard": {"progress": prog, "is_active": self.state == "printing"},
                "extruder": {"temperature": 220.0 if self.state == "printing" else 30.0, "target": 0},
                "heater_bed": {"temperature": 60.0 if self.state == "printing" else 25.0, "target": 0},
            }

    def metadata(self, filename):
        return {"filename": filename, "slicer": "OrcaSlicer", "estimated_time": self.duration,
                "filament_total": self.total_mm, "filament_weight_total": round(self.total_mm * 0.002982, 2),
                "size": 123456, "modified": time.time()}


SIM = Sim()


def make_handler(kind):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.do_GET()

        def do_GET(self):
            SIM.update()
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
            if kind == "moonraker":
                return self.moonraker(u.path, q, u.query)
            return self.plug(u.path, q)

        def moonraker(self, path, q, raw_query):
            if path == "/server/info":
                return self._send({"result": {"klippy_connected": True, "klippy_state": "ready",
                                              "moonraker_version": "mock-0.1"}})
            if path == "/printer/info":
                return self._send({"result": {"state": "ready", "hostname": "mock-k1max",
                                              "software_version": "mock"}})
            if path == "/printer/objects/query":
                wanted = [p.split("=")[0] for p in raw_query.split("&") if p]
                st = SIM.print_stats()
                return self._send({"result": {"eventtime": time.time(),
                                              "status": {k: v for k, v in st.items() if k in wanted}}})
            if path == "/server/files/metadata":
                fn = q.get("filename", "")
                if fn != SIM.filename and not any(h["filename"] == fn for h in SIM.history):
                    return self._send({"error": {"code": 404, "message": "Metadata not available for <%s>" % fn}}, 404)
                return self._send({"result": SIM.metadata(fn)})
            if path == "/server/history/list":
                limit = int(q.get("limit", 50))
                return self._send({"result": {"count": len(SIM.history), "jobs": SIM.history[:limit]}})
            if path == "/mock/start":
                SIM.start(q.get("filename", "cube.gcode"), q.get("duration", 30), q.get("mm", 1500))
                return self._send({"ok": True})
            if path == "/mock/cancel":
                SIM.stop("cancelled")
                return self._send({"ok": True})
            if path == "/mock/error":
                SIM.stop("error")
                return self._send({"ok": True})
            if path == "/mock/reset":
                SIM.reset()
                return self._send({"ok": True})
            if path == "/mock/state":
                return self._send(SIM.print_stats())
            return self._send({"error": {"code": 404, "message": "Not Found"}}, 404)

        def plug(self, path, q):
            p = SIM.power()
            if path == "/status":  # Shelly Gen1 (Plug S): total в ватт-минутах
                return self._send({"relays": [{"ison": True}],
                                   "meters": [{"power": p, "is_valid": True, "total": int(SIM.energy_wh * 60)}]})
            if path == "/rpc/Switch.GetStatus":  # Shelly Gen2
                return self._send({"id": 0, "source": "init", "output": True, "apower": p, "voltage": 230.1,
                                   "current": round(p / 230, 3), "aenergy": {"total": round(SIM.energy_wh, 3)}})
            if path == "/cm":  # Tasmota Status 8
                return self._send({"StatusSNS": {"Time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                 "ENERGY": {"Total": round(SIM.energy_wh / 1000, 3),
                                                            "Power": p, "Voltage": 230}}})
            return self._send({"error": "not found"}, 404)

    return H


def auto_loop(duration, pause):
    n = 0
    while True:
        time.sleep(pause)
        n += 1
        SIM.start("auto_cube_%d.gcode" % n, duration, 800 + 200 * n)
        time.sleep(duration + 1)
        SIM.update()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=7125)
    ap.add_argument("--plug-port", type=int, default=7126)
    ap.add_argument("--auto", type=float, default=0, help="длительность авто-печати, с (0 — выкл.)")
    ap.add_argument("--pause", type=float, default=10)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler("moonraker"))
    plug = ThreadingHTTPServer(("127.0.0.1", a.plug_port), make_handler("plug"))
    threading.Thread(target=plug.serve_forever, daemon=True).start()
    if a.auto:
        threading.Thread(target=auto_loop, args=(a.auto, a.pause), daemon=True).start()
    print("Moonraker-эмулятор: http://127.0.0.1:%d  розетки: http://127.0.0.1:%d" % (a.port, a.plug_port))
    srv.serve_forever()


if __name__ == "__main__":
    main()
