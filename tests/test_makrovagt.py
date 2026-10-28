"""Makrovagten: grænser, chok, sort svane, forældede data og at den aldrig fejler."""
import datetime as dt
import json

import makrovagt as mv

REF = "2026-10-22"


def days(n, start="2025-09-01"):
    d0 = dt.date.fromisoformat(start)
    return [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)]


def flat(sid, value, n=416):
    return [(d, value) for d in days(n)]


def calm_fred():
    return {"VIXCLS": flat("VIXCLS", 15.0), "BAMLH0A0HYM2": flat("", 3.0), "BAMLC0A0CM": flat("", 0.9),
            "STLFSI4": flat("", -0.8), "NFCI": flat("", -0.5), "T10Y3M": flat("", 0.6),
            "ICSA": [(d, 220000.0) for d in days(416)[::7]], "SAHMREALTIME": flat("", 0.1),
            "DCOILBRENTEU": flat("", 70.0), "DTWEXBGS": flat("", 120.0)}


def eod_flat(v=100.0, n=416):
    return [{"date": d, "close": v, "adjusted_close": v} for d in days(n)]


def calm_eod():
    return {s: eod_flat(20.0 if s == "VIX.INDX" else 100.0) for s, *_ in mv.EOD_SERIES}


def test_calm_market_is_green_and_has_all_signals():
    m = mv.build(calm_fred(), calm_eod(), None, REF)
    assert m["level"] == "green" and not m["black_swan"] and m["headline"].startswith("Grøn")
    ids = {s["id"] for s in m["signals"]}
    assert {"vix", "hy", "ig", "stlfsi", "nfci", "curve", "claims", "sahm", "oil", "usd", "usdsek", "gold",
            "dd_gspc", "dd_omxs30", "dd_stoxx50e"} <= ids
    assert m["counts"]["signals"] >= 15 and m["counts"]["stale"] == 0
    json.dumps(m)  # skal kunne ligge i canary.json


def test_sensitive_thresholds_give_yellow_early():
    f = calm_fred()
    f["VIXCLS"] = [(d, 23.0) for d, _ in f["VIXCLS"]]      # gul fra 22 (flad: ingen spike)
    e = calm_eod()
    e["GSPC.INDX"][-1] = dict(e["GSPC.INDX"][-1], close=92.0, adjusted_close=92.0)  # 8 % under top, dagsfald 8 % → chok
    m = mv.build(f, e, None, REF)
    vix = next(s for s in m["signals"] if s["id"] == "vix")
    spx = next(s for s in m["signals"] if s["id"] == "dd_gspc")
    assert vix["level"] == "yellow"
    assert spx["level"] == "red" and spx["shock"] and "dagschok" in spx["note"]
    assert m["black_swan"] and m["level"] == "red" and m["headline"].startswith("Sort svane-varsel")
    assert "panik" in m["plan"]


def test_credit_shock_and_vix_spike_are_shocks():
    f = calm_fred()
    f["BAMLH0A0HYM2"] = [(d, 3.0 if i < 400 else 3.9) for i, (d, _) in enumerate(f["BAMLH0A0HYM2"])]
    f["VIXCLS"] = [(d, 15.0 if i < 412 else 24.0) for i, (d, _) in enumerate(f["VIXCLS"])]
    m = mv.build(f, calm_eod(), None, REF)
    hy = next(s for s in m["signals"] if s["id"] == "hy")
    vix = next(s for s in m["signals"] if s["id"] == "vix")
    assert hy["shock"] and hy["level"] == "red" and "kreditchok" in hy["note"]
    assert vix["shock"] and "chok" in vix["note"]
    assert m["black_swan"]


def test_three_reds_without_shock_is_red_but_no_swan_under_the_live_rule():
    """Analysen 28/10/2026: tre røde temaer gav ingen tidligere varsler end chokkene, kun længere alarmer."""
    f = calm_fred()
    f["VIXCLS"] = [(d, 31.0) for d, _ in f["VIXCLS"]]        # rød, men ingen spike (flad)
    f["SAHMREALTIME"] = [(d, 0.6) for d, _ in f["SAHMREALTIME"]]
    f["STLFSI4"] = [(d, 1.4) for d, _ in f["STLFSI4"]]
    assert mv.SWAN_RULE == "kun_chok"
    m = mv.build(f, calm_eod(), None, REF)
    assert m["counts"]["red"] == 3 and m["counts"]["shocks"] == 0 and not m["black_swan"]
    assert m["level"] == "red" and m["headline"].startswith("Rød:") and m["swan_rule"] == "kun_chok"
    old = mv.build(f, calm_eod(), None, REF, swan_rule="tre_temaer")   # den gamle regel kan stadig vælges
    assert old["black_swan"]


