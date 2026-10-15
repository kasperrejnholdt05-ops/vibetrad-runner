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
def test_week_window_saturday():
    w = rw.week_window(sto("2026-10-17T08:00"))
    assert w == {"id": "2026-W42", "from": "2026-10-12", "to": "2026-10-16", "label": "Uge 42 · 12/10/2026–16/10/2026"}


def test_week_window_midweek_points_to_last_full_week():
    assert rw.week_window(sto("2026-10-14T12:00"))["id"] == "2026-W41"
    assert rw.week_window(sto("2026-10-16T15:00"))["id"] == "2026-W41"  # fredag før lukning


@pytest.mark.parametrize("utc,expect", [
    ("2026-10-17T06:00", True),    # sommertid: 06 UTC = 08 Stockholm
    ("2026-10-31T06:00", False),   # vintertid: 06 UTC = 07 Stockholm
    ("2026-10-31T07:00", True),    # vintertid: 07 UTC = 08 Stockholm
    ("2026-10-17T05:59", False),
    ("2026-10-16T06:00", False),   # fredag
])
def test_should_run_handles_dst(utc, expect):
    ref = dt.datetime.fromisoformat(utc).replace(tzinfo=UTC).astimezone(rw.STO)
    assert rw.should_run(ref, None, "schedule", False)[0] is expect


def test_should_run_skips_done_week_but_retries_failed():
    ref = sto("2026-10-17T09:30")
    assert rw.should_run(ref, {"week_id": "2026-W42", "status": "ok"}, "schedule", False)[0] is False
    assert rw.should_run(ref, {"week_id": "2026-W42", "status": "partial"}, "schedule", False)[0] is False
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
def test_plan_weekly_only_mid_month():
    p = rw.load_portfolio(json.dumps(PORTFOLIO))
    ref = sto("2026-10-17T08:00")
    plan = rw.build_plan(p, ref, rw.week_window(ref), reports=[])
    assert [s["preset"] for s in plan] == ["portfolio_review_board"]
    v = plan[0]["vars"]
    assert set(v) == {"portfolio", "review_period", "goal"} and "dansk" in v["goal"]
    assert "12/10/2026–16/10/2026" in v["review_period"]


def test_plan_first_saturday_and_earnings():
    p = rw.load_portfolio(json.dumps(PORTFOLIO))
    ref = sto("2026-10-03T08:00")
    reps = rw.upcoming_reports(p, sto("2026-10-17T08:00"), lookup=None)
    assert reps == [{"ticker": "QQQX.US", "name": "Hemmelig Soeg", "date": "2026-10-28", "source": "portefølje"}]
    plan = rw.build_plan(p, ref, rw.week_window(ref), reports=reps)
    assert [s["preset"] for s in plan] == ["portfolio_review_board", "risk_committee", "earnings_research_desk"]
    assert "Hemmelig Raket" in plan[1]["vars"]["goal"]  # største position stresstestes
    assert plan[2]["vars"]["target"].startswith("QQQX.US ")
    assert len(rw.build_plan(p, ref, rw.week_window(ref), reports=reps, max_swarms=2)) == 2


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
    assert s["tasks"][0]["role"] == "Analytiker 1" and s["label"] == "Porteføljegennemgang"


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
    env = {**os.environ, "VT_PORTFOLIO": json.dumps(PORTFOLIO), "VT_RESULTS_PASSPHRASE": PASS,
           "VT_PBKDF2_ITER": "1000", "GITHUB_OUTPUT": str(tmp_path / "out.txt"), **env_over}
    env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.run([sys.executable, str(ROOT / "runner" / "run_week.py"), "--results", str(tmp_path / "results"),
                           "--dry-run", *extra], capture_output=True, text=True, env=env, timeout=120)


def outputs(tmp_path):
    return dict(line.split("=", 1) for line in (tmp_path / "out.txt").read_text().split())


def test_end_to_end_dry_run_first_saturday(tmp_path):
    p = run_main(tmp_path, "--event", "schedule", "--now", "2026-10-03T08:01")
    assert p.returncode == 0, p.stderr
    log = p.stdout + p.stderr
    for secret in ("ZZTOP", "QQQX", "Hemmelig", "21480", "21 480", "reserveret"):
        assert secret not in log, f"{secret!r} lækket til loggen"
    o = outputs(tmp_path)
    assert o == {"ran": "true", "status": "ok", "week": "2026-W40"}
    res = tmp_path / "results"
    env = json.loads((res / "latest.enc.json").read_text())
    assert (res / "weeks" / "2026-W40.enc.json").exists()
    assert json.loads((res / "index.json").read_text())["latest"] == "2026-W40"
    for f in res.rglob("*.json"):
        assert "Hemmelig" not in f.read_text() and "ZZTOP" not in f.read_text()
    data = rw.decrypt(env, PASS)
    assert [r["preset"] for r in data["runs"]] == ["portfolio_review_board", "risk_committee"]
    assert data["status"] == "ok" and data["portfolio"]["holdings"][0]["name"] == "Hemmelig Raket"
    assert env["public"]["status"] == "ok" and env["public"]["completed"] == 2


def test_end_to_end_skip_then_partial(tmp_path):
    assert run_main(tmp_path, "--event", "schedule", "--now", "2026-10-17T08:05").returncode == 0
    p = run_main(tmp_path, "--event", "schedule", "--now", "2026-10-17T09:35")
    assert "allerede kørt" in p.stdout and outputs(tmp_path)["ran"] == "false"
    p = run_main(tmp_path, "--event", "workflow_dispatch", "--now", "2026-10-17T10:00", "--force-risk",
                 VT_DRY_FAIL="risk_committee")
    assert outputs(tmp_path)["status"] == "partial"
    idx = json.loads((tmp_path / "results" / "index.json").read_text())
    assert len(idx["weeks"]) == 1 and idx["weeks"][0]["status"] == "partial"


def test_gate_only_needs_no_secrets(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VT_", "ANTHROPIC"))}
    env["GITHUB_OUTPUT"] = str(tmp_path / "out.txt")
    p = subprocess.run([sys.executable, str(ROOT / "runner" / "run_week.py"), "--results", str(tmp_path / "r"),
                        "--gate-only", "--event", "schedule", "--now", "2026-10-17T08:00"],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0 and outputs(tmp_path)["ran"] == "true"


def test_missing_secrets_fail_clearly(tmp_path):
    p = run_main(tmp_path, "--event", "workflow_dispatch", VT_RESULTS_PASSPHRASE="kort")
    assert p.returncode == 2 and "VT_RESULTS_PASSPHRASE" in p.stdout
    p = run_main(tmp_path, "--event", "workflow_dispatch", VT_PORTFOLIO="{ikke json")
    assert p.returncode == 2 and "ikke gyldig JSON" in p.stdout and "ikke json" not in p.stdout
