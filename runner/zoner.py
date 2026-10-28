"""Zonemotoren: hvornår skal der købes under en korrektion eller et kollaps (ingen LLM, ingen porteføljedata).

Reglerne er de samme i runneren (her, testet og backtestet) og i Vibetrad (vagt_js), så siden og GitHub
altid siger det samme. Fælles testvektorer ligger i tests/fixtures/zoner_cases.json og køres i begge sprog.

Begreber
- **Top og episode**: en episode starter ved en ny top (højeste lukkekurs i episoden). Ny top = ny episode, og alle
  trancher er klar igen. Er toppen over ``TOP_DAYS`` handelsdage (3 år) gammel, flyttes den til det seneste års
  højeste lukkekurs (ny episode): OMXS30 nåede først 2000-toppen igen i 2015, så med all-time high var
  2008-krakket aldrig blevet en zone. Ved start bruges de seneste 3 års højeste lukkekurs.
- **Zoner**: fald fra toppen. Indeks: korrektion 10–20 %, bjørn 20–30 %, kollaps ≥ 30 %. Enkeltaktier svinger
  mere: 20–35 %, 35–50 %, ≥ 50 %. En aktie kan også have faste kursniveauer (fx SpaceX 131–140/105–118/75–95).
- **Tre trancher pr. zone** (Kaspers kollaps-strategi 26/10/2026):
  1. *Liggende*: limitordre ved zonens øverste kurs. Udløses, når en lukkekurs når den.
  2. *Bekræftelse*: første lukkekurs over de seneste ``CONFIRM_DAYS`` dages højeste lukkekurs efter bunden.
     Køber 2. tranche i alle zoner, der er nået (faldt kursen lige igennem korrektionen, købes dens
     bekræftelse også ved bunden).
  3. *Fundament*: ``FOUNDATION_DAYS`` handelsdage efter bunden uden ny bund, og kun efter bekræftelsen.
- **Nordnet** tager kun limitkurser op til ca. ``NORDNET_LIMIT_PCT`` % under kursen (26/10/2026: 113 blev afvist
  for SpaceX ved 147). Ligger tranchen dybere, bliver den en kursalarm i stedet for en ordre.

Datakontrol: EODHD's OMXS30 har et brud 14/04 → 20/04/1998, hvor kurserne er divideret med 4,13. ``guard``
finder den slags spring (mere end en halvering eller fordobling på én observation), skalerer de ældre kurser og
melder det, så et databrud aldrig ligner et krak.
"""
from __future__ import annotations

import datetime as dt
import statistics
from typing import Any, Iterable

TOP_DAYS = 756            # ca. 3 år
# Planen gælder fra Kaspers beslutning 27/10/2026. Trancher, motoren "udløser" før, blev aldrig købt (fx OMXC25's
# korrektion og bjørn under Novo-faldet 2024–25), så live-status skal regne dem som klar igen (``arm_from``).
# Backtest og afspilning bruger ikke datoen: de viser, hvad reglerne ville have gjort i hele historikken.
PLAN_START = "2026-10-27"
CONFIRM_DAYS = 10
FOUNDATION_DAYS = 20
NORDNET_LIMIT_PCT = 13.0
BREAK_RATIO = 2.0         # et spring over x2 eller under x0,5 på én observation er et databrud

ZONES_INDEX: tuple[tuple[str, str, float, float | None], ...] = (
    ("korrektion", "Korrektion", 10.0, 20.0),
    ("bjorn", "Bjørnemarked", 20.0, 30.0),
    ("kollaps", "Kollaps", 30.0, None),
)
ZONES_STOCK: tuple[tuple[str, str, float, float | None], ...] = (
    ("korrektion", "Korrektion", 20.0, 35.0),
    ("bjorn", "Bjørnemarked", 35.0, 50.0),
    ("kollaps", "Kollaps", 50.0, None),
)
# Reservens fordeling på zonerne (mest til de dybeste fald) og på trancherne i hver zone.
ZONE_WEIGHTS = {"korrektion": 0.2, "bjorn": 0.3, "kollaps": 0.5}
TRANCHE_WEIGHTS = (1 / 3, 1 / 3, 1 / 3)
KINDS = ("liggende", "bekraeftelse", "fundament")
KIND_DA = {"liggende": "Liggende ordre", "bekraeftelse": "Bekræftelse", "fundament": "Fundament"}

