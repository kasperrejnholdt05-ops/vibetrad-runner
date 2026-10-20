"""FRED: klient, snapshot, statuslinje og agent-facit. Ingen netværk: falsk opener."""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
import fred as fr  # noqa: E402

KEY = "abcdef0123456789abcdef0123456789"  # 32 tegn som en rigtig FRED-nøgle


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _obs(*pairs):
    return {"observations": [{"date": d, "value": v} for d, v in pairs]}


def opener_from(table, seen=None):
    """table: serie-id -> dict (JSON-svar) | Exception."""
    def op(req, timeout=0):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(req.full_url).query))
        if seen is not None:
            seen.append(q)
        hit = table.get(q["series_id"], table.get("*"))
        if isinstance(hit, Exception):
            raise hit
        return _Resp(json.dumps(hit).encode())
    return op


def http_error(code, body):
    return urllib.error.HTTPError("https://api.stlouisfed.org/x?api_key=" + KEY, code, "Bad Request", {},
                                  io.BytesIO(json.dumps(body).encode()))


BASE = _obs(("2026-09-08", "4.00"), ("2026-09-15", "4.05"), ("2026-10-13", "."), ("2026-10-14", "4.12"),
            ("2026-10-16", "9.99"))  # efter ref-dagen: må ikke bruges


def _quarters(first_year, values):
    """Kvartalsvise observationer fra Q1 ``first_year``; værdier som strenge."""
    out, y, m = [], first_year, 1
    for v in values:
        out.append((f"{y}-{m:02d}-01", v))
        m += 3
        if m > 12:
            y, m = y + 1, 1
    return _obs(*out)


# 46 kvartaler Q1 2015–Q2 2026: aktier (mio. USD) stiger 100 → 270 % af BNP (mia. USD, fast 30 000).
# Q3 2026 (2026-07-01) findes i BNP men ikke i aktieserien → må ikke give en række.
_EQ = [str(int((100 + i * 170 / 45) * 30_000 * 1000 / 100)) for i in range(46)]
BUFFETT_TABLE = {fr.BUFFETT_EQUITY: _quarters(2015, _EQ),
                 fr.BUFFETT_GDP: _quarters(2015, ["30000"] * 47)}


