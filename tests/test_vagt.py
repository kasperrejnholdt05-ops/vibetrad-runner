"""vagt.py: hele kæden offline (gemte kurser, falsk FRED), filformat, historik og exit-kode."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
sys.path.insert(0, str(ROOT / "tests"))
import make_zoner_fixtures as mk  # noqa: E402
import vagt  # noqa: E402


def _write_prices(folder: Path, n: int = 1400) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    v = mk.path([(0, 80), (500, 100), (700, 66), (n - 1, 104)])
    days = mk.days(len(v), "2020-01-01")
    for sym in ("GSPC.INDX", "NDX.INDX", "OMXS30.INDX", "OMXC25.INDX", "OBX.OL"):
        (folder / f"{sym}.json").write_text(json.dumps([{"date": d, "close": p} for d, p in zip(days, v)]), "utf-8")


class FakeFred:
    available = True
    dead = False

    def observations(self, sid, start=None, end=None):
        if sid == "VIXCLS":
            return [("2025-05-01", 18.0), ("2025-05-08", 19.0)]
        return []


def test_offline_run_writes_public_market_only_file(tmp_path):
    _write_prices(tmp_path / "px")
    rc = vagt.main(["--offline", str(tmp_path / "px"), "--out", str(tmp_path / "out"), "--ref-date", "2025-05-12"])
    assert rc == 0
    doc = json.loads((tmp_path / "out" / "vagt.json").read_text("utf-8"))
    assert doc["schema"] == 1 and doc["summary"]["markets_ok"] == 5
    m = doc["markets"][0]
    assert m["state"]["zone"] in ("normal", "korrektion", "bjorn", "kollaps") and len(m["state"]["tranches"]) == 9
    assert m["backtest"]["fills"] >= 1 and m["chart"][-1][0] == m["state"]["date"]
    assert {r["id"] for r in doc["replays"]} == {"2008", "2020", "2022"}
    text = json.dumps(doc)
    for word in ("cash", "holdings", "kontant", "VT_PORTFOLIO"):
        assert word not in text
    hist = json.loads((tmp_path / "out" / "vagt_history.json").read_text("utf-8"))
    assert len(hist) == 1 and set(hist[0]["zones"]) == {"sp500", "ndx", "omxs30", "omxc25", "obx"}


def test_history_is_capped_and_merge_appends(tmp_path):
    _write_prices(tmp_path / "px")
    out = tmp_path / "out"
    vagt.main(["--offline", str(tmp_path / "px"), "--out", str(out), "--ref-date", "2025-05-12"])
    (out / "vagt_history.json").write_text(json.dumps([{"at": str(i)} for i in range(vagt.HISTORY_MAX + 5)]), "utf-8")
    rc = vagt.main(["--merge", str(out / "vagt.json"), "--out", str(out)])
    assert rc == 0
    hist = json.loads((out / "vagt_history.json").read_text("utf-8"))
    assert len(hist) == vagt.HISTORY_MAX and "zones" in hist[-1]


def test_no_data_at_all_fails_the_run(tmp_path):
    (tmp_path / "tom").mkdir()
    rc = vagt.main(["--offline", str(tmp_path / "tom"), "--out", str(tmp_path / "out"), "--ref-date", "2025-05-12"])
    assert rc == 1


def test_build_uses_fred_and_survives_a_failing_market(tmp_path):
    _write_prices(tmp_path / "px")
    off = vagt.OfflineEod(str(tmp_path / "px"))

    def fetch(sym, a, b):
        if sym == "OBX.OL":
            raise TimeoutError("nede")
        return off.eod(sym, a, b)

    doc = vagt.build("2025-05-12", fetch, FakeFred(), off, generated_at="2025-05-12T18:00:00+02:00")
    obx = next(m for m in doc["markets"] if m["id"] == "obx")
    assert obx["ok"] is False and "TimeoutError" in obx["note"]
    assert any(s["id"] == "vix" for s in doc["macro"]["signals"])
    assert doc["summary"]["markets_ok"] == 4


def test_stale_prices_are_flagged(tmp_path):
    _write_prices(tmp_path / "px")
    rc = vagt.main(["--offline", str(tmp_path / "px"), "--out", str(tmp_path / "out"), "--ref-date", "2026-01-30"])
    assert rc == 0
    doc = json.loads((tmp_path / "out" / "vagt.json").read_text("utf-8"))
    assert any(i["kind"] == "forældet" for i in doc["markets"][0]["data"]["issues"])
