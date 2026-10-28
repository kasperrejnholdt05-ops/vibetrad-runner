"""Workflow-vagter: fast Ubuntu, Node 24-actions, vagthundens tider, at resultatet ikke går tabt, og ingen LLM."""
from __future__ import annotations

from pathlib import Path

import yaml

WF = Path(__file__).resolve().parents[1] / ".github" / "workflows"
NODE24 = {"actions/checkout": 5, "actions/setup-python": 6, "actions/setup-node": 5}


def _load(name):
    return yaml.safe_load((WF / name).read_text("utf-8"))


def test_only_the_known_workflows_exist():
    assert sorted(p.name for p in WF.glob("*.yml")) == ["analyse.yml", "tests.yml", "vagt.yml"]


def test_runners_pinned_and_actions_on_node24():
    for name in ("vagt.yml", "tests.yml", "analyse.yml"):
        for job in _load(name)["jobs"].values():
            assert job["runs-on"] == "ubuntu-24.04", name
            for st in job["steps"]:
                if "uses" in st:
                    action, ver = st["uses"].split("@")
                    assert action in NODE24 and int(ver.lstrip("v")) >= NODE24[action], (name, st["uses"])


def test_vagt_schedule_secrets_and_push():
    wf = _load("vagt.yml")
    on = wf.get("on") or wf.get(True)
    crons = [c["cron"] for c in on["schedule"]]
    assert crons == ["17 6 * * 1-5", "53 16 * * 1-5", "23 21 * * 1-5"] and "workflow_dispatch" in on
    (job,) = wf["jobs"].values()
    by = {st.get("name"): st for st in job["steps"]}
    assert set(by["Kør vagthunden"]["env"]) == {"EODHD_API_KEY", "FRED_API_KEY"}
    save = by["Gem resultat"]["run"]
    assert "for i in 1 2 3 4 5" in save and "--merge" in save and "git reset -q --hard origin/main" in save
    checkout = next(st for st in job["steps"] if str(st.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"] == {"ref": "main", "persist-credentials": False}
    text = (WF / "vagt.yml").read_text("utf-8")
    for gone in ("ANTHROPIC", "VT_PORTFOLIO", "VT_RESULTS_PASSPHRASE", "vibe-trading"):
        assert gone not in text


def test_results_are_saved_even_on_a_red_run_and_web_triggers_tests():
    (job,) = _load("vagt.yml")["jobs"].values()
    save = next(st for st in job["steps"] if st.get("name") == "Gem resultat")
    assert save.get("if") == "${{ !cancelled() }}"
    on = _load("tests.yml").get("on") or _load("tests.yml").get(True)
    assert "web/**" in on["push"]["paths"] and "runner/**" in on["push"]["paths"]


def test_analyse_is_manual_read_only_and_uses_only_the_two_keys():
    wf = _load("analyse.yml")
    on = wf.get("on") or wf.get(True)
    assert list(on) == ["workflow_dispatch"] and wf["permissions"] == {"contents": "read"}
    (job,) = wf["jobs"].values()
    run = next(st for st in job["steps"] if st.get("name") == "Kør analysen")
    assert set(run["env"]) == {"EODHD_API_KEY", "FRED_API_KEY"} and "runner/analyse.py" in run["run"]
    checkout = next(st for st in job["steps"] if str(st.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"] == {"persist-credentials": False}
    text = (WF / "analyse.yml").read_text("utf-8")
    for gone in ("ANTHROPIC", "VT_PORTFOLIO", "git push", "contents: write"):
        assert gone not in text