MARKETS: tuple[dict[str, str], ...] = (
    {"id": "sp500", "symbol": "GSPC.INDX", "name": "S&P 500", "region": "USA"},
    {"id": "ndx", "symbol": "NDX.INDX", "name": "Nasdaq 100", "region": "USA"},
    {"id": "omxs30", "symbol": "OMXS30.INDX", "name": "OMXS30", "region": "Sverige"},
    {"id": "omxc25", "symbol": "OMXC25.INDX", "name": "OMXC25", "region": "Danmark"},
    {"id": "obx", "symbol": "OBX.OL", "name": "OBX", "region": "Norge"},
)


# ---------------------------------------------------------------------------
# Datakontrol
def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x > 0 else None


def guard(rows: Iterable[dict | tuple], *, name: str = "") -> tuple[list[tuple[str, float]], list[dict]]:
    """Rensede lukkekurser [(dato, kurs)] + liste over fund (databrud, huller, dubletter, ugyldige kurser)."""
    raw: dict[str, float] = {}
    issues: list[dict] = []
    bad = 0
    for r in rows:
        if isinstance(r, dict):
            d = str(r.get("date") or "")[:10]
            v = _f(r.get("adjusted_close") if r.get("adjusted_close") is not None else r.get("close"))
        else:
            d, v = str(r[0])[:10], _f(r[1])
        if len(d) != 10 or v is None:
            bad += 1
            continue
        raw[d] = v  # dublet: den sidste vinder
    if bad:
        issues.append({"kind": "ugyldig", "count": bad, "note": f"{bad} rækker uden gyldig dato/kurs er sprunget over"})
    data = sorted(raw.items())
    for k in range(len(data) - 1, 0, -1):
        (d0, a), (d1, b) = data[k - 1], data[k]
        ratio = b / a
        if ratio > BREAK_RATIO or ratio < 1 / BREAK_RATIO:
            for i in range(k):
                data[i] = (data[i][0], data[i][1] * ratio)
            issues.append({"kind": "databrud", "from": d0, "to": d1, "ratio": round(ratio, 4),
                           "note": f"{name or 'Serien'} springer fra {a:g} til {b:g} ({_dmy(d0)} → {_dmy(d1)}); "
                                   "ældre kurser er skaleret, så bruddet ikke ligner et kursfald"})
    gaps = [(data[k - 1][0], data[k][0]) for k in range(1, len(data))
            if (dt.date.fromisoformat(data[k][0]) - dt.date.fromisoformat(data[k - 1][0])).days > 14]
    if gaps:
        issues.append({"kind": "huller", "count": len(gaps),
                       "note": f"{len(gaps)} huller på over 14 dage (største {_dmy(gaps[0][0])} → {_dmy(gaps[0][1])})"})
    return data, issues


def _dmy(d: str | None) -> str:
    try:
        return dt.date.fromisoformat(str(d)[:10]).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return "–"


# ---------------------------------------------------------------------------
# Zoner
def zone_table(top: float, zones: Any = ZONES_INDEX, levels: list[dict] | None = None) -> list[dict]:
    """Zonerne med kurser. ``levels`` = faste kursniveauer [{id, label, hi, lo}] (lo kan være None)."""
    if levels:
        out = []
        for z in levels:
            hi, lo = float(z["hi"]), (float(z["lo"]) if z.get("lo") is not None else None)
            out.append({"id": z["id"], "label": z.get("label") or z["id"], "price_hi": hi, "price_lo": lo,
                        "pct_hi": round((1 - hi / top) * 100, 2), "pct_lo": None if lo is None else round((1 - lo / top) * 100, 2)})
        return out
    return [{"id": zid, "label": lab, "pct_hi": a, "pct_lo": b, "price_hi": top * (1 - a / 100),
             "price_lo": None if b is None else top * (1 - b / 100)} for zid, lab, a, b in zones]


