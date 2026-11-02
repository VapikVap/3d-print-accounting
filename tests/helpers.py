import os

from app import db


def fresh_db():
    """Пустая временная база (PRINT_DB указывает во временную папку, см. tests/__init__.py)."""
    assert "print-tests-" in db.DB_PATH, "тесты не должны работать с рабочей базой"
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db.DB_PATH + suffix)
        except FileNotFoundError:
            pass
    db.init()
