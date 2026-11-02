"""Схема SQLite, миграции и доступ к данным.

Перед любой миграцией существующей базы делается резервная копия в data/backup/.
Секретные поля (SECRET_FIELDS) никогда не отдаются наружу: см. public_row().
"""
import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.environ.get("PRINT_DB", os.path.join(DATA_DIR, "print.db"))

# Поля, которые хранятся только в БД и не попадают в API, логи и Excel.
SECRET_FIELDS = ("access_code", "plug_key", "token")

_write_lock = threading.RLock()

MIGRATIONS = [
    # 1: исходная схема
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE filaments (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL,
        material     TEXT DEFAULT 'PLA',
        color        TEXT DEFAULT '',
        diameter_mm  REAL DEFAULT 1.75,
        density      REAL DEFAULT 1.24,
        price_kg     REAL DEFAULT 0,
        remaining_g  REAL DEFAULT 1000,
        archived     INTEGER DEFAULT 0
    );
    CREATE TABLE plugs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        kind          TEXT NOT NULL,          -- shelly1 | shelly2 | tasmota | tuya | yandex
        host          TEXT DEFAULT '',        -- IP или IP:порт
        device_id     TEXT DEFAULT '',        -- Tuya dev_id / Яндекс id устройства
        plug_key      TEXT DEFAULT '',        -- Tuya local_key (секрет)
        token         TEXT DEFAULT '',        -- OAuth-токен Яндекса (секрет)
        tuya_version  TEXT DEFAULT '',        -- 3.1/3.3/3.4/3.5, пусто = подобрать
        dps_power     TEXT DEFAULT '19',
        dps_energy    TEXT DEFAULT '17',
        power_scale   REAL DEFAULT 0.1,       -- множитель dps мощности -> Вт
        energy_scale  REAL DEFAULT 0.001,     -- множитель dps энергии -> кВт*ч
        yandex_instance TEXT DEFAULT 'power',
        enabled       INTEGER DEFAULT 1
    );
    CREATE TABLE printers (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        kind          TEXT NOT NULL,          -- moonraker | bambu
        host          TEXT DEFAULT '',
        port          INTEGER DEFAULT 0,
        serial        TEXT DEFAULT '',
        access_code   TEXT DEFAULT '',        -- код доступа Bambu (секрет)
        plug_id       INTEGER REFERENCES plugs(id) ON DELETE SET NULL,
        filament_id   INTEGER REFERENCES filaments(id) ON DELETE SET NULL,
        avg_power_w   REAL DEFAULT 150,       -- для оценки, если нет розетки
        depreciation_hour REAL DEFAULT 0,     -- амортизация, руб/ч
        enabled       INTEGER DEFAULT 1
    );
    CREATE TABLE jobs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        printer_id    INTEGER REFERENCES printers(id) ON DELETE SET NULL,
        printer_name  TEXT DEFAULT '',
        filename      TEXT DEFAULT '',
        external_id   TEXT DEFAULT '',
        status        TEXT DEFAULT 'printing', -- printing | paused | complete | cancelled | error | manual
        started_at    REAL,
        ended_at      REAL,
        duration_s    REAL DEFAULT 0,
        progress      REAL DEFAULT 0,          -- 0..1
        filament_id   INTEGER REFERENCES filaments(id) ON DELETE SET NULL,
        filament_mm   REAL,
        filament_g    REAL,
        filament_source TEXT DEFAULT '',       -- printer | plan | plan_progress | manual
        planned_g     REAL,
        planned_mm    REAL,
        planned_s     REAL,
        slicer_file   TEXT DEFAULT '',
        energy_kwh    REAL DEFAULT 0,
        energy_source TEXT DEFAULT '',         -- counter | power | estimate | manual
        stock_deducted_g REAL DEFAULT 0,
        cost_filament REAL DEFAULT 0,
        cost_energy   REAL DEFAULT 0,
        cost_depreciation REAL DEFAULT 0,
        cost_labor    REAL DEFAULT 0,
        cost_total    REAL DEFAULT 0,
        price         REAL DEFAULT 0,
        notes         TEXT DEFAULT ''
    );
    CREATE INDEX jobs_started ON jobs(started_at);
    CREATE TABLE events (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        ts     REAL,
        level  TEXT,
        source TEXT,
        message TEXT
    );
    """,
    # 2: модель принтера (для подсказок в интерфейсе)
    """
    ALTER TABLE printers ADD COLUMN model TEXT DEFAULT '';
    """,
    # 3: несколько пластиков в задании, слоты AMS, цвет катушки
    """
    ALTER TABLE filaments ADD COLUMN color_hex TEXT DEFAULT '';
    CREATE TABLE job_filaments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        slot        INTEGER DEFAULT 1,      -- номер пластика в проекте слайсера
        material    TEXT DEFAULT '',
        color_hex   TEXT DEFAULT '',
        planned_g   REAL,
        grams       REAL,
        filament_id INTEGER REFERENCES filaments(id) ON DELETE SET NULL,
        tray        TEXT DEFAULT '',        -- слот AMS: A1..D4, EXT
        match       TEXT DEFAULT '',        -- как найдена катушка: tray | color | type | manual
        deducted_g  REAL DEFAULT 0,
        cost        REAL DEFAULT 0
    );
    CREATE INDEX job_filaments_job ON job_filaments(job_id);
    CREATE TABLE printer_slots (
        printer_id  INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
        tray        TEXT NOT NULL,          -- A1..D4, EXT
        filament_id INTEGER REFERENCES filaments(id) ON DELETE SET NULL,
        PRIMARY KEY (printer_id, tray)
    );
    """,
]

DEFAULT_SETTINGS = {
    "tariff_kwh": "6.0",          # руб за кВт*ч
    "labor_hour": "0",            # руб за час работы оператора (на задание: labor_minutes)
    "labor_minutes": "0",
    "markup_percent": "0",
    "slicer_dir": "",
    "bambu_studio_path": "",      # из версии 0.0.1; теперь — slicers_custom
    "slicers_custom": "[]",       # слайсеры, добавленные вручную: [{"exe", "name"}]
    "default_slicer": "",         # путь к exe слайсера, который открывает файлы заданий
    "currency": "₽",
    "theme": "violet",            # violet | light | black | graphite | system
    "autostart": "0",             # запуск вместе с Windows (в трей) — включает пользователь
    "discovery_autoadd": "1",     # добавлять найденные в сети принтеры Bambu сами
    "discovery_ignored": "",      # серийные номера, которые пользователь удалил — не добавлять снова
}


def _connect(path=None):
    conn = sqlite3.connect(path or DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connect():
    conn = _connect()
    try:
        with _write_lock:
            yield conn
            conn.commit()
    finally:
        conn.close()


def backup(reason="manual"):
    """Копия БД в data/backup/. Возвращает путь или None, если базы ещё нет."""
    if not os.path.exists(DB_PATH):
        return None
    bdir = os.path.join(os.path.dirname(DB_PATH), "backup")
    os.makedirs(bdir, exist_ok=True)
    name = "print-%s-%s.db" % (time.strftime("%Y%m%d-%H%M%S"), reason)
    dst = os.path.join(bdir, name)
    src = _connect()
    try:
        out = sqlite3.connect(dst)
        with out:
            src.backup(out)  # консистентная копия даже при открытых соединениях
        out.close()
    finally:
        src.close()
    return dst


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    existed = os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0
    conn = _connect()
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    if version < len(MIGRATIONS) and existed:
        backup("before-migration-v%d" % (version + 1))
    with connect() as c:
        c.execute("PRAGMA journal_mode=WAL")
        for i in range(version, len(MIGRATIONS)):
            c.executescript(MIGRATIONS[i])
            c.execute("PRAGMA user_version = %d" % (i + 1))
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


# ---------- общие помощники ----------

def public_row(row):
    """Словарь без секретов: вместо значения — флаг has_<поле>."""
    if row is None:
        return None
    d = dict(row)
    for f in SECRET_FIELDS:
        if f in d:
            d["has_" + f] = bool(d.pop(f))
    return d


def secret_values():
    """Все сохранённые секреты (для вычищения из логов и сырых ответов)."""
    vals = set()
    with connect() as c:
        for r in c.execute("SELECT access_code FROM printers"):
            if r[0]:
                vals.add(r[0])
        for r in c.execute("SELECT plug_key, token FROM plugs"):
            for v in r:
                if v:
                    vals.add(v)
    return vals


def redact(obj, secrets=None):
    """Рекурсивно заменяет секреты на *** в строках/словарях/списках."""
    secrets = secrets if secrets is not None else secret_values()
    if isinstance(obj, str):
        for s in secrets:
            if s and len(s) >= 4:
                obj = obj.replace(s, "***")
        return obj
    if isinstance(obj, dict):
        return {k: ("***" if k in SECRET_FIELDS and v else redact(v, secrets)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v, secrets) for v in obj]
    return obj


def get_settings():
    with connect() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings")}


def set_settings(values):
    with connect() as c:
        for k, v in values.items():
            if k in DEFAULT_SETTINGS:
                c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (k, str(v)))


def all_rows(table, where="", params=()):
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM %s %s" % (table, where), params)]


def get_row(table, row_id):
    with connect() as c:
        r = c.execute("SELECT * FROM %s WHERE id = ?" % table, (row_id,)).fetchone()
        return dict(r) if r else None


def columns(table):
    with connect() as c:
        return [r["name"] for r in c.execute("PRAGMA table_info(%s)" % table)]


def insert(table, values):
    cols = [k for k in values if k in columns(table) and k != "id"]
    with connect() as c:
        cur = c.execute(
            "INSERT INTO %s (%s) VALUES (%s)" % (table, ",".join(cols), ",".join("?" * len(cols))),
            [values[k] for k in cols],
        )
        return cur.lastrowid


def update(table, row_id, values):
    cols = [k for k in values if k in columns(table) and k != "id"]
    if not cols:
        return
    with connect() as c:
        c.execute(
            "UPDATE %s SET %s WHERE id = ?" % (table, ",".join("%s = ?" % k for k in cols)),
            [values[k] for k in cols] + [row_id],
        )


def delete(table, row_id):
    with connect() as c:
        c.execute("DELETE FROM %s WHERE id = ?" % table, (row_id,))


def log_event(level, source, message):
    message = redact(str(message))
    with connect() as c:
        c.execute("INSERT INTO events(ts, level, source, message) VALUES (?,?,?,?)",
                  (time.time(), level, source, message))
        c.execute("DELETE FROM events WHERE id < (SELECT MAX(id) - 2000 FROM events)")
