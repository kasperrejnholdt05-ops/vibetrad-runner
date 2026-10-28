"""Tests for runner/eodhd.py (EODHD-klient og integration i Vibe-Trading) – kør med:  python -m pytest -q"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
import eodhd as eo  # noqa: E402

KEY = "sk-eodhd-HEMMELIG-123456"


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_for(routes: dict, seen: list):
    def _open(req, timeout=0):
        seen.append(req.full_url)
        for frag, val in routes.items():
            if frag in req.full_url:
                if isinstance(val, Exception):
                    raise val
                return Resp(json.dumps(val).encode())
        return Resp(b"[]")
    return _open


@pytest.mark.parametrize("src,want", [("AAPL.US", "AAPL.US"), ("NOVO-B.CO", "NOVO-B.CO"), ("SAP.DE", "SAP.XETRA"),
                                      ("VOD.L", "VOD.LSE"), ("USDSEK=X", "USDSEK.FOREX"), ("USD/SEK", "USDSEK.FOREX"),
                                      ("^GSPC", "GSPC.INDX"), ("GOOGL", "GOOGL.US"), ("00700.HK", "0700.HK"),
                                      ("inve-b.st", "INVE-B.ST")])
def test_symbol_mapping(src, want):
    assert eo.to_eodhd(src) == want


def test_reverse_mapping_currency_and_eu_detection():
    assert eo.to_yahoo("GOOGL.US") == "GOOGL" and eo.to_yahoo("SAP.XETRA") == "SAP.DE" and eo.to_yahoo("USDSEK.FOREX") == "USDSEK=X"
    assert eo.to_yahoo("BRK.B.US") == "BRK-B"
    assert [eo.currency_of(s) for s in ("NVDA.US", "NOVO-B.CO", "INVE-B.ST", "EQNR.OL", "SAP.DE", "USDSEK=X")] == \
        ["USD", "DKK", "SEK", "NOK", "EUR", "SEK"]
    assert eo.is_eu_symbol("NOVO-B.CO") and eo.is_eu_symbol("SAP.XETRA") and eo.is_eu_symbol("ERIC-B.ST")
    assert not eo.is_eu_symbol("NVDA.US") and not eo.is_eu_symbol("VOD.L") and not eo.is_eu_symbol("GC=F")


def test_client_builds_urls_caches_and_parses():
    seen: list = []
    c = eo.EodhdClient(KEY, opener=opener_for({
        "/eod/NOVO-B.CO": [{"date": "2026-10-14", "close": 255.95, "adjusted_close": 255.95}],
        "/calendar/earnings": {"earnings": [{"code": "NOVO-B.CO", "report_date": "2026-11-04"}]},
        "/fundamentals/SPCX.US": {"General": {"Name": "Space Exploration Technologies"}, "Highlights": {}},
        "/screener": {"data": [{"code": "AZN", "exchange": "ST"}]},
    }, seen), sleep=lambda s: None)
    assert c.eod("NOVO-B.CO", "2026-10-01", "2026-10-15")[0]["close"] == 255.95
    assert c.eod("NOVO-B.CO", "2026-10-01", "2026-10-15")  # fra cache
    assert len(seen) == 1 and "api_token=" + KEY in seen[0] and "fmt=json" in seen[0] and "from=2026-10-01" in seen[0]
    assert c.earnings(["NOVO-B.CO"], "2026-10-01", "2026-12-31")[0]["report_date"] == "2026-11-04"
    assert c.fundamentals("SPCX.US")["General"]["Name"].startswith("Space")
    assert c.screener([["exchange", "=", "ST"]], "market_capitalization.desc", 5)[0]["code"] == "AZN"
    assert c.calls == 1 + 1 + 10 + 5 and c.available


def test_errors_never_contain_the_key():
    seen: list = []
    err401 = urllib.error.HTTPError("https://eodhd.com/api/eod/X?api_token=" + KEY, 401, "nope", {}, None)
    c = eo.EodhdClient(KEY, opener=opener_for({"/eod/": err401}, seen), sleep=lambda s: None)
    with pytest.raises(eo.EodhdError) as e:
        c.eod("SPCX.US")
    assert KEY not in str(e.value) and "401" in str(e.value)
    with pytest.raises(eo.EodhdError):  # nøglen afvist → opgiv resten af kørslen uden nye kald
        c.eod("GOOGL.US")
    assert len(seen) == 1

    down = urllib.error.URLError(f"connect failed https://eodhd.com/api/x?api_token={KEY}")
    seen2: list = []
    now = {"t": 0.0}
    c2 = eo.EodhdClient(KEY, opener=opener_for({"/": down}, seen2), sleep=lambda s: None, retries=1,
                        clock=lambda: now["t"])
    for _ in range(6):
        with pytest.raises(eo.EodhdError) as e:
            c2.eod("SPCX.US")
        assert KEY not in str(e.value)
    assert len(seen2) == 8 and all(KEY not in m for m in c2.errors)  # 4 kald × 2 forsøg, så pause
    now["t"] = 301.0  # efter pausen prøves igen
    with pytest.raises(eo.EodhdError):
        c2.eod("SPCX.US")
    assert len(seen2) == 10


def test_403_blocks_only_that_endpoint():
    seen: list = []
    err403 = urllib.error.HTTPError("u", 403, "plan", {}, None)
    c = eo.EodhdClient(KEY, opener=opener_for({"/fundamentals/": err403, "/eod/": [{"date": "2026-10-14", "close": 1}]},
                                              seen), sleep=lambda s: None)
    with pytest.raises(eo.EodhdError) as e:
        c.fundamentals("SPCX.US")
    assert "403" in str(e.value) and "fundamentals" in str(e.value)
    with pytest.raises(eo.EodhdError):
        c.fundamentals("GOOGL.US")  # uden nyt kald
    assert c.eod("SPCX.US")[0]["close"] == 1 and len(seen) == 2


def test_redact():
    assert eo.redact(f"x?api_token={KEY}&a=1", KEY) == "x?api_token=***&a=1"


def test_retry_on_429_then_success_and_404_is_empty():
    calls = {"n": 0}

    def _open(req, timeout=0):
        calls["n"] += 1
        if "/div/" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        if calls["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "slow", {}, None)
        return Resp(b'[{"date":"2026-10-14","close":1}]')

    c = eo.EodhdClient(KEY, opener=_open, sleep=lambda s: None)
    assert c.eod("A.US")[0]["close"] == 1 and calls["n"] == 2
    assert c.dividends("A.US") == []


def test_missing_key_is_unavailable(monkeypatch):
    for k in eo.KEY_ENVS:
        monkeypatch.delenv(k, raising=False)
    c = eo.EodhdClient()
    assert not c.available
    with pytest.raises(eo.EodhdError):
        c.eod("A.US")
    monkeypatch.setenv("EODHD_API_TOKEN", "fra-plugin-navnet")
    assert eo.api_key() == "fra-plugin-navnet"


def test_economic_events_pass_type_filter():
    seen: list = []
    c = eo.EodhdClient(KEY, opener=opener_for({"/economic-events": [{"type": "Interest Rate Decision"}]}, seen),
                       sleep=lambda s: None)
    assert c.economic_events("SE", "2026-04-01", "2027-02-01", 200, event_type="Interest Rate Decision")[0]["type"] == "Interest Rate Decision"
    assert "type=Interest+Rate+Decision" in seen[-1] and "country=SE" in seen[-1]


def test_key_file_fallback_for_codespace(monkeypatch, tmp_path):
    for k in eo.KEY_ENVS:
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / "eodhd_key"
    f.write_text("fil-noegle-123456\n", "utf-8")
    monkeypatch.setenv("EODHD_KEY_FILE", str(f))
    assert eo.api_key() == "fil-noegle-123456"
    monkeypatch.setenv("EODHD_API_KEY", "env-vinder-123456")
    assert eo.api_key() == "env-vinder-123456"

