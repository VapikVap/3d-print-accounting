"""Слайсеры: поиск, запуск, профили трёх форматов (Orca/Bambu, PrusaSlicer, Cura).
Образцы повторяют структуру реальных установок: Bambu Studio 2.08 и Creality Print 7.3
(проверены на этом ПК); PrusaSlicer и Cura — по известным форматам их файлов."""
import json
import os
import tempfile
import unittest
from unittest import mock

from app import db, server, slicers
from tests.helpers import fresh_db


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text if isinstance(text, str) else json.dumps(text))


def _exe(folder, name):
    _w(os.path.join(folder, name), "")
    return os.path.join(folder, name)


class Env(unittest.TestCase):
    """Временные «Program Files», %APPDATA% и папка приложения; реестр подменён."""

    def setUp(self):
        fresh_db()
        self.root = tempfile.mkdtemp()
        self.pf = os.path.join(self.root, "PF")
        self.appdata = os.path.join(self.root, "AppData")
        self.base = os.path.join(self.root, "app")
        os.makedirs(self.base)
        self.registry = []
        self.patches = [mock.patch.object(slicers, "APPDATA", self.appdata),
                        mock.patch.object(db, "BASE_DIR", self.base),
                        mock.patch.object(slicers, "_registry_programs", lambda: list(self.registry))]
        for p in self.patches:
            p.start()
        slicers._cache.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def make_creality(self):
        d = os.path.join(self.pf, "Creality Print 7.3")
        fil = os.path.join(d, "resources", "profiles", "Creality", "filament")
        _w(os.path.join(fil, "fdm_filament_common.json"), {"name": "fdm_filament_common", "filament_type": ["PLA"],
                                                           "filament_density": ["0"], "filament_diameter": ["1.75"]})
        _w(os.path.join(fil, "fdm_filament_pla.json"), {"name": "fdm_filament_pla", "inherits": "fdm_filament_common",
                                                        "filament_density": ["1.24"]})
        for printer in ("K1 0.4 nozzle", "K1 Max 0.4 nozzle", "K2 Plus 0.4 nozzle"):  # копии под каждый принтер
            _w(os.path.join(fil, "Hyper PLA @Creality %s.json" % printer),
               {"name": "Hyper PLA @Creality %s" % printer, "inherits": "fdm_filament_pla", "instantiation": "true",
                "filament_type": ["PLA"], "filament_density": "1.24", "filament_cost": "30",
                "filament_vendor": ["Creality"], "default_filament_colour": '""'})
        bbl = os.path.join(d, "resources", "profiles", "BBL", "filament")
        _w(os.path.join(bbl, "fdm_filament_pla.json"), {"name": "fdm_filament_pla", "filament_type": ["PLA"],
                                                        "filament_density": ["1.24"]})
        _w(os.path.join(bbl, "Bambu PLA Basic @base.json"), {"name": "Bambu PLA Basic @base", "inherits": "fdm_filament_pla",
                                                             "filament_density": ["1.26"], "filament_vendor": ["Bambu Lab"]})
        exe = _exe(d, "CrealityPrint.exe")
        ad = os.path.join(self.appdata, "Creality", "Creality Print")
        for v in ("6.0", "7.3", ".7.3.staging-123"):
            os.makedirs(os.path.join(ad, v), exist_ok=True)
        _w(os.path.join(ad, "7.3", "Creality.conf"), json.dumps(
            {"app": {"version": "7.3.0.6157"}, "recent_projects": {"01": os.path.join(self.root, "proj", "a.3mf")},
             "access_code": {"SERIAL": "SECRET-CODE"}}) + "\n# MD5 checksum 0\n")
        os.makedirs(os.path.join(self.root, "proj"))
        _w(os.path.join(ad, "7.3", "user", "default", "filament", "Мой Hyper PLA.json"),
           {"name": "Мой Hyper PLA", "inherits": "Hyper PLA @Creality K1 0.4 nozzle", "filament_cost": ["1450"],
            "default_filament_colour": ["#FF8800"]})
        self.registry.append(("Creality Print 7.3", "7.3.0", exe, ""))
        return exe


