"""Bevis for vagthundens regler på den rigtige historik (ingen LLM, ingen porteføljedata).

Kører på GitHub via workflowet "Vibetrad analyse" (knaptryk), hvor nøglerne findes:

    python runner/analyse.py --out /tmp/analyse.json     # henter FRED og EODHD, skriver hele resultatet
    python runner/analyse.py --summary /tmp/analyse.json  # kort dansk opsummering til loggen

To spørgsmål:

1. **Sort svane** – hvor ofte ville varslet have været tændt siden 2007 under hver regel i ``makrovagt.SWAN_RULES``,
   hvor længe ad gangen, og ramte det de store fald i S&P 500? Makrovagten genberegnes uge for uge på data, som de
   så ud den dag (kun de seneste 430 dage, intet fra fremtiden), og "frisk" sammenlignes med 30 dage før.
2. **Top-alder** – hvor gammel må en top være, før zonerne måles fra det seneste års top i stedet? Zonemotoren
   backtestes på alle fem indeks med ``top_days`` 756 (3 år, i dag), 504 og 252, og "tidlige køb" tælles: trancher,
   der udløses, mens indekset er mindre end 5 % under sit seneste års top (OMXC25 i oktober 2026).

Kursindeks uden udbytte, lukkekurser, ingen kurtage. Fortiden er ingen garanti.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eodhd as eo  # noqa: E402
import fred as fr  # noqa: E402
import makrovagt as mv  # noqa: E402
import temaer as tm  # noqa: E402
import zoner as zn  # noqa: E402

TOP_DAYS_VARIANTS = (756, 504, 252)
MACRO_START = "2007-01-05"
WINDOW_DAYS = 430
EARLY_PCT = 5.0          # "tidligt køb": under 5 % fra det seneste års top
FWD_DAYS = 63            # ca. 3 måneder efter et varsel
HIT_DD = -10.0           # et varsel "rammer", hvis S&P 500 falder mindst 10 % inden for FWD_DAYS
DECLINE_DD = 15.0        # store fald: mindst 15 % under det seneste års top
WARN_DD = 10.0           # tidligt varsel: før faldet når 10 %
LEAD_DAYS = 91           # … og højst 3 måneder før toppen (kreditstress kom fx før toppen i oktober 2007)


# ---------------------------------------------------------------------------
# Hurtige udsnit (bisect i stedet for at filtrere hele historikken hver uge)
class Sliced:
    def __init__(self, series: dict[str, list[tuple[str, float]]], rows: dict[str, list[dict]]):
        self.series = {k: sorted(v) for k, v in series.items()}
        self.sdates = {k: [d for d, _ in v] for k, v in self.series.items()}
        self.rows = {k: sorted(v, key=lambda r: str(r.get("date"))[:10]) for k, v in rows.items()}
        self.rdates = {k: [str(r.get("date"))[:10] for r in v] for k, v in self.rows.items()}

    def at(self, day: str, window_days: int = WINDOW_DAYS) -> tuple[dict, dict]:
        lo = (dt.date.fromisoformat(day) - dt.timedelta(days=window_days)).isoformat()
        s = {k: v[bisect.bisect_left(self.sdates[k], lo):bisect.bisect_right(self.sdates[k], day)]
             for k, v in self.series.items()}
        r = {k: v[bisect.bisect_left(self.rdates[k], lo):bisect.bisect_right(self.rdates[k], day)]
             for k, v in self.rows.items()}
        return s, r


def _weeks(start: str, end: str, step: int = 7) -> list[str]:
    d, e, out = dt.date.fromisoformat(start), dt.date.fromisoformat(end), []
    while d <= e:
        out.append(d.isoformat())
        d += dt.timedelta(days=step)
    return out


def _runs(flags: list[bool]) -> list[tuple[int, int]]:
    """Sammenhængende stræk af True som (start, slut) inklusive."""
    out, start = [], None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        if not f and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def _close_on(sdates: list[str], closes: list[float], day: str) -> int | None:
    i = bisect.bisect_right(sdates, day) - 1
    return i if i >= 0 else None


def declines(series: list[tuple[str, float]], *, min_dd: float = DECLINE_DD, warn_dd: float = WARN_DD,
             start: str = "") -> list[dict]:
    """Store fald målt fra det seneste års (252 dages) højeste luk: top, dag for første ``warn_dd`` %, bund."""
    closes = [c for _, c in series]
    out: list[dict] = []
    seg: dict | None = None
    for i, (d, c) in enumerate(series):
        lo = max(0, i - 251)
        hi_i = max(range(lo, i + 1), key=lambda j: (closes[j], j))
        dd = (1 - c / closes[hi_i]) * 100
        if dd >= warn_dd:
            if seg is None:
                seg = {"top_date": series[hi_i][0], "top": closes[hi_i], "warn_date": d, "bottom_date": d, "max_dd": dd}
            elif dd > seg["max_dd"]:
                seg.update(bottom_date=d, max_dd=dd)
        elif seg is not None and dd < warn_dd / 2:
            if seg["max_dd"] >= min_dd and seg["top_date"] >= start:
                out.append(seg)
            seg = None
    if seg is not None and seg["max_dd"] >= min_dd and seg["top_date"] >= start:
        out.append(seg)
    return [{**s, "max_dd": round(s["max_dd"], 1)} for s in out]


# ---------------------------------------------------------------------------
# 1) Sort svane
def macro_replay(series: dict[str, list[tuple[str, float]]], rows: dict[str, list[dict]], spx: list[tuple[str, float]],
                 *, start: str = MACRO_START, end: str | None = None, step: int = 7,
                 rules: tuple[str, ...] = mv.SWAN_RULES) -> dict:
    sl = Sliced(series, rows)
    end = end or (spx[-1][0] if spx else start)
    weeks = _weeks(start, end, step)
    flags: dict[str, list[bool]] = {r: [] for r in rules}
    levels: dict[str, list[str]] = {r: [] for r in rules}
    causes: dict[str, list[str]] = {r: [] for r in rules}
    for day in weeks:
        s_now, r_now = sl.at(day)
        sig = mv.all_signals(s_now, r_now, None, day)
        prev_day = (dt.date.fromisoformat(day) - dt.timedelta(days=mv.SWAN_FRESH_DAYS)).isoformat()
        s_prev, r_prev = sl.at(prev_day)
        prev = {x["id"]: tm.counted_level(x) for x in mv.all_signals(s_prev, r_prev, None, prev_day) if not x["stale"]}
        for r in rules:
            a = mv.assess(sig, prev, r)
            flags[r].append(a["swan"])
            levels[r].append(a["level"])
            causes[r].append("chok" if a["shocks"] else "temaer" if a["swan"] else "")
    sdates, closes = [d for d, _ in spx], [c for _, c in spx]
    big = declines(spx, start=start)
    out: dict[str, Any] = {"start": weeks[0] if weeks else start, "end": weeks[-1] if weeks else end,
                           "weeks": len(weeks), "declines": big, "rules": {}}
    for r in rules:
        f = flags[r]
        runs = _runs(f)
        alarms = []
        for a, b in runs:
            i = _close_on(sdates, closes, weeks[a])
            fwd = None
            if i is not None and i + 1 < len(closes):
                fwd = (min(closes[i:i + FWD_DAYS + 1]) / closes[i] - 1) * 100
            alarms.append({"from": weeks[a], "to": weeks[b], "weeks": b - a + 1, "cause": causes[r][a],
                           "fwd_dd": None if fwd is None else round(fwd, 1)})
        fwds = [x["fwd_dd"] for x in alarms if x["fwd_dd"] is not None]
        hits = [x for x in fwds if x <= HIT_DD]
        warned = []
        on = [w for w, fl in zip(weeks, f) if fl]
        for dcl in big:
            lead = (dt.date.fromisoformat(dcl["top_date"]) - dt.timedelta(days=LEAD_DAYS)).isoformat()
            early = any(lead <= w <= dcl["warn_date"] for w in on)
            before_bottom = any(lead <= w <= dcl["bottom_date"] for w in on)
            warned.append({"top_date": dcl["top_date"], "max_dd": dcl["max_dd"], "early": early,
                           "before_bottom": before_bottom})
        out["rules"][r] = {
            "weeks_on": sum(f), "pct_on": round(100 * sum(f) / len(f), 1) if f else 0.0,
            "alarms": len(runs), "longest_weeks": max((b - a + 1 for a, b in runs), default=0),
            "hits": len(hits), "hit_rate": round(100 * len(hits) / len(fwds)) if fwds else None,
            "median_fwd_dd": round(statistics.median(fwds), 1) if fwds else None,
            "declines_early": sum(x["early"] for x in warned), "declines_before_bottom": sum(x["before_bottom"] for x in warned),
            "declines": len(warned), "warned": warned,
            "pct_on_last_2y": round(100 * sum(f[-104:]) / len(f[-104:]), 1) if f else 0.0,
            "now": {"week": weeks[-1] if weeks else None, "swan": f[-1] if f else None,
                    "level": levels[r][-1] if f else None},
            "alarm_list": alarms[-25:],
        }
    return out


# ---------------------------------------------------------------------------
# 2) Top-alder
def _dd_1y(closes: list[float], i: int) -> float:
    return (1 - closes[i] / max(closes[max(0, i - 251):i + 1])) * 100


def zone_variant(series: list[tuple[str, float]], top_days: int) -> dict:
    closes = [c for _, c in series]
    bt = zn.backtest(series, top_days=top_days)
    w = zn.walk(series, top_days=top_days, start=min(252, max(len(series) - 1, 0)))
    early = [f for f in w["fills"] if _dd_1y(closes, f["i"]) < EARLY_PCT]

    def fwd(f: dict, n: int) -> float | None:
        j = f["i"] + n
        return None if j >= len(closes) else (closes[j] / f["price"] - 1) * 100

    e1 = [x for x in (fwd(f, 252) for f in early) if x is not None]
    a1 = [x for x in (fwd(f, 252) for f in w["fills"]) if x is not None]
    a3 = [x for x in (fwd(f, 756) for f in w["fills"]) if x is not None]
    eps = [e for e in bt["episodes"] if e["fills"]]
    weighted = [e["fwd_3y_weighted"] for e in eps if e["fwd_3y_weighted"] is not None]
    deep = declines(series, min_dd=20.0, warn_dd=10.0, start=series[min(252, len(series) - 1)][0] if series else "")
    fill_dates = [f["date"] for f in w["fills"]]
    covered = sum(1 for d in deep if any(d["top_date"] <= x <= d["bottom_date"] for x in fill_dates))
    st = zn.analyse(series, kind="index", arm_from=zn.PLAN_START, top_days=top_days)
    return {
        "top_days": top_days, "fills": len(w["fills"]), "episodes_with_fills": len(eps),
        "early_fills": len(early), "early_fwd_1y_median": round(statistics.median(e1), 1) if e1 else None,
        "fwd_1y_median": round(statistics.median(a1), 1) if a1 else None,
        "fwd_3y_median": round(statistics.median(a3), 1) if a3 else None,
        "fwd_3y_weighted_avg": round(statistics.mean(weighted), 1) if weighted else None,
        "reserve_used_avg": round(statistics.mean([e["reserve_used_pct"] for e in eps]), 0) if eps else None,
        "deep_declines": len(deep), "deep_declines_bought": covered,
        "now": {"top_date": st.get("top_date"), "dd_pct": st.get("dd_pct"), "next": (st.get("next") or {}).get("text")},
    }


def zone_report(markets: dict[str, list[tuple[str, float]]], variants: tuple[int, ...] = TOP_DAYS_VARIANTS) -> dict:
    return {mid: {str(td): zone_variant(s, td) for td in variants} for mid, s in markets.items() if len(s) > 600}


# ---------------------------------------------------------------------------
def fetch_all(ref_day: str, fred_client: Any, eod_client: Any) -> tuple[dict, dict, dict]:
    import vagt  # noqa: PLC0415 - kun til historikstart pr. marked
    series: dict[str, list[tuple[str, float]]] = {}
    rows: dict[str, list[dict]] = {}
    markets: dict[str, list[tuple[str, float]]] = {}
    macro_from = (dt.date.fromisoformat(MACRO_START) - dt.timedelta(days=WINDOW_DAYS + 40)).isoformat()
    if fred_client is not None:
        for sid in dict.fromkeys([x[0] for x in mv.FRED_SERIES] + [x[0] for x in tm.FRED_EXTRA]):
            try:
                series[sid] = fred_client.observations(sid, macro_from, ref_day)
            except Exception:  # noqa: BLE001
                series[sid] = []
    if eod_client is not None:
        for sym in dict.fromkeys([x[0] for x in mv.EOD_SERIES] + [x[0] for x in tm.EOD_EXTRA]):
            try:
                rows[sym] = eod_client.eod(sym, macro_from, ref_day)
            except Exception:  # noqa: BLE001
                rows[sym] = []
        for m in zn.MARKETS:
            try:
                raw = eod_client.eod(m["symbol"], vagt.HISTORY_START.get(m["symbol"], "1990-01-01"), ref_day)
            except Exception:  # noqa: BLE001
                raw = []
            markets[m["id"]], _ = zn.guard(raw, name=m["name"])
    return series, rows, markets


def run(series: dict, rows: dict, markets: dict, ref_day: str) -> dict:
    spx = markets.get("sp500") or []
    return {"generated_for": ref_day, "macro": macro_replay(series, rows, spx) if spx else None,
            "zones": zone_report(markets),
            "settings": {"swan_rules": list(mv.SWAN_RULES), "swan_rule_live": mv.SWAN_RULE, "fresh_days": mv.SWAN_FRESH_DAYS,
                         "top_days_live": zn.TOP_DAYS, "top_days_variants": list(TOP_DAYS_VARIANTS),
                         "early_pct": EARLY_PCT, "hit_dd": HIT_DD, "fwd_days": FWD_DAYS, "decline_dd": DECLINE_DD,
                         "lead_days": LEAD_DAYS},
            "note": "Kursindeks uden udbytte; lukkekurser; ingen kurtage. Fortiden er ingen garanti."}


def summary(doc: dict) -> str:
    lines = [f"Analyse pr. {doc.get('generated_for')}"]
    m = doc.get("macro")
    if m:
        lines.append(f"Sort svane {m['start']}–{m['end']} ({m['weeks']} uger), {len(m['declines'])} store fald i S&P 500 (≥ {DECLINE_DD:g} %):")
        for r, x in m["rules"].items():
            lines.append(f"  {r:10} tændt {x['pct_on']:5.1f} % af ugerne (sidste 2 år {x['pct_on_last_2y']:5.1f} %) · {x['alarms']:3} varsler · "
                         f"længste {x['longest_weeks']:3} uger · ramte {x['hits']}/{x['alarms']} ({x['hit_rate']} %) · "
                         f"tidligt i {x['declines_early']}/{x['declines']} fald, før bunden i {x['declines_before_bottom']}/{x['declines']} · "
                         f"i dag {'TÆNDT' if x['now']['swan'] else 'slukket'}")
    for mid, var in (doc.get("zones") or {}).items():
        lines.append(f"Top-alder {mid}:")
        for td, x in var.items():
            lines.append(f"  {td:>3} dage: {x['fills']:3} trancher · tidlige {x['early_fills']:3} (1 år median {x['early_fwd_1y_median']}) · "
                         f"alle 1 år {x['fwd_1y_median']} · 3 år {x['fwd_3y_median']} · vægtet 3 år/nedtur {x['fwd_3y_weighted_avg']} · "
                         f"dybe fald købt {x['deep_declines_bought']}/{x['deep_declines']} · nu: {x['now']['next']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=None)
    ap.add_argument("--summary", default=None)
    ap.add_argument("--ref-date", default=None)
    a = ap.parse_args(argv)
    if a.summary:
        print(summary(json.loads(Path(a.summary).read_text(encoding="utf-8"))))
        return 0
    ref_day = a.ref_date or dt.date.today().isoformat()
    fred_client = fr.FredClient() if os.environ.get(fr.KEY_ENV) else None
    eod_client = eo.EodhdClient() if eo.api_key() else None
    if eod_client is None:
        print("FEJL: EODHD_API_KEY mangler")
        return 1
    series, rows, markets = fetch_all(ref_day, fred_client, eod_client)
    doc = run(series, rows, markets, ref_day)
    text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
    print(summary(doc))
    print("ANALYSE_JSON_START")
    print(text)
    print("ANALYSE_JSON_END")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
