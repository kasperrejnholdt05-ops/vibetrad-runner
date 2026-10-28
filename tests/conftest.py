"""Fælles test-opsætning: runner/ og tests/ på stien, og en EODHD-nøglefil på udviklerens maskine må aldrig
påvirke testene."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "runner", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture(autouse=True)
def _no_local_eodhd_key_file(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("EODHD_KEY_FILE", str(tmp_path_factory.getbasetemp() / "ingen-eodhd-noegle"))
