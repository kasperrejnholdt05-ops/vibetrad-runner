"""Trendvagten: 200-dages-reglen og ROC-nulstilling (runner/trend.py) og dens plads i datatjekket."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
import datatjek as dtj  # noqa: E402
import run_week as rw  # noqa: E402
import trend as tr  # noqa: E402


def rows_from(prices: list[float]) -> list[tuple[str, float]]:
    d0 = dt.date(2025, 1, 1)
    return [((d0 + dt.timedelta(days=i)).isoformat(), p) for i, p in enumerate(prices)]


def rising_then_flat(flat_days: int = 63, run_pct: float = 0.004) -> list[float]:
    """Stiger jævnt i 230 dage og går derefter sidelæns: ROC falder mod nul, gennemsnittet stiger stadig."""
    up = [100 * (1 + run_pct) ** i for i in range(230)]
    return up + [up[-1]] * flat_days


def test_reset_after_a_hot_run_is_the_entry_situation():
    s = tr.status(rows_from(rising_then_flat()))
    assert s["state"] == "reset" and s["dist_pct"] > 0 and s["slope_pct"] > 0
    assert abs(s["roc63_pct"]) <= tr.RESET_BAND and s["roc63_peak_pct"] >= tr.HOT_PEAK
    assert s["label"].startswith("Købsvindue") and "Visser" in s["meaning"]


def test_hot_intact_flat_under_break_and_short():
    hot = tr.status(rows_from([100 * 1.004 ** i for i in range(300)]))
    assert hot["state"] == "hot" and hot["roc63_pct"] > tr.HOT_PEAK
    calm = tr.status(rows_from([100 * 1.0004 ** i for i in range(300)]))
    assert calm["state"] == "intact"
    down = tr.status(rows_from([200 * 0.997 ** i for i in range(300)]))
    assert down["state"] == "under" and down["dist_pct"] < 0 and down["slope_pct"] < 0
    # Lige faldet under gennemsnittet efter en lang stigning
    brk = rising_then_flat(flat_days=0) + [rising_then_flat(flat_days=0)[-1] * 0.60] * 2
    assert tr.status(rows_from(brk))["state"] == "break"
    # Over gennemsnittet, men gennemsnittet falder: lang nedtur og et kort hop
    fl = [200 * 0.996 ** i for i in range(260)]
    fl += [fl[-1] * 1.9] * 3
    assert tr.status(rows_from(fl))["state"] == "flat"
    short = tr.status(rows_from([100.0] * 150))
    assert short["state"] == "short" and short["rows"] == 150
    assert "200-dages" in tr.rule_text() and "±3 %" in tr.rule_text()


class Src:
    def __init__(self, data: dict, available: bool = True):
        self.data, self.available, self.errors = data, available, []

    def prices(self, codes, start, end):
        return {c: {"rows": self.data[c]} for c in codes if c in self.data}


def test_trend_check_prefers_eodhd_and_falls_back_to_vibe_trading():
    insts = [{"ticker": "AAA.ST", "name": "A", "role": "beholdning"}, {"ticker": "BBB.US", "name": "B", "role": "målportefølje"},
             {"ticker": "CCC.CO", "name": "C", "role": "overvågning"}]
    eod = Src({"AAA.ST": rows_from(rising_then_flat()), "BBB.US": rows_from([1.0] * 50)})
    vt = Src({"BBB.US": rows_from([200 * 0.997 ** i for i in range(300)])})
    t = dtj.trend_check(insts, vt, eod, dt.datetime(2026, 10, 20, tzinfo=rw.STO))
    by = {x["ticker"]: x for x in t["items"]}
    assert by["AAA.ST"]["state"] == "reset" and by["AAA.ST"]["source"] == "eodhd"
    assert by["BBB.US"]["state"] == "under" and by["BBB.US"]["source"] == "vibe_trading" and by["BBB.US"]["role"] == "målportefølje"
    assert by["CCC.CO"]["state"] == "short" and by["CCC.CO"]["source"] is None
    dc = {"trend": t}
    lines = dtj.trend_lines(dc)
    assert lines[0].startswith("**Trendvagt (200-dages-reglen, Jordi Visser):**") and "ikke et handelssignal" in lines[0]
    assert any("A (AAA.ST, beholdning): Købsvindue" in x and "ROC 63 dage" in x for x in lines)
    assert "Trend mod tese" in dtj.cio_instruction({"status": "PASS", "trend": t}) and "Trend mod tese" not in dtj.cio_instruction({"status": "PASS"})


def test_target_from_vt_portfolio_reaches_instruments_text_and_visser_names():
    raw = ('{"holdings":[{"ticker":"ZZTOP.US","name":"Hemmelig Raket","qty":3,"cost_sek":3000,"kind":"stock"},'
           '{"ticker":"KERNE.XETRA","name":"Kernefond","qty":2,"cost_sek":7000,"kind":"etf"}],"cash_sek":500,'
           '"courtage_terms":"kurtagefrit i Norden til 30/06/2027; uden for Norden 0,25 %, min. 9 SEK; valuta 0,25 %",'
           '"watchlist":[{"ticker":"WATCH.ST","note":"kandidat"}],'
           '"target":[{"name":"Kernefond","ticker":"KERNE.XETRA","type":"etf","weight":60},'
           '{"name":"Satellit","ticker":"SATL.CO","type":"aktie","weight":15,"wait":"2026-11-05"},'
           '{"name":"Kontant","type":"kontant","weight":5,"ticker":"SKAL-IKKE-MED"},{"name":"x","type":"rod","weight":"nej?"}]}')
    try:
        rw.load_portfolio(raw)
    except rw.PortfolioError:
        pass
    p = rw.load_portfolio(raw.replace('"weight":"nej?"', '"weight":20'))
    assert p["target"][1] == {"name": "Satellit", "ticker": "SATL.CO", "type": "aktie", "weight": 15.0, "wait": "2026-11-05"}
    assert p["target"][2]["ticker"] is None and p["target"][3]["type"] == "etf"
    txt = rw.portfolio_text(p)
    assert "Målportefølje (fra strategikørslen" in txt and "SATL.CO købes tidligst 05/11/2026" in txt
    assert "Satser: kurtagefrit i Norden til 30/06/2027" in txt and "angiv ingen kurtagesats" not in txt
    roles = {i["ticker"]: i["role"] for i in dtj.instruments(p)}
    assert roles["SATL.CO"] == "målportefølje" and roles["KERNE.XETRA"] == "beholdning" and "SKAL-IKKE-MED" not in str(roles)
    names = rw.visser_names(p)
    assert names.startswith("Hemmelig Raket (ZZTOP.US, beholdning") and "Satellit (SATL.CO, målportefølje 15 %, købes tidligst 05/11/2026)" in names
    assert "WATCH.ST, overvågning: kandidat" in names and "KERNE" not in names


def test_visser_preset_is_shellfree_and_gets_eodhd(tmp_path, monkeypatch):
    monkeypatch.setenv("EODHD_API_KEY", "test")
    yaml = __import__("pytest").importorskip("yaml")
    empty = tmp_path / "src"
    empty.mkdir()
    changed = rw.prepare_shellfree_presets(tmp_path / "dst", empty, own_dir=rw.VIBETRAD_PRESETS_DIR)
    assert changed == ["visser_panel"]
    spec = yaml.safe_load((tmp_path / "dst" / "visser_panel.yaml").read_text("utf-8"))
    ids = [a["id"] for a in spec["agents"]]
    assert ids == ["fundamental_analyst", "catalyst_analyst", "technical_analyst", "risk_manager", "portfolio_manager"]
    for a in spec["agents"]:
        assert not rw.SHELL_TOOLS & set(a["tools"]), a["id"]
    by = {a["id"]: a for a in spec["agents"]}
    assert "get_eodhd_data" in by["catalyst_analyst"]["tools"] and "X is not available" in by["catalyst_analyst"]["system_prompt"]
    assert "Trend mod tese" in by["portfolio_manager"]["system_prompt"] and "{upstream_context}" in by["portfolio_manager"]["system_prompt"]
    assert [v["name"] for v in spec["variables"] if v.get("required")] == ["names"]
    assert spec["tasks"][-1]["depends_on"] == ["task-fundamentals", "task-catalysts", "task-trend", "task-risk"]
    # uden eksplicit kilde-mappe tages de egne presets ikke med (testene af de indbyggede presets)
    assert rw.prepare_shellfree_presets(tmp_path / "dst2", empty) == []