def zone_at(price: float, table: list[dict]) -> str:
    cur = "normal"
    for z in table:
        if price <= z["price_hi"]:
            cur = z["id"]
    return cur


def walk(series: list[tuple[str, float]], *, zones: Any = ZONES_INDEX, levels: list[dict] | None = None,
         top_days: int = TOP_DAYS, confirm_days: int = CONFIRM_DAYS, foundation_days: int = FOUNDATION_DAYS,
         start: int = 0, arm_from: str | None = None) -> dict:
    """Går lukkekurserne igennem dag for dag og returnerer alle udløste trancher + tilstanden på sidste dag.

    Én motor til både live-status (sidste dag) og backtest (alle dage), så de aldrig kan være uenige.
    Med faste kursniveauer (``levels``) bruges toppen kun til faldet i procent; zonerne ligger fast.
    Trancher udløses aldrig bagud i tid: starter en episode midt i et fald (start eller flyttet top), kan
    den liggende ordre højst udløses samme dag til dagens kurs.

    ``arm_from`` (dato): planen gælder først fra den dag. Trancher, der "udløstes" før, blev aldrig købt, så de
    er klar igen: på den første dag fra ``arm_from`` nulstilles episodens trancher og nåede zoner, og bunden
    regnes fra den dag. Ligger kursen da allerede under en zone, udløses den liggende ordre samme dag.
    """
    n = len(series)
    closes = [c for _, c in series]
    fills: list[dict] = []
    episodes: list[dict] = []
    ep: dict | None = None
    armed = not arm_from

    def close_ep(e: dict | None, end_i: int) -> None:
        if e is not None:
            b = e["bottom_i"]
            episodes.append({"top_date": series[e["top_i"]][0], "top": e["top"], "bottom_date": series[b][0],
                             "bottom": closes[b], "max_dd_pct": round((1 - closes[b] / e["top"]) * 100, 2),
                             "end_date": series[end_i][0]})

    def new_ep(ti: int, upto: int) -> dict:
        top = closes[ti]
        bi = min(range(ti, upto + 1), key=lambda j: (closes[j], -j))
        return {"top_i": ti, "top": top, "table": zone_table(top, zones, levels), "bottom_i": bi, "done": set(),
                "reached": set()}

    for i in range(n):
        c = closes[i]
        if i < start:
            continue
        if ep is None:
            lo = max(0, i - top_days + 1)
            ep = new_ep(max(range(lo, i + 1), key=lambda j: (closes[j], j)), i)
            if ep["top_i"] == i:
                continue  # første dag er selv toppen: intet at købe
        if c > ep["top"]:
            close_ep(ep, i - 1)
            ep = new_ep(i, i)  # ny top: ny episode, alle trancher klar igen
            continue
        if i - ep["top_i"] > top_days:
            # Toppen er over 3 år gammel (lang bjørn, fx 2000–03): flyt den til det seneste års højeste luk.
            close_ep(ep, i - 1)
            lo = max(0, i - 251)
            ep = new_ep(max(range(lo, i + 1), key=lambda j: (closes[j], j)), i)
            if ep["top_i"] == i:
                continue
        if not armed:
            if series[i][0] < arm_from and i < n - 1:  # ligger plandagen efter sidste kurs, gælder den fra sidste dag
                if c < closes[ep["bottom_i"]]:
                    ep["bottom_i"] = i
                continue
            armed = True
            ep["done"], ep["reached"], ep["bottom_i"] = set(), set(), i
        if c < closes[ep["bottom_i"]]:
            ep["bottom_i"] = i
        table = ep["table"]
        bottom = closes[ep["bottom_i"]]
        for z in table:
            if bottom <= z["price_hi"]:
                ep["reached"].add(z["id"])
        # 1) liggende ordre: en lukkekurs ved/under zonens øverste kurs
        for z in table:
            key = (z["id"], "liggende")
            if key not in ep["done"] and c <= z["price_hi"]:
                ep["done"].add(key)
                fills.append(_fill(series, i, z, "liggende", min(c, z["price_hi"]), ep))
        # 2) bekræftelse: luk over de seneste confirm_days' højeste luk efter bunden
        bi = ep["bottom_i"]
        if ep["reached"] and i > bi and i - confirm_days >= 0:
            prev_hi = max(closes[i - confirm_days:i])
            if c > prev_hi:
                for z in table:
                    key = (z["id"], "bekraeftelse")
                    if z["id"] in ep["reached"] and key not in ep["done"]:
                        ep["done"].add(key)
                        fills.append(_fill(series, i, z, "bekraeftelse", c, ep))
        # 3) fundament: foundation_days efter bunden uden ny bund, efter bekræftelsen
        if ep["reached"] and i - bi >= foundation_days:
            for z in table:
                key = (z["id"], "fundament")
                if z["id"] in ep["reached"] and (z["id"], "bekraeftelse") in ep["done"] and key not in ep["done"]:
                    ep["done"].add(key)
                    fills.append(_fill(series, i, z, "fundament", c, ep))
    close_ep(ep, n - 1)
    return {"fills": fills, "episode": ep, "episodes": episodes, "last_i": n - 1}


