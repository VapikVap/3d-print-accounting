"""HTTP API и раздача интерфейса. Слушает только 127.0.0.1.

Секреты (access_code, plug_key, token) принимаются при сохранении, но никогда не
возвращаются: вместо них в ответе флаги has_access_code / has_plug_key / has_token.
Пустое значение секрета при сохранении = «оставить прежний».
"""
import os
import re
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from app import REPO_URL, __version__, autostart, db, discovery, export, parts, printers, slicer, slicers

HOST = "127.0.0.1"
PORT = int(os.environ.get("PRINT_PORT", "8765"))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

APP_ID = "print-accounting"

app = Flask(__name__, static_folder=None)
poller = printers.Poller()
finder = discovery.Discovery()  # автопоиск Bambu в сети; запускается в start_background()/main()
on_show = None  # desktop.py подставляет функцию «показать окно»


def _allowed_hosts():
    return {"127.0.0.1:%d" % PORT, "localhost:%d" % PORT}


@app.before_request
def guard():
    """Защита от чужих сайтов в браузере: подмена DNS (Host) и подделка запросов (Origin)."""
    if app.testing:
        return None
    if request.host not in _allowed_hosts():
        abort(403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in _allowed_hosts()}:
            abort(403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            abort(403)
    return None

FIELDS = {
    "printers": ("name", "model", "kind", "host", "port", "serial", "access_code", "plug_id", "filament_id",
                 "avg_power_w", "depreciation_hour", "enabled"),
    "plugs": ("name", "kind", "host", "device_id", "plug_key", "token", "tuya_version", "dps_power",
              "dps_energy", "power_scale", "energy_scale", "yandex_instance", "enabled"),
    "filaments": ("name", "material", "color", "color_hex", "diameter_mm", "density", "price_kg", "remaining_g",
                  "archived"),
}
KINDS = {"printers": ("moonraker", "bambu"), "plugs": ("shelly1", "shelly2", "tasmota", "tuya", "yandex")}
NUMERIC = {"port", "plug_id", "filament_id", "avg_power_w", "depreciation_hour", "enabled", "power_scale",
           "energy_scale", "diameter_mm", "density", "price_kg", "remaining_g", "archived",
           "filament_g", "filament_mm", "energy_kwh", "duration_s", "planned_g", "started_at"}


def _clean(table, data, creating):
    out = {}
    for k in FIELDS[table]:
        if k not in data:
            continue
        v = data[k]
        if k in db.SECRET_FIELDS:
            if data.get("clear_" + k):
                out[k] = ""
            elif v:
                out[k] = str(v).strip()
            continue  # пустой секрет = не менять
        if k in NUMERIC:
            if v in ("", None):
                v = None
            else:
                try:
                    v = float(v)
                    if k in ("port", "plug_id", "filament_id", "enabled", "archived"):
                        v = int(v)
                except (TypeError, ValueError):
                    abort(400, "поле %s: ожидается число" % k)
        elif isinstance(v, str):
            v = v.strip()
        out[k] = v
    for k in db.SECRET_FIELDS:
        if data.get("clear_" + k) and k in FIELDS[table]:
            out[k] = ""
    if creating and not out.get("name"):
        abort(400, "укажите название")
    if table in KINDS and "kind" in out and out["kind"] not in KINDS[table]:
        abort(400, "неизвестный тип: %s" % out["kind"])
    return out


@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": e.description}), 400


@app.errorhandler(403)
def forbidden(e):
    return jsonify({"error": "запрещено"}), 403


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "не найдено"}), 404


@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")


# ---------- справочники ----------

def _crud(table):
    def list_rows():
        return jsonify([db.public_row(r) for r in db.all_rows(table, "ORDER BY id")])

    def create():
        vals = _clean(table, request.get_json(force=True) or {}, True)
        if table == "printers" and not vals.get("port"):
            vals["port"] = 8883 if vals.get("kind") == "bambu" else 7125
        rid = db.insert(table, vals)
        return jsonify(db.public_row(db.get_row(table, rid))), 201

    def update(rid):
        if not db.get_row(table, rid):
            abort(404)
        db.update(table, rid, _clean(table, request.get_json(force=True) or {}, False))
        return jsonify(db.public_row(db.get_row(table, rid)))

    def remove(rid):
        row = db.get_row(table, rid)
        if table == "printers" and row and row["kind"] == "bambu" and row["serial"]:
            # удалённый принтер не добавлять снова автопоиском (вернуть — «Найдены в сети»)
            discovery.set_ignored(discovery.ignored() | {row["serial"]})
        db.delete(table, rid)
        return jsonify({"ok": True})

    app.add_url_rule("/api/%s" % table, "list_" + table, list_rows, methods=["GET"])
    app.add_url_rule("/api/%s" % table, "create_" + table, create, methods=["POST"])
    app.add_url_rule("/api/%s/<int:rid>" % table, "update_" + table, update, methods=["PUT"])
    app.add_url_rule("/api/%s/<int:rid>" % table, "delete_" + table, remove, methods=["DELETE"])


