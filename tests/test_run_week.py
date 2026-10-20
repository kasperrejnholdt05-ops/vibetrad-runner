"""Tests for runner/run_week.py – kør med:  python -m pytest -q"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
import datatjek as dtj  # noqa: E402
import run_week as rw  # noqa: E402

UTC = dt.timezone.utc
PASS = "korrekt-hest-batteri-haefteklamme"
PORTFOLIO = {
    "account": "ISK Nordnet", "as_of": "14/10/2026",
    "holdings": [
        {"ticker": "ZZTOP.US", "name": "Hemmelig Raket", "qty": 13, "cost_sek": 21480, "max_weight": 10},
        {"ticker": "QQQX.US", "name": "Hemmelig Soeg", "qty": 3, "cost_sek": 9315},
    ],
    "cash_sek": 2615, "cash_note": "reserveret til ordre",
    "next_reports": {"QQQX.US": "2026-10-28"},
}


def sto(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=rw.STO)


# --- uge og tidsvagt -------------------------------------------------------
def test_week_window_saturday_is_full_current_week():
    w = rw.week_window(sto("2026-10-17T08:00"))
    assert w == {"id": "2026-W42", "from": "2026-10-12", "to": "2026-10-16", "full": True,
                 "label": "Uge 42 · 12/10/2026–16/10/2026"}


@pytest.mark.parametrize("when,week,to,full", [
    ("2026-10-15T11:34", "2026-W42", "2026-10-14", False),  # torsdag formiddag: til og med onsdagens lukning
    ("2026-10-15T22:30", "2026-W42", "2026-10-15", False),  # torsdag efter NYSE-lukning
    ("2026-10-16T15:00", "2026-W42", "2026-10-15", False),  # fredag før lukning
    ("2026-10-18T20:00", "2026-W42", "2026-10-16", True),   # søndag
    ("2026-10-12T10:00", "2026-W41", "2026-10-09", True),   # mandag før første lukning -> forrige uge
    ("2026-10-12T23:00", "2026-W42", "2026-10-12", False),  # mandag efter lukning
])
def test_week_window_follows_calendar(when, week, to, full):
    w = rw.week_window(sto(when))
    assert (w["id"], w["to"], w["full"]) == (week, to, full)
    assert ("ugen til dato" in w["label"]) is (not full)


@pytest.mark.parametrize("utc,expect", [
    ("2026-10-17T06:00", True),    # sommertid: 06 UTC = 08 Stockholm
    ("2026-10-31T06:00", False),   # vintertid: 06 UTC = 07 Stockholm
    ("2026-10-31T07:00", True),    # vintertid: 07 UTC = 08 Stockholm
    ("2026-10-17T05:59", False),
    ("2026-10-16T06:00", False),   # fredag
])
def test_should_run_handles_dst(utc, expect, monkeypatch):
    monkeypatch.setenv("VT_SCHEDULE", "on")
    ref = dt.datetime.fromisoformat(utc).replace(tzinfo=UTC).astimezone(rw.STO)
    assert rw.should_run(ref, None, "schedule", False)[0] is expect


def test_schedule_is_off_by_default_but_button_always_runs(monkeypatch):
    monkeypatch.delenv("VT_SCHEDULE", raising=False)
    ok, why = rw.should_run(sto("2026-10-17T08:05"), None, "schedule", False)
    assert ok is False and "VT_SCHEDULE" in why
    assert rw.should_run(sto("2026-10-14T10:00"), None, "workflow_dispatch", False)[0] is True


def test_should_run_skips_done_week_but_retries_failed(monkeypatch):
    monkeypatch.setenv("VT_SCHEDULE", "on")
    ref = sto("2026-10-17T09:30")
    full = {"week_id": "2026-W42", "week_to": "2026-10-16"}
    assert rw.should_run(ref, {**full, "status": "ok"}, "schedule", False)[0] is False
    assert rw.should_run(ref, {**full, "status": "partial"}, "schedule", False)[0] is False
    # en manuel kørsel midt i ugen (til og med onsdag) må ikke blokere lørdagens fulde uge
    assert rw.should_run(ref, {**full, "status": "ok", "week_to": "2026-10-14"}, "schedule", False)[0] is True
    assert rw.should_run(ref, {**full, "status": "ok", "dry_run": True}, "schedule", False)[0] is True
    assert rw.should_run(ref, {"week_id": "2026-W42", "status": "failed"}, "schedule", False)[0] is True
    assert rw.should_run(ref, {"week_id": "2026-W41", "status": "ok"}, "schedule", False)[0] is True
    assert rw.should_run(sto("2026-10-14T10:00"), None, "workflow_dispatch", False)[0] is True


# --- portefølje ------------------------------------------------------------
def test_load_portfolio_validates_without_leaking():
    with pytest.raises(rw.PortfolioError) as e:
        rw.load_portfolio('{"holdings": [{"ticker": "ZZTOP.US", "qty": "sytten"}]')
    assert "ZZTOP" not in str(e.value)
    with pytest.raises(rw.PortfolioError):
        rw.load_portfolio("")
    with pytest.raises(rw.PortfolioError):
        rw.load_portfolio('{"holdings": []}')
    with pytest.raises(rw.PortfolioError, match="qty"):
        rw.load_portfolio('{"holdings": [{"ticker": "A.US", "qty": "x"}]}')


def test_portfolio_text_and_weights():
    p = rw.load_portfolio(json.dumps(PORTFOLIO))
    txt = rw.portfolio_text(p)
    assert "Hemmelig Raket (ZZTOP.US), 13 stk, anskaffelse 21 480 SEK, maks. vægt 10 %" in txt
    assert "Kontant: 2 615 SEK (reserveret til ordre)" in txt
    w = {x["ticker"]: x for x in rw.cost_weights(p)}
    assert w["ZZTOP.US"]["over_max"] is True and w["ZZTOP.US"]["weight_cost_pct"] == pytest.approx(64.3, abs=0.1)


def test_journal_and_rules_reach_the_cio_but_junk_does_not():
    raw = dict(PORTFOLIO, rules="  Maks. 10 % pr. aktie.\n Kill → sælg inden 5 dage. ", journal=[
        {"ticker": "zztop.us", "date": "2026-10-06", "action": "Køb", "thesis": "Rumfart  vokser 30 %/år", "kill": "Starship fejler to gange",
         "review": "2027-01-15", "status": "udført", "amount_sek": 21480},
        {"ticker": "", "thesis": "ingen ticker"}, "ikke en post",
        {"ticker": "QQQ.US", "date": "snart", "action": "Hold", "thesis": "x" * 500}] + [{"ticker": f"T{i}.US"} for i in range(60)])
    p = rw.load_portfolio(json.dumps(raw))
    assert p["rules"] == "Maks. 10 % pr. aktie. Kill → sælg inden 5 dage." and len(p["journal"]) == 40
    j = p["journal"][0]
    assert j == {"ticker": "ZZTOP.US", "date": "2026-10-06", "action": "Køb", "thesis": "Rumfart vokser 30 %/år",
                 "kill": "Starship fejler to gange", "review": "2027-01-15", "status": "udført", "amount_sek": 21480.0}
    assert p["journal"][1]["date"] is None and len(p["journal"][1]["thesis"]) == 300
    txt = rw.portfolio_text(p)
    assert "Investorens egne regler (Vibetrad → Plan), som alle forslag skal respektere: Maks. 10 % pr. aktie." in txt
    assert "- 06/10/2026 Køb ZZTOP.US · udført · 21 480 SEK · tese: Rumfart vokser 30 %/år · tager fejl hvis: Starship fejler to gange · opfølgning 15/01/2027" in txt
    assert "Opfølgning på journalen" in rw.journal_instruction(p) and rw.journal_instruction(rw.load_portfolio(json.dumps(PORTFOLIO))) == ""
    assert rw.load_portfolio(json.dumps(dict(PORTFOLIO, rules=42, journal="nej")))["journal"] == []


CASH_ONLY = {"account": "ISK Nordnet", "currency": "SEK", "holdings": [], "cash_sek": 41234.56, "monthly_deposit_sek": 15000,
             "mandate": {"horizon_years": 5, "max_drawdown_pct": 50, "universe": "frit", "single_stock_cap_pct": 20},
             "rules": "Loft 20 % pr. aktie."}


def test_cash_only_portfolio_with_mandate():
    with pytest.raises(rw.PortfolioError, match="kontant"):
        rw.load_portfolio('{"holdings": []}')
    p = rw.load_portfolio(json.dumps(CASH_ONLY))
    assert p["holdings"] == [] and p["cash_sek"] == 41234.56
    assert p["mandate"] == {"horizon_years": 5, "max_drawdown_pct": 50, "universe": "frit", "single_stock_cap_pct": 20, "objective": None}
    txt = rw.portfolio_text(p)
    assert "Ingen værdipapirer – alt står kontant" in txt and "Mandat: horisont 5 år" in txt and "op til 50 %" in txt
    assert "frit: fonde, ETF'er og enkeltaktier" in txt and "loft 20 % pr. enkeltaktie" in txt
    # grænser og ukendt univers
    q = rw.load_portfolio(json.dumps(dict(CASH_ONLY, mandate={"horizon_years": 99, "max_drawdown_pct": 1, "universe": "alt", "single_stock_cap_pct": 500, "objective": " x " * 200})))
    assert q["mandate"]["horizon_years"] == 40 and q["mandate"]["max_drawdown_pct"] == 5 and q["mandate"]["universe"] == "kerne+satellit"
    assert q["mandate"]["single_stock_cap_pct"] == 100 and len(q["mandate"]["objective"]) == 300
    assert rw.load_portfolio(json.dumps(dict(CASH_ONLY, mandate="nej")))["mandate"] is None
    assert rw.cost_weights(p) == [] and rw.free_cash(p) == pytest.approx(41234.56)


def test_strategy_plan_seven_teams_and_mandate_in_goals(monkeypatch):
    monkeypatch.delenv("VT_SKIP_PRESETS", raising=False)
    p = rw.load_portfolio(json.dumps(CASH_ONLY))
    ref = rw.now_sto("2026-10-21T12:00")
    plan = rw.build_plan(p, ref, rw.week_window(ref), max_swarms=7, plan_kind="fuld")  # uden beholdning → strategi
    assert [s["preset"] for s in plan] == ["macro_strategy_forum", "sector_rotation_team", "etf_allocation_desk", "fund_selection_panel",
                                           "equity_research_team", "risk_committee", "portfolio_review_board"]
    assert plan[-1]["cio"] and "Byg én samlet investeringsstrategi fra bunden" in plan[-1]["vars"]["goal"]
    # Målportefølje-tabellen er kun CIO'ens (system-prompt) og står øverst; analytikerne får hver deres opgave
    assert "## Målportefølje" not in plan[-1]["vars"]["goal"]
    extra = plan[-1]["system_extra"]
    cio = extra["chief_investment_officer"]
    assert "## Målportefølje" in cio and rw.TARGET_TABLE_HEAD in cio and "Køb fra" in rw.TARGET_TABLE_HEAD
    assert cio.index("## Resumé") < cio.index("## Målportefølje") < cio.index("3. Derefter resten")
    assert "hele stk" in cio and "egen målte fald" in cio and "Norden" in cio
    assert set(extra) == {"chief_investment_officer", "attribution_analyst", "risk_inspector", "execution_analyst"}
    for role in ("attribution_analyst", "risk_inspector", "execution_analyst"):
        assert "lav ikke din egen tabel 'Målportefølje'" in extra[role] and "{task}" not in extra[role]
    assert "enkeltaktier falder ofte mere end indekset" in plan[5]["vars"]["goal"]
    for st in plan[:-1]:
        v = " ".join(str(x) for x in st["vars"].values())
        assert "41 235 SEK kontant nu" in v and "15 000 SEK" in v and "horisont 5 år" in v and "op til 50 %" in v, st["preset"]
    assert "5–8 vækstaktier" in plan[4]["vars"]["goal"] and "hver højst 20 %" in plan[4]["vars"]["goal"]
    assert all("gælder mandatet" in " ".join(str(x) for x in st["vars"].values()) for st in plan[:5])
    assert "ACWI.US" in plan[5]["vars"]["goal"] and "tålte fald på 50 %" in plan[5]["vars"]["goal"]
    # kun fonde: idéjagten bliver en temasøgning; strategi kan vælges eksplicit med beholdning
    q = rw.load_portfolio(json.dumps(dict(CASH_ONLY, mandate=dict(CASH_ONLY["mandate"], universe="kun fonde"))))
    plan2 = rw.build_plan(q, ref, rw.week_window(ref), max_swarms=7, plan_kind="strategi")
    assert "tillader ikke enkeltaktier" in plan2[4]["vars"]["goal"]
    plan3 = rw.build_plan(rw.load_portfolio(json.dumps(PORTFOLIO)), ref, rw.week_window(ref), max_swarms=7, plan_kind="strategi")
    assert [s["preset"] for s in plan3][0] == "macro_strategy_forum" and len(plan3) == 7
    # max_swarms under 7 beholder CIO'en
    assert [s["preset"] for s in rw.build_plan(p, ref, rw.week_window(ref), max_swarms=4, plan_kind="strategi")][-1] == "portfolio_review_board"
    # presets findes i Vibe-Trading, og alle krævede variabler er sat
    try:
        from src.swarm.presets import load_preset
    except Exception:
        pytest.skip("vibe-trading-ai ikke installeret")
    for st in plan:
        spec = load_preset(st["preset"])
        for var in spec.get("variables", []):
            if var.get("required"):
                assert str(st["vars"].get(var["name"], "")).strip(), (st["preset"], var["name"])


def test_dry_run_strategy_from_cash(tmp_path):
    p = run_main(tmp_path, "--event", "workflow_dispatch", "--now", "2026-10-21T12:00", VT_PORTFOLIO=json.dumps(CASH_ONLY), VT_PLAN="strategi")
    assert p.returncode == 0, p.stderr
    log = p.stdout + p.stderr
    assert "Plan: Makrostrategi (5 år), Sektorer og temaer, ETF-allokering, Fondsvalg (ISK-kerne), Idéjagt: vækstaktier, Risikoudvalg, Porteføljegennemgang (CIO)" in log
    assert "Idéjagt uden for tech" not in log
    assert "Resultat: ok · 7/7 swarms" in log and "41 235" not in log and "41234" not in log
    data = rw.decrypt(json.loads((tmp_path / "results" / "latest.enc.json").read_text()), PASS)
    assert data["totals"]["plan_kind"] == "strategi" and data["portfolio"]["holdings"] == [] and len(data["runs"]) == 7
    assert data["runs"][4]["label"] == "Idéjagt: vækstaktier" and data["runs"][0]["label"] == "Makrostrategi (5 år)"


# --- plan ------------------------------------------------------------------
def presets(plan):
    return [s["preset"] for s in plan]


def test_plan_full_week_with_watchlist():
    p = rw.load_portfolio(json.dumps({**PORTFOLIO, "watchlist": [{"ticker": "NOVA-B.CO", "note": "limit"}]}))
    ref = sto("2026-10-17T08:00")  # 3. lørdag (fondsvalg), uge 42 -> rotation 0 (investeringskomité)
    reps = rw.upcoming_reports(p, ref, lookup=None)
    plan = rw.build_plan(p, ref, rw.week_window(ref), reports=reps)
    assert presets(plan) == ["risk_committee", "earnings_research_desk", "fund_selection_panel",
                             "investment_committee", "portfolio_review_board"]
    assert plan[-1].get("cio") and "12/10/2026–16/10/2026" in plan[-1]["vars"]["review_period"]
    assert "Uge 42" in plan[-1]["vars"]["review_period"]
    assert plan[3]["vars"]["target"] == "NOVA-B.CO" and plan[3]["vars"]["market"].startswith("Danmark")
    assert "Hemmelig Raket" in plan[0]["vars"]["goal"] and "portfolio_risk_xray" in plan[0]["vars"]["goal"]


def test_plan_first_saturday_odd_week_and_core():
    p = rw.load_portfolio(json.dumps(PORTFOLIO))
    ref = sto("2026-11-07T08:00")  # 1. lørdag (ETF), uge 45 -> rotation 1 af 2 = Visser-panelet (aktier i beholdningen)
    plan = rw.build_plan(p, ref, rw.week_window(ref), reports=[])
    assert presets(plan) == ["risk_committee", "etf_allocation_desk", "visser_panel", "portfolio_review_board"]
    assert "Hemmelig Raket (ZZTOP.US, beholdning" in plan[2]["vars"]["names"] and "ingen daghandel" in plan[2]["vars"]["focus"]
    ref44 = sto("2026-10-31T08:00")  # uge 44 -> rotation 0 uden overvågningsliste = fundamental analyse
    p44 = rw.build_plan(p, ref44, rw.week_window(ref44), reports=[])
    assert "fundamental_research_team" in presets(p44) and "visser_panel" not in presets(p44)
    assert presets(rw.build_plan(p, ref, rw.week_window(ref), plan_kind="kerne")) == ["risk_committee", "portfolio_review_board"]
    capped = rw.build_plan(p, ref, rw.week_window(ref), max_swarms=2)
    assert presets(capped) == ["risk_committee", "portfolio_review_board"]  # CIO bevares altid


def test_cio_gets_other_teams_conclusions():
    runs = [{"preset": "risk_committee", "label": "Risikoudvalg", "status": "completed", "final_report": "VaR 95 % = 4,1 %"},
            {"preset": "x", "label": "Fejlet", "status": "error", "final_report": None},
            {"preset": "y", "label": "Sprunget", "status": "skipped", "skip_reason": "Budget brugt", "tasks": []}]
    g = rw.cio_goal_with_inputs("Mål.", runs)
    assert g.startswith("Mål.") and "### Risikoudvalg" in g and "VaR 95 % = 4,1 %" in g
    # teams uden rapport nævnes, så CIO'en ikke tror, at de er enige
    assert "### Fejlet: ingen rapport (error)" in g and "### Sprunget: ingen rapport (skipped: Budget brugt)" in g
    assert rw.cio_goal_with_inputs("Mål.", []) == "Mål."


def _long_report(n: int) -> str:
    intro = f"# Team {n}\n\n" + ("Indledning med baggrund og metode. " * 160)
    body = "\n\n".join(f"## {k}. Analyse {k}\n" + ("Mellemregning og tabeller. " * 60) for k in range(1, 5))
    return (f"{intro}\n\n{body}\n\n## 5. Samlet konklusion\nKONKLUSION-{n}: sælg ikke, men trim {n} pp før regnskab.\n\n"
            "## 6. Datakilder og forbehold\n" + ("Kilde. " * 300))


def test_cio_digest_keeps_every_team_and_their_conclusions():
    labels = ["Risikoudvalg", "Regnskabsdesk", "ETF-allokering", "Investeringskomité"]
    runs = [{"preset": f"p{n}", "label": lab, "status": "completed", "final_report": _long_report(n), "tasks": []}
            for n, lab in enumerate(labels, 1)]
    assert all(len(r["final_report"]) > 12000 for r in runs)
    g = rw.cio_goal_with_inputs("Mål.", runs)
    extra = g[len("Mål."):]
    assert len(extra) <= 16000
    for n, lab in enumerate(labels, 1):  # det 4. team blev før skåret helt væk, og konklusionerne stod sidst
        assert f"### {lab}" in extra and f"KONKLUSION-{n}: sælg ikke, men trim {n} pp" in extra, lab
    assert "Kilde. Kilde. Kilde. Kilde. Kilde." not in extra  # kilder og forbehold prioriteres lavest
    assert "Indledning med baggrund" in extra  # indledningen er med, men kort


def test_report_digest_short_text_and_no_headings():
    assert rw.report_digest("Kort rapport.", 100) == "Kort rapport."
    flat = "START " + "midte " * 2000 + "SLUTKONKLUSION"
    d = rw.report_digest(flat, 1000)
    assert d.startswith("START") and d.rstrip().endswith("SLUTKONKLUSION") and len(d) <= 1010


# Mønstre fra testkørslerne 15/10 og 16/10 (vægte og symboler er ændret, så ingen rigtig portefølje står i repoet)
PLAN_W42 = ("## Plan\n- **Load methodology:** `risk-analysis` and `volatility` skills.\n"
            "- **Fetch price history:** weekly bars for AAA.US, BBB.US since the IPO (12/06/2026) through 15/10/2026.\n"
            "- **Core risk engine:** `portfolio_risk_xray` with current weights (20,0 % AAA / 12,5 % BBB / 7,5 % CCC / "
            "60,0 % DDD) for volatility, max drawdown, historical VaR/ES.\n"
            "- **Tail-risk cross-check:** `quantlib_call` (`historical_var`, `historical_cvar` at 95 %/99 %).\n"
            "- Write `report.md` in Danish with a Datakvalitet section.\n\nExecuting the data-gathering calls now.")
NOT_REPORTS = {
    "plan med vægte i %": PLAN_W42,
    "dansk statusbesked": ("Jeg har indhentet de nødvendige prisdata (jf. facit-tabellen, 15/09–14/10/2026, samt USD/SEK 9,96). "
                           "VaR/CVaR-delen bygger på det tilgængelige daglige datasæt og er markeret som et lille-stikprøve-"
                           "estimat. Jeg færdiggør nu rapporten."),
    "kort engelsk status": "USD/SEK = 9,974 (15/10/2026). Now let me get AAA and BBB indicators.",
    "engelsk plan": ("I have good data now. Let me also get CCC and DDD for the same period, then USD/SEK, then write the "
                     "full analysis script using quantlib functions."),
    "simpel plan": ("Plan\nLoad methodology: risk skills.\nFetch price history for the holdings.\n"
                    "Core risk engine: portfolio_risk_xray.\nExecuting the data-gathering calls now."),
}
REPORTS = {
    "konklusion": "## Konklusion\nVaR 95 % er 4,30 %, ES 7,30 %, drawdown −34,7 %. Anbefaling: reducér gradvist. " * 3,
    "tørkørsel": "Tørkørsel: Analytiker 1 for x.",
    "plan-overskrift med vurdering": ("## Plan for overgangen\nTrin 1: nye indskud til kernen.\n## 2. Samlet vurdering\n"
                                      "Koncentrationen er høj.\n\nJeg vil følge op efter regnskabet."),
    "tabel": "Risikotal\n| Mål | Niveau |\n|---|---|\n| VaR | 4,3 % |\nLet me know.",
    "fed konklusion": "**1. Konklusion:** Porteføljen er koncentreret. Jeg færdiggør nu rapporten.",
}


@pytest.mark.parametrize("name", sorted(NOT_REPORTS))
def test_plans_and_status_messages_are_not_reports(name):
    assert rw.classify_report(NOT_REPORTS[name]) == "plan_only", name


@pytest.mark.parametrize("name", sorted(REPORTS))
def test_real_reports_are_reports(name):
    assert rw.classify_report(REPORTS[name]) == "ok", name


def test_agents_without_report_are_flagged_for_log_and_cio():
    plan_text = PLAN_W42
    assert rw.classify_report("(no summary)") == "empty" and rw.classify_report(None) == "empty"
    real = REPORTS["konklusion"]
    agents = [SimpleNamespace(id=f"a{i}", role=r) for i, r in enumerate(("Head of Risk", "Risk Inspector", "Tail Risk Analyst"))]
    tasks = [SimpleNamespace(id=f"t{i}", agent_id=f"a{i}", status="completed", summary=txt, error=None, worker_iterations=5,
                             started_at=None, completed_at=None) for i, txt in enumerate((real, plan_text, None))]
    run = SimpleNamespace(id="r1", status="completed", agents=agents, tasks=tasks, final_report=real,
                          total_input_tokens=10, total_output_tokens=10, completed_at=None, provider="x", model="y")
    s = rw.summarize_run(run, "risk_committee", "r", rw.now_sto(), 1.0, price_in=2, price_out=10)
    assert s["agents_without_report"] == ["Risk Inspector", "Tail Risk Analyst"]
    assert [t["report"] for t in s["tasks"]] == ["ok", "plan_only", "empty"]
    g = rw.cio_goal_with_inputs("Mål.", [s])
    assert "(Agenter uden rapport i teamet: Risk Inspector, Tail Risk Analyst)" in g


def test_dry_run_flags_agent_without_report_and_shows_real_spend(tmp_path):
    res = tmp_path / "results"
    res.mkdir()
    (res / "ledger.json").write_text(json.dumps({"schema": 1, "runs": [
        {"run_id": "2026-W41", "generated_at": "2026-10-15T12:23:38+02:00", "cost_usd": 10.04, "dry_run": False},
        {"run_id": "t", "generated_at": "2026-10-15T19:28:47+02:00", "cost_usd": 0.05, "dry_run": True}]}))
    pf = dict(PORTFOLIO, monthly_deposit_sek=15000, courtage_class="Medium")
    p = run_main(tmp_path, "--event", "workflow_dispatch", "--now", "2026-10-16T12:00", VT_BUDGET_RESET_DAY="23",
                 VT_DRY_EMPTY="risk_committee", VT_PORTFOLIO=json.dumps(pf))
    assert p.returncode == 0, p.stderr
    assert "brugt siden 23/09/2026: 10.04 af 95 USD" in p.stdout  # tørkørslen viser nu det rigtige forbrug
    assert "· Risikoudvalg: completed" in p.stdout and "1 af 2 agenter uden rapport: Analytiker 2" in p.stdout
    log = p.stdout + p.stderr
    for secret in ("15000", "15 000", "Medium"):
        assert secret not in log, f"{secret!r} lækket til loggen"
    data = rw.decrypt(json.loads((res / "latest.enc.json").read_text()), PASS)
    risk = next(r for r in data["runs"] if r["preset"] == "risk_committee")
    assert risk["agents_without_report"] == ["Analytiker 2"] and risk["tasks"][1]["report"] == "empty"
    assert data["totals"]["month_spent_before_usd"] == 10.04


def test_shellfree_presets_swap_bash_for_quant_tools(tmp_path):
    import yaml
    src = tmp_path / "src"
    src.mkdir()
    (src / "risk_committee.yaml").write_text(yaml.safe_dump({"name": "risk_committee", "agents": [
        {"id": "a", "system_prompt": "Analyse.", "tools": ["bash", "read_file", "get_market_data"]},
        {"id": "b", "system_prompt": "Samler.", "tools": ["read_file", "get_options_chain", "read_url"],
         "skills": ["hedging-strategy", "volatility"]}]}))
    (src / "plain.yaml").write_text(yaml.safe_dump({"name": "plain", "agents": [{"id": "c", "system_prompt": "x", "tools": ["web_search"]}]}))
    changed = rw.prepare_shellfree_presets(tmp_path / "dst", src)
    assert changed == ["plain", "risk_committee"]  # plain får kun research-discipline på sidste agent
    spec = yaml.safe_load((tmp_path / "dst" / "risk_committee.yaml").read_text())
    a, b = spec["agents"]
    assert "bash" not in a["tools"] and {"quantlib_call", "portfolio_risk_xray"} <= set(a["tools"])
    assert a["tools"].count("get_market_data") == 1 and "Shell and file tools are DISABLED" in a["system_prompt"]
    # filværktøjer, read_url og optionsværktøjer fjernes; options-skillen med
    assert "bash" not in b["tools"] and not {"read_file", "read_url", "get_options_chain"} & set(b["tools"])
    assert {"quantlib_call", "portfolio_risk_xray", "load_skill"} <= set(b["tools"]) and "research-discipline" in b["skills"]
    assert b["skills"][0] == "volatility" and "hedging-strategy" not in b["skills"]
    assert "Vibetrad risk brief" in b["system_prompt"] and "never options or futures" in b["system_prompt"]


def test_month_spent_reads_ledger(tmp_path):
    (tmp_path / "ledger.json").write_text(json.dumps({"runs": [
        {"generated_at": "2026-10-15T12:23:38+02:00", "cost_usd": 10.0},
        {"generated_at": "2026-10-17T09:00:00+02:00", "cost_usd": 20.5},
        {"generated_at": "2026-10-17T09:00:00+02:00", "cost_usd": 99, "dry_run": True},
        {"generated_at": "2026-09-26T09:00:00+02:00", "cost_usd": 50}]}))
    assert rw.month_spent(tmp_path, sto("2026-10-24T08:00"), reset_day=1) == 30.5
    assert rw.month_spent(tmp_path / "nope", sto("2026-10-24T08:00"), reset_day=1) == 0.0


def test_budget_follows_billing_period(tmp_path, monkeypatch):
    # Max-kreditten fornyes d. 23. og udløber ved periodens slutning: loftet skal følge perioden
    (tmp_path / "ledger.json").write_text(json.dumps({"runs": [
        {"generated_at": "2026-09-26T09:00:00+02:00", "cost_usd": 50},
        {"generated_at": "2026-10-15T12:23:38+02:00", "cost_usd": 10.04},
        {"generated_at": "2026-10-23T22:30:00+02:00", "cost_usd": 30}]}))
    assert rw.budget_period_start(sto("2026-10-16T09:00"), 23) == rw.dt.date(2026, 9, 23)
    assert rw.budget_period_start(sto("2026-10-23T00:10"), 23) == rw.dt.date(2026, 10, 23)
    assert rw.budget_period_start(sto("2026-01-05T09:00"), 23) == rw.dt.date(2025, 12, 23)
    assert rw.budget_period_start(sto("2026-03-15T09:00"), 31) == rw.dt.date(2026, 2, 28)
    assert rw.month_spent(tmp_path, sto("2026-10-16T09:00"), reset_day=23) == 60.04
    assert rw.month_spent(tmp_path, sto("2026-10-24T09:00"), reset_day=23) == 30.0
    monkeypatch.setenv("VT_BUDGET_RESET_DAY", "23")
    assert rw.month_spent(tmp_path, sto("2026-10-24T09:00")) == 30.0
    monkeypatch.setenv("VT_BUDGET_RESET_DAY", "x")  # ugyldig værdi: kalendermåned
    assert rw.budget_period_start(sto("2026-10-16T09:00")) == rw.dt.date(2026, 10, 1)


def test_console_shows_budget_period(tmp_path):
    p = run_main(tmp_path, "--event", "workflow_dispatch", "--now", "2026-10-16T12:00", VT_BUDGET_RESET_DAY="23")
    assert p.returncode == 0, p.stderr
    assert "brugt siden 23/09/2026: 0.00 af 95 USD" in p.stdout
    data = rw.decrypt(json.loads((tmp_path / "results" / "latest.enc.json").read_text()), PASS)
    assert data["totals"]["budget_period_start"] == "2026-09-23"


def test_upcoming_reports_horizon_and_lookup_fallback():
    p = rw.load_portfolio(json.dumps(PORTFOLIO))
    assert rw.upcoming_reports(p, sto("2026-10-01T08:00"), lookup=None) == []  # 27 dage ude
    got = rw.upcoming_reports(p, sto("2026-10-30T08:00"), lookup=lambda t: dt.date(2026, 11, 2) if t == "ZZTOP.US" else None)
    assert [r["ticker"] for r in got] == ["ZZTOP.US"] and got[0]["source"] == "yahoo"


# --- swarm-kørsel ----------------------------------------------------------
def test_run_swarm_times_out_and_cancels():
    class Slow(rw.FakeRuntime):
        cancelled = False

        def start_run(self, preset, user_vars, include_shell_tools=False):
            r = super().start_run(preset, user_vars)
            r.status = "running"
            return r

        def cancel_run(self, rid):
            self.cancelled = True
            return True

    rt, t = Slow(), [0.0]

    def clock():
        return t[0]

    def sleep(s):
        t[0] += s

    run, stopped = rw.run_swarm(rt, rt, "risk_committee", {"goal": "x"}, timeout_s=10, poll_s=1, clock=clock, sleep=sleep)
    assert rt.cancelled and stopped == "timeout"
    summary = rw.summarize_run(run, "risk_committee", "r", sto("2026-10-17T08:00"), 600.0, price_in=2, price_out=10,
                               stopped=stopped)
    assert summary["status"] == "timeout"
    # Budgettet tjekkes undervejs: tokens ud over budgettet afbryder kørslen, og de færdige rapporter bevares
    rt2, t[0] = Slow(), 0.0
    run2, stopped2 = rw.run_swarm(rt2, rt2, "risk_committee", {"goal": "x"}, timeout_s=10_000, poll_s=1, clock=clock,
                                  sleep=sleep, budget_usd=0.0, price_in=2, price_out=10)
    assert rt2.cancelled is (float(getattr(run2, "total_input_tokens", 0) or 0) > 0) or stopped2 in (None, "budget")


@pytest.mark.skipif(importlib.util.find_spec("src") is None, reason="Vibe-Trading er ikke installeret")
def test_timeout_does_not_write_on_vibe_tradings_run_model():
    """19/10: run.timed_out = True gav ValueError på pydantic-modellen SwarmRun, og kørslen blev gemt som fejl."""
    from src.swarm.models import SwarmRun

    class Rt:
        cancelled = False

        def __init__(self):
            self.run = SwarmRun(id="r1", preset_name="risk_committee", status="running", user_vars={},
                                created_at="2026-10-17T08:00:00+00:00")

        def start_run(self, preset, user_vars, include_shell_tools=False):
            return self.run

        def load_run(self, rid):
            return self.run

        def cancel_run(self, rid):
            self.cancelled = True

    rt, t = Rt(), [0.0]
    run, stopped = rw.run_swarm(rt, rt, "risk_committee", {}, timeout_s=5, poll_s=1, clock=lambda: t[0],
                                sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert rt.cancelled and stopped == "timeout" and run is rt.run
    s = rw.summarize_run(run, "risk_committee", "test", rw.now_sto(), 10, price_in=3, price_out=15, stopped=stopped)
    assert s["status"] == "timeout"


def test_summarize_run_costs_and_roles():
    rt = rw.FakeRuntime()
    run = rt.start_run("portfolio_review_board", {"portfolio": "p"})
    s = rw.summarize_run(run, "portfolio_review_board", "r", rw.now_sto(), 12.4, price_in=3, price_out=15)
    assert s["est_cost_usd"] == pytest.approx(1200 / 1e6 * 3 + 300 / 1e6 * 15, abs=0.01)
    assert s["tasks"][0]["role"] == "Analytiker 1" and s["label"] == "Porteføljegennemgang (CIO)"


# --- kryptering ------------------------------------------------------------
def test_encrypt_roundtrip_and_tamper():
    pub = {"week_id": "2026-W42", "status": "ok"}
    env = rw.encrypt({"hej": "æøå", "n": [1, 2]}, PASS, pub, iterations=1000)
    assert rw.decrypt(env, PASS) == {"hej": "æøå", "n": [1, 2]}
    assert "æøå" not in json.dumps(env) and "hej" not in env["ct"]
    with pytest.raises(Exception):
        rw.decrypt(env, PASS + "x")
    env["public"]["status"] = "failed"  # metadata er bundet som AAD
    with pytest.raises(Exception):
        rw.decrypt(env, PASS)


def test_fit_payload_shrinks_to_db_limit():
    big = "".join(chr(0x4E00 + (i * 7919) % 20000) for i in range(60_000))  # ukomprimerbar tekst
    payload = {"week": {"id": "x"}, "runs": [{"final_report": big, "tasks": [{"summary": big} for _ in range(4)]}]}
    env = rw.fit_payload(payload, PASS, {"week_id": "x"}, 1000)
    assert len(json.dumps(env)) <= rw.MAX_ENVELOPE_BYTES
    assert rw.decrypt(env, PASS)["truncated"] is True


@pytest.mark.skipif(not shutil.which("node"), reason="node mangler")
@pytest.mark.parametrize("cost", [22.61, 8.0, 0.0, 35.0, 0.1 + 0.2])
def test_browser_webcrypto_can_decrypt(tmp_path, cost):
    """Også hele dollarbeløb (8.0) og 0.0: før 19/10/2026 gav de "8.0" i Python og "8" i browseren → ulåselig."""
    pub = {"week_id": "2026-W42", "week_label": "Uge 42 · 12/10/2026–16/10/2026", "runs": 2, "dry_run": False,
           "presets": ["portfolio_review_board"], "status": "ok", "cost_usd": round(cost, 2),
           "nested": {"b": 1.0, "a": [2.0, 2.5]}}
    env = rw.encrypt({"week": {"id": "2026-W42"}, "tekst": "Æble"}, PASS, pub, iterations=2000)
    f = tmp_path / "env.json"
    f.write_text(json.dumps(env))
    out = subprocess.run(["node", str(ROOT / "tests" / "decrypt_webcrypto.mjs"), str(f), PASS],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"week": {"id": "2026-W42"}, "tekst": "Æble"}


def test_aad_matches_javascript_number_formatting():
    assert rw.aad_bytes({"cost_usd": 8.0, "runs": 5, "x": 1.5}) == b'{"cost_usd":8,"runs":5,"x":1.5}'
    assert rw.aad_bytes({"b": {"d": 2.0, "c": [1.0, True]}, "a": 0.0}) == b'{"a":0,"b":{"c":[1,true],"d":2}}'
    for bad in (float("nan"), 1e-05):
        with pytest.raises(ValueError):
            rw.aad_bytes({"x": bad})


# --- ende-til-ende (tørkørsel som subprocess, så GitHub-loggen kan inspiceres) --
def run_main(tmp_path, *extra, **env_over):
    # CI sætter GITHUB_RUN_ID, GITHUB_EVENT_NAME osv.; de må ikke smitte af på testene (run_id, trigger)
    base = {k: v for k, v in os.environ.items() if not k.startswith(("VT_", "GITHUB_", "ANTHROPIC"))}
    env = {**base, "VT_PORTFOLIO": json.dumps(PORTFOLIO), "VT_RESULTS_PASSPHRASE": PASS, "VT_SCHEDULE": "on",
           "VT_PBKDF2_ITER": "1000", "GITHUB_OUTPUT": str(tmp_path / "out.txt"), "EODHD_API_KEY": "",
           "EODHD_API_TOKEN": "", "FRED_API_KEY": "", "VT_DATATJEK_VT": "off", **env_over}
    if "ANTHROPIC_API_KEY" not in env_over:
        env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.run([sys.executable, str(ROOT / "runner" / "run_week.py"), "--results", str(tmp_path / "results"),
                           "--dry-run", *extra], capture_output=True, text=True, env=env, timeout=120)


def outputs(tmp_path):
    return dict(line.split("=", 1) for line in (tmp_path / "out.txt").read_text().split())


def test_unexpected_error_shows_where_but_never_the_message(tmp_path, monkeypatch, capfd):
    # En uventet fejl må kun vise fejltype og sted (offentlig kode) i GitHub-loggen; beskeden kan rumme porteføljedata.
    def boom():
        raise KeyError("ZZTOP.ST 2 615 SEK")

    monkeypatch.setattr(rw, "main", boom)
    monkeypatch.setattr(rw.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out.txt"))
    assert rw._main_guarded() == 3
    out = capfd.readouterr().out
    assert "FEJL: uventet KeyError i runneren ved test_run_week.py:" in out and "boom" in out
    assert "ZZTOP" not in out and "2 615" not in out
    assert "ZZTOP.ST 2 615 SEK" in (tmp_path / "vibetrad-vt.log").read_text()  # kun i den private log
    assert outputs(tmp_path) == {"ran": "false", "status": "error"}


def test_end_to_end_dry_run_first_saturday(tmp_path):
    p = run_main(tmp_path, "--event", "schedule", "--now", "2026-10-03T08:01")  # uge 40 (lige), første lørdag
    assert p.returncode == 0, p.stderr
    log = p.stdout + p.stderr
    for secret in ("ZZTOP", "QQQX", "Hemmelig", "21480", "21 480", "reserveret"):
        assert secret not in log, f"{secret!r} lækket til loggen"
    o = outputs(tmp_path)
    assert {k: o[k] for k in ("ran", "status", "week")} == {"ran": "true", "status": "ok", "week": "2026-W40"}
    assert o["run_id"].startswith("2026-W40_")
    res = tmp_path / "results"
    env = json.loads((res / "latest.enc.json").read_text())
    assert (res / "weeks" / f"{o['run_id']}.enc.json").exists() and env["public"]["run_id"] == o["run_id"]
    assert json.loads((res / "index.json").read_text())["latest"] == o["run_id"]
    for f in res.rglob("*.json"):
        assert "Hemmelig" not in f.read_text() and "ZZTOP" not in f.read_text()
    data = rw.decrypt(env, PASS)
    assert [r["preset"] for r in data["runs"]] == ["risk_committee", "etf_allocation_desk",
                                                   "fundamental_research_team", "portfolio_review_board"]
    assert data["status"] == "ok" and data["portfolio"]["holdings"][0]["name"] == "Hemmelig Raket"
    assert env["public"]["status"] == "ok" and env["public"]["completed"] == 4
    assert env["public"]["week_to"] == "2026-10-02" and env["public"]["cost_usd"] > 0
    assert json.loads((res / "ledger.json").read_text())["runs"][0]["week_id"] == "2026-W40"


def test_end_to_end_skip_then_partial(tmp_path):
    # en tørkørsel blokerer ikke lørdagen, men en rigtig kørsel for hele ugen gør
    assert run_main(tmp_path, "--event", "schedule", "--now", "2026-10-17T08:05").returncode == 0
    p = run_main(tmp_path, "--event", "schedule", "--now", "2026-10-17T09:35")
    assert outputs(tmp_path)["ran"] == "true"
    led = tmp_path / "results" / "latest.enc.json"
    env = json.loads(led.read_text())
    env["public"]["dry_run"] = False
    led.write_text(json.dumps(env))
    p = run_main(tmp_path, "--event", "schedule", "--now", "2026-10-17T09:40")
    assert "allerede kørt" in p.stdout and outputs(tmp_path)["ran"] == "false"
    p = run_main(tmp_path, "--event", "workflow_dispatch", "--now", "2026-10-17T10:00", "--force-risk",
                 VT_DRY_FAIL="risk_committee")
    assert outputs(tmp_path)["status"] == "partial"
    idx = json.loads((tmp_path / "results" / "index.json").read_text())
    # hver kørsel gemmes for sig (knaptryk flere gange om ugen overskriver ikke hinanden)
    assert len(idx["weeks"]) >= 2 and idx["weeks"][0]["status"] == "partial"
    assert len({w["run_id"] for w in idx["weeks"]}) == len(idx["weeks"])


def test_run_id_unique_per_github_run_and_attempt(tmp_path):
    # samme GitHub-kørsel genkørt ("Re-run") i samme minut må ikke overskrive det første resultat
    for attempt in ("1", "2"):
        p = run_main(tmp_path, "--event", "workflow_dispatch", "--now", "2026-10-17T10:00",
                     GITHUB_RUN_ID="4242", GITHUB_RUN_ATTEMPT=attempt)
        assert p.returncode == 0, p.stderr
    ids = [w["run_id"] for w in json.loads((tmp_path / "results" / "index.json").read_text())["weeks"]]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert {i.rsplit("_", 1)[1] for i in ids} == {"4242", "4242-2"}


def test_console_explains_why_only_eodhd_is_checked(tmp_path):
    # ADVARSEL med kun én kilde er forventet, når Vibe-Trading er slået fra; loggen skal sige hvorfor
    p = run_main(tmp_path, "--event", "workflow_dispatch", "--now", "2026-10-17T10:00")
    assert p.returncode == 0, p.stderr
    assert "Vibe-Trading nej (slået fra med VT_DATATJEK_VT=off)" in p.stdout


def test_gate_only_needs_no_secrets(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VT_", "ANTHROPIC"))}
    env["GITHUB_OUTPUT"] = str(tmp_path / "out.txt")
    env["VT_SCHEDULE"] = "on"
    p = subprocess.run([sys.executable, str(ROOT / "runner" / "run_week.py"), "--results", str(tmp_path / "r"),
                        "--gate-only", "--event", "schedule", "--now", "2026-10-17T08:00"],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0 and outputs(tmp_path)["ran"] == "true"


def test_missing_secrets_fail_clearly(tmp_path):
    p = run_main(tmp_path, "--event", "workflow_dispatch", VT_RESULTS_PASSPHRASE="kort")
    assert p.returncode == 2 and "VT_RESULTS_PASSPHRASE" in p.stdout
    p = run_main(tmp_path, "--event", "workflow_dispatch", VT_PORTFOLIO="{ikke json")
    assert p.returncode == 2 and "ikke gyldig JSON" in p.stdout and "ikke json" not in p.stdout


def test_skip_presets_and_force_risk(monkeypatch):
    p = rw.load_portfolio(json.dumps(PORTFOLIO))
    ref = sto("2026-10-17T08:00")
    reps = rw.upcoming_reports(p, ref, lookup=None)
    monkeypatch.setenv("VT_SKIP_PRESETS", "risk_committee,fundamental_research_team")
    assert presets(rw.build_plan(p, ref, rw.week_window(ref), reports=reps)) == [
        "earnings_research_desk", "fund_selection_panel", "portfolio_review_board"]
    assert "risk_committee" in presets(rw.build_plan(p, ref, rw.week_window(ref), reports=reps, force_risk=True))
    monkeypatch.setenv("VT_SKIP_PRESETS", "portfolio_review_board,risk_committee,earnings_research_desk,fund_selection_panel,fundamental_research_team")
    assert presets(rw.build_plan(p, ref, rw.week_window(ref), reports=reps)) == ["portfolio_review_board"]  # CIO kan ikke slås fra


def test_budget_skips_extras_but_keeps_cio(tmp_path):
    p = run_main(tmp_path, "--event", "workflow_dispatch", "--now", "2026-10-17T08:05",
                 VT_RUN_BUDGET_USD="0.012", VT_CIO_RESERVE_USD="0.005", VT_MIN_SWARM_USD="0.001")
    assert p.returncode == 0, p.stderr
    data = rw.decrypt(json.loads((tmp_path / "results" / "latest.enc.json").read_text()), PASS)
    st = {r["preset"]: r["status"] for r in data["runs"]}
    assert st["portfolio_review_board"] == "completed" and "skipped" in st.values()
    assert data["status"] == "ok"  # budget-spring er ikke en fejl


@pytest.mark.parametrize("day,preset", [("2026-10-03", "etf_allocation_desk"), ("2026-10-10", "macro_rates_fx_desk"),
                                        ("2026-10-17", "fund_selection_panel"), ("2026-10-24", "value_investing_committee"),
                                        ("2026-10-31", "equity_research_team")])
def test_monthly_slot_by_saturday(day, preset):
    p = rw.load_portfolio(json.dumps(PORTFOLIO))
    ref = sto(day + "T08:00")
    assert preset in presets(rw.build_plan(p, ref, rw.week_window(ref), reports=[]))


def test_button_runs_rotate_to_least_recent_team(monkeypatch):
    p = rw.load_portfolio(json.dumps({**PORTFOLIO, "watchlist": [{"ticker": "NOVA-B.CO"}]}))
    ref = sto("2026-10-15T11:34")
    hist = [{"generated_at": "2026-10-10T09:00:00+02:00", "presets": ["etf_allocation_desk", "investment_committee"]},
            {"generated_at": "2026-10-12T09:00:00+02:00", "presets": ["macro_rates_fx_desk", "sentiment_intelligence_team"]},
            {"generated_at": "2026-10-13T09:00:00+02:00", "presets": ["fund_selection_panel"], "dry_run": True}]
    names = presets(rw.build_plan(p, ref, rw.week_window(ref), reports=[], history=hist, by_date=False))
    # fondsvalg kørte kun som tørkørsel -> tæller ikke; aldrig-kørte teams vælges først i fast rækkefølge
    assert "fund_selection_panel" in names and "visser_panel" in names  # aldrig kørt -> vælges før komitéen
    monkeypatch.setenv("VT_MONTHLY_SLOT", "2")
    assert "macro_rates_fx_desk" in presets(rw.build_plan(p, ref, rw.week_window(ref), reports=[], history=hist, by_date=False))


def test_extra_skills_are_added_with_load_skill(tmp_path):
    import yaml
    src = tmp_path / "src"
    src.mkdir()
    (src / "portfolio_review_board.yaml").write_text(yaml.safe_dump({"name": "portfolio_review_board", "agents": [
        {"id": "risk_inspector", "system_prompt": "x", "tools": ["read_file"], "skills": ["risk-analysis"]},
        {"id": "chief_investment_officer", "system_prompt": "y", "tools": ["read_file"], "skills": ["asset-allocation"]}]}))
    assert rw.prepare_shellfree_presets(tmp_path / "dst", src) == ["portfolio_review_board"]
    a, cio = yaml.safe_load((tmp_path / "dst" / "portfolio_review_board.yaml").read_text())["agents"]
    assert a["skills"] == ["risk-analysis"] and "load_skill" not in a["tools"]
    assert cio["skills"][:1] == ["asset-allocation"] and {"investor-lenses", "research-discipline"} <= set(cio["skills"])
    assert "thesis-tracker" not in cio["skills"]  # fil-skill uden read_file
    assert cio["skills"].count("research-discipline") == 1 and "load_skill" in cio["tools"]
    assert {"report_audit", "financial_rigor", "get_market_data"} <= set(cio["tools"]) and "Faktatjek: PASS" in cio["system_prompt"]
    assert "report_audit" not in a["tools"]


def test_spacex_gets_private_company_framework():
    p = rw.load_portfolio(json.dumps({"holdings": [{"ticker": "SPCX.US", "name": "SpaceX", "cost_sek": 21480}],
                                      "next_reports": {"SPCX.US": "2026-11-03"}}))
    ref = sto("2026-10-24T08:00")  # 4. lørdag -> værdikomité på SpaceX + regnskabsdesk
    plan = rw.build_plan(p, ref, rw.week_window(ref), reports=rw.upcoming_reports(p, ref, lookup=None))
    by = {s["preset"]: s for s in plan}
    assert "private-company-research" in by["earnings_research_desk"]["vars"]["target"]
    assert "private-company-research" in by["value_investing_committee"]["vars"]["market"]
    assert "private-company-research" in rw.EXTRA_SKILLS["value_investing_committee"]["*last*"]


# --- EODHD og datatjek ------------------------------------------------------
def test_presets_get_eodhd_tool_guidance_and_brief(tmp_path, monkeypatch):
    import yaml
    monkeypatch.setenv("EODHD_API_KEY", "k" * 20)
    monkeypatch.setenv("FRED_API_KEY", "f" * 20)
    src = tmp_path / "src"
    src.mkdir()
    (src / "macro_rates_fx_desk.yaml").write_text(yaml.safe_dump({"name": "macro_rates_fx_desk", "agents": [
        {"id": "rates", "system_prompt": "Renter.", "tools": ["bash", "get_market_data"]},
        {"id": "lead", "system_prompt": "Samler.", "tools": ["read_file"]}]}))
    (src / "portfolio_review_board.yaml").write_text(yaml.safe_dump({"name": "portfolio_review_board", "agents": [
        {"id": "chief_investment_officer", "system_prompt": "CIO.", "tools": ["read_file"]}]}))
    brief = "## Vibetrad datatjek 15/10/2026: PASS\n| a | b |"
    rw.prepare_shellfree_presets(tmp_path / "dst", src, brief=brief)
    rates, lead = yaml.safe_load((tmp_path / "dst" / "macro_rates_fx_desk.yaml").read_text())["agents"]
    assert {"get_eodhd_data", "get_macro_series"} <= set(rates["tools"]) and "SE10Y.GBOND" in rates["system_prompt"]
    assert "get_macro_series" in lead["tools"] and "get_eodhd_data" in lead["tools"]  # hele desken
    assert "| a | b |" in rates["system_prompt"] and "Vibetrad datatjek" in lead["system_prompt"]
    assert rates["system_prompt"].rstrip().endswith("never derivatives.")  # makro-deskens egen Vibetrad-instruks sidst
    (cio,) = yaml.safe_load((tmp_path / "dst" / "portfolio_review_board.yaml").read_text())["agents"]
    assert {"get_eodhd_data", "etf_holdings", "report_audit"} <= set(cio["tools"]) and "Datakvalitet" in cio["system_prompt"]
    assert len(cio["tools"]) == len(set(cio["tools"]))


def test_same_ticker_twice_gives_one_earnings_desk():
    p = rw.load_portfolio(json.dumps(dict(PORTFOLIO, holdings=PORTFOLIO["holdings"] + [PORTFOLIO["holdings"][1]])))
    reps = rw.upcoming_reports(p, sto("2026-10-20T08:00"), lookup=None)
    assert [r["ticker"] for r in reps] == ["QQQX.US"]


def test_upcoming_reports_prefers_verified_dates():
    p = rw.load_portfolio(json.dumps(PORTFOLIO))  # porteføljen siger QQQX 28/10
    got = rw.upcoming_reports(p, sto("2026-10-20T08:00"), lookup=None,
                              verified={"QQQX.US": "2026-10-29", "ZZTOP.US": "2026-11-20"})
    assert got == [{"ticker": "QQQX.US", "name": "Hemmelig Soeg", "date": "2026-10-29", "source": "datatjek"}]


def test_dry_run_has_datacheck_and_eodhd_key_never_leaks(tmp_path):
    key = "eodhd-NOEGLE-0123456789"
    p = run_main(tmp_path, "--event", "workflow_dispatch", "--now", "2026-10-15T12:00",
                 EODHD_API_KEY=key, EODHD_BASE_URL="http://127.0.0.1:9", EODHD_RETRIES="0")
    assert p.returncode == 0, p.stderr
    log = p.stdout + p.stderr
    assert "Datatjek: " in log and key not in log
    for secret in ("ZZTOP", "QQQX", "Hemmelig"):
        assert secret not in log
    env = json.loads((tmp_path / "results" / "latest.enc.json").read_text())
    assert env["public"]["datacheck"] in ("PASS", "ADVARSEL", "FEJL")
    data = rw.decrypt(env, PASS)
    dc = data["datacheck"]
    assert dc["sources"]["eodhd"]["available"] is True and dc["sources"]["eodhd"]["errors"]
    assert key not in json.dumps(data) and key not in (tmp_path / "results" / "index.json").read_text()
    assert data["eodhd"]["key"] is True
    p2 = run_main(tmp_path / "b", "--event", "workflow_dispatch", "--now", "2026-10-15T12:00", VT_DATATJEK="off")
    env2 = json.loads((tmp_path / "b" / "results" / "latest.enc.json").read_text())
    assert p2.returncode == 0 and env2["public"]["datacheck"] == "ikke kørt"


def test_key_gated_tools_are_left_out_without_keys(tmp_path, monkeypatch):
    import yaml
    for k in ("EODHD_API_KEY", "EODHD_API_TOKEN", "FRED_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    src = tmp_path / "src"
    src.mkdir()
    (src / "macro_rates_fx_desk.yaml").write_text(yaml.safe_dump({"name": "macro_rates_fx_desk", "agents": [
        {"id": "rates", "system_prompt": "Renter.", "tools": ["get_market_data"]}]}))
    rw.prepare_shellfree_presets(tmp_path / "dst", src)
    (a,) = yaml.safe_load((tmp_path / "dst" / "macro_rates_fx_desk.yaml").read_text())["agents"]
    assert "get_eodhd_data" not in a["tools"] and "get_macro_series" not in a["tools"]
    assert "Data sources (Vibetrad)" not in a["system_prompt"]


def test_eodhd_tool_is_not_promised_when_install_failed(tmp_path, monkeypatch):
    import yaml
    monkeypatch.setenv("EODHD_API_KEY", "k" * 20)
    src = tmp_path / "src"
    src.mkdir()
    (src / "risk_committee.yaml").write_text(yaml.safe_dump({"name": "risk_committee", "agents": [
        {"id": "a", "system_prompt": "x", "tools": ["get_market_data"]}]}))
    rw.prepare_shellfree_presets(tmp_path / "dst", src, eodhd_ready=False)
    (a,) = yaml.safe_load((tmp_path / "dst" / "risk_committee.yaml").read_text())["agents"]
    assert "get_eodhd_data" not in a["tools"] and "Data sources (Vibetrad)" not in a["system_prompt"]
    rw.prepare_shellfree_presets(tmp_path / "dst2", src, eodhd_ready=True)
    (a,) = yaml.safe_load((tmp_path / "dst2" / "risk_committee.yaml").read_text())["agents"]
    assert "get_eodhd_data" in a["tools"]


# --- FRED -------------------------------------------------------------------
FRED_KEY = "abcdef0123456789abcdef0123456789"


def test_dry_run_reports_missing_fred_key(tmp_path):
    p = run_main(tmp_path, "--event", "workflow_dispatch", "--now", "2026-10-15T12:00")
    assert p.returncode == 0, p.stderr
    assert "FRED: nej (secret FRED_API_KEY mangler)" in p.stdout
    data = rw.decrypt(json.loads((tmp_path / "results" / "latest.enc.json").read_text()), PASS)
    assert data["fred"]["key"] is False and data["fred"]["calls"] == 0


def test_dry_run_checks_fred_against_fake_server_and_key_never_leaks(tmp_path):
    import http.server
    import threading
    import urllib.parse

    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            u = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            seen.append((u.path, q))
            body = {"observations": [{"date": "2026-09-10", "value": "4.00"}, {"date": "2026-10-14", "value": "4.12"}]}
            if q.get("series_id") == "NCBEILQ027S":  # Buffett: aktier (mio. USD) kvartalsvis
                body = {"observations": [{"date": "2026-01-01", "value": "60000000"}, {"date": "2026-04-01", "value": "66000000"}]}
            elif q.get("series_id") == "GDP":  # BNP (mia. USD)
                body = {"observations": [{"date": "2026-01-01", "value": "30000"}, {"date": "2026-04-01", "value": "30000"}]}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        p = run_main(tmp_path, "--event", "workflow_dispatch", "--now", "2026-10-15T12:00",
                     FRED_API_KEY=FRED_KEY, FRED_BASE_URL=f"http://127.0.0.1:{srv.server_address[1]}")
    finally:
        srv.shutdown()
    assert p.returncode == 0, p.stderr
    log = p.stdout + p.stderr
    assert ("FRED: ja, nøglen virker (7 af 7 serier, 9 kald · US 10-årig statsrente 4,12 % pr. 14/10/2026 · "
            "Buffett-indikator 220 % (Q2 2026))") in log
    assert FRED_KEY not in log
    assert {path for path, _ in seen} == {"/series/observations"} and all(q["api_key"] == FRED_KEY for _, q in seen)
    assert seen[0][1]["observation_end"] == "2026-10-15"
    res = tmp_path / "results"
    data = rw.decrypt(json.loads((res / "latest.enc.json").read_text()), PASS)
    assert data["fred"]["ok"] is True and data["fred"]["series"][0]["value"] == 4.12
    assert data["fred"]["buffett"]["value"] == 220.0 and data["fred"]["buffett"]["prev"] == 200.0
    assert FRED_KEY not in json.dumps(data) and FRED_KEY not in (res / "index.json").read_text()


def test_fred_tool_for_regime_agent_and_cio_with_guidance_and_brief(tmp_path, monkeypatch):
    import yaml
    monkeypatch.setenv("FRED_API_KEY", FRED_KEY)
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    monkeypatch.delenv("EODHD_API_TOKEN", raising=False)
    src = tmp_path / "src"
    src.mkdir()
    (src / "risk_committee.yaml").write_text(yaml.safe_dump({"name": "risk_committee", "agents": [
        {"id": "drawdown_analyst", "system_prompt": "dd", "tools": ["get_market_data"]},
        {"id": "regime_detector", "system_prompt": "regime", "tools": ["get_market_data"]}]}))
    (src / "portfolio_review_board.yaml").write_text(yaml.safe_dump({"name": "portfolio_review_board", "agents": [
        {"id": "chief_investment_officer", "system_prompt": "CIO.", "tools": ["read_file"]}]}))
    brief = "## Vibetrad datatjek\n\n## Makro (FRED, St. Louis Fed) pr. 15/10/2026\n| a |"
    rw.prepare_shellfree_presets(tmp_path / "dst", src, brief=brief)
    dd, regime = yaml.safe_load((tmp_path / "dst" / "risk_committee.yaml").read_text())["agents"]
    (cio,) = yaml.safe_load((tmp_path / "dst" / "portfolio_review_board.yaml").read_text())["agents"]
    assert "get_macro_series" in regime["tools"] and "Macro data (FRED" in regime["system_prompt"]
    assert "get_macro_series" not in dd["tools"] and "Macro data (FRED" not in dd["system_prompt"]
    assert "get_macro_series" in cio["tools"] and "DEXSDUS" in cio["system_prompt"]
    assert all("Makro (FRED" in a["system_prompt"] for a in (dd, regime, cio))  # facit til alle
    rw.prepare_shellfree_presets(tmp_path / "dst2", src, fred_ready=False)  # nøglen afvist i nøgletjekket
    _, regime2 = yaml.safe_load((tmp_path / "dst2" / "risk_committee.yaml").read_text())["agents"]
    assert "get_macro_series" not in regime2["tools"] and "Macro data (FRED" not in regime2["system_prompt"]
    monkeypatch.delenv("FRED_API_KEY")
    rw.prepare_shellfree_presets(tmp_path / "dst3", src)
    (cio3,) = yaml.safe_load((tmp_path / "dst3" / "portfolio_review_board.yaml").read_text())["agents"]
    assert "get_macro_series" not in cio3["tools"]


def test_transition_policy_goal_text_and_risk_scenarios():
    raw = dict(PORTFOLIO, weight_policy="target")
    p = rw.load_portfolio(json.dumps(raw))
    assert p["weight_policy"] == "target" and "langsigtet mål 10 %" in rw.portfolio_text(p)
    plan = rw.build_plan(p, sto("2026-10-17T08:00"), rw.week_window(sto("2026-10-17T08:00")), reports=[])
    by = {s["preset"]: s for s in plan}
    cio = by["portfolio_review_board"]["vars"]["goal"]
    assert "overgangsplan" in cio and "i stedet for at sælge ned til 10 % med det samme" in cio
    risk = by["risk_committee"]["vars"]["goal"]
    for txt in ("−30 %", "−50 %", "USD/SEK −10 %", "aktiemarkedet samlet −25 %", "Brug VaR kun som supplement", "kort kurshistorik"):
        assert txt in risk, txt
    # en goal fra VT_PORTFOLIO vinder stadig, og en ukendt politik bliver "limit"
    p2 = rw.load_portfolio(json.dumps(dict(PORTFOLIO, weight_policy="nonsens", goal="Egen tekst.")))
    assert p2["weight_policy"] == "limit" and "maks. vægt" in rw.portfolio_text(p2)
    plan2 = rw.build_plan(p2, sto("2026-10-17T08:00"), rw.week_window(sto("2026-10-17T08:00")), reports=[])
    assert next(s for s in plan2 if s["preset"] == "portfolio_review_board")["vars"]["goal"].startswith("Egen tekst.")


def test_deposit_and_courtage_reach_the_agents_safely():
    p = rw.load_portfolio(json.dumps(dict(PORTFOLIO, weight_policy="target", monthly_deposit_sek=15000,
                                          courtage_class="Medium<script>")))
    assert p["monthly_deposit_sek"] == 15000 and p["courtage_class"] == "Mediumscript"
    t = rw.portfolio_text(p)
    assert "Månedligt indskud: 15 000 SEK" in t and "kurtageklasse Mediumscript" in t and "angiv ingen kurtagesats" in t
    for bad in ("15000", -5, True, 0, 1e9):
        q = rw.load_portfolio(json.dumps(dict(PORTFOLIO, monthly_deposit_sek=bad)))
        assert q["monthly_deposit_sek"] is None, bad
        assert "Månedligt indskud: ikke oplyst" in rw.portfolio_text(q) and "kurtageklasse ikke oplyst" in rw.portfolio_text(q)
    goal = next(s for s in rw.build_plan(p, sto("2026-10-17T08:00"), rw.week_window(sto("2026-10-17T08:00")), reports=[])
                if s.get("cio"))["vars"]["goal"]
    assert "måneders indskud" in goal


@pytest.mark.parametrize("slot,preset,key", [("1", "etf_allocation_desk", "market"), ("3", "fund_selection_panel", "goal")])
def test_etf_and_fund_teams_only_allocate_new_money(slot, preset, key, monkeypatch):
    monkeypatch.setenv("VT_MONTHLY_SLOT", slot)
    p = rw.load_portfolio(json.dumps(dict(PORTFOLIO, monthly_deposit_sek=15000)))
    plan = rw.build_plan(p, sto("2026-10-16T12:00"), rw.week_window(sto("2026-10-16T12:00")), reports=[])
    txt = next(s for s in plan if s["preset"] == preset)["vars"][key]
    assert "nye indskud (ca. 15 000 SEK om måneden)" in txt and "sælg ikke de eksisterende aktier" in txt
    assert "fordel ikke hele porteføljens værdi" in txt and "Angiv ingen kurtagesats" in txt
    assert "UCITS" in txt and "kun dataproxy" in txt and "nye indskud (ca. 15 000 SEK om måneden) og kontanten (2 615 SEK)" in txt


def test_symbol_guidance_for_agents_that_fetch_prices(tmp_path):
    import yaml
    src = tmp_path / "src"
    src.mkdir()
    (src / "etf_allocation_desk.yaml").write_text(yaml.safe_dump({"name": "etf_allocation_desk", "agents": [
        {"id": "screener", "system_prompt": "Screen.", "tools": ["bash", "get_market_data"]}]}))
    (src / "custom_desk.yaml").write_text(yaml.safe_dump({"name": "custom_desk", "agents": [
        {"id": "writer", "system_prompt": "Skriv.", "tools": []}]}))
    rw.prepare_shellfree_presets(tmp_path / "dst", src)
    (screener,) = yaml.safe_load((tmp_path / "dst" / "etf_allocation_desk.yaml").read_text())["agents"]
    (writer,) = yaml.safe_load((tmp_path / "dst" / "custom_desk.yaml").read_text())["agents"]
    assert "ACWI.US" in screener["system_prompt"] and "China A-share" in screener["system_prompt"]
    assert "never present a number from that call" in screener["system_prompt"]
    assert "share class" in screener["system_prompt"] and "whole-company figure" in screener["system_prompt"]
    assert "China A-share" not in writer["system_prompt"] and "whole-company figure" not in writer["system_prompt"]
    assert "NOVO-B.CO" in screener["system_prompt"] and "USDSEK=X" in screener["system_prompt"]
    assert "at least 30 daily bars" in screener["system_prompt"]


def test_every_agent_gets_the_isk_and_nordnet_rules(tmp_path):
    import yaml
    src = tmp_path / "src"
    src.mkdir()
    (src / "custom_desk.yaml").write_text(yaml.safe_dump({"name": "custom_desk", "agents": [
        {"id": "writer", "system_prompt": "Skriv.", "tools": ["read_file"]}]}))
    assert rw.prepare_shellfree_presets(tmp_path / "dst", src) == ["custom_desk"]
    (writer,) = yaml.safe_load((tmp_path / "dst" / "custom_desk.yaml").read_text())["agents"]
    txt = writer["system_prompt"]
    assert txt.startswith("Skriv.") and "UCITS" in txt and "Nordnet" in txt and "PRIIPs" in txt
    assert "No options, futures, short selling or leverage" in txt and "reserved" in txt


def test_cio_prompt_requires_disagreements_scenarios_and_falsifiers():
    cio = rw.EXTRA_PROMPT["portfolio_review_board"]["chief_investment_officer"]
    for txt in ("Uenigheder mellem teams", "loss scenarios in SEK", "Jeg tager fejl, hvis", "months of deposits",
                "delivered no report", "ALL FOUR", "USD/SEK -10 %", "your own team", "count them correctly",
                "buyable at Nordnet"):
        assert txt in cio, txt
    assert "never compare it with" in rw.FRED_GUIDANCE


@pytest.mark.parametrize("bad", [{"cash_sek": "87 654 SEK"}, {"cash_sek": [1]}, {"holdings": [{"ticker": "AAA", "max_weight": "10 %"}]},
                                 {"goal": 5, "watchlist": 7, "next_reports": [1]}, {"monthly_deposit_sek": "x"}])
def test_bad_portfolio_types_never_reach_the_public_log(tmp_path, bad):
    """Review 19/10: cash_sek som tekst gav en traceback med beløbet på stderr. Nu: config_error uden værdien."""
    pf = dict(PORTFOLIO, **bad)
    p = run_main(tmp_path, "--event", "workflow_dispatch", VT_PORTFOLIO=json.dumps(pf))
    log = p.stdout + p.stderr
    if p.returncode == 2:
        assert "FEJL" in log and "Traceback" not in log
    else:  # felter, der kan tolkes mildt (goal som tal, watchlist der ikke er en liste), ignoreres
        assert p.returncode == 0, log
    for secret in ("87 654", "87654", "10 %", "Traceback"):
        assert secret not in log


def test_portfolio_numbers_accept_text_with_spaces_and_bare_tickers_get_us():
    p = rw.load_portfolio(json.dumps({"holdings": [{"ticker": "googl", "qty": "3", "cost_sek": "9 315", "max_weight": "12,5"}],
                                      "cash_sek": "2 615,50", "next_reports": {"GOOGL": "2026-10-28"},
                                      "next_reports_source": {"googl": "eodhd"}, "watchlist": [{"ticker": "novo-b.co"}, {"x": 1}],
                                      "goal": 7, "as_of": 20261019}))
    h = p["holdings"][0]
    assert h["ticker"] == "GOOGL.US" and h["qty"] == 3 and h["cost_sek"] == 9315 and h["max_weight"] == 12.5
    assert p["cash_sek"] == 2615.5 and p["next_reports"] == {"GOOGL.US": "2026-10-28"}
    assert p["next_reports_source"] == {"GOOGL.US": "eodhd"} and p["watchlist"] == [{"ticker": "NOVO-B.CO"}]
    assert p["goal"] == "7" and p["as_of"] == "20261019"
    reps = rw.upcoming_reports(p, sto("2026-10-20T08:00"), lookup=None, verified={"GOOGL.US": "2026-10-29"})
    assert reps[0]["date"] == "2026-10-29" and reps[0]["source"] == "datatjek"


def test_env_numbers_with_comma_and_garbage(tmp_path):
    p = run_main(tmp_path, "--event", "workflow_dispatch", VT_RUN_BUDGET_USD="35,5")
    assert p.returncode == 0 and "Budget: 35.50 USD" in p.stdout
    p = run_main(tmp_path / "b", "--event", "workflow_dispatch", VT_RUN_BUDGET_USD="mange")
    assert p.returncode == 2 and "VT_RUN_BUDGET_USD skal være et tal" in p.stdout and "Traceback" not in p.stdout + p.stderr


@pytest.mark.parametrize("text,want", [
    ("## Vurdering\n" + "Tal og konklusioner. " * 150 + "\n\nLet me know if you want the 2022 window as well.", "ok"),
    ("**Scenarier jeg vil køre**\n- SPCX -30 %\n- USD/SEK -10 %\nLet me get the prices now.", "plan_only"),
    ("Kort svar uden overskrift. Let me know.", "ok"),
], ids=["let-me-know-tail", "scenario-plan", "short-let-me-know"])
def test_classifier_review_cases(text, want):
    assert rw.classify_report(text) == want


def test_cash_note_keeps_its_decimal_commas():
    """Testen 19/10: '318,40 DKK' i kontantnoten blev til '318 40 DKK', fordi hele linjen fik kommaer skiftet ud."""
    p = rw.load_portfolio(json.dumps({"holdings": [{"ticker": "AAA.US", "qty": 1, "cost_sek": 1000}], "cash_sek": 2615,
                                      "cash_note": "Heraf 1 640 SEK reserveret til åben limitordre: Demo Pharma B, 5 stk à 318,40 DKK",
                                      "next_reports_source": {"aaa.us": "EODHD", "BBB.US": "gæt"}}))
    txt = rw.portfolio_text(p)
    assert "- Kontant: 2 615 SEK (Heraf 1 640 SEK reserveret til åben limitordre: Demo Pharma B, 5 stk à 318,40 DKK)" in txt
    assert "Ledig til nye køb: 975 SEK (1 640 SEK er reserveret og kan ikke bruges)" in txt
    assert p["next_reports_source"] == {"AAA.US": "eodhd"}
    assert rw.load_portfolio(json.dumps({"holdings": [{"ticker": "A"}], "next_reports_source": "eodhd"}))["next_reports_source"] == {}


# --- Rettelser i Vibe-Trading 0.1.16 (kørslen 19/10/2026) -------------------------------------------------

def _tool_msgs(sizes):
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for i, n in enumerate(sizes):
        msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]},
                 {"role": "tool", "tool_call_id": f"c{i}", "content": f"R{i}:" + "x" * n}]
    return msgs


def test_tool_results_are_kept_until_the_budget_is_used():
    msgs = _tool_msgs([3000] * 8)  # 8 kurser à 3 000 tegn: Vibe-Trading 0.1.16 ryddede de 5 ældste
    assert rw._vibetrad_compact_tool_results(msgs, 3, budget=40_000) == 0
    assert all(m["content"].startswith(f"R{i}:") for i, m in enumerate(x for x in msgs if x["role"] == "tool"))


def test_over_budget_clears_oldest_to_half_with_a_clear_marker():
    msgs = _tool_msgs([10_000] * 6 + [50, 10_000])
    assert rw._vibetrad_compact_tool_results(msgs, 3, budget=40_000) == 5
    tools = [m["content"] for m in msgs if m["role"] == "tool"]
    assert all(t.startswith("[CLEARED FROM CONTEXT: this tool call SUCCEEDED and returned 10003 characters")
               for t in tools[:5])
    assert tools[5].startswith("R5:") and tools[6] == "R6:" + "x" * 50 and tools[7].startswith("R7:")
    assert sum(len(t) for t in tools) <= 20_000 + 3 * 10_003
    # Kørt igen: intet nyt at rydde (markører ryddes ikke igen), og de 3 nyeste røres aldrig
    assert rw._vibetrad_compact_tool_results(msgs, 3, budget=40_000) == 0
    big = _tool_msgs([30_000] * 3)
    assert rw._vibetrad_compact_tool_results(big, 3, budget=40_000) == 0


def test_failed_calls_get_an_error_marker_and_short_results_stay():
    err = '{"status": "error", "error": "No price data for DEMO.US: ' + "x" * 300 + '"}'
    msgs = _tool_msgs([12_000, 12_000, 150, 12_000, 12_000, 12_000])
    tools = [m for m in msgs if m["role"] == "tool"]
    tools[1]["content"] = err + " " * 11_000  # stor fejl-kuvert (json med efterstillede mellemrum)
    rw._vibetrad_compact_tool_results(msgs, 3, budget=40_000)
    assert tools[0]["content"].startswith("[CLEARED FROM CONTEXT: this tool call SUCCEEDED")
    assert tools[1]["content"].startswith("[CLEARED FROM CONTEXT: this tool call FAILED") and "Never present" in tools[1]["content"]
    assert tools[2]["content"] == "R2:" + "x" * 150  # markøren ville være længere end resultatet
    assert rw._looks_like_error('{"ok": false}') and rw._looks_like_error('{"status":"error"') and not rw._looks_like_error("[]")


def test_tool_context_budget_from_env(monkeypatch):
    monkeypatch.setenv("VT_TOOL_CONTEXT_CHARS", "12000")
    assert rw._tool_context_budget() == 12_000
    monkeypatch.setenv("VT_TOOL_CONTEXT_CHARS", "10")
    assert rw._tool_context_budget() == 5_000
    monkeypatch.setenv("VT_TOOL_CONTEXT_CHARS", "mange")
    assert rw._tool_context_budget() == rw.VT_TOOL_CONTEXT_CHARS


@pytest.mark.parametrize("model,omit", [("claude-sonnet-5", True), ("claude-opus-5-5", True), ("claude-haiku-5", True),
                                        ("claude-sonnet-4-5", False), ("claude-opus-4-1", False), ("gpt-5", False),
                                        ("claude-sonnet-50", False)])
def test_fifth_generation_claude_models_skip_temperature(model, omit):
    assert bool(rw._NO_TEMPERATURE.match(model)) is omit


def test_stream_retry_plan_defaults_and_env(monkeypatch):
    assert rw._stream_retry_plan() == (4, [5.0, 10.0, 20.0, 40.0])
    monkeypatch.setenv("VT_STREAM_RETRIES", "6")
    assert rw._stream_retry_plan() == (6, [5.0, 10.0, 20.0, 40.0, 60.0, 60.0])
    monkeypatch.setenv("VT_STREAM_RETRIES", "tolv")
    assert rw._stream_retry_plan()[0] == 4
    monkeypatch.setenv("VT_STREAM_RETRIES", "0")
    assert rw._stream_retry_plan() == (0, [])


@pytest.mark.skipif(importlib.util.find_spec("src") is None, reason="Vibe-Trading er ikke installeret")
def test_stream_retries_ride_out_overload_but_not_client_errors(monkeypatch):
    from src.providers import chat as c

    calls: list[str] = []
    slept: list[float] = []
    monkeypatch.setattr(rw.time, "sleep", lambda s: slept.append(s))

    class Boom(Exception):
        def __init__(self, code):
            super().__init__(f"Error code: {code}")
            self.status_code = code

    def fake_stream(self, messages, *a, **k):
        calls.append(messages)
        if len(calls) <= 2:
            raise c.ProviderStreamError(provider="anthropic", model="m", original=Boom(529))
        return "svar"

    monkeypatch.setattr(c.ChatLLM, "stream_chat", fake_stream, raising=False)
    assert rw._patch_stream_retries() == "ok (4 ekstra forsøg)"
    assert rw._patch_stream_retries() == "ok (allerede anvendt)"
    llm = c.ChatLLM.__new__(c.ChatLLM)
    assert c.ChatLLM.stream_chat(llm, "m1", should_cancel=lambda: False) == "svar"
    assert len(calls) == 3 and slept == [5.0, 10.0]  # 529 → to forsøg mere, stigende ventetid

    calls.clear(); slept.clear()

    def fail_400(self, messages, *a, **k):
        calls.append(messages)
        raise c.ProviderStreamError(provider="anthropic", model="m", original=Boom(400))

    c.ChatLLM._vibetrad_orig_stream_chat = fail_400
    monkeypatch.setattr(c.ChatLLM, "stream_chat", fail_400, raising=False)
    assert rw._patch_stream_retries() == "ok (4 ekstra forsøg)"
    with pytest.raises(c.ProviderStreamError):
        c.ChatLLM.stream_chat(llm, "m2")
    assert len(calls) == 1 and slept == []  # 400 er endelig: ingen gentagelse

    calls.clear()

    def fail_always(self, messages, *a, **k):
        calls.append(messages)
        raise c.ProviderStreamError(provider="anthropic", model="m", original=Boom(529))

    monkeypatch.setattr(c.ChatLLM, "stream_chat", fail_always, raising=False)
    rw._patch_stream_retries()
    with pytest.raises(c.ProviderStreamError):
        c.ChatLLM.stream_chat(llm, "m3")
    assert len(calls) == 5 and slept == [5.0, 10.0, 20.0, 40.0]  # 1 + 4 forsøg, så gives fejlen videre


@pytest.mark.skipif(importlib.util.find_spec("src") is None, reason="Vibe-Trading er ikke installeret")
def test_deliverable_contract_accepts_synthesis_without_tool_calls():
    # 19/10/2026 aften: strateg, redaktør, dommer og attribution-agenten svarede ud fra upstream + facit uden
    # værktøjskald og blev afvist ("output contract not met: data agent produced no tool calls and no report.md"),
    # så fire af seks teams faldt. Uden filværktøjer findes report.md ikke.
    from src.swarm import worker as w

    assert rw._patch_deliverable_contract() == "ok"
    assert rw._patch_deliverable_contract() == "ok (allerede anvendt)"
    report = "## Konklusion\n\nVaR 95 % er 4,3 %. | a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
    assert w._classify_deliverable(report, is_data_agent=True, report_written=False, data_tool_calls=0) is None
    # Vibe-Tradings øvrige kontrol gælder stadig
    assert w._classify_deliverable("", is_data_agent=True, report_written=False, data_tool_calls=0) == "empty deliverable"
    assert "fabricated" in w._classify_deliverable("Tal baseret på mock data: 1, 2, 3 " * 5, is_data_agent=True,
                                                   report_written=False, data_tool_calls=3)
    # og agenten i _run_worker_impl bruger den udskiftede funktion (modul-global)
    assert w._run_worker_impl.__globals__["_classify_deliverable"] is w._classify_deliverable


def test_patches_degrade_without_vibe_trading(monkeypatch):
    import importlib.util

    if importlib.util.find_spec("src") is not None:
        pytest.skip("Vibe-Trading er installeret: se integrationstesten")
    monkeypatch.setenv("LANGCHAIN_MODEL_NAME", "claude-sonnet-5")
    st = rw.patch_vibe_trading()
    assert st["tool_context"].startswith("ikke anvendt") and st["temperature"].startswith("ikke anvendt")
    monkeypatch.setenv("VT_TEMPERATURE", "on")
    assert rw._patch_temperature() == "ikke ændret"


def test_patched_vibe_trading_worker_keeps_tool_results(tmp_path, monkeypatch):
    """Integration: den rigtige ReAct-løkke i Vibe-Trading med en falsk LLM (køres, hvor Vibe-Trading findes)."""
    w = pytest.importorskip("src.swarm.worker")
    from src.agent.tools import BaseTool, ToolRegistry
    from src.providers.chat import LLMResponse, ToolCallRequest
    from src.swarm.models import SwarmAgentSpec, SwarmTask

    monkeypatch.setenv("LANGCHAIN_MODEL_NAME", "claude-sonnet-5")
    st = rw.patch_vibe_trading()
    assert st["tool_context"].startswith("ok") and "_vibetrad_compact_tool_results" in w._run_worker_impl.__code__.co_names
    import dis
    import inspect

    src = Path(inspect.getsourcefile(w._run_worker_impl)).read_text("utf-8").splitlines()
    block = next(i + 1 for i, line in enumerate(src) if 'tool_msgs = [m for m in messages if m.get("role") == "tool"]' in line)
    calls = [i.positions.lineno for i in dis.get_instructions(w._run_worker_impl)
             if i.argval == "_vibetrad_compact_tool_results"]
    assert calls == [block]  # tracebacks peger stadig på de rigtige linjer i worker.py
    from src.providers import llm

    assert "claude-sonnet-5" in llm._ANTHROPIC_TEMPERATURE_UNSUPPORTED

    class Prices(BaseTool):
        name, description = "get_market_data", "fake prices"
        parameters = {"type": "object", "properties": {"i": {"type": "integer"}}}

        def execute(self, **kw):
            return f"R{kw.get('i')}:" + "x" * 3000

    def registry(*a, **k):
        r = ToolRegistry()
        r.register(Prices())
        return r

    seen: list[list[str]] = []

    class FakeLLM:
        model_name = "fake"

        def stream_chat(self, messages, tools=None, timeout=None, on_text_chunk=None, **kw):
            done = [m["content"] for m in messages if m.get("role") == "tool"]
            seen.append(done)
            if len(done) < 8:
                return LLMResponse(content="", finish_reason="tool_calls", tool_calls=[
                    ToolCallRequest(id=f"c{len(done)}", name="get_market_data", arguments={"i": len(done)})])
            return LLMResponse(content="## Resultat\n" + "Rapport med alle tal. " * 5, finish_reason="stop")

    monkeypatch.setattr(w, "build_swarm_registry", registry)
    res = w._run_worker_impl(SwarmAgentSpec(id="a", role="analyst", system_prompt="Analyse.", tools=["get_market_data"]),
                             SwarmTask(id="t", agent_id="a", prompt_template="Go"), {}, {}, tmp_path, llm=FakeLLM())
    assert str(res.status).endswith("completed")
    assert [t[:3] for t in seen[-1]] == [f"R{i}:" for i in range(8)]  # 0.1.16 uden rettelsen: 5 × "[cleared]"


# --- Ledig kontant (kørslen 19/10/2026 fordelte "kontanten", selv om det meste var reserveret) ---------------

@pytest.mark.parametrize("note,want", [
    ("Heraf 2 400 SEK reserveret til åben limitordre: Demo Pharma B, 5 stk à 318,40 DKK", 2400.0),
    ("Reserveret til ordre: 1 250 SEK", 1250.0), ("2.400 kr. er bundet i ordren", 2400.0),
    ("500 SEK ledig, 700 SEK reserveret", 700.0), ("reserveret til ordre; 212 SEK ledig", 0.0),
    ("Heraf 1 640,50 SEK reserveret", 1640.5), ("afsat 300 kr til gebyr", 300.0),
    ("Indskud 15 000 SEK den 25.; 1 000 SEK reserveret", 1000.0), ("ordre på 3 stk à 252 DKK", 0.0), ("", 0.0),
    # nægtelser, punktum som decimaltegn, "ca.", gentagelser, engelsk og svensk
    ("Ordren er gennemført; 1 640 SEK er ikke længere reserveret", 0.0), ("Intet reserveret – 2 615 SEK ledig", 0.0),
    ("Ikke reserveret: 2 615 SEK", 0.0), ("Heraf 1640.50 SEK reserveret", 1640.5), ("(reserveret ca. 450 kr)", 450.0),
    ("Heraf 1 640 SEK reserveret (1 640 SEK reserveret til ordren)", 1640.0), ("Reserved 500 SEK for an order", 500.0),
    ("500 kr spärrat för order", 500.0), ("ikke en del af ordren; 700 SEK reserveret", 700.0),
])
def test_reserved_cash_is_read_from_the_note(note, want):
    assert rw.reserved_from_note(note) == want


def test_free_cash_in_portfolio_text_scope_and_brief():
    base = {"holdings": [{"ticker": "AAA.US", "name": "Aaa", "qty": 1, "cost_sek": 1000}], "cash_sek": 2615,
            "cash_note": "Heraf 1 640 SEK reserveret til åben limitordre", "monthly_deposit_sek": 15000}
    p = rw.load_portfolio(json.dumps(base))
    assert p["cash_reserved_sek"] == 1640 and rw.free_cash(p) == 975
    assert "Ledig til nye køb: 975 SEK (1 640 SEK er reserveret og kan ikke bruges)" in rw.portfolio_text(p)
    ref = sto("2026-10-17T08:00")  # 3. lørdag: fondsvalg
    plan = rw.build_plan(p, ref, rw.week_window(ref), reports=[])
    goal = next(s for s in plan if s["preset"] == "fund_selection_panel")["vars"]["goal"]
    assert "nye indskud (ca. 15 000 SEK om måneden) og den ledige kontant (975 SEK; resten er reserveret)" in goal
    # Et eksplicit tal vinder over noten, og det kan ikke overstige kontanten
    p2 = rw.load_portfolio(json.dumps(dict(base, cash_reserved_sek=500)))
    assert p2["cash_reserved_sek"] == 500 and rw.free_cash(p2) == 2115
    p3 = rw.load_portfolio(json.dumps(dict(base, cash_reserved_sek=99999)))
    assert p3["cash_reserved_sek"] == 2615 and rw.free_cash(p3) == 0
    p4 = rw.load_portfolio(json.dumps(dict(base, cash_reserved_sek=True, cash_note="")))
    assert p4["cash_reserved_sek"] == 0
    assert rw.load_portfolio(json.dumps(dict(base, cash_reserved_sek="800")))["cash_reserved_sek"] == 800
    assert rw.load_portfolio(json.dumps(dict(base, cash_reserved_sek="mange")))["cash_reserved_sek"] == 1640  # noten
    # Uden reservation: kontanten med beløb
    p5 = rw.load_portfolio(json.dumps(dict(base, cash_note="")))
    goal5 = next(s for s in rw.build_plan(p5, ref, rw.week_window(ref), reports=[])
                 if s["preset"] == "fund_selection_panel")["vars"]["goal"]
    assert "og kontanten (2 615 SEK)" in goal5 and "Ledig til nye køb" not in rw.portfolio_text(p5)
    # Datatjekkets facit viser også den ledige del
    brief = dtj.agent_brief({"ref_date": "2026-10-16", "status": "PASS", "instruments": [], "earnings": [], "fx": [],
                             "sources": {"vibe_trading": {"available": False}, "eodhd": {"available": False}},
                             "portfolio": {"cash_sek": 2615, "cash_reserved_sek": 1640, "total_sek": 3615}})
    assert "Kontant 2 615 SEK (heraf 1 640 SEK reserveret, 975 SEK ledig)" in brief


# --- Instrukser pr. trin og CIO-digest (19/10/2026) -------------------------------------------------------

def test_step_preset_fills_placeholders_and_gives_cio_the_digest(tmp_path):
    import yaml
    (tmp_path / "portfolio_review_board.yaml").write_text(yaml.safe_dump({"name": "portfolio_review_board", "agents": [
        {"id": "attribution_analyst", "system_prompt": "Portefølje: {portfolio}. Periode {review_period}. {upstream_context} {\"json\": 1}", "tools": []},
        {"id": "chief_investment_officer", "system_prompt": "CIO {goal}", "tools": []}]}))
    assert rw.prepare_step_preset(tmp_path, "portfolio_review_board", {"portfolio": "AAA 3 stk", "goal": "Mål", "n": 5},
                                  {"chief_investment_officer": "## Konklusioner\nDIGEST"})
    a, cio = yaml.safe_load((tmp_path / "portfolio_review_board.yaml").read_text())["agents"]
    assert a["system_prompt"] == "Portefølje: AAA 3 stk. Periode {review_period}. {upstream_context} {\"json\": 1}"
    assert cio["system_prompt"] == "CIO Mål\n\n## Konklusioner\nDIGEST\n" and "DIGEST" not in a["system_prompt"]
    assert rw.prepare_step_preset(tmp_path, "findes_ikke", {}) is False


def test_cio_digest_counts_teams_and_names_missing_ones():
    runs = [{"preset": "risk_committee", "label": "Risikoudvalg", "status": "completed", "final_report": "## Konklusion\nA " * 50, "tasks": []},
            {"preset": "earnings_research_desk", "label": "Regnskabsdesk", "status": "completed", "final_report": "## Konklusion\nB " * 50,
             "tasks": [{"role": "Analytiker 2", "report": "plan_only"}]},
            {"preset": "sentiment_intelligence_team", "label": "Sentiment", "status": "timeout", "tasks": []},
            {"preset": "fund_selection_panel", "label": "Fondsvalg", "status": "skipped", "skip_reason": "budget", "tasks": []}]
    d = rw.cio_digest(runs)
    assert "2 team(s) med rapport (Risikoudvalg, Regnskabsdesk); 2 uden rapport (Sentiment, Fondsvalg)" in d
    assert "Agenter uden rapport i teamet: Analytiker 2" in d and "### Fondsvalg: ingen rapport (skipped: budget)" in d
    assert rw.cio_goal_with_inputs("MÅL", runs).startswith("MÅL\n\n## Konklusioner") and rw.cio_digest([]) == ""


def test_cio_digest_passes_on_partial_reports_from_a_failed_team():
    # Vibe-Trading markerer hele teamet "failed", når én agent fejler, og synteseagenten blokeres; de
    # færdige agenters rapporter må ikke gå tabt for CIO'en.
    fundamentals = "## Konklusion\nOmsætningen voksede 40 % " * 40
    runs = [{"preset": "risk_committee", "label": "Risikoudvalg", "status": "completed", "final_report": "## Konklusion\nA " * 50, "tasks": []},
            {"preset": "earnings_research_desk", "label": "Regnskabsdesk", "status": "failed", "final_report": None, "tasks": [
                {"role": "Fundamental Analyst", "status": "completed", "summary": fundamentals, "report": "ok"},
                {"role": "Revision Tracker", "status": "completed", "summary": "## Plan\nExecuting now.", "report": "plan_only"},
                {"role": "Event Options Analyst", "status": "failed", "summary": "", "report": None,
                 "error": "LLM call failed at iteration 7: Error code: 400 - prompt is too long for ZZTOP.US"},
                {"role": "Earnings Strategist", "status": "blocked", "summary": None, "report": None,
                 "error": "Blocked: upstream not completed (task-options-event=failed)"}]},
            {"preset": "sentiment_intelligence_team", "label": "Sentiment", "status": "failed", "tasks": [
                {"role": "A", "status": "failed", "summary": "", "report": None, "error": "x"}]},
            {"preset": "fund_selection_panel", "label": "Fondsvalg", "status": "skipped", "skip_reason": "budget",
             "tasks": [{"role": "B", "status": "completed", "summary": "## Konklusion\nskal ikke tælle", "report": "ok"}]}]
    d = rw.cio_digest(runs)
    assert ("1 team(s) med rapport (Risikoudvalg); 1 med delrapport fra enkelte agenter (Regnskabsdesk); "
            "2 uden rapport (Sentiment, Fondsvalg)") in d
    assert "### Regnskabsdesk: delrapport (failed; ingen samlet rapport, fejlet/blokeret: Event Options Analyst, Earnings Strategist)" in d
    assert "#### Fundamental Analyst" in d and "Omsætningen voksede 40 %" in d
    assert "Revision Tracker" not in d.split("### Regnskabsdesk")[1].split("###")[0] or "Executing now" not in d  # planer gives ikke videre
    assert "ZZTOP" not in d and "skal ikke tælle" not in d
    assert "### Sentiment: ingen rapport (failed)" in d and "### Fondsvalg: ingen rapport (skipped: budget)" in d


def test_error_kind_names_the_type_but_never_the_text():
    cases = {
        "Blocked: upstream not completed (task-options-event=failed)": "blokeret af en fejlet agent",
        "LLM call failed at iteration 7: Error code: 400 - {'message': 'prompt is too long: 213000 tokens for ZZTOP.US'}":
            "LLM-kald fejlede i iteration 7 (konteksten for lang)",
        "LLM call failed at iteration 3: Error code: 529 - overloaded_error": "LLM-kald fejlede i iteration 3 (overbelastning/rate limit)",
        "LLM call failed at iteration 5: Error code: 400 - invalid tool_use for ZZTOP": "LLM-kald fejlede i iteration 5 (HTTP 400)",
        "LLM call failed at iteration 12: Connection error.": "LLM-kald fejlede i iteration 12",
        "Failed to render prompt template: Single '}' encountered": "prompt-skabelon kunne ikke udfyldes",
        "Worker for task x exceeded layer deadline (3600s)": "tidsgrænse",
        "output contract not met: data agent produced no tool calls and no report.md":
            "svar uden værktøjskald afvist af Vibe-Trading (leverancekontrakt)",
        "output contract not met: plan-only stub (no executed analysis / conclusion)": "kun en plan (leverancekontrakt)",
        "output contract not met: raw tool-result envelope, not analysis": "tomt eller ubrugeligt svar (leverancekontrakt)",
        "ValueError: no symbol has at least 30 valid bars for ZZTOP.US": "anden fejl",
        None: "anden fejl",
    }
    for err, want in cases.items():
        got = rw._error_kind(err)
        assert got == want, (err, got)
        assert "ZZTOP" not in got


def test_dry_run_cio_gets_digest_in_goal(tmp_path):
    p = run_main(tmp_path, "--event", "workflow_dispatch", "--now", "2026-10-17T09:00")
    assert p.returncode == 0, p.stderr
    data = rw.decrypt(json.loads((tmp_path / "results" / "latest.enc.json").read_text()), PASS)
    cio = next(r for r in data["runs"] if r["preset"] == "portfolio_review_board")
    assert cio["status"] == "completed"


@pytest.mark.skipif(importlib.util.find_spec("src") is None, reason="Vibe-Trading er ikke installeret")
def test_vibe_trading_prompt_and_grounding_patches(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_MODEL_NAME", "claude-sonnet-5")
    st = rw.patch_vibe_trading()
    assert st["prompt"].startswith("ok") and st["grounding"].startswith("ok")
    from src.swarm import grounding as g, worker as w
    from src.swarm.models import SwarmAgentSpec

    text = w.build_worker_prompt(SwarmAgentSpec(id="a", role="x", system_prompt="Hej", tools=["get_market_data"]), {}, "", "")
    assert "HARD LIMIT" not in text and "state your plan" not in text and "never finish with a plan" in text
    # kun symboler, der står ordret i teksten: ingen SEK.US, ISK.US, NOVO.US, PASS.US
    assert g.extract_symbols_from_user_vars({"g": "ISK i SEK: NOVO-B.CO, ACWI.US, GOOGL.US. Datatjek PASS."}) == ["ACWI.US", "GOOGL.US"]


def test_looks_cut_catches_reports_that_end_mid_sentence():
    body = "## Resumé\nTekst. " * 200
    assert rw.looks_cut(body + "\nINVE-B.ST-kursen (420,95 SEK) er hentet direkte fra E")
    assert rw.looks_cut(body + "\n| Guld (fysisk ETC) – GLD.US | 29")
    assert not rw.looks_cut(body + "\nFaktatjek: PASS")
    assert not rw.looks_cut(body + "\nDer er ikke afgivet ordrer.")
    assert not rw.looks_cut(body + "\n| Kontant | | | kontant | 5 | | |")
    assert not rw.looks_cut("kort tekst uden punktum") and not rw.looks_cut(None)


def test_summary_and_digest_flag_cut_reports():
    from types import SimpleNamespace as NS
    long_ok = "## Konklusion\n" + "Fint. " * 500 + "\nFaktatjek: PASS"
    long_cut = "## Konklusion\n" + "Fint. " * 500 + "\nhentet direkte fra E"
    run = NS(id="r1", status="completed", total_input_tokens=1, total_output_tokens=1, final_report=long_cut,
             agents=[NS(id="a", role="Risk Budgeter"), NS(id="b", role="Macro")],
             tasks=[NS(id="t1", agent_id="a", status="completed", summary=long_cut, error=None),
                    NS(id="t2", agent_id="b", status="completed", summary=long_ok, error=None)])
    s = rw.summarize_run(run, "etf_allocation_desk", "x", rw.now_sto("2026-10-20T12:00"), 1, price_in=2, price_out=10)
    assert s["agents_cut"] == ["Risk Budgeter"] and s["final_report_cut"] and s["tasks"][1]["cut"] is False
    d = rw.cio_digest([s])
    assert "Afkortet midt i teksten – resten mangler: Risk Budgeter og teamets slutrapport" in d
