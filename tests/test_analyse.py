"""analyse.py: sort svane-reglerne og top-alderen målt på syntetiske forløb med kendt facit."""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
sys.path.insert(0, str(ROOT / "tests"))
import analyse as an  # noqa: E402
import make_zoner_fixtures as mk  # noqa: E402
import makrovagt as mv  # noqa: E402
import temaer as tm  # noqa: E402

CALM = {"VIXCLS": 15.0, "BAMLH0A0HYM2": 3.3, "BAMLC0A0CM": 0.95, "STLFSI4": -0.5, "NFCI": -0.4, "T10Y3M": 1.0,
        "SAHMREALTIME": 0.1, "DCOILBRENTEU": 70.0, "DTWEXBGS": 120.0, "DGS10": 4.0, "DGS30": 4.5, "DGS2": 3.8,
        "DFII10": 1.5, "DFEDTARU": 4.0, "T10YIE": 2.2, "THREEFYTP10": 0.3, "BAMLH0A3HYC": 7.0, "BAMLC0A4CBBB": 1.2,
        "SOFR": 3.85, "IORB": 3.90, "RRPONTSYD": 400.0, "DHHNGSP": 3.0, "USEPUINDXD": 120.0}


def _days(a: str, b: str) -> list[str]:
    d, e, out = dt.date.fromisoformat(a), dt.date.fromisoformat(b), []
    while d <= e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def _world(start="2005-01-03", end="2011-12-30", stress_from="2009-01-05"):
    """Roligt marked; fra ``stress_from`` springer olie, CCC og den 10-årige rente op og bliver der (tre røde temaer)."""
    days = _days(start, end)
    jump = {"DCOILBRENTEU": 130.0, "BAMLH0A3HYC": 13.0, "DGS10": 5.6}
    series = {sid: [(d, (jump[sid] if (sid in jump and d >= stress_from) else v)) for d in days] for sid, v in CALM.items()}
    series["ICSA"] = [(d, 210000.0) for d in days[::5]]
    up = [{"date": d, "close": 100.0 + i * 0.01, "adjusted_close": 100.0 + i * 0.01} for i, d in enumerate(days)]
    rows = {s: up for s, *_ in mv.EOD_SERIES}
    rows.update({s: up for s, _ in tm.EOD_EXTRA})
    rows["VIX.INDX"] = [{"date": d, "close": 15.0} for d in days]
    rows["USDSEK.FOREX"] = [{"date": d, "close": 10.0} for d in days]
    # S&P 500: stiger, falder 25 % fra marts 2009 til juni 2009, kommer igen
    spx_v = []
    for d in days:
        if d < "2009-03-02":
            v = 1000.0
        elif d < "2009-06-01":
            v = 1000.0 - 250.0 * (dt.date.fromisoformat(d) - dt.date(2009, 3, 2)).days / 91
        else:
            v = min(1000.0, 750.0 + 0.8 * (dt.date.fromisoformat(d) - dt.date(2009, 6, 1)).days)
        spx_v.append((d, v))
    return series, rows, spx_v


def test_runs_and_declines():
    assert an._runs([False, True, True, False, True]) == [(1, 2), (4, 4)]
    assert an._runs([]) == []
    _, _, spx = _world()
    big = an.declines(spx)
    assert len(big) == 1 and big[0]["top_date"] < "2009-03-03" and 24 <= big[0]["max_dd"] <= 25.1
    assert big[0]["warn_date"] < big[0]["bottom_date"]


