"""Tests for runner/datatjek.py (krydstjek Vibe-Trading × EODHD) – kør med:  python -m pytest -q"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
import datatjek as dtj  # noqa: E402
import run_week as rw  # noqa: E402

REF = dt.datetime(2026, 10, 15, 12, 0, tzinfo=rw.STO)
WEEK = rw.week_window(REF)  # uge 42 til dato: 12/10–14/10 (seneste US-lukning)
P = rw.load_portfolio("""{"account":"ISK Nordnet","currency":"SEK","holdings":[
  {"ticker":"SPCX.US","name":"SpaceX","qty":13,"cost_sek":21480,"max_weight":10},
  {"ticker":"GOOGL.US","name":"Alphabet A","qty":3,"cost_sek":9315,"max_weight":10}],
  "cash_sek":2615,"watchlist":[{"ticker":"NOVO-B.CO","note":"aaben limitordre 5 stk, regnskab 04/11/2026"}],
  "next_reports":{"GOOGL.US":"2026-10-28","SPCX.US":"2026-11-03"}}""")

DAYS = ["2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12",
        "2026-10-13", "2026-10-14"]
BASE = {"SPCX.US": 150.0, "GOOGL.US": 350.0, "NOVO-B.CO": 255.0, "ACWI.US": 160.0, "USDSEK=X": 10.0, "DKKSEK=X": 1.5}


def series(code: str, bump: float = 0.0, days=DAYS) -> dict:
    b = BASE[code]
    rows = [(d, round(b * (1 + 0.002 * i) * (1 + (bump if d == days[-1] else 0)), 6)) for i, d in enumerate(days)]
    return {"rows": rows, "source": "yahoo", "currency": None}


class Fake:
    def __init__(self, label, px, profiles=None, earnings=None, actions=None, available=True):
        self.label, self._px, self._prof = label, px, profiles or {}
        self._earn, self._act = earnings or {}, actions or {}
        self.available, self.errors = available, []

    def prices(self, codes, start, end):
        return {c: self._px[c] for c in codes if c in self._px}

    def profile(self, code):
        return self._prof.get(code, {})

    def earnings(self, codes, start, end):
        return {c: self._earn[c] for c in codes if c in self._earn}

    def actions(self, code, start):
        return self._act.get(code, [])


def sources(**over):
    px = {c: series(c) for c in BASE}
    vt_prof = {"SPCX.US": {"name": "Space Exploration Technologies Corp.", "currency": "USD", "market_cap": 2.0e12,
                           "earnings_dates": ["2026-11-03"]},
               "GOOGL.US": {"name": "Alphabet Inc.", "currency": "USD", "market_cap": 4.2e12, "earnings_dates": ["2026-10-28"]},
               "NOVO-B.CO": {"name": "Novo Nordisk A/S", "currency": "DKK", "earnings_dates": ["2026-11-04"]}}
    eod_prof = {"SPCX.US": {"name": "Space Exploration Technologies Corp. Class A Common Stock", "currency": "USD",
                            "market_cap": 2.005e12, "ipo_date": "2026-06-12"},
                "GOOGL.US": {"name": "Alphabet Inc Class A", "currency": "USD", "market_cap": 4.25e12},
                "NOVO-B.CO": {"name": "Novo Nordisk A/S", "currency": "DKK"}}
    earn = {"SPCX.US": [{"date": "2026-11-03", "when": "AfterMarket"}], "GOOGL.US": [{"date": "2026-10-28", "when": "AfterMarket"}],
            "NOVO-B.CO": [{"date": "2026-11-04", "when": "BeforeMarket"}]}
    vt = Fake("Vibe-Trading", over.pop("vt_px", px), over.pop("vt_prof", vt_prof))
    eod = Fake("EODHD", over.pop("eod_px", {k: dict(v, source="eodhd") for k, v in px.items()}),
               over.pop("eod_prof", eod_prof), over.pop("earn", earn), over.pop("actions", {}),
               available=over.pop("eod_available", True))
    return vt, eod


def run(**over):
    vt, eod = sources(**over)
    return dtj.run(P, WEEK, REF, vt=vt, eod=eod)


def test_agreeing_sources_pass_with_verified_dates_and_weights():
    dc = run()
    assert dc["status"] == "PASS", dc["issues"]
    assert {i["ticker"] for i in dc["instruments"]} == {"SPCX.US", "GOOGL.US", "NOVO-B.CO", "ACWI.US"}
    assert {f["pair"] for f in dc["fx"]} == {"USDSEK", "DKKSEK"}
    assert dtj.verified_reports(dc) == {"SPCX.US": "2026-11-03", "GOOGL.US": "2026-10-28", "NOVO-B.CO": "2026-11-04"}
    pf = dc["portfolio"]
    assert pf["chosen"] == "eodhd" and pf["diff_pct"] == 0
    spcx = next(r for r in pf["rows"] if r["ticker"] == "SPCX.US")
    assert spcx["over_max"] and spcx["weight_pct"] > 50  # 13 stk × ~153 USD × 10 SEK af ~33 000 SEK
    last = {c: series(c)["rows"][-1][1] for c in BASE}
    want = 2615 + 13 * last["SPCX.US"] * last["USDSEK=X"] + 3 * last["GOOGL.US"] * last["USDSEK=X"]
    assert pf["total_sek"] == pytest.approx(want, abs=0.05) and pf["value_sek"]["vibe_trading"] == pf["value_sek"]["eodhd"]
    assert dc["sources"]["vibe_trading"]["served_by"] == {"yahoo": 6}


def test_week_return_uses_last_close_before_week():
    rows = series("SPCX.US")["rows"]
    want = (rows[-1][1] / rows[5][1] - 1) * 100  # 09/10 → 14/10
    assert dtj.week_return(rows, WEEK["from"], WEEK["to"]) == pytest.approx(want)
    assert dtj.week_return(rows[6:], WEEK["from"], WEEK["to"]) is None


@pytest.mark.parametrize("bump,status", [(0.003, "PASS"), (0.012, "ADVARSEL"), (0.03, "FEJL")])
def test_price_tolerances(bump, status):
    eod_px = {c: dict(series(c), source="eodhd") for c in BASE}
    eod_px["GOOGL.US"] = dict(series("GOOGL.US", bump), source="eodhd")
    dc = run(eod_px=eod_px)
    g = next(i for i in dc["instruments"] if i["ticker"] == "GOOGL.US")
    assert dc["status"] == status
    if status != "PASS":
        assert any("GOOGL.US" in s and "afviger" in s for s in dc["issues"])
    assert g["compare_date"] == "2026-10-14"


def test_missing_source_and_stale_data():
    eod_px = {c: dict(series(c), source="eodhd") for c in BASE if c != "NOVO-B.CO"}
    vt_px = {c: series(c) for c in BASE}
    vt_px["ACWI.US"] = series("ACWI.US", days=DAYS[:3])  # seneste kurs 06/10 → forældet
    eod_px["ACWI.US"] = dict(series("ACWI.US", days=DAYS[:3]), source="eodhd")
    dc = run(eod_px=eod_px, vt_px=vt_px)
    rows = {i["ticker"]: i for i in dc["instruments"]}
    assert rows["NOVO-B.CO"]["status"] == "warn" and "kun Vibe-Trading" in rows["NOVO-B.CO"]["notes"][0]
    assert rows["ACWI.US"]["status"] == "fail" and any("forældede" in n for n in rows["ACWI.US"]["notes"])
    assert dc["status"] == "FEJL"


def test_portfolio_date_that_disagrees_is_flagged():
    p = dict(P, next_reports={"GOOGL.US": "2026-10-21", "SPCX.US": "2026-11-03"})
    vt, eod = sources()
    dc = dtj.run(p, WEEK, REF, vt=vt, eod=eod)
    g = next(e for e in dc["earnings"] if e["ticker"] == "GOOGL.US")
    assert g["verified"] == "2026-10-28" and g["agree"] == 2 and g["status"] == "warn"
    assert any("ret datoen i VT_PORTFOLIO" in n for n in g["notes"])
    n = next(e for e in dc["earnings"] if e["ticker"] == "NOVO-B.CO")
    assert n["portfolio"] == "2026-11-04"  # læst fra noten på overvågningslisten


def test_one_day_apart_is_ok_and_no_date_is_warned():
    earn = {"SPCX.US": [{"date": "2026-11-04", "when": None}], "GOOGL.US": [{"date": "2026-10-28"}]}
    dc = run(earn=earn, vt_prof={"SPCX.US": {"earnings_dates": ["2026-11-03"]}})
    s = next(e for e in dc["earnings"] if e["ticker"] == "SPCX.US")
    assert s["status"] == "ok" and s["verified"] == "2026-11-03"  # porteføljen + Yahoo mod EODHD (1 dag)
    vt, eod = sources(earn={})
    nv = next(e for e in dtj.run(P, WEEK, REF, vt=vt, eod=eod)["earnings"] if e["ticker"] == "NOVO-B.CO")
    assert nv["verified"] == "2026-11-04" and nv["agree"] == 2 and nv["eodhd"] is None  # porteføljen + Yahoo
    vt, eod = sources(earn={}, vt_prof={})
    dc = dtj.run(dict(P, next_reports={}, watchlist=[]), WEEK, REF, vt=vt, eod=eod)
    assert all(e["status"] == "warn" and e["verified"] is None for e in dc["earnings"])


def test_currency_mismatch_fails_and_names_compared():
    eod_prof = {"GOOGL.US": {"name": "NVIDIA Corp", "currency": "EUR"}}
    dc = run(eod_prof=eod_prof)
    f = next(x for x in dc["fundamentals"] if x["ticker"] == "GOOGL.US")
    assert f["status"] == "fail" and f["name_match"] is False
    assert any("valuta EUR" in n for n in f["notes"]) and dc["status"] == "FEJL"
    assert dtj.names_match("Space Exploration Technologies Corp.", "Space Exploration Technologies Corp. Class A Common Stock")
    assert dtj.names_match("Novo Nordisk A/S", "NOVO NORDISK B") is True


def test_lockup_and_corporate_actions_are_events_not_errors():
    dc = run(actions={"GOOGL.US": [{"kind": "dividend", "date": "2026-09-08", "value": 0.21, "currency": "USD"}]})
    assert dc["status"] == "PASS"
    texts = [e["text"] for e in dc["events"]]
    assert any("lock-up udløber ca. 09/12/2026" in t for t in texts)
    assert any("udbytte 0,21 USD" in t for t in texts)


def test_without_eodhd_key_is_a_warning_with_clear_reason():
    dc = run(eod_available=False)
    assert dc["status"] == "ADVARSEL" and dc["issues"][0].startswith("EODHD-nøglen mangler")
    assert dc["portfolio"]["chosen"] == "vibe_trading" and dc["portfolio"]["total_sek"] > 0


def test_brief_and_cio_instruction():
    dc = run()
    b = dtj.agent_brief(dc)
    assert b.startswith("## Vibetrad datatjek 15/10/2026: PASS")
    assert "| SpaceX (SPCX.US) |" in b and "(maks 10 %)" in b and "03/11/2026 efter lukning (3 kilder)" in b and "| Novo Nordisk A/S (NOVO-B.CO) |" in b
    assert "USD/SEK" in b and "lock-up" in b and len(b) <= 5600
    assert "**Ugen i SEK inkl. valuta:**" in b and "**Resultat siden køb (kun aktier):**" in b
    assert "| Uge (lokal / SEK) |" in b and "**Indskud:**" not in b  # intet indskud oplyst i P
    assert "Datakvalitet" in dtj.cio_instruction(dc) and dtj.agent_brief(None) == ""
    eod_px = {c: dict(series(c, 0.05 if c == "GOOGL.US" else 0), source="eodhd") for c in BASE}
    bad = run(eod_px=eod_px)
    assert "FEJL GOOGL.US" in dtj.cio_instruction(bad) and "Afvigelser:" in dtj.agent_brief(bad)


def test_helpers():
    assert dtj._date("04/11/2026") == "2026-11-04" and dtj._date({"raw": 1793664000}) == "2026-11-03"
    assert dtj.fx_pairs([{"ticker": "A.US"}, {"ticker": "B.CO"}, {"ticker": "C.ST"}]) == ["DKKSEK=X", "USDSEK=X"]
    assert dtj.worst(["ok", "info", "warn"]) == "warn" and dtj.worst(["ok", "fail", "warn"]) == "fail"
    assert dtj.portfolio_dates({"watchlist": [{"ticker": "x.co", "note": "Regnskab: 4.11.2026"}]}) == {"X.CO": "2026-11-04"}


def test_real_source_classes_degrade_without_dependencies(monkeypatch):
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    monkeypatch.delenv("EODHD_API_TOKEN", raising=False)
    e = dtj.EodhdSource()
    assert e.available is False
    v = dtj.VibeTradingSource()
    v.available = False  # ingen netværk i testen, uanset om vibe-trading-ai er installeret
    dc = dtj.run(P, WEEK, REF, vt=v, eod=e)
    assert dc["status"] == "FEJL" and dc["portfolio"]["total_sek"] is None
    assert any("EODHD-nøglen mangler" in i for i in dc["issues"]) and any("Vibe-Trading var ikke" in i for i in dc["issues"])
    assert not any("None" in i for i in dc["issues"])  # manglende værdier vises som "–", ikke Pythons None


def test_no_rows_up_to_today_is_a_failure_not_a_crash():
    future = {c: {"rows": [("2026-10-20", 1.0)], "source": "yahoo"} for c in BASE}
    dc = run(vt_px=future, eod_px=future)
    assert dc["status"] == "FEJL" and all(i["status"] == "fail" for i in dc["instruments"])


def test_review_edge_cases_dates_qty_currency_and_windows():
    # ugyldige eller amerikanske datoer vælter ikke tjekket
    assert dtj._date("31/11/2026") is None and dtj._date("10/28/2026") is None and dtj._date("2026-02-30") is None
    p = dict(P, next_reports={"GOOGL.US": "10/28/2026"}, watchlist=[{"ticker": "NOVO-B.CO", "note": "regnskab 31/11/2026"}])
    vt, eod = sources()
    assert dtj.run(p, WEEK, REF, vt=vt, eod=eod)["status"] in ("PASS", "ADVARSEL")
    # manglende antal → ingen vægte, men en klar advarsel
    p2 = rw.load_portfolio(json.dumps({"holdings": [{"ticker": "SPCX.US", "name": "S", "qty": 13},
                                                     {"ticker": "GOOGL.US", "name": "G", "qty": None}], "cash_sek": 0}))
    vt, eod = sources()
    dc = dtj.run(p2, WEEK, REF, vt=vt, eod=eod)
    assert dc["portfolio"]["total_sek"] is None and dc["portfolio"]["missing_qty"] == ["GOOGL.US"]
    assert all(r["weight_pct"] is None for r in dc["portfolio"]["rows"]) and any("antal mangler" in i for i in dc["issues"])
    # bare tickers får .US
    assert dtj.instruments({"holdings": [{"ticker": "nvda"}]})[0]["ticker"] == "NVDA.US"
    # Yahoo-vindue omkring facit tæller som enigt
    vt, eod = sources(vt_prof={"GOOGL.US": {"earnings_dates": ["2026-10-26", "2026-10-30"], "earnings_estimate": True}})
    g = next(e for e in dtj.run(P, WEEK, REF, vt=vt, eod=eod)["earnings"] if e["ticker"] == "GOOGL.US")
    assert g["status"] == "ok" and g["agree"] == 3 and g["vibe_trading_to"] == "2026-10-30"
    vt, eod = sources(vt_prof={"GOOGL.US": {"earnings_dates": ["2026-11-02", "2026-11-06"]}})
    g = next(e for e in dtj.run(P, WEEK, REF, vt=vt, eod=eod)["earnings"] if e["ticker"] == "GOOGL.US")
    assert g["status"] == "warn" and "02/11/2026–06/11/2026" in g["notes"][0]


def test_unfinished_intraday_bar_is_ignored():
    vt_px = {c: series(c) for c in BASE}
    for c in vt_px:  # Yahoo leverer dagens ufærdige bar (15/10), EODHD ikke
        vt_px[c] = dict(vt_px[c], rows=vt_px[c]["rows"] + [("2026-10-15", vt_px[c]["rows"][-1][1] * 1.04)])
    dc = run(vt_px=vt_px)
    assert dc["status"] == "PASS", dc["issues"]
    assert {i["vt"]["date"] for i in dc["instruments"]} == {"2026-10-14"}


def test_london_pence_are_converted():
    days = DAYS
    vt_px = {"VOD.L": {"rows": [(d, 0.95) for d in days], "source": "yahoo", "currency": "GBP"},
             "GBPSEK=X": {"rows": [(d, 12.5) for d in days], "source": "yahoo"}}
    eod_px = {"VOD.L": {"rows": [(d, 95.0) for d in days], "source": "eodhd"},
              "GBPSEK=X": {"rows": [(d, 12.5) for d in days], "source": "eodhd"}}
    p = rw.load_portfolio(json.dumps({"holdings": [{"ticker": "VOD.L", "name": "Vodafone", "qty": 1000}], "cash_sek": 0}))
    dc = dtj.run(p, WEEK, REF, vt=Fake("v", vt_px), eod=Fake("e", eod_px))
    v = next(i for i in dc["instruments"] if i["ticker"] == "VOD.L")
    assert v["status"] == "ok" and any("pence" in n for n in v["notes"])
    assert dc["portfolio"]["value_sek"]["eodhd"] == pytest.approx(1000 * 0.95 * 12.5)


def test_yahoo_fx_dated_one_day_early_is_aligned_and_value_uses_common_fx():
    # Set live i codespacet 15/10/2026: Yahoo (via Vibe-Trading) daterer DKK/SEK-baren for 14/10 som 13/10
    eod_px = {c: dict(series(c), source="eodhd") for c in BASE}
    vt_px = {c: series(c) for c in BASE}
    for pair in ("USDSEK=X", "DKKSEK=X"):
        rows = eod_px[pair]["rows"]
        vt_px[pair] = {"rows": [((dt.date.fromisoformat(d) - dt.timedelta(days=1)).isoformat(), c) for d, c in rows],
                       "source": "yahoo"}
    dc = run(vt_px=vt_px, eod_px=eod_px)
    fx = {f["pair"]: f for f in dc["fx"]}
    assert fx["USDSEK"]["status"] == "ok" and "en dag for tidligt" in fx["USDSEK"]["info"]
    assert fx["USDSEK"]["vt"]["date"] == fx["USDSEK"]["eodhd"]["date"] == "2026-10-14"
    assert dc["status"] == "PASS", dc["issues"]
    # forskellig valutakurs i kilderne må ikke give falsk afvigelse i porteføljeværdien
    vt_px2 = {c: series(c) for c in BASE}
    vt_px2["USDSEK=X"] = series("USDSEK=X", bump=0.006)
    dc2 = run(vt_px=vt_px2)
    assert dc2["portfolio"]["diff_pct"] == 0 and dc2["portfolio"]["status"] == "ok"
    assert next(f for f in dc2["fx"] if f["pair"] == "USDSEK")["status"] == "warn"


# --- Kort kurshistorik, gammel portefølje og vægtpolitik (red team 16/10/2026) ---------------
class FakeWithHistory(Fake):
    def __init__(self, *a, history=None, **k):
        super().__init__(*a, **k)
        self._hist = history or {}

    def history(self, code, start, end):
        return [r for r in self._hist.get(code, []) if start <= r[0] <= end]


def test_short_history_uses_real_rows_and_reaches_the_brief():
    vt, eod = sources()
    # 30 handelsdage fra børsnoteringen: op til 200, ned til 100 (−50 %), tilbage til 150
    days = [(dt.date(2026, 6, 12) + dt.timedelta(days=k)).isoformat() for k in range(60)]
    days = [d for d in days if dt.date.fromisoformat(d).weekday() < 5][:30]
    closes = [150 + 5 * k for k in range(11)] + [200 - 10 * k for k in range(1, 11)] + [100 + 5 * k for k in range(1, 10)]
    eod = FakeWithHistory("EODHD", eod._px, eod._prof, eod._earn, history={"SPCX.US": list(zip(days, closes))})
    dc = dtj.run(P, WEEK, REF, vt=vt, eod=eod)
    (s,) = dc["short_history"]
    assert s == {"ticker": "SPCX.US", "name": "SpaceX", "ipo_date": "2026-06-12", "trading_days": 30,
                 "approx": False, "max_drawdown_pct": -50.0}
    assert dc["status"] == "PASS"  # kort historik er information, ikke en datafejl
    brief = dtj.agent_brief(dc)
    assert "**Kort historik:** SpaceX (SPCX.US) har kun 30 handelsdage (børsnoteret 12/06/2026), største fald fra top til bund -50 %" in brief
    assert "og for porteføljen (vægt" in brief and "statistisk usikre" in brief
    assert all(x["ticker"] != "GOOGL.US" for x in dc["short_history"])  # ingen IPO-dato = ingen markering


def test_short_history_estimated_when_prices_unavailable():
    dc = run()  # den simple falske EODHD-kilde har ingen history()
    (s,) = dc["short_history"]
    assert s["approx"] is True and s["max_drawdown_pct"] is None and s["trading_days"] == dtj._weekdays("2026-06-12", WEEK["to"])
    assert "har kun ca. " in dtj.agent_brief(dc) and "største fald" not in dtj.agent_brief(dc)
    assert dtj._weekdays("2026-10-12", "2026-10-18") == 5 and dtj._weekdays("2026-10-18", "2026-10-12") == 0


@pytest.mark.parametrize("as_of, warn", [("09/10/2026", False), ("08/10/2026", False), ("07/10/2026", True), ("2026-10-01", True)])
def test_stale_portfolio_warns_after_seven_days(as_of, warn):
    vt, eod = sources()
    dc = dtj.run(dict(P, as_of=as_of), WEEK, REF, vt=vt, eod=eod)
    hit = [i for i in dc["issues"] if i.startswith("ADVARSEL portefølje: VT_PORTFOLIO er opgjort")]
    assert bool(hit) is warn and (dc["status"] == "ADVARSEL") is warn
    if warn:
        assert "dage gammel). Opdatér beholdningen i Vibetrad og kopiér VT_PORTFOLIO til GitHub." in hit[0]
        assert hit[0] in dtj.agent_brief(dc)  # agenterne får samme besked


def test_stale_portfolio_threshold_can_be_changed(monkeypatch):
    vt, eod = sources()
    monkeypatch.setenv("VT_PORTFOLIO_MAX_AGE_DAYS", "14")
    dc = dtj.run(dict(P, as_of="01/10/2026"), WEEK, REF, vt=vt, eod=eod)
    assert dc["portfolio_age_days"] == 14 and dc["status"] == "PASS"
    vt, eod = sources()
    dc = dtj.run(dict(P), WEEK, REF, vt=vt, eod=eod)  # ingen dato: kan ikke vurderes, ingen advarsel
    assert dc["portfolio_age_days"] is None and dc["status"] == "PASS"


def test_transition_policy_changes_the_weight_label():
    p = dict(P, weight_policy="target")
    vt, eod = sources()
    brief = dtj.agent_brief(dtj.run(p, WEEK, REF, vt=vt, eod=eod))
    assert "(langsigtet mål 10 %)" in brief and "(maks 10 %)" not in brief
    vt, eod = sources()
    assert "(maks 10 %)" in dtj.agent_brief(dtj.run(P, WEEK, REF, vt=vt, eod=eod))


def _fx_rows(values):
    return {"rows": list(zip(DAYS, values)), "source": "yahoo", "currency": None}


def test_week_in_sek_includes_currency_and_uses_friday_fx():
    usd = [10.0] * 6 + [9.95, 9.92, 9.90]  # 09/10 = 10,00 → 14/10 = 9,90 (−1 %)
    px = {c: series(c) for c in BASE}
    px["USDSEK=X"] = _fx_rows(usd)
    vt, eod = sources(vt_px=px, eod_px={k: dict(v, source="eodhd") for k, v in px.items()})
    dc = dtj.run(P, WEEK, REF, vt=vt, eod=eod)
    ws, rows = dc["portfolio"]["week_sek"], {r["ticker"]: r for r in dc["portfolio"]["rows"]}
    sp, gg = series("SPCX.US")["rows"], series("GOOGL.US")["rows"]
    v0 = 13 * sp[5][1] * 10.0 + 3 * gg[5][1] * 10.0 + 2615  # fredag 09/10 med fredagens valutakurs
    v1 = 13 * sp[-1][1] * 9.9 + 3 * gg[-1][1] * 9.9 + 2615
    assert ws["start_sek"] == pytest.approx(v0, abs=0.05) and ws["end_sek"] == pytest.approx(v1, abs=0.05)
    assert ws["pct"] == pytest.approx((v1 / v0 - 1) * 100, abs=0.01) and ws["fx_pp"] < -0.9
    assert ws["fx"]["USD"] == {"from_date": "2026-10-09", "from": 10.0, "to_date": "2026-10-14", "to": 9.9}
    local = (sp[-1][1] / sp[5][1] - 1) * 100
    assert rows["SPCX.US"]["week_pct_local"] == pytest.approx(local, abs=0.01)
    assert rows["SPCX.US"]["week_pct_sek"] == pytest.approx(((1 + local / 100) * 0.99 - 1) * 100, abs=0.01)
    b = dtj.agent_brief(dc)
    assert "**Ugen i SEK inkl. valuta:** " in b and "USD/SEK 10,0000 (09/10/2026) → 9,9000 (14/10/2026)" in b


def test_profit_and_loss_and_deposit_path():
    p = dict(P, monthly_deposit_sek=15000.0)
    vt, eod = sources()
    dc = dtj.run(p, WEEK, REF, vt=vt, eod=eod)
    pf = dc["portfolio"]
    rows = {r["ticker"]: r for r in pf["rows"]}
    tv = rows["SPCX.US"]["value_sek"] + rows["GOOGL.US"]["value_sek"]
    assert pf["pl"]["cost_sek"] == 21480 + 9315 and pf["pl"]["pl_sek"] == pytest.approx(tv - 30795, abs=0.05)
    assert pf["pl"]["cash_sek"] == 2615  # kontanten indgår ikke i resultatet
    path = {(d["ticker"], d["level_pct"]): d["months"] for d in pf["deposit_path"]}
    total, sp = pf["total_sek"], rows["SPCX.US"]["value_sek"]
    assert set(path) == {("SPCX.US", 50.0), ("SPCX.US", 25.0), ("SPCX.US", 10.0), ("GOOGL.US", 10.0)}
    assert path[("SPCX.US", 10.0)] == pytest.approx((sp / 0.10 - total) / 15000, abs=0.05)
    b = dtj.agent_brief(dc)
    assert "**Indskud:** 15 000 SEK om måneden" in b and "SpaceX under 50 % om" in b and "Alphabet A under 10 % om" in b


@pytest.mark.parametrize("bump,status", [(0.004, "ok"), (0.008, "warn")])
def test_fx_noise_below_half_percent_and_explained_notes(bump, status):
    eod_px = {c: dict(series(c), source="eodhd") for c in BASE}
    eod_px["USDSEK=X"] = dict(series("USDSEK=X", bump), source="eodhd")
    dc = run(eod_px=eod_px)
    usd = next(f for f in dc["fx"] if f["pair"] == "USDSEK")
    assert usd["status"] == status
    if status == "warn":
        note = usd["notes"][0]
        assert "(Vibe-Trading 10," in note and ", EODHD 10," in note
        assert "kilderne lukker valutadøgnet på forskellige tidspunkter" in note and "DEXSDUS" in note
        assert any(s.startswith("ADVARSEL USDSEK") for s in dc["issues"])


def test_market_cap_is_whole_company_and_full_brief_fits():
    """Hele porteføljen (4 aktier, overvågning, indskud, kort historik, begivenheder og afvigelser) skal kunne stå i
    facit uden at 'Afvigelser' og markedsværdien skæres væk, og markedsværdien skal være hele selskabets."""
    base = dict(BASE, **{"NVDA.US": 180.0, "AVGO.US": 340.0})
    full = rw.load_portfolio("""{"account":"ISK Nordnet","currency":"SEK","monthly_deposit_sek":15000,
      "courtage_class":"Medium","weight_policy":"target","holdings":[
      {"ticker":"SPCX.US","name":"SpaceX","qty":13,"cost_sek":21480,"max_weight":10},
      {"ticker":"GOOGL.US","name":"Alphabet A","qty":3,"cost_sek":9315,"max_weight":10},
      {"ticker":"NVDA.US","name":"NVIDIA","qty":4,"cost_sek":6120,"max_weight":10},
      {"ticker":"AVGO.US","name":"Broadcom","qty":2,"cost_sek":5230,"max_weight":10}],
      "cash_sek":2615,"watchlist":[{"ticker":"NOVO-B.CO","note":"aaben limitordre 5 stk, regnskab 04/11/2026"}],
      "next_reports":{"GOOGL.US":"2026-10-28","SPCX.US":"2026-11-03","NVDA.US":"2026-11-18","AVGO.US":"2026-12-09"}}""")
    px = {c: {"rows": [(d, round(b * (1 + 0.002 * i), 6)) for i, d in enumerate(DAYS)], "source": "yahoo", "currency": None}
          for c, b in base.items()}
    prof = {"GOOGL.US": {"name": "Alphabet Inc Class A", "currency": "USD", "market_cap": 4.271e12},
            "NVDA.US": {"name": "NVIDIA Corporation", "currency": "USD"},
            "AVGO.US": {"name": "Broadcom Inc", "currency": "USD", "market_cap": 1.62e12}}
    vt_prof = {"NVDA.US": {"name": "NVIDIA Corporation", "currency": "USD", "market_cap": 4.4e12},
               "GOOGL.US": {"name": "Alphabet Inc.", "currency": "USD", "market_cap": 4.26e12}}
    vt = Fake("Vibe-Trading", px, vt_prof)
    eod = Fake("EODHD", {k: dict(v, source="eodhd") for k, v in px.items()}, prof)
    dc = dtj.run(full, WEEK, REF, vt=vt, eod=eod)
    line = next(x for x in dtj.agent_brief(dc, limit=99999).splitlines() if x.startswith("**Markedsværdi"))
    assert "GOOGL.US 4 271 mia. USD (EODHD)" in line  # hele selskabet fra EODHD, ikke kun A-aktierne
    assert "NVDA.US 4 400 mia. USD (Vibe-Trading)" in line  # reserve, når EODHD mangler tallet
    assert "aldrig markedsværdien ud fra én aktieklasses" in line
    # Værst tænkelige længde: seks lange begivenheder og seks lange afvigelser oveni
    dc["events"] = [{"ticker": "SPCX.US", "kind": "dividend", "date": "2026-12-01", "text": "Begivenhed " + "x" * 140}] * 6
    dc["issues"] = ["ADVARSEL " + "y" * 180] * 6
    dc["short_history"] = [{"ticker": "SPCX.US", "name": "SpaceX", "trading_days": 87, "ipo_date": "2026-06-12",
                            "max_drawdown_pct": -49.0}]
    b = dtj.agent_brief(dc)
    assert len(b) <= 5600 and "[… afkortet]" not in b
    for must in ("**Ugen i SEK inkl. valuta:**", "**Resultat siden køb (kun aktier):**", "**Indskud:** 15 000 SEK",
                 "**Markedsværdi", "**Kort historik:**", "Afvigelser: ADVARSEL", "| Broadcom (AVGO.US) |"):
        assert must in b, must
    cut = dtj.agent_brief(dc, limit=600)
    assert cut.endswith("[… afkortet]") and "\n[… afkortet]" in cut and len(cut) <= 600 + 20
