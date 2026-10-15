"""Fælles test-opsætning: en EODHD-nøglefil på udviklerens maskine må aldrig påvirke testene."""
import pytest


@pytest.fixture(autouse=True)
def _no_local_eodhd_key_file(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("EODHD_KEY_FILE", str(tmp_path_factory.getbasetemp() / "ingen-eodhd-noegle"))