for _t in FIELDS:
    _crud(_t)


def _safe_result(res):
    return jsonify(db.redact(res))


@app.route("/api/printers/<int:rid>/test", methods=["POST"])
def test_printer(rid):
    p = db.get_row("printers", rid) or abort(404)
    res = printers.printer_test(p, poller)
    db.log_event("info" if res["ok"] else "warn", p["name"], "Проверка связи: " + res["message"])
    return _safe_result(res)


@app.route("/api/plugs/<int:rid>/test", methods=["POST"])
def test_plug(rid):
    p = db.get_row("plugs", rid) or abort(404)
    res = printers.plug_test(p)
    if res.get("ok") and res.get("tuya_version") and res["tuya_version"] != p.get("tuya_version"):
        db.update("plugs", rid, {"tuya_version": res["tuya_version"]})
        res["message"] += " (версия сохранена)"
    db.log_event("info" if res["ok"] else "warn", p["name"], "Проверка связи: " + res["message"])
    return _safe_result(res)


# ---------- состояние ----------

@app.route("/api/state")
def state():
    out = []
    for p in db.all_rows("printers", "ORDER BY id"):
        live = dict(poller.live.get(p["id"]) or {})
        job = printers.active_job(p["id"])
        out.append({"printer": db.public_row(p), "live": live, "job": _with_parts([job])[0] if job else None,
                    "slots": parts.slot_map(p["id"])})
    plugs = [{"plug": db.public_row(p), "live": poller.plug_live.get(p["id"])} for p in db.all_rows("plugs")]
    return jsonify(db.redact({"printers": out, "plugs": plugs, "ts": time.time()}))


# ---------- задания ----------

JOB_EDIT = ("filament_id", "filament_g", "energy_kwh", "duration_s", "notes", "filename", "planned_g", "status")


def _with_parts(jobs):
    """Добавить к заданиям их пластики (одним запросом)."""
    if not jobs:
        return jobs
    ids = [j["id"] for j in jobs]
    rows = db.all_rows("job_filaments", "WHERE job_id IN (%s) ORDER BY slot" % ",".join("?" * len(ids)), tuple(ids))
    by = {}
    for r in rows:
        by.setdefault(r["job_id"], []).append(r)
    for j in jobs:
        j["parts"] = by.get(j["id"], [])
    return jobs


@app.route("/api/jobs")
def list_jobs():
    limit = min(int(request.args.get("limit", 200)), 2000)
    return jsonify(_with_parts(db.all_rows("jobs", "ORDER BY started_at DESC LIMIT ?", (limit,))))


@app.route("/api/jobs/<int:jid>/parts/<int:part_id>", methods=["PUT"])
def update_part(jid, part_id):
    """Сменить катушку у пластика задания (выбор пользователя — больше не перезаписывается)."""
    part = db.get_row("job_filaments", part_id)
    if not part or part["job_id"] != jid:
        abort(404)
    d = request.get_json(force=True) or {}
    fid = d.get("filament_id")
    fid = int(fid) if fid not in (None, "") else None
    if fid != part["filament_id"]:
        parts.return_stock(part)
        db.update("job_filaments", part_id, {"filament_id": fid, "match": "manual"})
    job = printers.recalc_job(jid)
    return jsonify(_with_parts([job])[0])


@app.route("/api/printers/<int:rid>/slots", methods=["GET", "PUT"])
def printer_slots(rid):
    """Слоты AMS: что сообщает принтер + какая ваша катушка к слоту привязана."""
    db.get_row("printers", rid) or abort(404)
    if request.method == "PUT":
        d = request.get_json(force=True) or {}
        tray = str(d.get("tray") or "")
        if not re.fullmatch(r"[A-D][1-4]|HT[1-8]|EXT", tray):
            abort(400, "неизвестный слот")
        fid = d.get("filament_id")
        fid = int(fid) if fid not in (None, "") else None
        parts.set_slot(rid, tray, fid)
        job = printers.active_job(rid)
        if job:  # идущая печать из этого слота — сразу на новую катушку
            for p in parts.list_parts(job["id"]):
                if p["tray"] == tray and p["match"] != "manual":
                    db.update("job_filaments", p["id"], {"filament_id": fid, "match": "tray" if fid else ""})
            printers.recalc_job(job["id"])
    live = poller.live.get(rid) or {}
    return jsonify({"trays": live.get("trays") or [], "active_tray": live.get("active_tray"),
                    "slots": parts.slot_map(rid)})


