"""Слайсеры («студии» производителей): поиск, запуск, профили филамента, недавние папки.

Где ищем:
  1. установленные программы Windows (реестр Uninstall: DisplayName, DisplayIcon/InstallLocation);
  2. переносные копии в папке приложения (как «Bambu Studio» рядом с app/);
  3. добавленные вручную (settings.slicers_custom).

Профили филамента — по семейству формата, которое определяется по папкам программы:
  orca  — Bambu Studio, OrcaSlicer, Creality Print, ElegooSlicer, Anycubic Slicer Next,
          QIDI Studio, Snapmaker Orca: resources/profiles/<Производитель>/filament/*.json,
          наследование через inherits; свои — <AppData>/user/*/filament;
  prusa — PrusaSlicer: INI-наборы resources/profiles/*.ini, секции [filament:...];
  cura  — UltiMaker Cura: share/cura/resources/materials/*.xml.fdm_material.
Bambu Studio и Creality Print проверены на реальных установках; Orca-форки, Prusa и Cura —
по известным форматам, на этом ПК не установлены.

Из файлов настроек слайсеров (.conf, .ini, .cfg) читаются только пути недавних проектов
и версия: там же лежат коды доступа принтеров и данные входа — их не трогаем.
"""
import configparser
import glob
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET

from . import db
from .slicer import normalize_color

APPDATA = os.environ.get("APPDATA", "")
OPEN_EXTS = (".3mf", ".gcode", ".stl", ".step", ".stp", ".obj", ".amf")

# (id, название, regex по имени в «Программах», имена exe, папки в %APPDATA%, «свой» производитель)
KNOWN = [
    ("bambu", "Bambu Studio", r"^bambu ?studio", ("bambu-studio.exe",), ("BambuStudio",), "BBL"),
    ("orca", "OrcaSlicer", r"^orca ?slicer", ("orca-slicer.exe", "OrcaSlicer.exe"), ("OrcaSlicer",), ""),
    ("creality", "Creality Print", r"^creality ?print", ("CrealityPrint.exe", "Creality Print.exe"),
     ("Creality/Creality Print/*",), "Creality"),
    ("elegoo", "ElegooSlicer", r"^elegoo ?slicer", ("elegoo-slicer.exe", "ElegooSlicer.exe"), ("ElegooSlicer",), "Elegoo"),
    ("anycubic", "Anycubic Slicer Next", r"^anycubic ?slicer", ("AnycubicSlicerNext.exe", "AnycubicSlicer.exe"),
     ("AnycubicSlicerNext",), "Anycubic"),
    ("qidi", "QIDI Studio", r"^qidi ?studio", ("qidi-studio.exe", "QIDIStudio.exe"), ("QIDIStudio",), "Qidi"),
    ("snapmaker", "Snapmaker Orca", r"^snapmaker.?orca", ("Snapmaker_Orca.exe", "snapmaker-orca.exe"),
     ("Snapmaker_Orca",), "Snapmaker"),
    ("prusa", "PrusaSlicer", r"^prusaslicer", ("prusa-slicer.exe",), ("PrusaSlicer",), "PrusaResearch"),
    ("superslicer", "SuperSlicer", r"^superslicer", ("superslicer.exe",), ("SuperSlicer",), ""),
    ("cura", "UltiMaker Cura", r"^ulti ?maker cura", ("UltiMaker-Cura.exe", "Ultimaker-Cura.exe", "Cura.exe"),
     ("cura/*",), "ultimaker"),
    ("flashprint", "FlashPrint", r"^flashprint", ("FlashPrint.exe",), (), ""),
    ("ideamaker", "ideaMaker", r"^ideamaker", ("ideaMaker.exe",), (), ""),
    ("simplify3d", "Simplify3D", r"^simplify3d", ("Simplify3D.exe",), (), ""),
]
KNOWN_BY_ID = {k[0]: k for k in KNOWN}
PROFILE_FIELDS = ("filament_type", "filament_density", "filament_diameter", "filament_cost", "filament_vendor",
                  "default_filament_colour", "filament_colour")


# ======================= поиск программ =======================