def _fill(series: list[tuple[str, float]], i: int, z: dict, kind: str, price: float, ep: dict) -> dict:
    return {"date": series[i][0], "i": i, "zone": z["id"], "kind": kind, "price": price,
            "top": ep["top"], "top_date": series[ep["top_i"]][0], "dd_pct": round((1 - price / ep["top"]) * 100, 2)}


def analyse(series: list[tuple[str, float]], *, kind: str = "index", levels: list[dict] | None = None,
            name: str = "", symbol: str = "", nordnet_limit_pct: float = NORDNET_LIMIT_PCT,
            recent_days: int = 5, arm_from: str | None = None, top_days: int = TOP_DAYS) -> dict:
    """Status på sidste dag: zone, fald, bund, alle ni trancher med status og næste skridt."""
    zones = ZONES_STOCK if kind == "stock" else ZONES_INDEX
    if len(series) < CONFIRM_DAYS + 2:
        return {"symbol": symbol, "name": name, "kind": kind, "ok": False, "note": "for få kurser"}
    w = walk(series, zones=zones, levels=levels, arm_from=arm_from, top_days=top_days)
    ep = w["episode"]
    closes = [c for _, c in series]
    i = len(series) - 1
    c = closes[i]
    top = ep["top"]
    table = ep["table"]
    bi = ep["bottom_i"]
    bottom = closes[bi]
    ep_fills = [f for f in w["fills"] if f["i"] >= ep["top_i"]]
    done = {(f["zone"], f["kind"]): f for f in ep_fills}
    prev_hi = max(closes[i - CONFIRM_DAYS + 1:i + 1]) if i >= CONFIRM_DAYS else None
    tr = []
    for z in table:
        reached = z["id"] in ep["reached"]
        for n, k in enumerate(KINDS, start=1):
            f = done.get((z["id"], k))
            t = {"zone": z["id"], "zone_label": z["label"], "n": n, "kind": k, "kind_label": KIND_DA[k]}
            if f:
                age = i - f["i"]
                t.update(status="ny" if age < recent_days else "udløst", date=f["date"], price=round(f["price"], 4))
            elif k == "liggende":
                p = z["price_hi"]
                dist = (c / p - 1) * 100
                t.update(status="venter", price=round(p, 4), distance_pct=round(dist, 2),
                         order="limit" if dist <= nordnet_limit_pct else "alarm",
                         alarm_at=round(p / (1 - nordnet_limit_pct / 100), 4))
            elif not reached:
                t.update(status="ikke nået")
            elif k == "bekraeftelse":
                t.update(status="venter", level=round(prev_hi, 4) if prev_hi else None,
                         note="køb, når en lukkekurs er over de seneste 10 dages højeste")
            else:
                need = done.get((z["id"], "bekraeftelse"))
                days_left = max(FOUNDATION_DAYS - (i - bi), 0)
                t.update(status="venter", days_left=days_left,
                         note=("efter bekræftelsen og " if not need else "") + f"{FOUNDATION_DAYS} handelsdage uden ny bund"
                              + (f" ({days_left} tilbage)" if days_left else ""))
            tr.append(t)
    zone = zone_at(c, table)
    dd = (1 - c / top) * 100
    max_dd = (1 - bottom / top) * 100
    out = {"symbol": symbol, "name": name, "kind": kind, "ok": True, "date": series[i][0], "close": round(c, 4),
           "top": round(top, 4), "top_date": series[ep["top_i"]][0], "dd_pct": round(dd, 2),
           "bottom": round(bottom, 4), "bottom_date": series[bi][0], "max_dd_pct": round(max_dd, 2),
           "zone": zone, "zone_label": next((z["label"] for z in table if z["id"] == zone), "Normal"),
           "deepest": next((z["id"] for z in reversed(table) if z["id"] in ep["reached"]), "normal"),
           "confirm_level": round(prev_hi, 4) if prev_hi else None,
           "zones": [{**{k: (round(v, 4) if isinstance(v, float) else v) for k, v in z.items()}, "reached": z["id"] in ep["reached"]}
                     for z in table],
           "tranches": tr, "fixed_levels": bool(levels), "arm_from": arm_from}
    out["next"] = next_step(out, nordnet_limit_pct)
    return out


