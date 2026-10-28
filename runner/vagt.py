"""Vagthunden: makrovagt + zonemotor for USA og Norden. Skriver results/vagt.json (kun markedsdata, offentlig).

Kører på GitHub tre gange hver hverdag (efter lukning i Norden og i USA, og om morgenen) uden LLM og uden
porteføljedata. Vibetrad henter filen via synkroniseringen og regner selv dine beløb, ordresedler og aktier
live med EODHD, når du åbner siden.

    python runner/vagt.py                 # rigtig kørsel (EODHD_API_KEY og FRED_API_KEY fra miljøet)
    python runner/vagt.py --out /tmp/x    # skriv et andet sted hen
    python runner/vagt.py --offline DIR   # brug gemte kurser (DIR/<symbol>.json) – til test
    python runner/vagt.py --merge FIL     # læg en allerede bygget vagt.json ind igen (efter afvist push)
    python runner/vagt.py --plan-start D  # anden startdato for planen (ÅÅÅÅ-MM-DD) – til test

Exit-kode 1, hvis ingen af de fem markeder gav kurser (fx udløbet EODHD-nøgle): zonerne er vagthundens kerne, så
kørslen bliver rød, og GitHub sender en mail. Filen skrives og gemmes stadig, så Vibetrad viser "0/5 markeder" i
stedet for gårsdagens tal. Databrud, huller og forældede kurser stopper aldrig kørslen; de står i ``data``.

Live-status regner fra planens start (``zoner.PLAN_START``, 27/10/2026): trancher, reglerne ville have udløst før,
blev aldrig købt og er derfor klar igen. Backtest og afspilning viser hele historikken uden den dato.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eodhd as eo  # noqa: E402
import fred as fr  # noqa: E402
import makrovagt as mv  # noqa: E402
import zoner as zn  # noqa: E402

SCHEMA = 1
HISTORY_MAX = 180
# Afspilning ("Bevis" i Vibetrad): hvad motoren ville have vist og købt uge for uge.
REPLAYS: tuple[tuple[str, str, str, str], ...] = (
    ("2008", "Finanskrisen 2007–09", "2007-07-01", "2010-12-31"),
    ("2020", "Corona-krakket 2020", "2019-12-01", "2021-03-31"),
    ("2022", "Rentechokket 2022", "2021-11-01", "2024-03-31"),
)
REPLAY_MARKETS = ("sp500", "omxs30")
HISTORY_START = {"GSPC.INDX": "1960-01-01", "NDX.INDX": "1985-01-01", "OMXS30.INDX": "1986-01-01",
                 "OMXC25.INDX": "2010-01-01", "OBX.OL": "1999-01-01"}


def stockholm_now() -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Europe/Stockholm"))
    except Exception:  # noqa: BLE001
        return dt.datetime.now(dt.timezone.utc)


def _r(obj: Any, d: int = 4) -> Any:
    if isinstance(obj, float):
        return round(obj, d)
    if isinstance(obj, dict):
        return {k: _r(v, d) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_r(v, d) for v in obj]
    return obj


def chart_points(series: list[tuple[str, float]], days: int = 756, step: int = 5) -> list[list]:
    """Ugentlige lukkekurser for de seneste ``days`` handelsdage – altid med sidste dag, og aldrig to gange."""
    pts = [[d, round(c, 2)] for d, c in series[-days::step]]
    if series and (not pts or pts[-1][0] != series[-1][0]):
        pts.append([series[-1][0], round(series[-1][1], 2)])
    return pts


def market_block(m: dict, rows: list[dict], ref_day: str,
                 plan_start: str | None = zn.PLAN_START) -> tuple[dict, list[tuple[str, float]]]:
    series, issues = zn.guard(rows, name=m["name"])
    out: dict[str, Any] = {"id": m["id"], "name": m["name"], "region": m["region"], "symbol": m["symbol"],
                           "data": {"rows": len(series), "from": series[0][0] if series else None,
                                    "to": series[-1][0] if series else None, "issues": issues}}
    if len(series) < 300:
        out["ok"] = False
        out["note"] = "for få kurser fra EODHD"
        return out, series
    age = (dt.date.fromisoformat(ref_day) - dt.date.fromisoformat(series[-1][0])).days
    if age > 5:
        issues.append({"kind": "forældet", "note": f"seneste kurs er {age} dage gammel ({zn._dmy(series[-1][0])})"})
    st = zn.analyse(series, kind="index", name=m["name"], symbol=m["symbol"], arm_from=plan_start)
    bt = zn.backtest(series)
    out.update(ok=True, state=st, backtest={
        "from": bt["from"], "years": bt["years"], "fills": bt["fills"], "by_zone": bt["by_zone"], "by_kind": bt["by_kind"],
        "episodes": [e for e in bt["episodes"] if e["max_dd_pct"] >= 15][-14:], "note": bt["note"]})
    out["chart"] = chart_points(series)  # 3 års lukkekurser (ugentligt) til grafen på siden
    return out, series


def build(ref_day: str, fetch_eod, fred_client: Any, eod_client: Any, *, generated_at: str,
          plan_start: str | None = zn.PLAN_START) -> dict:
    buffett = None
    if fred_client is not None and getattr(fred_client, "available", False):
        try:
            start_q = (dt.date.fromisoformat(ref_day) - dt.timedelta(days=365 * (fr.BUFFETT_YEARS + 1))).isoformat()
            buffett = fr.buffett_ratio(fred_client.observations(fr.BUFFETT_EQUITY, start_q, ref_day),
                                       fred_client.observations(fr.BUFFETT_GDP, start_q, ref_day), ref_day)
        except Exception:  # noqa: BLE001 - Buffett er baggrund; vagten kører videre
            buffett = None
    macro = mv.collect(ref_day, fred_client, eod_client, buffett)
    macro["generated_at"] = generated_at
    markets, series_by = [], {}
    for m in zn.MARKETS:
        try:
            rows = fetch_eod(m["symbol"], HISTORY_START.get(m["symbol"], "1990-01-01"), ref_day)
        except Exception as exc:  # noqa: BLE001
            rows = []
            markets.append({"id": m["id"], "name": m["name"], "region": m["region"], "symbol": m["symbol"], "ok": False,
                            "note": f"EODHD-fejl: {type(exc).__name__}"})
            continue
        blk, series = market_block(m, rows, ref_day, plan_start)
        markets.append(blk)
        series_by[m["id"]] = series
    replays = []
    for rid, label, a, b in REPLAYS:
        per = {}
        for mid in REPLAY_MARKETS:
            s = series_by.get(mid)
            if s and s[0][0] <= a:
                per[mid] = zn.replay(s, a, b)
        replays.append({"id": rid, "label": label, "start": a, "end": b, "markets": per})
    ok = [x for x in markets if x.get("ok")]
    zones = {x["id"]: x["state"]["zone"] for x in ok}
    worst = max(zones.values(), key=lambda z: ["normal", "korrektion", "bjorn", "kollaps"].index(z)) if zones else "normal"
    return _r({
        "schema": SCHEMA, "generated_at": generated_at, "ref_date": ref_day,
        "summary": {"macro_level": macro.get("level"), "black_swan": macro.get("black_swan"), "worst_zone": worst,
                    "zones": zones, "markets_ok": len(ok), "markets": len(markets)},
        "macro": macro, "markets": markets, "replays": replays,
        "rules": {"plan_start": plan_start, "top_days": zn.TOP_DAYS, "confirm_days": zn.CONFIRM_DAYS, "foundation_days": zn.FOUNDATION_DAYS,
                  "nordnet_limit_pct": zn.NORDNET_LIMIT_PCT, "zones_index": [list(z) for z in zn.ZONES_INDEX],
                  "zones_stock": [list(z) for z in zn.ZONES_STOCK], "zone_weights": zn.ZONE_WEIGHTS},
        "note": "Kun markedsdata (EODHD og FRED). Analyse og regler, ikke rådgivning. Kursindeks uden udbytte.",
    })


def history_entry(doc: dict) -> dict:
    s = doc["summary"]
    return {"at": doc["generated_at"], "macro": s["macro_level"], "swan": s["black_swan"], "zones": s["zones"],
            "dd": {m["id"]: m["state"]["dd_pct"] for m in doc["markets"] if m.get("ok")}}


def write(doc: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "vagt.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    hp = out_dir / "vagt_history.json"
    try:
        hist = json.loads(hp.read_text(encoding="utf-8"))
        if not isinstance(hist, list):
            hist = []
    except (OSError, ValueError):
        hist = []
    hist.append(history_entry(doc))
    hp.write_text(json.dumps(hist[-HISTORY_MAX:], ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


class OfflineEod:
    """Kurser fra gemte filer (DIR/<symbol>.json) – til test uden nøgle."""

    available = True

    def __init__(self, folder: str):
        self.folder = Path(folder)

    def eod(self, symbol: str, start: str | None = None, end: str | None = None, period: str = "d") -> list[dict]:
        p = self.folder / f"{symbol}.json"
        if not p.exists():
            return []
        rows = json.loads(p.read_text(encoding="utf-8"))
        return [r for r in rows if (not start or r["date"] >= start) and (not end or r["date"] <= end)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "results"))
    ap.add_argument("--offline", default=None)
    ap.add_argument("--ref-date", default=None)
    ap.add_argument("--merge", default=None)
    ap.add_argument("--plan-start", default=zn.PLAN_START)
    a = ap.parse_args(argv)
    if a.merge:
        write(json.loads(Path(a.merge).read_text(encoding="utf-8")), Path(a.out))
        return 0
    now = stockholm_now()
    ref_day = a.ref_date or now.date().isoformat()
    if a.offline:
        eod_client, fred_client = OfflineEod(a.offline), None
    else:
        eod_client = eo.EodhdClient() if eo.api_key() else None
        fred_client = fr.FredClient() if os.environ.get(fr.KEY_ENV) else None
    fetch = (lambda s, f, t: eod_client.eod(s, f, t)) if eod_client else (lambda s, f, t: [])
    doc = build(ref_day, fetch, fred_client, eod_client, generated_at=now.isoformat(timespec="seconds"),
                plan_start=a.plan_start or None)
    write(doc, Path(a.out))
    s = doc["summary"]
    # Loggen er offentlig: kun status, ingen tal om dig (der er ingen) og ingen nøgler.
    print(f"Vagt {ref_day}: {mv.status_line(doc['macro'])} · markeder {s['markets_ok']}/{s['markets']} · "
          f"værste zone {s['worst_zone']} · " + ", ".join(f"{k} {v}" for k, v in s["zones"].items()))
    for m in doc["markets"]:
        for i in (m.get("data") or {}).get("issues", []):
            print(f"  data {m['name']}: {i['kind']}")
    if not s["markets_ok"]:
        print("FEJL: ingen markeder gav kurser – tjek EODHD_API_KEY (zonerne kan ikke regnes)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