def _registry_programs():
    """[(DisplayName, DisplayVersion, exe | None, InstallLocation)] из реестра Windows."""
    if os.name != "nt":
        return []
    import winreg
    out = []
    roots = ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"))
    for hive, path in roots:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as k:
                        vals = {}
                        for name in ("DisplayName", "DisplayVersion", "DisplayIcon", "InstallLocation"):
                            try:
                                vals[name] = str(winreg.QueryValueEx(k, name)[0])
                            except OSError:
                                vals[name] = ""
                except OSError:
                    continue
                if vals["DisplayName"]:
                    icon = vals["DisplayIcon"].split(",")[0].strip().strip('"')
                    exe = icon if icon.lower().endswith(".exe") and os.path.isfile(icon) else None
                    out.append((vals["DisplayName"], vals["DisplayVersion"], exe, vals["InstallLocation"].strip('"')))
    return out


def _match_known(display_name):
    n = (display_name or "").strip().lower()
    for k in KNOWN:
        if re.search(k[2], n):
            return k
    return None


def _find_exe_in(folder, names):
    for name in names:
        p = os.path.join(folder, name)
        if os.path.isfile(p):
            return p
    return None


def custom_list():
    try:
        return [c for c in json.loads(db.get_settings().get("slicers_custom") or "[]") if c.get("exe")]
    except ValueError:
        return []


def set_custom(items):
    db.set_settings({"slicers_custom": json.dumps(items, ensure_ascii=False)})


def _entry(kind, exe, version="", source="", name=None):
    k = KNOWN_BY_ID.get(kind)
    folder = os.path.dirname(exe)
    return {"id": exe.lower(), "kind": kind, "name": name or (k[1] if k else os.path.splitext(os.path.basename(exe))[0]),
            "exe": exe, "version": version, "source": source, "family": detect_family(folder)}


def kind_by_exe(exe):
    base = os.path.basename(exe).lower()
    for k in KNOWN:
        if base in (e.lower() for e in k[3]):
            return k[0]
    return ""


def detect_family(folder):
    prof = os.path.join(folder, "resources", "profiles")
    if glob.glob(os.path.join(prof, "*", "filament")):
        return "orca"
    if glob.glob(os.path.join(prof, "*.ini")):
        return "prusa"
    if os.path.isdir(os.path.join(folder, "share", "cura", "resources", "materials")):
        return "cura"
    return ""


def find_all():
    """Все найденные слайсеры (без повторов по пути к exe)."""
    found = {}
    for name, ver, exe, loc in _registry_programs():
        k = _match_known(name)
        if not k:
            continue
        exe = exe or (_find_exe_in(loc, k[3]) if loc else None)
        if exe and exe.lower() not in found:
            found[exe.lower()] = _entry(k[0], exe, ver, "установлен", name=name if k[0] == "creality" else None)
    for d in glob.glob(os.path.join(db.BASE_DIR, "*")):  # переносные копии рядом с приложением
        if os.path.isdir(d):
            for k in KNOWN:
                exe = _find_exe_in(d, k[3])
                if exe and exe.lower() not in found:
                    found[exe.lower()] = _entry(k[0], exe, "", "в папке приложения")
    legacy = (db.get_settings().get("bambu_studio_path") or "").strip().strip('"')  # настройка из 0.0.1
    customs = custom_list() + ([{"exe": legacy, "name": "Bambu Studio"}] if legacy else [])
    for c in customs:
        exe = c["exe"]
        if os.path.isdir(exe):
            exe = _find_exe_in(exe, [e for k in KNOWN for e in k[3]]) or ""
        if exe and os.path.isfile(exe) and exe.lower() not in found:
            found[exe.lower()] = _entry(kind_by_exe(exe), exe, "", "добавлен вручную", name=c.get("name"))
    items = list(found.values())
    for it in items:
        it["appdata"] = appdata_dir(it)
        if not it["version"] and it["appdata"]:
            it["version"] = str((_read_conf(it["appdata"]).get("app") or {}).get("version") or "")
    # переносная копия той же версии, что и установленная, — одна и та же программа
    installed = {(i["kind"], i["version"]) for i in items if i["source"] == "установлен"}
    items = [i for i in items if i["source"] == "установлен" or (i["kind"], i["version"]) not in installed
             or not i["kind"]]
    default = default_id(items)
    for it in items:
        it["default"] = it["id"] == default
    return items


def default_id(items):
    want = (db.get_settings().get("default_slicer") or "").lower()
    if any(i["id"] == want for i in items):
        return want
    for kind in ("bambu", "orca", "creality"):
        for i in items:
            if i["kind"] == kind:
                return i["id"]
    return items[0]["id"] if items else ""


