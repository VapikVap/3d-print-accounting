"""Bambu Studio: профили (наследование), поиск программы, запуск, миграция БД."""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from app import bambu_studio as bs, db, server
from tests.helpers import fresh_db


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


class PresetsTest(unittest.TestCase):
    """Структура как в resources/profiles/BBL/filament Bambu Studio 2.x."""

    def setUp(self):
        fresh_db()
        self.appdata = tempfile.mkdtemp()
        sysdir = os.path.join(self.appdata, "system", "BBL", "filament")
        _write(os.path.join(sysdir, "fdm_filament_common.json"),
               {"name": "fdm_filament_common", "filament_type": ["PLA"], "filament_density": ["0"],
                "filament_diameter": ["1.75"], "filament_cost": ["0"], "filament_vendor": ["Generic"]})
        _write(os.path.join(sysdir, "fdm_filament_pet.json"),
               {"name": "fdm_filament_pet", "inherits": "fdm_filament_common", "filament_type": ["PETG"],
                "filament_density": ["1.27"]})
        _write(os.path.join(sysdir, "fdm_filament_pla.json"),
               {"name": "fdm_filament_pla", "inherits": "fdm_filament_common", "filament_density": ["1.24"]})
        _write(os.path.join(sysdir, "Bambu PLA Basic @base.json"),
               {"name": "Bambu PLA Basic @base", "inherits": "fdm_filament_pla", "filament_density": ["1.26"],
                "filament_cost": ["19.99"], "filament_vendor": ["Bambu Lab"]})
        _write(os.path.join(sysdir, "P1P", "Bambu PLA Basic @BBL P1P.json"),
               {"name": "Bambu PLA Basic @BBL P1P", "inherits": "Bambu PLA Basic @base"})
        _write(os.path.join(sysdir, "Generic PETG @base.json"),
               {"name": "Generic PETG @base", "inherits": "fdm_filament_pet"})
        _write(os.path.join(self.appdata, "user", "default", "filament", "Мой PETG.json"),
               {"name": "Мой PETG", "inherits": "Generic PETG @base", "filament_cost": ["1490"]})
        self.patch = mock.patch.object(bs, "APPDATA", self.appdata)
        self.patch.start()
        bs._cache.update(key=None, presets=[])

    def tearDown(self):
        self.patch.stop()

    def test_presets_resolve_inheritance(self):
        pr = {p["name"]: p for p in bs.filament_presets()}
        self.assertEqual(set(pr), {"Bambu PLA Basic", "Generic PETG", "Мой PETG"})  # без копий под каждый принтер
        pla = pr["Bambu PLA Basic"]
        self.assertEqual((pla["material"], pla["density"], pla["diameter_mm"], pla["vendor"]), ("PLA", 1.26, 1.75, "Bambu Lab"))
        self.assertIsNone(pla["price_kg"])  # системная цена условная — не берём
        self.assertEqual(pr["Generic PETG"]["material"], "PETG")
        mine = pr["Мой PETG"]
        self.assertEqual((mine["source"], mine["density"], mine["price_kg"]), ("user", 1.27, 1490.0))
        self.assertEqual(bs.filament_presets()[0]["name"], "Мой PETG")  # свои профили первыми

    def test_recent_folders_reads_only_paths(self):
        d = tempfile.mkdtemp()
        conf = {"app": {"download_path": d, "version": "02.08.02.61"},
                "recent_projects": {"001": os.path.join(d, "a.3mf")},
                "access_code": {"01P00A000000001": "SECRET"}}
        with open(os.path.join(self.appdata, "BambuStudio.conf"), "w", encoding="utf-8") as f:
            f.write(json.dumps(conf) + "\n# MD5 checksum 0\n")
        server.app.testing = True
        body = server.app.test_client().get("/api/bambu-studio").get_data(as_text=True)
        self.assertNotIn("SECRET", body)
        info = json.loads(body)
        self.assertEqual(info["recent_folders"], [d])
        self.assertEqual(info["version"], "02.08.02.61")


class LaunchTest(unittest.TestCase):
    def setUp(self):
        fresh_db()
        self.dir = tempfile.mkdtemp()
        self.exe = os.path.join(self.dir, "bambu-studio.exe")
        open(self.exe, "w").close()
        db.set_settings({"bambu_studio_path": self.dir})  # можно указать папку, а не файл

    def test_find_exe_from_folder(self):
        self.assertEqual(bs.find_exe(), self.exe)

    def test_open_file_and_reject_other_types(self):
        f3mf = os.path.join(self.dir, "Куб.gcode.3mf")
        open(f3mf, "w").close()
        bad = os.path.join(self.dir, "evil.bat")
        open(bad, "w").close()
        with mock.patch.object(bs.subprocess, "Popen") as popen:
            self.assertEqual(bs.open_file(f3mf), "")
            self.assertEqual(popen.call_args[0][0], [self.exe, f3mf])
            self.assertIn("не открывает", bs.open_file(bad))
            self.assertIn("не найден", bs.open_file(os.path.join(self.dir, "нет.3mf")))
            self.assertEqual(popen.call_count, 1)

    def test_not_found(self):
        db.set_settings({"bambu_studio_path": os.path.join(self.dir, "нет")})
        self.assertIsNone(bs.find_exe())
        self.assertIn("не найдена", bs.open_file())


class MigrationTest(unittest.TestCase):
    def test_v1_database_gets_model_column_and_backup(self):
        fresh_db()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db.DB_PATH + suffix)
            except FileNotFoundError:
                pass
        conn = sqlite3.connect(db.DB_PATH)
        conn.executescript(db.MIGRATIONS[0])
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO printers(name, kind) VALUES ('старый', 'bambu')")
        conn.commit()
        conn.close()
        bdir = os.path.join(os.path.dirname(db.DB_PATH), "backup")
        before = set(os.listdir(bdir)) if os.path.isdir(bdir) else set()
        db.init()
        new = set(os.listdir(bdir)) - before
        self.assertTrue(any("before-migration-v2" in n for n in new))
        p = db.all_rows("printers")[0]
        self.assertEqual((p["name"], p["model"]), ("старый", ""))


if __name__ == "__main__":
    unittest.main()
