"""Zonemotoren: datakontrol, zoner, trancher, Nordnet-grænsen, backtest og samme facit i Python og JavaScript."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
sys.path.insert(0, str(ROOT / "tests"))
import make_zoner_fixtures as mk  # noqa: E402
import zoner as zn  # noqa: E402

FX = json.loads((ROOT / "tests" / "fixtures" / "zoner_cases.json").read_text("utf-8"))
CASES = {c["id"]: c for c in FX["cases"]}


def series_of(case_id: str) -> list[tuple[str, float]]:
    s, _ = zn.guard([{"date": d, "close": p} for d, p in CASES[case_id]["series"]])
    return s


def test_fixtures_are_current():
    """Facit er bygget af den nuværende motor (ellers: python tests/make_zoner_fixtures.py og tjek JS)."""
    for case in CASES.values():
        got = json.loads(json.dumps(mk.expected(case)))
        assert got == case["expected"], case["id"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node findes ikke")
def test_javascript_engine_matches_python():
    out = subprocess.run(["node", str(ROOT / "tests" / "zoner_js_check.mjs")], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr + out.stdout


def test_guard_rescales_a_data_break_and_reports_it():
    rows = [{"date": f"2020-01-{d:02d}", "close": 4000.0 + d} for d in range(1, 10)]
    rows += [{"date": f"2020-01-{d:02d}", "close": (4000.0 + d) / 4.13} for d in range(10, 20)]
    s, issues = zn.guard(rows, name="OMXS30")
    assert [i["kind"] for i in issues] == ["databrud"]
    assert issues[0]["from"] == "2020-01-09" and issues[0]["to"] == "2020-01-10"
    jumps = [abs(b / a - 1) for (_, a), (_, b) in zip(s, s[1:])]
    assert max(jumps) < 0.01  # intet falsk krak tilbage
    assert s[-1][1] == pytest.approx(4019 / 4.13)


def test_guard_skips_bad_rows_dedupes_and_finds_gaps():
    rows = [{"date": "2020-01-01", "close": 10}, {"date": "2020-01-01", "close": 11}, {"date": "x", "close": 1},
            {"date": "2020-01-02", "close": None}, {"date": "2020-03-01", "close": 12}, ("2020-03-02", "12.5")]
    s, issues = zn.guard(rows)
    assert s == [("2020-01-01", 11.0), ("2020-03-01", 12.0), ("2020-03-02", 12.5)]
    assert {i["kind"] for i in issues} == {"ugyldig", "huller"}


def test_v_crash_fires_all_nine_tranches_in_order():
    s = series_of("v_krak")
    w = zn.walk(s)
    kinds = [(f["zone"], f["kind"]) for f in w["fills"]]
    assert kinds[:3] == [("korrektion", "liggende"), ("bjorn", "liggende"), ("kollaps", "liggende")]
    assert {k for z, k in kinds[3:6]} == {"bekraeftelse"} and {k for z, k in kinds[6:]} == {"fundament"}
    liggende = [f for f in w["fills"] if f["kind"] == "liggende"]
    for f, pct in zip(liggende, (10, 20, 30)):
        assert f["price"] <= f["top"] * (1 - pct / 100) + 1e-9 and f["top"] == max(p for _, p in s[:f["i"]])
    bottom_i = min(range(len(s)), key=lambda i: s[i][1])
    conf = [f for f in w["fills"] if f["kind"] == "bekraeftelse"]
    assert all(f["i"] > bottom_i for f in conf)
    found = [f for f in w["fills"] if f["kind"] == "fundament"]
    assert all(f["i"] - bottom_i >= zn.FOUNDATION_DAYS for f in found)


def test_correction_only_never_touches_deeper_zones():
    w = zn.walk(series_of("kun_korrektion"))
    assert {f["zone"] for f in w["fills"]} == {"korrektion"}
    assert [f["kind"] for f in w["fills"]] == ["liggende", "bekraeftelse", "fundament"]


def test_new_top_rearms_all_tranches_and_nothing_fires_on_the_top_day():
    s = series_of("v_krak")
    a = zn.analyse(s)
    assert a["dd_pct"] < 10 and all(t["status"] in ("venter", "ikke nået") for t in a["tranches"])
    assert a["next"]["type"] == "ordre"
    w = zn.walk(s)
    tops = {f["top_date"] for f in w["fills"]}
    assert len(tops) == 1


def test_long_bear_reanchors_top_after_three_years():
    w = zn.walk(series_of("lang_bjorn"))
    tops = sorted({f["top_date"] for f in w["fills"]})
    assert len(tops) == 2  # én flytning af toppen i en bjørn, der varer over 3 år
    deepest = max(w["episodes"], key=lambda e: e["max_dd_pct"])
    assert deepest["max_dd_pct"] > 40


def test_mid_fall_status_limit_alarm_and_waiting_confirmation():
    a = zn.analyse(series_of("midt_i_faldet"))
    assert a["zone"] == "bjorn" and 20 < a["dd_pct"] < 30
    st = {(t["zone"], t["kind"]): t for t in a["tranches"]}
    assert st[("korrektion", "liggende")]["status"] in ("udløst", "ny")
    assert st[("korrektion", "bekraeftelse")]["status"] == "venter" and st[("korrektion", "bekraeftelse")]["level"]
    k = st[("kollaps", "liggende")]
    assert k["status"] == "venter" and k["order"] == ("limit" if k["distance_pct"] <= 13 else "alarm")
    assert st[("kollaps", "bekraeftelse")]["status"] == "ikke nået"
    assert a["next"]["type"] in ("koeb", "ordre", "alarm")


def test_nordnet_limit_turns_deep_orders_into_alarms():
    s = [(f"2026-01-{d:02d}", 100.0) for d in range(1, 29)]
    a = zn.analyse(s)
    by = {t["zone"]: t for t in a["tranches"] if t["kind"] == "liggende"}
    assert by["korrektion"]["order"] == "limit"  # 10 % under kursen
    assert by["kollaps"]["order"] == "alarm" and by["kollaps"]["alarm_at"] == pytest.approx(70 / 0.87, rel=1e-6)
    assert "kursalarm" not in a["next"]["text"].lower()  # det første skridt er korrektionens ordre
    a2 = zn.analyse(s, nordnet_limit_pct=5)
    assert a2["next"]["type"] == "alarm" and "Sæt kursalarm ved" in a2["next"]["text"]


def test_stock_with_fixed_levels_uses_the_levels():
    c = CASES["aktie_faste_niveauer"]
    a = zn.analyse(series_of("aktie_faste_niveauer"), kind="stock", levels=c["levels"])
    assert a["fixed_levels"] and [z["price_hi"] for z in a["zones"]] == [140, 118, 95]
    assert all(f["price"] <= 140 for f in zn.walk(series_of("aktie_faste_niveauer"), levels=c["levels"])["fills"])


def test_stock_zones_are_wider_than_index_zones():
    assert [z[2] for z in zn.ZONES_STOCK] == [20, 35, 50]
    a = zn.analyse(series_of("aktie_procent"), kind="stock")
    assert a["zones"][0]["pct_hi"] == 20


def test_backtest_summary_and_reserve_weights():
    b = zn.backtest(series_of("v_krak"), warmup_days=20)
    assert b["fills"] == 9 and b["by_zone"]["kollaps"]["fills"] == 3
    big = [e for e in b["episodes"] if e["fills"]]
    assert big[0]["reserve_used_pct"] == 100 and big[0]["zones"] == ["korrektion", "bjorn", "kollaps"]
    assert sum(zn.ZONE_WEIGHTS.values()) == pytest.approx(1.0)


def test_replay_points_and_fills():
    s = series_of("v_krak")
    rp = zn.replay(s, s[250][0], s[600][0], step=10)
    assert rp["points"][0]["date"] == s[250][0] and rp["points"][-1]["date"] == s[600][0]
    assert len(rp["fills"]) == 9 and {p["zone"] for p in rp["points"]} >= {"normal", "kollaps"}


def test_too_short_series_is_not_ok():
    assert zn.analyse([("2026-01-01", 1.0)])["ok"] is False


def test_plan_start_rearms_tranches_that_were_never_bought():
    """En tranche, der 'udløstes' før planen startede, blev aldrig købt: den er klar igen fra plandagen."""
    s = series_of("v_krak")
    arm = s[380][0]  # midt i faldet, efter at alle tre liggende ordrer ville være udløst
    w = zn.walk(s, arm_from=arm)
    assert w["fills"] and min(f["date"] for f in w["fills"]) >= arm
    first_day = [f for f in w["fills"] if f["date"] == arm]
    assert {f["kind"] for f in first_day} == {"liggende"}  # kursen er allerede under zonerne: køb samme dag
    a = zn.analyse(series_of("aktie_procent"), kind="stock", arm_from=series_of("aktie_procent")[-1][0])
    ny = [t for t in a["tranches"] if t["status"] == "ny"]
    assert ny and all(t["kind"] == "liggende" for t in ny) and a["next"]["type"] == "koeb"


def test_plan_start_after_last_price_counts_from_the_last_day():
    s = series_of("aktie_procent")
    a = zn.analyse(s, kind="stock", arm_from="2099-01-01")
    b = zn.analyse(s, kind="stock", arm_from=s[-1][0])
    assert [t["status"] for t in a["tranches"]] == [t["status"] for t in b["tranches"]]
    assert all(t.get("distance_pct", 0) >= 0 for t in a["tranches"] if t["status"] == "venter" and t["kind"] == "liggende")
