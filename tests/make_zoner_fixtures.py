"""Bygger de fælles testvektorer for zonemotoren (Python = facit; JavaScript skal give det samme).

    python tests/make_zoner_fixtures.py   # kun når reglerne bevidst ændres – og så i begge sprog
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
import zoner as zn  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "zoner_cases.json"


def days(n: int, start: str = "2018-01-01") -> list[str]:
    d, out = dt.date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def path(points: list[tuple[int, float]], wiggle: float = 0.012) -> list[float]:
    """Stykvis lineær kurs mellem (dag, kurs)-punkter + deterministisk støj (ingen tilfældighed)."""
    out = []
    for (d0, p0), (d1, p1) in zip(points, points[1:]):
        for k in range(d1 - d0):
            base = p0 + (p1 - p0) * k / (d1 - d0)
            out.append(round(base * (1 + wiggle * math.sin(0.9 * (d0 + k)) * math.cos(0.23 * (d0 + k))), 4))
    out.append(points[-1][1])
    return out


def cases() -> list[dict]:
    c = []
    v = path([(0, 80), (300, 100), (420, 62), (620, 110)])
    c.append({"id": "v_krak", "kind": "index", "series": list(zip(days(len(v)), v))})
    k = path([(0, 90), (260, 100), (330, 84.5), (520, 104)])
    c.append({"id": "kun_korrektion", "kind": "index", "series": list(zip(days(len(k)), k))})
    lb = path([(0, 70), (250, 100), (500, 82), (700, 70), (1000, 55), (1150, 48), (1300, 60)], 0.02)
    c.append({"id": "lang_bjorn", "kind": "index", "series": list(zip(days(len(lb)), lb))})
    mid = path([(0, 95), (200, 100), (280, 77.5)], 0.008)
    c.append({"id": "midt_i_faldet", "kind": "index", "series": list(zip(days(len(mid)), mid))})
    sp = path([(0, 135), (40, 225), (76, 105), (120, 170), (160, 147)], 0.02)
    c.append({"id": "aktie_faste_niveauer", "kind": "stock", "series": list(zip(days(len(sp), "2026-05-01"), sp)),
              "levels": [{"id": "korrektion", "label": "Korrektion", "hi": 140, "lo": 131},
                         {"id": "bjorn", "label": "Bjørnemarked", "hi": 118, "lo": 105},
                         {"id": "kollaps", "label": "Kollaps", "hi": 95, "lo": 75}]})
    st = path([(0, 40), (120, 100), (200, 45), (330, 70)], 0.03)
    c.append({"id": "aktie_procent", "kind": "stock", "series": list(zip(days(len(st)), st))})
    c.append({"id": "v_krak_plan_midt_i", "kind": "index", "series": list(zip(days(len(v)), v)), "arm_from": days(len(v))[380]})
    c.append({"id": "aktie_plan_i_dag", "kind": "stock", "series": list(zip(days(len(st)), st)), "arm_from": days(len(st))[-1]})
    c.append({"id": "aktie_plan_i_morgen", "kind": "stock", "series": list(zip(days(len(st)), st)), "arm_from": "2099-01-01"})
    br = [(d, p * (4.13 if i < 150 else 1)) for i, (d, p) in enumerate(zip(days(400), path([(0, 700), (399, 760)])))]
    c.append({"id": "databrud", "kind": "index", "series": br, "raw": True})
    return c


def expected(case: dict) -> dict:
    rows = [{"date": d, "close": p} for d, p in case["series"]]
    series, issues = zn.guard(rows, name=case["id"])
    levels = case.get("levels")
    zones = zn.ZONES_STOCK if case["kind"] == "stock" else zn.ZONES_INDEX
    arm = case.get("arm_from")
    w = zn.walk(series, zones=zones, levels=levels, arm_from=arm)
    return {
        "guard": {"n": len(series), "first": series[0], "issues": [{k: v for k, v in i.items() if k != "note"} for i in issues]},
        "fills": [{k: (round(v, 6) if isinstance(v, float) else v) for k, v in f.items()} for f in w["fills"]],
        "episodes": w["episodes"],
        "analyse": zn.analyse(series, kind=case["kind"], levels=levels, name=case["id"], arm_from=arm),
        "backtest": zn.backtest(series, kind=case["kind"], levels=levels, warmup_days=20),
    }


def build() -> dict:
    out = []
    for case in cases():
        out.append({**case, "expected": expected(case)})
    return {"note": "Genereret af tests/make_zoner_fixtures.py fra runner/zoner.py. Ret aldrig i hånden.", "cases": out}


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"skrev {OUT} ({OUT.stat().st_size // 1024} KB)")
