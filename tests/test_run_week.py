"""Tests for runner/run_week.py – kør med:  python -m pytest -q"""
from __future__ import annotations

import datetime as dt
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
    ref = sto("2026-11-07T08:00")  # 1. lørdag (ETF), uge 45 -> rotation 0 uden overvågningsliste = fundamental analyse
    plan = rw.build_plan(p, ref, rw.week_window(ref), reports=[])
    assert presets(plan) == ["risk_committee", "etf_allocation_desk", "fundamental_research_team", "portfolio_review_board"]
    assert plan[2]["vars"]["target"] in ("ZZTOP.US", "QQQX.US")
    assert presets(rw.build_plan(p, ref, rw.week_window(ref), plan_kind="kerne")) == ["risk_committee", "portfolio_review_board"]
    capped = rw.build_plan(p, ref, rw.week_window(ref), max_swarms=2)
    assert presets(capped) == ["risk_committee", "portfolio_review_board"]  # CIO bevares altid


def test_cio_gets_other_teams_conclusions():
    runs = [{"preset": "risk_committee", "label": "Risikoudvalg", "status": "completed", "final_report": "VaR 95 % = 4,1 %"},
            {"preset": "x", "label": "Fejlet", "status": "error", "final_report": None}]
    g = rw.cio_goal_with_inputs("Mål.", runs)
    assert g.startswith("Mål.") and "### Risikoudvalg" in g and "VaR 95 % = 4,1 %" in g and "Fejlet" not in g
    assert rw.cio_goal_with_inputs("Mål.", []) == "Mål."


def test_shellfree_presets_swap_bash_for_quant_tools(tmp_path):
    import yaml
    src = tmp_path / "src"
    src.mkdir()
    (src / "risk_committee.yaml").write_text(yaml.safe_dump({"name": "risk_committee", "agents": [
        {"id": "a", "system_prompt": "Analyse.", "tools": ["bash", "read_file", "get_market_data"]},
        {"id": "b", "system_prompt": "Samler.", "tools": ["read_file"]}]}))
    (src / "plain.yaml").write_text(yaml.safe_dump({"name": "plain", "agents": [{"id": "c", "system_prompt": "x", "tools": ["web_search"]}]}))
    changed = rw.prepare_shellfree_presets(tmp_path / "dst", src)
    assert changed == ["plain", "risk_committee"]  # plain får kun research-discipline på sidste agent
    spec = yaml.safe_load((tmp_path / "dst" / "risk_committee.yaml").read_text())
    a, b = spec["agents"]
    assert "bash" not in a["tools"] and {"quantlib_call", "portfolio_risk_xray"} <= set(a["tools"])
    assert a["tools"].count("get_market_data") == 1 and "Shell access is DISABLED" in a["system_prompt"]
    assert "Shell access" not in b["system_prompt"] and "bash" not in b["tools"]
    assert b["tools"] == ["read_file", "load_skill"] and "research-discipline" in b["skills"]


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

    run = rw.run_swarm(rt, rt, "risk_committee", {"goal": "x"}, timeout_s=10, poll_s=1, clock=clock, sleep=sleep)
    assert rt.cancelled and run.timed_out
    s = rw.summarize_run(run, "risk_committee", "test", rw.now_sto(), 10, price_in=3, price_out=15)
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
def test_browser_webcrypto_can_decrypt(tmp_path):
    pub = {"week_id": "2026-W42", "week_label": "Uge 42 · 12/10/2026–16/10/2026", "runs": 2, "dry_run": False,
           "presets": ["portfolio_review_board"], "status": "ok"}
    env = rw.encrypt({"week": {"id": "2026-W42"}, "tekst": "Æble"}, PASS, pub, iterations=2000)
    f = tmp_path / "env.json"
    f.write_text(json.dumps(env))
    out = subprocess.run(["node", str(ROOT / "tests" / "decrypt_webcrypto.mjs"), str(f), PASS],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"week": {"id": "2026-W42"}, "tekst": "Æble"}


# --- ende-til-ende (tørkørsel som subprocess, så GitHub-loggen kan inspiceres) --
def run_main(tmp_path, *extra, **env_over):
    # CI sætter GITHUB_RUN_ID, GITHUB_EVENT_NAME osv.; de må ikke smitte af på testene (run_id, trigger)
    base = {k: v for k, v in os.environ.items() if not k.startswith(("VT_", "GITHUB_", "ANTHROPIC"))}
    env = {**base, "VT_PORTFOLIO": json.dumps(PORTFOLIO), "VT_RESULTS_PASSPHRASE": PASS, "VT_SCHEDULE": "on",
           "VT_PBKDF2_ITER": "1000", "GITHUB_OUTPUT": str(tmp_path / "out.txt"), "EODHD_API_KEY": "",
           "EODHD_API_TOKEN": "", "FRED_API_KEY": "", "VT_DATATJEK_VT": "off", **env_over}
    env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.run([sys.executable, str(ROOT / "runner" / "run_week.py"), "--results", str(tmp_path / "results"),
                           "--dry-run", *extra], capture_output=True, text=True, env=env, timeout=120)


def outputs(tmp_path):
    return dict(line.split("=", 1) for line in (tmp_path / "out.txt").read_text().split())


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
                                                   "sentiment_intelligence_team", "portfolio_review_board"]
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
    assert "fund_selection_panel" in names and "technical_analysis_panel" in names
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
    assert cio["skills"][:1] == ["asset-allocation"] and {"thesis-tracker", "investor-lenses", "research-discipline"} <= set(cio["skills"])
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
    assert "private-company-research" in rw.EXTRA_SKILLS["portfolio_review_board"]["chief_investment_officer"]


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
    assert rates["system_prompt"].rstrip().endswith("| a | b |") and "Vibetrad datatjek" in lead["system_prompt"]
    (cio,) = yaml.safe_load((tmp_path / "dst" / "portfolio_review_board.yaml").read_text())["agents"]
    assert {"get_eodhd_data", "etf_holdings", "report_audit"} <= set(cio["tools"]) and "Datakvalitet" in cio["system_prompt"]
    assert len(cio["tools"]) == len(set(cio["tools"]))


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
    assert "FRED: ja, nøglen virker (7 af 7 serier, 7 kald · US 10-årig statsrente 4,12 % pr. 14/10/2026)" in log
    assert FRED_KEY not in log
    assert {path for path, _ in seen} == {"/series/observations"} and all(q["api_key"] == FRED_KEY for _, q in seen)
    assert seen[0][1]["observation_end"] == "2026-10-15"
    res = tmp_path / "results"
    data = rw.decrypt(json.loads((res / "latest.enc.json").read_text()), PASS)
    assert data["fred"]["ok"] is True and data["fred"]["series"][0]["value"] == 4.12
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
    for txt in ("−30 %", "−50 %", "USD/SEK −10 %", "amerikansk tech samlet −25 %", "Brug VaR kun som supplement", "kort kurshistorik"):
        assert txt in risk, txt
    # en goal fra VT_PORTFOLIO vinder stadig, og en ukendt politik bliver "limit"
    p2 = rw.load_portfolio(json.dumps(dict(PORTFOLIO, weight_policy="nonsens", goal="Egen tekst.")))
    assert p2["weight_policy"] == "limit" and "maks. vægt" in rw.portfolio_text(p2)
    plan2 = rw.build_plan(p2, sto("2026-10-17T08:00"), rw.week_window(sto("2026-10-17T08:00")), reports=[])
    assert next(s for s in plan2 if s["preset"] == "portfolio_review_board")["vars"]["goal"].startswith("Egen tekst.")