class FindTest(Env):
    def test_registry_portable_custom_and_dedupe(self):
        crex = self.make_creality()
        bambu_installed = _exe(os.path.join(self.pf, "Bambu Studio"), "bambu-studio.exe")
        self.registry.append(("Bambu Studio", "02.08.02.61", bambu_installed, ""))
        self.registry.append(("Notepad++", "8.0", None, ""))
        portable = _exe(os.path.join(self.base, "Bambu Studio"), "bambu-studio.exe")  # та же версия рядом
        _w(os.path.join(self.appdata, "BambuStudio", "BambuStudio.conf"), json.dumps({"app": {"version": "02.08.02.61"}}))
        prusa = _exe(os.path.join(self.root, "Prusa"), "prusa-slicer.exe")
        slicers.set_custom([{"exe": prusa, "name": ""}])

        items = {i["exe"]: i for i in slicers.find_all()}
        self.assertEqual(set(items), {crex, bambu_installed, prusa})  # копия той же версии не дублируется
        self.assertNotIn(portable, items)
        cr = items[crex]
        self.assertEqual((cr["kind"], cr["family"], cr["name"]), ("creality", "orca", "Creality Print 7.3"))
        self.assertTrue(cr["appdata"].endswith(os.path.join("Creality Print", "7.3")))  # не 6.0 и не .staging
        self.assertEqual(items[prusa]["kind"], "prusa")
        self.assertEqual(items[prusa]["source"], "добавлен вручную")
        self.assertTrue(items[bambu_installed]["default"])  # по умолчанию — Bambu Studio

        db.set_settings({"default_slicer": crex.lower()})
        self.assertTrue({i["exe"]: i for i in slicers.find_all()}[crex]["default"])

    def test_legacy_setting_from_001(self):
        exe = _exe(os.path.join(self.root, "Old Bambu"), "bambu-studio.exe")
        db.set_settings({"bambu_studio_path": os.path.dirname(exe)})  # в 0.0.1 можно было указать папку
        self.assertEqual([i["exe"] for i in slicers.find_all()], [exe])

    def test_open_only_known_slicers_and_files(self):
        crex = self.make_creality()
        f = os.path.join(self.root, "Деталь.gcode.3mf")
        _w(f, "")
        bad = os.path.join(self.root, "evil.bat")
        _w(bad, "")
        with mock.patch.object(slicers.subprocess, "Popen") as popen:
            self.assertEqual(slicers.open_file(f, crex.lower()), "")
            self.assertEqual(popen.call_args[0][0], [crex, f])
            self.assertIn("не открывает", slicers.open_file(bad))
            self.assertIn("не найден", slicers.open_file(f, r"c:\windows\system32\cmd.exe"))
            self.assertEqual(popen.call_count, 1)

    def test_recent_folders_and_no_secrets_in_api(self):
        self.make_creality()
        server.app.testing = True
        c = server.app.test_client()
        body = c.get("/api/slicers").get_data(as_text=True)
        self.assertNotIn("SECRET-CODE", body)
        info = json.loads(body)
        self.assertEqual(info["recent_folders"], [os.path.join(self.root, "proj")])
        self.assertEqual(info["items"][0]["version"], "7.3.0")

    def test_custom_api(self):
        server.app.testing = True
        c = server.app.test_client()
        self.assertEqual(c.post("/api/slicers/custom", json={"exe": os.path.join(self.root, "нет.exe")}).status_code, 400)
        txt = os.path.join(self.root, "a.txt")
        _w(txt, "")
        self.assertEqual(c.post("/api/slicers/custom", json={"exe": txt}).status_code, 400)
        exe = _exe(os.path.join(self.root, "Cura"), "UltiMaker-Cura.exe")
        self.assertEqual(c.post("/api/slicers/custom", json={"exe": exe}).status_code, 200)
        self.assertEqual([i["kind"] for i in slicers.find_all()], ["cura"])
        c.delete("/api/slicers/custom", json={"exe": exe})
        self.assertEqual(slicers.find_all(), [])