@app.route("/api/jobs", methods=["POST"])
def create_job():
    d = request.get_json(force=True) or {}
    vals = {k: d.get(k) for k in JOB_EDIT if k in d}
    vals = {k: (None if v == "" else v) for k, v in vals.items()}
    printer_id = d.get("printer_id") or None
    p = db.get_row("printers", printer_id) if printer_id else None
    vals.update({"printer_id": printer_id, "printer_name": p["name"] if p else d.get("printer_name", ""),
                 "status": "manual", "started_at": d.get("started_at") or time.time(),
                 "filament_source": "manual" if vals.get("filament_g") is not None else "",
                 "energy_source": "manual" if vals.get("energy_kwh") else ""})
    vals["ended_at"] = vals["started_at"] + float(vals.get("duration_s") or 0)
    jid = db.insert("jobs", vals)
    return jsonify(printers.recalc_job(jid)), 201


@app.route("/api/jobs/<int:jid>", methods=["PUT"])
def update_job(jid):
    job = db.get_row("jobs", jid) or abort(404)
    d = request.get_json(force=True) or {}
    vals = {}
    for k in JOB_EDIT:
        if k in d:
            v = d[k]
            if k in NUMERIC or k == "filament_id":
                v = None if v in ("", None) else float(v)
                if k == "filament_id" and v is not None:
                    v = int(v)
            vals[k] = v
    if "filament_g" in vals and vals["filament_g"] != job["filament_g"]:
        vals["filament_source"] = "manual"
    if "energy_kwh" in vals and vals["energy_kwh"] != job["energy_kwh"]:
        vals["energy_source"] = "manual"
    if "filament_id" in vals and vals["filament_id"] != job["filament_id"] and job["stock_deducted_g"]:
        # вернуть списанное на прежнюю катушку, новая спишется при пересчёте
        old = db.get_row("filaments", job["filament_id"]) if job["filament_id"] else None
        if old:
            db.update("filaments", old["id"], {"remaining_g": (old["remaining_g"] or 0) + job["stock_deducted_g"]})
        vals["stock_deducted_g"] = 0
    db.update("jobs", jid, vals)
    return jsonify(printers.recalc_job(jid))


@app.route("/api/jobs/<int:jid>", methods=["DELETE"])
def delete_job(jid):
    job = db.get_row("jobs", jid) or abort(404)
    for part in parts.list_parts(jid):
        parts.return_stock(part)
    if job["filament_id"] and job["stock_deducted_g"]:
        f = db.get_row("filaments", job["filament_id"])
        if f:
            db.update("filaments", f["id"], {"remaining_g": (f["remaining_g"] or 0) + job["stock_deducted_g"]})
    db.delete("jobs", jid)
    return jsonify({"ok": True})


# ---------- настройки, слайсер, журнал, экспорт ----------

@app.route("/api/settings", methods=["GET", "PUT"])
def settings():
    if request.method == "PUT":
        d = request.get_json(force=True) or {}
        if "theme" in d and d["theme"] not in THEMES:
            abort(400, "неизвестная тема")
        if "autostart" in d and not app.testing:
            err = autostart.apply(str(d["autostart"]) == "1")
            if err:
                abort(400, "Автозапуск: %s" % err)
        db.set_settings(d)
    s = db.get_settings()
    s["autostart_active"] = autostart.is_enabled()
    return jsonify(s)


THEMES = ("violet", "light", "black", "graphite", "system")


@app.route("/api/slicer/files")
def slicer_files():
    folder = db.get_settings().get("slicer_dir")
    if not folder:
        return jsonify({"folder": "", "files": [], "error": "Папка слайсера не выбрана"})
    if not os.path.isdir(folder):
        return jsonify({"folder": folder, "files": [], "error": "Папка не найдена"})
    files = slicer.scan(folder)
    return jsonify({"folder": folder, "files": [dict(f, plates=None) for f in files[:200]], "error": ""})


