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

    def cycle():
        n = 0
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