def get(slicer_id):
    for it in find_all():
        if it["id"] == (slicer_id or "").lower():
            return it
    return None


def open_file(path=None, slicer_id=None):
    """Запуск слайсера (по умолчанию или выбранного) с файлом или без. Текст ошибки или ''."""
    items = find_all()
    sid = (slicer_id or default_id(items)).lower()
    sl = next((i for i in items if i["id"] == sid), None)
    if not sl:
        return "Слайсер не найден — добавьте его на вкладке «Слайсеры»"
    args = [sl["exe"]]
    if path:
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return "Файл не найден: %s" % path
        if not path.lower().endswith(OPEN_EXTS):
            return "Этот тип файла слайсер не открывает"
        args.append(path)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(args, cwd=os.path.dirname(sl["exe"]), creationflags=flags, close_fds=True)
    return ""


# ======================= настройки слайсера: только пути и версия =======================

def appdata_dir(sl):
    k = KNOWN_BY_ID.get(sl["kind"])
    if not k or not APPDATA:
        return ""
    cands = []
    for pattern in k[4]:
        cands += [p for p in glob.glob(os.path.join(APPDATA, pattern)) if os.path.isdir(p)]

    def vkey(p):  # «7.3» > «6.0»; служебные .staging/.migration — в конец
        name = os.path.basename(p)
        nums = [int(x) for x in re.findall(r"\d+", name)]
        return (not name.startswith("."), nums)
    cands.sort(key=vkey, reverse=True)
    return os.path.normpath(cands[0]) if cands else ""


def _read_conf(folder):
    """Orca-семейство: <папка>/<что-то>.conf — JSON (+ строка с контрольной суммой)."""
    for f in sorted(glob.glob(os.path.join(folder, "*.conf"))):
        try:
            with open(f, encoding="utf-8") as fh:
                m = re.search(r"\{.*\}", fh.read(), re.S)
            if m:
                return json.loads(m.group(0))
        except (OSError, ValueError):
            continue
    return {}


def recent_folders(items=None):
    """Папки недавних проектов всех слайсеров — кандидаты в «папку нарезки»."""
    out = []

    def add(p):
        p = (p or "").strip()
        if p and os.path.isfile(p):
            p = os.path.dirname(p)
        if p and os.path.isdir(p) and p not in out:
            out.append(p)
    for sl in items if items is not None else find_all():
        ad = sl.get("appdata") or ""
        if not ad:
            continue
        if sl["family"] == "orca" or sl["kind"] in ("bambu", "orca", "creality"):
            conf = _read_conf(ad)
            for v in (conf.get("recent_projects") or {}).values():
                add(os.path.dirname(str(v)))
            rec = conf.get("recent") or {}
            add(rec.get("last_opened_folder"))
            add((conf.get("app") or {}).get("download_path"))
        elif sl["family"] == "prusa":
            cp = _ini(os.path.join(ad, "PrusaSlicer.ini"))
            if cp.has_section("recent_projects"):
                for _k, v in cp.items("recent_projects"):
                    add(os.path.dirname(v))
            if cp.has_section("recent"):
                add(cp.get("recent", "last_output_path", fallback=""))
        elif sl["family"] == "cura":
            cp = _ini(os.path.join(ad, "cura.cfg"))
            for v in (cp.get("general", "recent_files", fallback="") or "").split(";"):
                add(os.path.dirname(v.strip()))
    return out


def _ini(path):
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if not text.lstrip().startswith("["):
            text = "[__root__]\n" + text  # пользовательские пресеты Prusa — без заголовка секции
        cp.read_string(text)
    except (OSError, configparser.Error):
        pass
    return cp


# ======================= профили филамента =======================

def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    if isinstance(v, str) and ";" in v:
        return v.split(";")[0]
    return v


def _num(v):
    try:
        return float(str(_first(v)).strip().strip('"'))
    except (TypeError, ValueError):
        return None


def _preset(name, src, vendor, material, density, diameter, price, color, slicer):
    return {"name": name, "source": src, "vendor": vendor or "", "material": (material or "").strip(),
            "density": density, "diameter_mm": diameter or 1.75, "price_kg": price,
            "color": normalize_color(color), "slicer": slicer}


def _base_name(name):
    """«Hyper PLA @Creality K1 0.4 nozzle» / «Bambu PLA Basic @base» -> «Hyper PLA» / «Bambu PLA Basic»."""
    return re.sub(r"\s*@.*$", "", name).strip()


