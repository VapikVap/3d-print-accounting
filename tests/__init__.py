"""Тесты работают на временной базе — data/print.db не трогается."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="print-tests-")
os.environ["PRINT_DB"] = os.path.join(_TMP, "test.db")