def test_swan_rules_on_a_regime_that_stays_red():
    """Tre temaer springer rødt og bliver der: "tre_temaer" er tændt i årevis, "frisk" og "kun_chok" kun i starten."""
    series, rows, spx = _world()
    rep = an.macro_replay(series, rows, spx, start="2008-01-04", end="2011-12-30")
    r = rep["rules"]
    assert set(r) == set(mv.SWAN_RULES) and rep["weeks"] > 200
    assert r["tre_temaer"]["longest_weeks"] > 100 and r["tre_temaer"]["now"]["swan"]
    assert r["frisk"]["longest_weeks"] <= 6 and not r["frisk"]["now"]["swan"]
    assert r["kun_chok"]["longest_weeks"] <= 6 and r["kun_chok"]["pct_on"] <= r["frisk"]["pct_on"]
    assert r["frisk"]["now"]["level"] == "red"        # niveauet er stadig rødt – kun varslet slukker
    first = r["frisk"]["alarm_list"][0]
    assert "2009-01-05" <= first["from"] <= "2009-01-12"
    # varslet i januar kom før faldet i marts og nåede det
    assert r["frisk"]["declines_early"] == 1 and r["frisk"]["warned"][0]["early"]


def test_assess_fresh_rule_and_unknown_past():
    sig = [{"id": "ccc", "level": "red", "shock": False, "stale": False, "theme": "kredit", "label": "CCC"},
           {"id": "brent_lvl", "level": "red", "shock": False, "stale": False, "theme": "energi", "label": "Brent"},
           {"id": "dgs10", "level": "red", "shock": False, "stale": False, "theme": "renter", "label": "Rente"}]
    old = {"ccc": "red", "brent_lvl": "red", "dgs10": "red"}
    assert mv.assess(sig, old, "tre_temaer")["swan"]
    assert not mv.assess(sig, old, "frisk")["swan"] and mv.assess(sig, old, "frisk")["level"] == "red"
    assert mv.assess(sig, {**old, "dgs10": "yellow"}, "frisk")["swan"]      # ét nyt rødt er nok
    assert mv.assess(sig, None, "frisk")["swan"]                             # ukendt fortid: hellere alarm
    assert not mv.assess(sig, None, "kun_chok")["swan"]
    try:
        mv.assess(sig, None, "findes_ikke")
        raise AssertionError
    except ValueError:
        pass


def test_build_reports_rule_and_fresh_reds():
    series, rows, _ = _world()
    s2, r2 = mv.truncate(series, rows, "2009-03-02", 430)
    prev = mv.levels_at(s2, r2, None, "2009-01-30")
    m = mv.build(s2, r2, None, "2009-03-02", prev_levels=prev, swan_rule="frisk")
    assert m["swan_rule"] == "frisk" and m["fresh_reds"] == [] and not m["black_swan"] and m["level"] == "red"
    m2 = mv.build(s2, r2, None, "2009-03-02", prev_levels=prev, swan_rule="tre_temaer")
    assert m2["black_swan"]


def test_top_age_variants_and_summary():
    v = mk.path([(0, 80), (300, 100), (420, 76), (700, 93), (760, 86), (1300, 96), (1500, 80), (1900, 99)])
    s = list(zip(mk.days(len(v), "2016-01-04"), v))
    rep = an.zone_report({"omxc25": s})
    x = rep["omxc25"]
    assert set(x) == {"756", "504", "252"}
    for td, y in x.items():
        assert y["top_days"] == int(td) and y["fills"] >= y["early_fills"] >= 0 and y["now"]["next"]
    assert x["252"]["fills"] >= x["756"]["fills"]   # en yngre top giver nye episoder efter lange fald
    doc = {"generated_for": "2026-10-28", "macro": None, "zones": rep}
    text = an.summary(doc)
    assert "Top-alder omxc25" in text and "252 dage" in text
    json.dumps(doc)


def test_replay_is_fast_enough_for_github():
    """20 års ugentlig genberegning skal kunne køre på få minutter (workflowet har 30)."""
    series, rows, spx = _world(start="2005-01-03", end="2008-12-31", stress_from="2099-01-01")
    t0 = time.perf_counter()
    an.macro_replay(series, rows, spx, start="2006-06-02", end="2008-12-31")
    per_week = (time.perf_counter() - t0) / 135
    assert per_week * 1030 < 600, per_week