class PresetsTest(Env):
    def test_orca_family_creality(self):
        self.make_creality()
        pr = slicers.filament_presets()
        names = [(p["name"], p["source"]) for p in pr]
        self.assertEqual(names[0], ("Мой Hyper PLA", "user"))
        self.assertEqual(names.count(("Hyper PLA", "system")), 1)  # три копии под принтеры — одна запись
        mine = pr[0]
        self.assertEqual((mine["material"], mine["density"], mine["price_kg"], mine["color"]), ("PLA", 1.24, 1450.0, "#FF8800"))
        hyper = next(p for p in pr if p["name"] == "Hyper PLA")
        self.assertEqual((hyper["vendor"], hyper["price_kg"], hyper["slicer"]), ("Creality", None, "Creality Print 7.3"))
        basic = next(p for p in pr if p["name"] == "Bambu PLA Basic")
        self.assertEqual((basic["vendor"], basic["density"]), ("Bambu Lab", 1.26))
        system = [p["name"] for p in pr if p["source"] == "system"]
        self.assertLess(system.index("Hyper PLA"), system.index("Bambu PLA Basic"))  # свой производитель первым

    def test_prusa_ini(self):
        d = os.path.join(self.pf, "PrusaSlicer")
        _w(os.path.join(d, "resources", "profiles", "PrusaResearch.ini"), """[vendor]
name = Prusa Research

[filament:*common*]
filament_diameter = 1.75
filament_density = 1.24
filament_cost = 25

[filament:*PET*]
inherits = *common*
filament_type = PETG
filament_density = 1.27

[filament:Prusament PETG]
inherits = *PET*
filament_vendor = Prusa Polymers
filament_colour = #FF8000

[filament:Generic PLA]
inherits = *common*
filament_type = PLA
filament_vendor = Generic
""")
        exe = _exe(d, "prusa-slicer.exe")
        _w(os.path.join(self.appdata, "PrusaSlicer", "filament", "Мой PETG.ini"),
           "inherits = Prusament PETG\nfilament_cost = 1700\n")
        self.registry.append(("PrusaSlicer 2.8.1", "2.8.1", exe, ""))
        pr = {(p["name"], p["source"]): p for p in slicers.filament_presets()}
        self.assertEqual(set(pr), {("Мой PETG", "user"), ("Prusament PETG", "system"), ("Generic PLA", "system")})
        pp = pr[("Prusament PETG", "system")]
        self.assertEqual((pp["material"], pp["density"], pp["vendor"], pp["color"], pp["price_kg"]),
                         ("PETG", 1.27, "Prusa Polymers", "#FF8000", None))
        self.assertEqual(pr[("Мой PETG", "user")]["price_kg"], 1700.0)

    def test_cura_materials(self):
        d = os.path.join(self.pf, "UltiMaker Cura 5.8.1")
        _w(os.path.join(d, "share", "cura", "resources", "materials", "generic_pla.xml.fdm_material"),
           """<?xml version="1.0" encoding="UTF-8"?>
<fdmmaterial xmlns="http://www.ultimaker.com/material" version="1.3">
  <metadata>
    <name><brand>Generic</brand><material>PLA</material><color>Generic</color></name>
    <GUID>506c9f0d-e3aa-4bd4-b2d2-23e2425b1aa9</GUID>
    <color_code>#ffc924</color_code>
  </metadata>
  <properties><density>1.24</density><diameter>2.85</diameter></properties>
</fdmmaterial>""")
        exe = _exe(d, "UltiMaker-Cura.exe")
        _w(os.path.join(self.appdata, "cura", "5.8", "cura.cfg"),
           '[general]\nrecent_files = \n\n[cura]\nmaterial_settings = {"506c9f0d-e3aa-4bd4-b2d2-23e2425b1aa9": '
           '{"spool_cost": 2000, "spool_weight": 1000}}\n')
        self.registry.append(("UltiMaker Cura 5.8.1", "5.8.1", exe, ""))
        pr = slicers.filament_presets()
        self.assertEqual(len(pr), 1)
        p = pr[0]
        self.assertEqual((p["name"], p["material"], p["density"], p["diameter_mm"], p["color"], p["price_kg"]),
                         ("Generic PLA", "PLA", 1.24, 2.85, "#FFC924", 2000.0))

    def test_broken_profiles_do_not_break_others(self):
        self.make_creality()
        d = os.path.join(self.pf, "UltiMaker Cura 5.8.1")
        _w(os.path.join(d, "share", "cura", "resources", "materials", "bad.xml.fdm_material"), "<не xml")
        self.registry.append(("UltiMaker Cura 5.8.1", "5.8.1", _exe(d, "UltiMaker-Cura.exe"), ""))
        self.assertTrue(any(p["name"] == "Hyper PLA" for p in slicers.filament_presets()))


if __name__ == "__main__":
    unittest.main()