def test_api_key_from_env(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", f"  {KEY} ")
    assert fr.api_key() == KEY and fr.FredClient().available
    monkeypatch.delenv("FRED_API_KEY")
    assert fr.api_key() == "" and not fr.FredClient().available


def test_missing_key_makes_no_calls(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    calls = []
    snap = fr.snapshot("2026-10-15", fr.FredClient(opener=opener_from({"*": BASE}, calls)))
    assert snap["key"] is False and snap["ok"] is False and snap["calls"] == 0 and not calls
    assert fr.status_line(snap) == "FRED: nej (secret FRED_API_KEY mangler)"
    assert fr.agent_brief(snap) == ""


def test_snapshot_latest_change_gaps_and_cutoff():
    seen = []
    snap = fr.snapshot("2026-10-15", fr.FredClient(KEY, opener=opener_from({"*": BASE}, seen)))
    assert snap["ok"] and snap["calls"] == len(fr.SERIES) + 2 and len(snap["series"]) == len(fr.SERIES)  # +2: Buffett
    row = snap["series"][0]
    assert row["id"] == "DGS10" and row["value"] == 4.12 and row["date"] == "2026-10-14"  # "." sprunget over
    assert row["prev_date"] == "2026-09-08" and row["prev"] == 4.0 and row["change"] == pytest.approx(0.12)
    assert row["stale"] is False
    q = seen[0]
    assert q["observation_end"] == "2026-10-15" and q["observation_start"] == "2026-08-01" and q["file_type"] == "json"
    assert [x["series_id"] for x in seen[-2:]] == [fr.BUFFETT_EQUITY, fr.BUFFETT_GDP]
    assert seen[-1]["observation_start"] == "2015-10-18"  # 11 års kvartaler, så 10 års kontekst altid er fuld


def test_stale_series_flagged_with_own_tolerance():
    old = _obs(("2026-09-01", "9.40"), ("2026-10-02", "9.50"))  # 13 dage gammel
    snap = fr.snapshot("2026-10-15", fr.FredClient(KEY, opener=opener_from({"*": old})))
    by = {r["id"]: r for r in snap["series"]}
    assert by["DGS10"]["stale"] is True  # dagsserie: maks 7 dage
    assert by["DEXSDUS"]["stale"] is False  # H.10 udgives ugentligt: maks 16 dage
    assert "(forældet)" in fr.agent_brief(snap)


def test_bad_key_stops_after_first_call_and_never_leaks():
    calls = []
    err = http_error(400, {"error_code": 400, "error_message":
                           "Bad Request.  The value for variable api_key is not registered."})
    snap = fr.snapshot("2026-10-15", fr.FredClient(KEY, opener=opener_from({"*": err}, calls)))
    assert len(calls) == 1 and snap["ok"] is False and snap["key"] is True
    assert fr.status_line(snap) == "FRED: nøglen virker ikke (forkert nøgle)"
    assert KEY not in json.dumps(snap) and fr.agent_brief(snap) == ""


def test_unknown_series_does_not_sink_the_rest():
    err = http_error(400, {"error_code": 400, "error_message": "Bad Request.  The series does not exist."})
    snap = fr.snapshot("2026-10-15", fr.FredClient(KEY, opener=opener_from({"VIXCLS": err, "*": BASE})))
    by = {r["id"]: r for r in snap["series"]}
    assert snap["ok"] and by["VIXCLS"]["value"] is None and "kender ikke serien VIXCLS" in by["VIXCLS"]["error"]
    assert "1 serie(r) fejlede" in snap["error"]
    assert fr.status_line(snap).startswith(f"FRED: ja, nøglen virker ({len(fr.SERIES) - 1} af {len(fr.SERIES)} serier")


def test_retry_on_429_then_success():
    state = {"n": 0}

    def op(req, timeout=0):
        state["n"] += 1
        if state["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, io.BytesIO(b"{}"))
        return _Resp(json.dumps(BASE).encode())

    sleeps = []
    c = fr.FredClient(KEY, opener=op, sleep=sleeps.append, retries=1)
    assert c.observations("DGS10")[-1] == ("2026-10-16", 9.99) and sleeps == [1.5] and c.calls == 1


def test_network_error_is_redacted():
    boom = urllib.error.URLError(f"connect failed for https://api.stlouisfed.org/?api_key={KEY}")
    c = fr.FredClient(KEY, opener=opener_from({"*": boom}), sleep=lambda s: None, retries=0)
    with pytest.raises(fr.FredError) as exc:
        c.observations("DGS10")
    assert KEY not in str(exc.value) and "api_key=***" in str(exc.value)
    calls = []
    snap = fr.snapshot("2026-10-15", fr.FredClient(KEY, opener=opener_from({"*": boom}, calls),
                                                  sleep=lambda s: None, retries=0))
    assert len(calls) == 2 and snap["ok"] is False  # nedbrud: stopper efter to serier i træk
    assert fr.status_line(snap).startswith("FRED: ingen data (FRED svarede ikke (DGS10)") and KEY not in fr.status_line(snap)
    assert fr.redact(f"x api_key={KEY}&y=1 {KEY}", KEY) == "x api_key=***&y=1 ***"


def test_status_line_and_brief_format():
    snap = fr.snapshot("2026-10-15", fr.FredClient(KEY, opener=opener_from({"*": BASE, **BUFFETT_TABLE})))
    line = fr.status_line(snap)
    assert line == (f"FRED: ja, nøglen virker ({len(fr.SERIES)} af {len(fr.SERIES)} serier, {len(fr.SERIES) + 2} kald · "
                    "US 10-årig statsrente 4,12 % pr. 14/10/2026 · Buffett-indikator 270 % (Q2 2026))")
    brief = fr.agent_brief(snap)
    assert brief.startswith("## Makro (FRED, St. Louis Fed) pr. 15/10/2026")
    assert "| US 10-årig statsrente (DGS10) | 4,12 % | 14/10/2026 | +0,12 pp |" in brief
    assert "| USD/SEK (Fed H.10, ugentlig – kun makrokontekst, ikke til omregning) (DEXSDUS) | 4,1200 SEK | 14/10/2026 | +0,1200 SEK |" in brief
    assert "| VIX (forventet aktievolatilitet) (VIXCLS) | 4,12 | 14/10/2026 | +0,12 |" in brief
    assert "Buffett-indikator (amerikanske ikke-finansielle selskabers aktier / BNP, Fed Z.1 + BEA): 270 % pr. Q2 2026 (forrige kvartal 266 %)" in brief
    assert "percentil 100 – over 10-års gennemsnittet" in brief and "aldrig som timing-signal" in brief
    assert len(brief) < 2000


def test_buffett_ratio_ten_year_context():
    eq = [(d, float(v)) for d, v in ((o["date"], o["value"]) for o in BUFFETT_TABLE[fr.BUFFETT_EQUITY]["observations"])]
    gdp = [(d, float(v)) for d, v in ((o["date"], o["value"]) for o in BUFFETT_TABLE[fr.BUFFETT_GDP]["observations"])]
    b = fr.buffett_ratio(eq, gdp, "2026-10-15")
    assert b["value"] == 270.0 and b["date"] == "2026-04-01" and b["quarter"] == "Q2 2026"
    assert b["prev"] == pytest.approx(266.2, abs=0.05) and b["years"] == 10 and b["quarters"] == 41  # Q2 2016–Q2 2026
    assert b["min"] == pytest.approx(118.9, abs=0.05) and b["max"] == 270.0 and b["avg"] == pytest.approx(194.4, abs=0.05)
    assert b["percentile"] == 100
    # ref-dag før seneste kvartal: kun data til og med ref-dagen tæller
    assert fr.buffett_ratio(eq, gdp, "2020-02-01")["quarter"] == "Q1 2020"
    # kvartal uden BNP-match giver ingen række; tomme serier giver None
    assert fr.buffett_ratio([("2026-07-01", 5.0)], gdp[:-1], "2026-10-15") is None
    assert fr.buffett_ratio([], [], "2026-10-15") is None
    assert fr.buffett_line(None) is None


def test_buffett_error_does_not_sink_snapshot():
    table = {"*": BASE, fr.BUFFETT_EQUITY: http_error(400, {"error_message": "Bad Request. The series does not exist."})}
    snap = fr.snapshot("2026-10-15", fr.FredClient(KEY, opener=opener_from(table)))
    assert snap["ok"] and snap["buffett"] is None and "kender ikke serien" in snap["buffett_error"]
    assert "Buffett" not in fr.status_line(snap) and "Buffett" not in fr.agent_brief(snap)


def test_status_line_without_snapshot():
    assert fr.status_line(None) == "FRED: ikke kørt"