def next_step(a: dict, nordnet_limit_pct: float = NORDNET_LIMIT_PCT) -> dict:
    """Det ene vigtigste skridt for papiret i dag (dansk tekst + type)."""
    new = [t for t in a["tranches"] if t["status"] == "ny"]
    if new:
        t = new[-1]
        return {"type": "koeb", "text": f"Køb nu: {t['kind_label'].lower()} i {t['zone_label'].lower()} "
                                         f"(udløst {_dmy(t['date'])} ved {_nf(t['price'])})"}
    pend = [t for t in a["tranches"] if t["status"] == "venter" and t["kind"] == "liggende"]
    if pend:
        t = pend[0]
        if t["order"] == "limit":
            return {"type": "ordre", "text": f"Læg limitordre: {t['zone_label'].lower()} ved {_nf(t['price'])} "
                                              f"({_nf(t['distance_pct'], 1)} % under kursen)"}
        return {"type": "alarm", "text": f"Ingen ordre endnu: {t['zone_label'].lower()} ved {_nf(t['price'])} ligger "
                                          f"{_nf(t['distance_pct'], 1)} % under kursen. Sæt kursalarm ved {_nf(t['alarm_at'])} "
                                          f"(Nordnet tager kun ca. {nordnet_limit_pct:g} % under kursen)"}
    conf = [t for t in a["tranches"] if t["status"] == "venter" and t["kind"] == "bekraeftelse"]
    if conf:
        return {"type": "vent", "text": f"Vent på bekræftelse: luk over {_nf(conf[0].get('level'))}"}
    return {"type": "ro", "text": "Alle trancher i denne nedtur er brugt. Ingen handling før ny top."}


def _nf(v: float | None, d: int = 2) -> str:
    if v is None:
        return "–"
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ---------------------------------------------------------------------------
# Backtest og afspilning
def _fwd(closes: list[float], i: int, days: int) -> float | None:
    j = i + days
    return None if j >= len(closes) else closes[j] / closes[i] - 1