@app.route("/api/slicers")
def slicers_info():
    """Найденные слайсеры (установленные, рядом с приложением, добавленные вручную)."""
    items = slicers.find_all()
    return jsonify({"items": items, "recent_folders": slicers.recent_folders(items),
                    "known": [{"kind": k[0], "name": k[1]} for k in slicers.KNOWN]})


@app.route("/api/slicers/filaments")
def slicers_filaments():
    return jsonify(slicers.filament_presets())


@app.route("/api/slicers/open", methods=["POST"])
def slicers_open():
    d = request.get_json(silent=True) or {}
    err = slicers.open_file(d.get("path") or None, d.get("slicer_id") or None)
    if err:
        abort(400, err)
    return jsonify({"ok": True})


@app.route("/api/slicers/custom", methods=["POST", "DELETE"])
def slicers_custom():
    """Добавить/убрать слайсер вручную — по пути к его .exe (выбирает пользователь)."""
    d = request.get_json(silent=True) or {}
    exe = os.path.normpath(str(d.get("exe") or "").strip().strip('"'))
    items = [c for c in slicers.custom_list() if os.path.normcase(c["exe"]) != os.path.normcase(exe)]
    if request.method == "POST":
        if not (exe.lower().endswith(".exe") and os.path.isfile(exe)):
            abort(400, "Нужен путь к .exe слайсера")
        items.append({"exe": exe, "name": str(d.get("name") or "").strip()
                      or os.path.splitext(os.path.basename(exe))[0]})
    elif db.get_settings().get("bambu_studio_path", "").strip().strip('"').lower() in (exe.lower(), os.path.dirname(exe).lower()):
        db.set_settings({"bambu_studio_path": ""})  # путь из версии 0.0.1
    slicers.set_custom(items)
    return jsonify({"ok": True})


@app.route("/api/events")
def events():
    return jsonify(db.all_rows("events", "ORDER BY id DESC LIMIT 200"))


@app.route("/api/backup", methods=["POST"])
def make_backup():
    path = db.backup("manual")
    return jsonify({"ok": bool(path), "path": path})


@app.route("/api/export.xlsx")
def export_xlsx():
    def ts(name):
        v = request.args.get(name)
        return time.mktime(time.strptime(v, "%Y-%m-%d")) if v else None
    data = export.build(ts("from"), ts("to") and ts("to") + 86400)
    fname = "print-jobs-%s.xlsx" % time.strftime("%Y%m%d")
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=%s" % fname})


@app.route("/api/discovered")
def discovered():
    """Принтеры Bambu, которые сейчас объявляют себя в сети."""
    known = {p["serial"]: p["id"] for p in db.all_rows("printers") if p["serial"]}
    skip = discovery.ignored()
    items = [dict(i, printer_id=known.get(i["serial"]), ignored=i["serial"] in skip) for i in finder.listing()]
    return jsonify({"items": items, "listening": [p for p in finder.ports if p not in finder.errors],
                    "errors": finder.errors, "autoadd": db.get_settings().get("discovery_autoadd") == "1"})


@app.route("/api/discovered/<serial>/add", methods=["POST"])
def add_discovered(serial):
    info = finder.found.get(serial) or abort(404)
    discovery.set_ignored(discovery.ignored() - {serial})
    return jsonify({"ok": True, "printer_id": discovery.register(info, force=True)})


@app.route("/api/app/ping")
def ping():
    return jsonify({"app": APP_ID, "version": __version__, "repo": REPO_URL})


@app.route("/api/app/show", methods=["POST"])
def show_window():
    """Повторный запуск приложения просит уже открытую копию показать окно."""
    if on_show:
        on_show()
    return jsonify({"ok": bool(on_show)})


def start_background():
    """База + опрос + HTTP-сервер в фоновом потоке. Возвращает сервер (для shutdown)."""
    from werkzeug.serving import make_server
    db.init()
    srv = make_server(HOST, PORT, app, threaded=True)
    poller.start()
    finder.start()
    import threading
    # профили слайсеров (тысячи файлов) — прочитать заранее, чтобы окно катушки открывалось сразу
    threading.Thread(target=slicers.filament_presets, name="presets", daemon=True).start()
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    db.log_event("info", "server", "Запуск приложения")
    return srv


def main():
    db.init()
    poller.start()
    finder.start()
    db.log_event("info", "server", "Запуск на http://%s:%d" % (HOST, PORT))
    print("Учёт 3D-печати: http://%s:%d" % (HOST, PORT))
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