def test_stale_series_do_not_trigger():
    f = calm_fred()
    f["VIXCLS"] = [(d, 45.0) for d in days(300)]               # slutter 27/06/2026 – forældet
    m = mv.build(f, calm_eod(), None, REF)
    vix = next(s for s in m["signals"] if s["id"] == "vix")
    assert vix["stale"] and vix["level"] == "green" and "forældet" in vix["note"]
    assert m["level"] == "green" and m["counts"]["stale"] >= 1


def test_vix_falls_back_to_eodhd_when_fred_is_missing():
    f = calm_fred()
    del f["VIXCLS"]
    e = calm_eod()
    e["VIX.INDX"] = [{"date": d, "close": 33.0} for d in days(416)]
    m = mv.build(f, e, None, REF)
    vix = next(s for s in m["signals"] if s["id"] == "vix")
    assert vix["level"] == "red" and vix["value"] == 33.0


def test_curve_uninversion_and_buffett_background():
    f = calm_fred()
    f["T10Y3M"] = [(d, -0.5 if i < 380 else 0.2) for i, (d, _) in enumerate(f["T10Y3M"])]
    b = {"value": 210.0, "date": "2026-04-01", "quarter": "Q2 2026", "percentile": 97, "years": 10}
    m = mv.build(f, calm_eod(), b, REF)
    curve = next(s for s in m["signals"] if s["id"] == "curve")
    buf = next(s for s in m["signals"] if s["id"] == "buffett")
    assert curve["level"] == "yellow" and "retter sig op" in curve["note"]
    assert buf["level"] == "yellow" and m["level"] == "yellow" and m["headline"].count("Buffett") == 1
    assert m["headline"].startswith("Gul: tidlige tegn i Rentekurve") and "aktier er dyre" in m["headline"]
    # Buffett alene gør aldrig rød
    m2 = mv.build(calm_fred(), calm_eod(), b, REF)
    assert m2["level"] == "yellow" and m2["counts"]["red"] == 0


def test_oil_and_currency_moves():
    f = calm_fred()
    f["DCOILBRENTEU"] = [(d, 70.0 if i < 400 else 98.0) for i, (d, _) in enumerate(f["DCOILBRENTEU"])]
    e = calm_eod()
    e["USDSEK.FOREX"] = [{"date": d, "close": 10.0 if i < 400 else 10.6} for i, d in enumerate(days(416))]
    m = mv.build(f, e, None, REF)
    oil = next(s for s in m["signals"] if s["id"] == "oil")
    sek = next(s for s in m["signals"] if s["id"] == "usdsek")
    assert oil["level"] == "red" and oil["shock"] and "oliechok" in oil["note"]
    assert sek["level"] == "yellow" and "kroner" in sek["note"]


def test_collect_never_raises_and_counts_sources():
    class BadFred:
        available, dead = True, False

        def observations(self, *a, **k):
            raise RuntimeError("nede")

    class Eod:
        available = True

        def eod(self, sym, start, end):
            return eod_flat(20.0 if sym == "VIX.INDX" else 100.0)

    m = mv.collect(REF, BadFred(), Eod())
    n_fred = len(set([x[0] for x in mv.FRED_SERIES] + [x[0] for x in mv.tm.FRED_EXTRA]))
    n_eod = len(mv.EOD_SERIES) + len(mv.tm.EOD_EXTRA)
    assert m["sources"]["fred"] == f"0 af {n_fred} serier"
    assert m["sources"]["eodhd"] == f"{n_eod} af {n_eod} serier"
    assert any(s["id"] == "vix" for s in m["signals"])  # reserve fra EODHD
    assert mv.collect(REF, None, None)["counts"]["signals"] == 0
    assert "Makrovagt: grøn" in mv.status_line(m)
    assert mv.agent_brief(m).startswith("**Makrovagt (grøn")
    # "frisk"-reglen får farverne fra 30 dage før med; uden data er fortiden ukendt
    assert m["swan_rule"] == mv.SWAN_RULE and isinstance(m["fresh_reds"], list)
    assert mv.collect(REF, None, None)["fresh_reds"] is None

