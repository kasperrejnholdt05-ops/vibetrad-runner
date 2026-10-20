"""tools/vt_med_eodhd.py: web-UI/terminal/MCP i codespacet får ugekørslens Vibetrad-regler."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "runner"))
import vt_med_eodhd as vm  # noqa: E402

PF = {"holdings": [], "cash_sek": 1234.5, "monthly_deposit_sek": 1000,
      "mandate": {"horizon_years": 5, "max_drawdown_pct": 50, "universe": "frit", "single_stock_cap_pct": 20}}


def test_pop_opt_removes_flag_from_args():
    a = ["serve", "--port", "8899", "--portfolio-file", "~/p.json"]
    assert vm._pop_opt(a, "--portfolio-file") == "~/p.json" and a == ["serve", "--port", "8899"]
    b = ["serve", "--portfolio-file=/x.json"]
    assert vm._pop_opt(b, "--portfolio-file") == "/x.json" and b == ["serve"]
    assert vm._pop_opt(["serve"], "--portfolio-file") is None


def test_vanilla_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("VT_VANILLA", "1")
    r = vm.apply_vibetrad({}, dst=tmp_path)
    assert r["error"] == "slået fra med VT_VANILLA" and not list(tmp_path.iterdir())
    assert vm._rules_status(r).startswith("Vibetrad-regler: ikke anvendt")


def test_presets_rules_portfolio_and_reset(monkeypatch, tmp_path):
    pytest.importorskip("src.swarm.presets")
    monkeypatch.delenv("VT_VANILLA", raising=False)
    pf = tmp_path / "pf.json"
    pf.write_text(json.dumps(PF), "utf-8")
    dst = tmp_path / "presets"
    r = vm.apply_vibetrad({"tool": True, "key": True}, str(pf), dst=dst)
    assert r["error"] is None and r["portfolio"] is True and len(r["presets"]) >= 25
    assert "contract" in r["patches"] and "retries" in r["patches"]
    txt = (dst / "macro_strategy_forum.yaml").read_text("utf-8")
    assert txt.startswith(vm.MARK) and "Investor constraints (Vibetrad)" in txt and "Riksbank" in txt
    assert "Mandat: horisont 5 år" in txt and "Kontant: 1 234 SEK" in txt
    assert "- bash" not in txt  # ingen shell i browserens swarms
    st = vm._rules_status(r)
    assert "presets tilpasset" in st and "portefølje og mandat med" in st
    (dst / "egen.yaml").write_text("name: egen\n", "utf-8")  # en preset, Kasper selv har lavet, røres ikke
    n = vm.reset_presets(dst)
    assert n == len(r["presets"]) and [f.name for f in dst.iterdir()] == ["egen.yaml"]