def backtest(series: list[tuple[str, float]], *, kind: str = "index", levels: list[dict] | None = None,
             warmup_days: int = 252, top_days: int = TOP_DAYS) -> dict:
    """Alle trancher, motoren ville have udløst i historikken, og hvad der skete bagefter.

    For hver udløst tranche: afkast efter 1 og 3 år og det yderligere fald, før kursen var tilbage på
    købskursen. Samlet pr. zone og pr. trancheform, plus et vægtet tal for hele reserven pr. nedtur
    (zonevægte 20/30/50, trancher 1/3 hver). Kursindeks uden udbytte: afkastet er lidt for lavt.
    """
    zones = ZONES_STOCK if kind == "stock" else ZONES_INDEX
    w = walk(series, zones=zones, levels=levels, start=min(warmup_days, max(len(series) - 1, 0)), top_days=top_days)
    closes = [c for _, c in series]
    rows = []
    for f in w["fills"]:
        i = f["i"]
        lo = f["price"]
        for x in closes[i + 1:]:
            if x >= f["price"]:
                break  # tilbage på købskursen: det yderligere fald er slut
            lo = min(lo, x)
        rows.append({**f, "fwd_1y": _fwd(closes, i, 252), "fwd_3y": _fwd(closes, i, 756),
                     "further_dd": lo / f["price"] - 1})

    def agg(sel: list[dict]) -> dict:
        def st(key: str) -> dict:
            v = [r[key] for r in sel if r[key] is not None]
            if not v:
                return {"n": 0}
            return {"n": len(v), "median": round(statistics.median(v) * 100, 1), "worst": round(min(v) * 100, 1),
                    "best": round(max(v) * 100, 1), "positive": sum(x > 0 for x in v)}
        return {"fills": len(sel), "fwd_1y": st("fwd_1y"), "fwd_3y": st("fwd_3y"), "further_dd": st("further_dd")}

    by_zone = {z[0]: agg([r for r in rows if r["zone"] == z[0]]) for z in zones}
    by_kind = {k: agg([r for r in rows if r["kind"] == k]) for k in KINDS}
    by_top: dict[str, list[dict]] = {}
    for r in rows:
        by_top.setdefault(r["top_date"], []).append(r)
    ep_sum = []
    for e in w["episodes"]:
        sel = by_top.get(e["top_date"], [])
        if not sel and e["max_dd_pct"] < 10:
            continue
        used = sum(ZONE_WEIGHTS[r["zone"]] * TRANCHE_WEIGHTS[KINDS.index(r["kind"])] for r in sel)
        r3 = [(ZONE_WEIGHTS[r["zone"]] * TRANCHE_WEIGHTS[KINDS.index(r["kind"])], r["fwd_3y"]) for r in sel if r["fwd_3y"] is not None]
        wavg = sum(wt * x for wt, x in r3) / sum(wt for wt, _ in r3) if r3 else None
        ep_sum.append({**{k: (round(v, 2) if isinstance(v, float) else v) for k, v in e.items()}, "fills": len(sel),
                       "first_fill": sel[0]["date"] if sel else None, "reserve_used_pct": round(used * 100),
                       "fwd_3y_weighted": None if wavg is None else round(wavg * 100, 1),
                       "zones": sorted({r["zone"] for r in sel}, key=[z[0] for z in zones].index)})
    years = (dt.date.fromisoformat(series[-1][0]) - dt.date.fromisoformat(series[0][0])).days / 365.25 if series else 0
    return {"from": series[0][0] if series else None, "to": series[-1][0] if series else None,
            "years": round(years, 1), "fills": len(rows), "by_zone": by_zone, "by_kind": by_kind,
            "episodes": ep_sum,
            "note": "Kursindeks uden udbytte; lukkekurser; ingen kurtage. Fortiden er ingen garanti."}


def replay(series: list[tuple[str, float]], start: str, end: str, *, kind: str = "index", step: int = 5) -> dict:
    """Uge for uge: hvad motoren viste og købte mellem ``start`` og ``end`` (til 'Bevis'-afsnittet)."""
    zones = ZONES_STOCK if kind == "stock" else ZONES_INDEX
    w = walk(series, zones=zones)
    idx = [i for i, (d, _) in enumerate(series) if start <= d <= end]
    if not idx:
        return {"start": start, "end": end, "points": [], "fills": []}
    pts = []
    for i in idx[::step] + ([idx[-1]] if idx[-1] not in idx[::step] else []):
        sub = walk(series[:i + 1], zones=zones)
        ep = sub["episode"]
        c = series[i][1]
        pts.append({"date": series[i][0], "close": round(c, 2), "dd_pct": round((1 - c / ep["top"]) * 100, 1),
                    "zone": zone_at(c, ep["table"])})
    fills = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in f.items() if k != "i"}
             for f in w["fills"] if start <= f["date"] <= end]
    return {"start": start, "end": end, "points": pts, "fills": fills}
