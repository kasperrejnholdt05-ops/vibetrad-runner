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
    assert "for i in 1 2 3 4 5" in save and "git pull -q --rebase origin main" in save and "git rebase --abort" in save
    assert "git push -q origin HEAD:main && exit 0" in save
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
    assert "secrets." not in yaml.safe_dump(job.get("env", {}))