def orca_presets(sl):
    """Orca-семейство. Системные — по одному представителю на пластик (а не копия под каждый
    принтер), свои — все. Цена системных профилей условная, берём только из своих."""
    folder = os.path.dirname(sl["exe"])
    ad = sl.get("appdata") or ""
    vendor_dirs = {}
    for d in glob.glob(os.path.join(folder, "resources", "profiles", "*", "filament")):
        vendor_dirs[os.path.basename(os.path.dirname(d))] = d
    for d in glob.glob(os.path.join(ad, "system", "*", "filament")) if ad else []:
        vendor_dirs[os.path.basename(os.path.dirname(d))] = d  # обновлённые слайсером из сети — главнее

    files = {}   # vendor -> {имя: путь}
    for vendor, d in vendor_dirs.items():
        idx = files.setdefault(vendor, {})
        for root, _dirs, names in os.walk(d):
            for fn in names:
                if fn.endswith(".json"):
                    idx.setdefault(fn[:-5], os.path.join(root, fn))
    user_files = {}
    for d in glob.glob(os.path.join(ad, "user", "*", "filament")) if ad else []:
        for root, _dirs, names in os.walk(d):
            for fn in names:
                if fn.endswith(".json"):
                    user_files[fn[:-5]] = os.path.join(root, fn)
    parsed = {}

    def load(path):
        if path not in parsed:
            try:
                with open(path, encoding="utf-8") as f:
                    parsed[path] = json.load(f)
            except (OSError, ValueError):
                parsed[path] = {}
        return parsed[path]

    def find(name, vendor):
        if vendor and name in files.get(vendor, {}):
            return files[vendor][name]
        for v in ("OrcaFilamentLibrary",) + tuple(files):
            if name in files.get(v, {}):
                return files[v][name]
        return user_files.get(name)

    def resolve(path, vendor, depth=0):
        d = load(path)
        parent = d.get("inherits")
        merged = {}
        if parent and depth < 12:
            pp = find(parent, vendor)
            if pp:
                merged = dict(resolve(pp, vendor, depth + 1))
        merged.update({k: v for k, v in d.items() if k in PROFILE_FIELDS})
        return merged

    own = (KNOWN_BY_ID.get(sl["kind"]) or ("",) * 6)[5]
    out = []
    for name, path in sorted(user_files.items()):
        r = resolve(path, "")
        out.append(_preset(name, "user", _first(r.get("filament_vendor")), _first(r.get("filament_type")),
                           _num(r.get("filament_density")), _num(r.get("filament_diameter")),
                           _num(r.get("filament_cost")), _first(r.get("default_filament_colour") or r.get("filament_colour")),
                           sl["name"]))
    for vendor, idx in sorted(files.items(), key=lambda kv: (kv[0] != own, kv[0].lower())):
        reps = {}
        for name, path in idx.items():
            if name.startswith("fdm_") or name.lower().startswith("fdm "):
                continue
            base = _base_name(name)
            if base not in reps or name.endswith("@base"):
                reps[base] = path
        for base, path in sorted(reps.items()):
            r = resolve(path, vendor)
            material = _first(r.get("filament_type"))
            if not material:
                continue
            out.append(_preset(base, "system", _first(r.get("filament_vendor")) or vendor, material,
                               _num(r.get("filament_density")), _num(r.get("filament_diameter")), None,
                               _first(r.get("default_filament_colour")), sl["name"]))
    return out


def prusa_presets(sl):
    """PrusaSlicer: секции [filament:Имя] в наборах производителей; '*…*' — абстрактные."""
    folder = os.path.dirname(sl["exe"])
    ad = sl.get("appdata") or ""
    sections = {}
    bundles = glob.glob(os.path.join(folder, "resources", "profiles", "*.ini"))
    bundles += glob.glob(os.path.join(ad, "vendor", "*.ini")) if ad else []  # обновлённые наборы
    for b in bundles:
        cp = _ini(b)
        for s in cp.sections():
            if s.startswith("filament:"):
                sections[s[9:]] = dict(cp.items(s))
    user = {}
    for f in glob.glob(os.path.join(ad, "filament", "*.ini")) if ad else []:
        cp = _ini(f)
        sec = cp.sections()[0] if cp.sections() else None
        if sec:
            user[os.path.splitext(os.path.basename(f))[0]] = dict(cp.items(sec))

    def resolve(d, depth=0):
        merged = {}
        for parent in [p.strip().strip('"') for p in (d.get("inherits") or "").split(";") if p.strip()]:
            if parent in sections and depth < 12:
                merged.update(resolve(sections[parent], depth + 1))
        merged.update(d)
        return merged
    out = []
    for name, d in sorted(user.items()):
        r = resolve(d)
        out.append(_preset(name, "user", r.get("filament_vendor"), r.get("filament_type"), _num(r.get("filament_density")),
                           _num(r.get("filament_diameter")), _num(r.get("filament_cost")), r.get("filament_colour"), sl["name"]))
    for name, d in sorted(sections.items()):
        if name.startswith("*"):
            continue
        r = resolve(d)
        if not r.get("filament_type"):
            continue
        out.append(_preset(name, "system", r.get("filament_vendor"), r.get("filament_type"), _num(r.get("filament_density")),
                           _num(r.get("filament_diameter")), None, r.get("filament_colour"), sl["name"]))
    return out


