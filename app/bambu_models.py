"""Коды моделей Bambu Lab (DevModel в объявлении SSDP, printer_model_id в 3mf).

Таблица снята из Bambu Studio 02.08.02.61 (resources/printers/*.json, поле model_id
и display_name). Если Bambu Studio установлена — читаем её файлы: там будут и
модели новее этой таблицы.
"""
import glob
import json
import os

KNOWN = {
    "BL-P001": "Bambu Lab X1 Carbon", "BL-P002": "Bambu Lab X1", "C11": "Bambu Lab P1P",
    "C12": "Bambu Lab P1S", "C13": "Bambu Lab X1E", "N1": "Bambu Lab A1 mini", "N2S": "Bambu Lab A1",
    "N6": "Bambu Lab X2D", "N7": "Bambu Lab P2S", "N9": "Bambu Lab A2L", "O1C": "Bambu Lab H2C",
    "O1C2": "Bambu Lab H2C", "O1D": "Bambu Lab H2D", "O1E": "Bambu Lab H2D Pro", "O1S": "Bambu Lab H2S",
    # старое имя X1C в объявлении SSDP
    "3DPrinter-X1-Carbon": "Bambu Lab X1 Carbon", "3DPrinter-X1": "Bambu Lab X1",
}

_cache = {}


def _from_studio():
    if "studio" in _cache:
        return _cache["studio"]
    out = {}
    try:
        from . import slicers
        exe = next((s["exe"] for s in slicers.find_all() if s["kind"] == "bambu"), None)
    except Exception:
        exe = None
    if exe:
        for f in glob.glob(os.path.join(os.path.dirname(exe), "resources", "printers", "*.json")):
            try:
                with open(f, encoding="utf-8") as fh:
                    base = (json.load(fh) or {}).get("00.00.00.00") or {}
            except Exception:
                continue
            if base.get("model_id") and base.get("display_name"):
                out[base["model_id"]] = base["display_name"]
    _cache["studio"] = out
    return out


def model_name(code):
    """'C12' -> 'Bambu Lab P1S'; неизвестный код — 'Bambu Lab (код XYZ)'."""
    code = (code or "").strip()
    if not code:
        return ""
    return _from_studio().get(code) or KNOWN.get(code) or "Bambu Lab (код %s)" % code
