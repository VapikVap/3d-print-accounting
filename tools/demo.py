"""Демо-режим: приложение на ВРЕМЕННОЙ базе + эмулятор P1S с AMS 2 Pro.

Как с настоящим принтером: эмулятор объявляет себя в сети, приложение находит его
и добавляет само — остаётся ввести код доступа 12345678. Затем печатается
двухцветная деталь. Рабочая база data/print.db не трогается; поиск слушает
отдельный порт 20210, чтобы эмулятор не попал в рабочее приложение.
    .venv\\Scripts\\python.exe tools\\demo.py    ->  http://127.0.0.1:8766
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["PRINT_DB"] = os.path.join(tempfile.mkdtemp(prefix="print-demo-"), "demo.db")
os.environ.setdefault("PRINT_PORT", "8766")
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

import threading  # noqa: E402
import time  # noqa: E402

import mock_bambu  # noqa: E402
from app import bambu_ftp, db, discovery, server  # noqa: E402

DEMO_DISCOVERY_PORT = 20210


def seed_showcase(mqtt_port):
    """--showcase: принтер уже подключён, катушки привязаны к AMS, есть история заданий —
    чтобы снимки экрана выглядели как в работе. Все данные вымышленные."""
    from app import parts, printers
    db.set_settings({"markup_percent": "30", "labor_hour": "300", "labor_minutes": "5",
                     "autostart": "1"})  # как у настроенного приложения — без «Начала работы»
    pid = db.insert("printers", {"name": "P1S Combo", "model": "Bambu Lab P1S", "kind": "bambu", "host": "127.0.0.1",
                                 "port": mqtt_port, "serial": mock_bambu.SERIAL, "access_code": mock_bambu.ACCESS_CODE,
                                 "avg_power_w": 120, "depreciation_hour": 15})
    for tray, fid in (("A1", 1), ("A2", 2), ("A3", 3)):
        parts.set_slot(pid, tray, fid)
    now = time.time()
    history = [  # (часов назад, имя, длительность ч, [(слот, тип, цвет, г)], статус, прогресс)
        (70, "Кашпо с узором", 5.2, [(1, "PLA", "#FFFFFF", 86.4)], "complete", 1.0),
        (52, "Шестерня редуктора", 2.1, [(3, "PETG", "#C12E1F", 31.7)], "complete", 1.0),
        (30, "Брелок с логотипом", 0.9, [(1, "PLA", "#FFFFFF", 6.2), (2, "PLA", "#000000", 2.4)], "complete", 1.0),
        (26, "Держатель телефона", 1.6, [(2, "PLA", "#000000", 24.8)], "cancelled", 0.42),
        (8, "Табличка на дверь", 2.7, [(1, "PLA", "#FFFFFF", 28.9), (2, "PLA", "#000000", 11.3)], "complete", 1.0),
    ]
    for hours_ago, name, dur_h, fils, status, prog in history:
        started = now - hours_ago * 3600
        dur = dur_h * 3600 * (prog if status != "complete" else 1)
        jid = db.insert("jobs", {"printer_id": pid, "printer_name": "P1S Combo", "filename": name, "status": status,
                                 "started_at": started, "ended_at": started + dur, "duration_s": dur, "progress": prog,
                                 "planned_g": sum(f[3] for f in fils), "planned_s": dur_h * 3600,
                                 "energy_kwh": round(120 * dur / 3600 / 1000, 4), "energy_source": "estimate"})
        parts.set_parts(jid, pid, [{"slot": s, "material": m, "color_hex": c, "planned_g": g} for s, m, c, g in fils],
                        [{"tray": t, "material": "PLA" if t != "A3" else "PETG", "empty": False,
                          "color_hex": {"A1": "#FFFFFF", "A2": "#000000", "A3": "#C12E1F"}[t]} for t in ("A1", "A2", "A3")])
        g = parts.scale(jid, prog)
        db.update("jobs", jid, {"filament_g": g, "filament_source": "plan" if status == "complete" else "plan_progress"})
        printers.recalc_job(jid)


def main():
    db.init()
    printer, mqtt, ftp = mock_bambu.start(mqtt_port=0, ftp_port=0, print_secs=45, ssdp_port=DEMO_DISCOVERY_PORT)
    bambu_ftp.PORT = ftp.port
    # эмулятор слушает MQTT не на 8883 — подставим порт, когда принтер добавится автопоиском
    orig_register = discovery.register

    def register(info, force=False):
        pid = orig_register(info, force)
        if pid and info["serial"] == mock_bambu.SERIAL:
            db.update("printers", pid, {"port": mqtt.port})
        return pid
    server.finder = discovery.Discovery(ports=(DEMO_DISCOVERY_PORT,), on_found=register)
    server.discovery.register = register

    for name, color, hexv, price, rem in (("Bambu PLA Basic", "белый", "#FFFFFF", 1890, 1000),
                                          ("Bambu PLA Basic", "чёрный", "#000000", 1890, 700)):
        db.insert("filaments", {"name": name, "material": "PLA", "color": color, "color_hex": hexv,
                                "price_kg": price, "remaining_g": rem, "density": 1.26})
    db.insert("filaments", {"name": "Bambu PETG HF", "material": "PETG", "color": "красный", "color_hex": "#C12E1F",
                            "price_kg": 2100, "remaining_g": 1000, "density": 1.28})
    db.set_settings({"tariff_kwh": "6.5"})
    showcase = "--showcase" in sys.argv
    if showcase:
        seed_showcase(mqtt.port)
        server.autostart.is_enabled = lambda: True  # демо в реестр не пишет — только для вида настроек

    def cycle():
        n = 0
        if showcase:  # для снимков экрана: сразу длинная печать, чтобы застать её на середине
            time.sleep(3)
            printer.start_print("Органайзер для инструмента", 300)
            time.sleep(10 ** 6)
        while True:
            time.sleep(20)
            n += 1
            printer.start_print("%s %d" % (mock_bambu.FILE_NAME, n), 45)
            if n == 2:
                printer.start_drying(180)
            time.sleep(55)
    threading.Thread(target=cycle, daemon=True).start()
    server.poller.start()
    server.finder.start()
    print("Демо: http://127.0.0.1:%d — код доступа эмулятора: %s" % (server.PORT, mock_bambu.ACCESS_CODE))
    server.app.run(host=server.HOST, port=server.PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
