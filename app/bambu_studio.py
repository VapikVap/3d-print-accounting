"""Связь с Bambu Studio (слайсер): поиск программы, запуск, профили филамента,
недавние папки проектов.

Из BambuStudio.conf читаются только пути (recent_projects, recent, download_path) —
там же лежат коды доступа принтеров и данные входа, их не трогаем.
Профили: system/BBL/filament в %APPDATA%\\BambuStudio (обновляются из сети),
иначе resources/profiles/BBL/filament рядом с программой; пользовательские —
%APPDATA%\\BambuStudio\\user\\*\\filament. Наследование (inherits) раскрывается.
"""
import glob
import json
import os
import re
import subprocess

from . import db

APPDATA = os.path.join(os.environ.get("APPDATA", ""), "BambuStudio")
OPEN_EXTS = (".3mf", ".gcode", ".stl", ".step", ".stp", ".obj")
PROFILE_FIELDS = ("filament_type", "filament_density", "filament_diameter", "filament_cost", "filament_vendor",
                  "default_filament_colour")


def candidates():
    paths = [os.path.join(db.BASE_DIR, "Bambu Studio", "bambu-studio.exe")]  # папка в проекте
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if root:
            paths.append(os.path.join(root, "Bambu Studio", "bambu-studio.exe"))
            paths.append(os.path.join(root, "Programs", "Bambu Studio", "bambu-studio.exe"))
    for drive in "CDEF":
        paths.append(r"%s:\Program Files\Bambu Studio\bambu-studio.exe" % drive)
    return paths


def find_exe():
    custom = (db.get_settings().get("bambu_studio_path") or "").strip().strip('"')
    if custom:
        if os.path.isdir(custom):
            custom = os.path.join(custom, "bambu-studio.exe")
        return custom if os.path.isfile(custom) else None
    for p in candidates():
        if os.path.isfile(p):
            return p
    return None


def _read_conf():
    p = os.path.join(APPDATA, "BambuStudio.conf")
    try:
        with open(p, encoding="utf-8") as f:
            txt = f.read()
        return json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
    except Exception:
        return {}


def info():
    exe = find_exe()
    conf = _read_conf()
    version = (conf.get("app") or {}).get("version") or ""
    return {"exe": exe, "found": bool(exe), "version": version,
            "configured": bool((db.get_settings().get("bambu_studio_path") or "").strip()),
            "recent_folders": recent_folders(conf), "profiles": len(filament_presets())}


def recent_folders(conf=None):
    """Папки, где недавно были проекты Bambu Studio (кандидаты в «папку нарезки»)."""
    conf = conf if conf is not None else _read_conf()
    out = []

    def add(p):
        if p and os.path.isdir(p) and p not in out:
            out.append(p)
    for v in (conf.get("recent_projects") or {}).values():
        add(os.path.dirname(str(v)))
    rec = conf.get("recent") or {}
    add(rec.get("last_opened_folder"))
    add(rec.get("settings_folder"))
    add((conf.get("app") or {}).get("download_path"))
    return out


def open_file(path=None):
    """Запуск Bambu Studio (с файлом или без). Возвращает текст ошибки или ''."""
    exe = find_exe()
    if not exe:
        return "Bambu Studio не найдена — укажите путь в «Настройках»"
    args = [exe]
    if path:
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return "Файл не найден: %s" % path
        if not path.lower().endswith(OPEN_EXTS):
            return "Этот тип файла Bambu Studio не открывает"
        args.append(path)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(args, cwd=os.path.dirname(exe), creationflags=flags, close_fds=True)
    return ""


# ---------- профили филамента ----------

def _profile_dirs():
    exe = find_exe()
    system = os.path.join(APPDATA, "system", "BBL", "filament")
    dirs = []
    if os.path.isdir(system):
        dirs.append(("system", system))
    elif exe:
        dirs.append(("system", os.path.join(os.path.dirname(exe), "resources", "profiles", "BBL", "filament")))
    for u in glob.glob(os.path.join(APPDATA, "user", "*", "filament")):
        dirs.append(("user", u))
    return dirs


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _num(v):
    try:
        return float(_first(v))
    except (TypeError, ValueError):
        return None


_cache = {"key": None, "presets": []}


def filament_presets():
    """Готовые к выбору профили: системные '@base' (по одному на материал) и пользовательские."""
    dirs = _profile_dirs()
    key = tuple((src, d, os.path.getmtime(d) if os.path.isdir(d) else 0) for src, d in dirs)
    if _cache["key"] == key:
        return _cache["presets"]
    # Имя профиля = имя файла. Сначала только список файлов (быстро), разбираем лишь
    # нужные: '@base', пользовательские и их родителей по цепочке inherits.
    paths, entries = {}, []
    for src, d in dirs:
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if not fn.endswith(".json"):
                    continue
                name = fn[:-5]
                if src == "user" or name not in paths:
                    paths[name] = os.path.join(root, fn)
                if src == "user" or name.endswith("@base"):
                    entries.append((src, name))
    parsed = {}

    def load(name):
        if name not in parsed:
            try:
                parsed[name] = _read_json(paths[name]) if name in paths else {}
            except Exception:
                parsed[name] = {}
        return parsed[name]

    def resolve(name, depth=0):
        d = load(name)
        parent = d.get("inherits")
        merged = resolve(parent, depth + 1) if parent and depth < 10 else {}
        merged = dict(merged)
        merged.update({k: v for k, v in d.items() if k in PROFILE_FIELDS})
        return merged

    seen, out = set(), []
    for src, name in entries:
        if name in seen:
            continue
        if src == "system" and not name.endswith("@base"):
            continue
        seen.add(name)
        r = resolve(name)
        ftype = _first(r.get("filament_type")) or ""
        out.append({
            "name": re.sub(r"\s*@base$", "", name),
            "source": src,
            "vendor": _first(r.get("filament_vendor")) or "",
            "material": ftype,
            "density": _num(r.get("filament_density")),
            "diameter_mm": _num(r.get("filament_diameter")) or 1.75,
            # цена в системных профилях условная (не ваши рубли) — берём только из своих профилей
            "price_kg": _num(r.get("filament_cost")) if src == "user" else None,
            "color": _first(r.get("default_filament_colour")) or "",
        })
    out.sort(key=lambda p: (p["source"] != "user", p["vendor"] != "Bambu Lab", p["name"].lower()))
    _cache.update(key=key, presets=out)
    return out