CURA_NS = "{http://www.ultimaker.com/material}"


def _cura_material(path):
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    meta = root.find(CURA_NS + "metadata")
    props = root.find(CURA_NS + "properties")
    if meta is None or props is None:
        return None
    nm = meta.find(CURA_NS + "name")
    txt = lambda el, tag: ((el.find(CURA_NS + tag).text or "").strip() if el is not None and el.find(CURA_NS + tag) is not None else "")
    brand, material, color = txt(nm, "brand"), txt(nm, "material"), txt(nm, "color")
    label = txt(nm, "label") or " ".join(x for x in (brand, material, color if color.lower() != "generic" else "") if x)
    return {"guid": txt(meta, "GUID"), "name": label, "vendor": brand, "material": material,
            "color": txt(meta, "color_code"), "density": _num(txt(props, "density")),
            "diameter": _num(txt(props, "diameter"))}


def cura_presets(sl):
    """Cura: материалы *.xml.fdm_material; цену за катушку Cura хранит в cura.cfg (material_settings)."""
    folder = os.path.dirname(sl["exe"])
    ad = sl.get("appdata") or ""
    prices = {}
    if ad:
        try:
            ms = json.loads(_ini(os.path.join(ad, "cura.cfg")).get("cura", "material_settings", fallback="{}") or "{}")
            for guid, v in ms.items():
                if v.get("spool_cost") and v.get("spool_weight"):
                    prices[guid] = float(v["spool_cost"]) / float(v["spool_weight"]) * 1000.0
        except (ValueError, TypeError, AttributeError):
            pass
    out = []
    for src, pattern in (("user", os.path.join(ad, "materials", "*.xml.fdm_material") if ad else ""),
                         ("system", os.path.join(folder, "share", "cura", "resources", "materials", "**", "*.xml.fdm_material"))):
        if not pattern:
            continue
        for f in sorted(glob.glob(pattern, recursive=True)):
            m = _cura_material(f)
            if m and m["material"]:
                out.append(_preset(m["name"], src, m["vendor"], m["material"], m["density"], m["diameter"],
                                   prices.get(m["guid"]), m["color"], sl["name"]))
    return out


_cache = {}


def filament_presets(items=None):
    """Профили всех найденных слайсеров: свои — первыми, без повторов одного и того же пластика."""
    items = items if items is not None else find_all()
    out, seen = [], set()
    for sl in sorted(items, key=lambda s: not s.get("default")):
        folder = os.path.dirname(sl["exe"])
        stamp = (sl["exe"], sl.get("appdata"), os.path.getmtime(folder) if os.path.isdir(folder) else 0,
                 os.path.getmtime(sl["appdata"]) if sl.get("appdata") and os.path.isdir(sl["appdata"]) else 0)
        if _cache.get(sl["id"], (None,))[0] != stamp:
            fn = {"orca": orca_presets, "prusa": prusa_presets, "cura": cura_presets}.get(sl["family"])
            try:
                _cache[sl["id"]] = (stamp, fn(sl) if fn else [])
            except Exception as e:  # битые профили одного слайсера не ломают остальные
                db.log_event("warn", sl["name"], "Профили не прочитаны: %s" % e)
                _cache[sl["id"]] = (stamp, [])
        for p in _cache[sl["id"]][1]:
            key = (p["source"], p["vendor"].lower(), p["name"].lower(), p["material"].upper(), p["density"])
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    out.sort(key=lambda p: p["source"] != "user")
    return out
