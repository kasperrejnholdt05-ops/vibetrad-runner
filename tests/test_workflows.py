"""Workflow-vagter: fast Ubuntu-version, Node 24-actions, og at en betalt kørsel aldrig går tabt."""
from __future__ import annotations

from pathlib import Path

import yaml

WF = Path(__file__).resolve().parents[1] / ".github" / "workflows"
NODE24 = {"actions/checkout": 5, "actions/setup-python": 6, "actions/setup-node": 5, "actions/upload-artifact": 6}


def _load(name):
    return yaml.safe_load((WF / name).read_text("utf-8"))


def _steps(wf):
    for job in wf["jobs"].values():
        yield job, job.get("steps", [])


def test_runners_pinned_and_actions_on_node24():
    for name in ("vibetrad-weekly.yml", "tests.yml"):
        for job, steps in _steps(_load(name)):
            assert job["runs-on"] == "ubuntu-24.04", name
            for st in steps:
                if "uses" in st:
                    action, ver = st["uses"].split("@")
                    assert action in NODE24 and int(ver.lstrip("v")) >= NODE24[action], (name, st["uses"])


def test_results_push_retries_and_backup_always_runs():
    (job, steps), = _steps(_load("vibetrad-weekly.yml"))
    by = {st.get("name"): st for st in steps}
    save = by["Gem krypteret resultat"]["run"]
    assert "for i in 1 2 3 4 5" in save and "git reset -q --hard origin/main" in save and "--merge-results" in save
    assert "git push -q origin HEAD:main && exit 0" in save and "x-access-token" in save
    assert by["Gem krypteret resultat"]["env"]["GH_PUSH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    checkout = next(st for st in steps if str(st.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"] == {"ref": "main", "persist-credentials": False}
    assert job["env"]["VIBE_TRADING_SEARCH_BING_FALLBACK"] == "0"
    backup = by["Backup af krypteret resultat"]
    assert backup["uses"].startswith("actions/upload-artifact@") and backup["if"].startswith("always()")
    assert "results/latest.enc.json" in backup["with"]["path"] and backup["with"]["retention-days"] == 14
    names = [st.get("name") for st in steps]
    assert names.index("Backup af krypteret resultat") == names.index("Gem krypteret resultat") + 1


def test_secrets_reach_only_the_run_step():
    (job, steps), = _steps(_load("vibetrad-weekly.yml"))
    run = next(st for st in steps if st.get("name") == "Kør Vibe-Trading swarms")
    for key in ("ANTHROPIC_API_KEY", "VT_PORTFOLIO", "VT_RESULTS_PASSPHRASE", "EODHD_API_KEY", "FRED_API_KEY"):
        assert f"secrets.{key}" in run["env"][key]
        for st in steps:
            if st is not run:
                assert f"secrets.{key}" not in yaml.safe_dump(st), (key, st.get("name"))
    # GitHubs eget token kun i push-trinnet (checkout gemmer det ikke)
    for st in steps:
        if st.get("name") != "Gem krypteret resultat":
            assert "secrets.GITHUB_TOKEN" not in yaml.safe_dump(st), st.get("name")
    assert "secrets." not in yaml.safe_dump(job.get("env", {}))


def test_merge_results_is_idempotent_and_keeps_the_newest_latest(tmp_path):
    import json
    import subprocess
    import sys
    sys.path.insert(0, str(WF.parents[1] / "runner"))
    import run_week as rw

    results = tmp_path / "results"
    pub_a = {"run_id": "2026-W42_a", "week_id": "2026-W42", "generated_at": "2026-10-17T09:00:00+02:00", "cost_usd": 12.5,
             "status": "ok", "dry_run": False, "runs": 2, "completed": 2}
    pub_b = dict(pub_a, run_id="2026-W42_b", generated_at="2026-10-17T11:00:00+02:00", cost_usd=3.0)
    env_a = rw.encrypt({"week": {"id": "2026-W42"}}, "p" * 20, pub_a, iterations=1000)
    env_b = rw.encrypt({"week": {"id": "2026-W42"}}, "p" * 20, pub_b, iterations=1000)
    rw.write_results(results, env_b)  # den nyere kørsel ligger allerede på main
    f = tmp_path / "kuvert.enc.json"
    f.write_text(json.dumps(env_a))
    for _ in range(2):  # to gange: index og ledger må ikke få dubletter
        out = subprocess.run([sys.executable, str(WF.parents[1] / "runner" / "run_week.py"), "--results", str(results),
                              "--merge-results", str(f)], capture_output=True, text=True)
        assert out.returncode == 0 and "lagt ind" in out.stdout, out.stdout + out.stderr
    idx = json.loads((results / "index.json").read_text())
    led = json.loads((results / "ledger.json").read_text())
    assert [w["run_id"] for w in idx["weeks"]] == ["2026-W42_b", "2026-W42_a"] and idx["latest"] == "2026-W42_b"
    assert sorted(e["run_id"] for e in led["runs"]) == ["2026-W42_a", "2026-W42_b"]
    assert json.loads((results / "latest.enc.json").read_text())["public"]["run_id"] == "2026-W42_b"
    assert (results / "weeks" / "2026-W42_a.enc.json").exists()
    assert rw.month_spent(results, rw.now_sto("2026-10-18T12:00")) == 15.5


def test_canary_has_its_own_queue_and_paid_runs_never_cancel_each_other():
    """21/10/2026: den natlige kanariefugl ventede bag en ugerapport og blev annulleret med den – den har aldrig
    nået at skrive results/canary.json. Den har nu sin egen kø; ugerapporterne afbryder aldrig hinanden."""
    wf = _load("vibetrad-weekly.yml")
    c = wf["concurrency"]
    assert c["cancel-in-progress"] is False
    g = c["group"]
    assert "'vibetrad-canary'" in g and "'vibetrad-weekly'" in g
    expr = "(github.event_name == 'schedule' && endsWith(github.event.schedule, ' * * *'))"
    assert "inputs.canary" in g and expr in g
    on = wf.get("on", wf.get(True))
    crons = [c["cron"] for c in on["schedule"]]
    nightly = [c for c in crons if c.endswith(" * * *")]
    weekly = [c for c in crons if not c.endswith(" * * *")]
    # flere natlige fyringer (GitHub dropper cron), alle før sync-opgaven kl. 07:40 svensk tid (05:40 UTC sommertid)
    assert len(nightly) >= 3 and all(int(c.split()[1]) * 60 + int(c.split()[0]) < 5 * 60 + 40 for c in nightly)
    # lørdagskørslerne må aldrig blive til kanariefugle
    assert weekly and all(c.endswith(" * * 6") for c in weekly)
    env = wf["jobs"][next(iter(wf["jobs"]))]["env"]
    assert expr in env["VT_CANARY"] and expr in env["VT_DRY_RUN"]
