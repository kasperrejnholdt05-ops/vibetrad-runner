"""Makro for Europa og Norden fra EODHD: centralbanker, inflation, ledighed, vækst og statsrenter.

FRED dækker kun USA. Investoren handler i SEK, DKK og EUR, så agenterne skal have Riksbanken, Nationalbanken,
ECB og Fed i samme facit: seneste rentebeslutning, næste møde (med markedets forventning), seneste inflation,
ledighed og BNP-vækst fra EODHD's økonomiske kalender, samt 10-årige statsrenter (EODHD GBOND-serier).
Ingen LLM-kald. Omkring 10 EODHD-kald pr. kørsel.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

COUNTRIES: list[tuple[str, str, str]] = [  # (ISO-2 i EODHD's kalender, centralbank, valuta)
    ("SE", "Riksbanken", "SEK"),
    ("DK", "Nationalbanken", "DKK"),
    ("EU", "ECB", "EUR"),
    ("US", "Fed", "USD"),
]
BONDS: list[tuple[str, str]] = [("SE10Y.GBOND", "Sverige 10 år"), ("DK10Y.GBOND", "Danmark 10 år"),
                                ("DE10Y.GBOND", "Tyskland 10 år")]
_RATE = "Interest Rate Decision"
_SERIES: list[tuple[str, str, str | None]] = [  # (type i kalenderen, visningsnavn, comparison)
    ("Inflation Rate", "inflation (år/år)", "yoy"),
    ("CPIF", "CPIF (år/år)", "yoy"),
    ("Core Inflation Rate", "kerneinflation (år/år)", "yoy"),
    ("Unemployment Rate", "ledighed", None),
    ("GDP Growth Rate", "BNP-vækst (år/år)", "yoy"),
]


def _d(value: Any) -> str | None:
    s = str(value or "")[:10]
    try:
        dt.date.fromisoformat(s)
        return s
    except ValueError:
        return None


def _num(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def summarize_events(events: list[dict], ref_day: str) -> dict:
    """Seneste rentebeslutning, næste møde og seneste nøgletal fra én landekalender."""
    out: dict[str, Any] = {"rate": None, "rate_date": None, "next_meeting": None, "next_estimate": None,
                           "next_previous": None, "series": []}
    rates = [e for e in events if e.get("type") == _RATE and _d(e.get("date"))]
    past = [e for e in rates if _d(e["date"]) <= ref_day and _num(e.get("actual")) is not None]
    future = [e for e in rates if _d(e["date"]) > ref_day]
    if past:
        last = max(past, key=lambda e: _d(e["date"]))
        out["rate"], out["rate_date"] = _num(last["actual"]), _d(last["date"])
    if future:
        nxt = min(future, key=lambda e: _d(e["date"]))
        out["next_meeting"] = _d(nxt["date"])
        out["next_estimate"] = _num(nxt.get("estimate"))
        out["next_previous"] = _num(nxt.get("previous"))
        if out["rate"] is None and out["next_previous"] is not None:  # ingen beslutning i vinduet: "previous" er niveauet
            out["rate"] = out["next_previous"]
    for typ, label, cmp in _SERIES:
        rows = [e for e in events if e.get("type") == typ and (cmp is None or e.get("comparison") == cmp)
                and _d(e.get("date")) and _d(e["date"]) <= ref_day and _num(e.get("actual")) is not None]
        if rows:
            last = max(rows, key=lambda e: _d(e["date"]))
            out["series"].append({"type": typ, "label": label, "value": _num(last["actual"]),
                                  "previous": _num(last.get("previous")), "period": last.get("period"),
                                  "date": _d(last["date"])})
    return out


def snapshot(client: Any, ref_day: str, *, back_days: int = 200, ahead_days: int = 120) -> dict:
    """Hent kalender og statsrenter. ``client`` er runnerens EodhdClient (economic_events, eod)."""
    ref = dt.date.fromisoformat(ref_day)
    start, end = (ref - dt.timedelta(days=back_days)).isoformat(), (ref + dt.timedelta(days=ahead_days)).isoformat()
    snap: dict[str, Any] = {"ref_date": ref_day, "source": "eodhd", "countries": [], "bonds": [], "errors": []}
    recent = (ref - dt.timedelta(days=45)).isoformat()
    for iso2, bank, ccy in COUNTRIES:
        try:
            # Rentebeslutninger alene over et langt vindue (få rækker), nøgletal kun for de sidste 45 dage, så
            # kalenderens grænse på 200 rækker ikke skærer beslutningen væk. USA's nøgletal kommer fra FRED.
            events = list(client.economic_events(iso2, start, end, 200, event_type=_RATE))
            if iso2 != "US":
                events += [e for e in client.economic_events(iso2, recent, ref_day, 200) if e.get("type") != _RATE]
            row = {"country": iso2, "bank": bank, "currency": ccy, **summarize_events(events, ref_day)}
        except Exception as exc:  # noqa: BLE001 - makro må aldrig stoppe kørslen
            row = {"country": iso2, "bank": bank, "currency": ccy, "error": f"{type(exc).__name__}"}
            snap["errors"].append(f"{iso2}: {type(exc).__name__}")
        snap["countries"].append(row)
    for sym, label in BONDS:
        try:
            rows = [r for r in client.eod(sym, (ref - dt.timedelta(days=45)).isoformat(), ref_day) if _num(r.get("close")) is not None]
            if not rows:
                continue
            last = rows[-1]
            month_ago = (ref - dt.timedelta(days=30)).isoformat()
            prev = next((r for r in rows if _d(r.get("date")) and _d(r["date"]) >= month_ago), rows[0])
            snap["bonds"].append({"symbol": sym, "label": label, "value": _num(last["close"]), "date": _d(last.get("date")),
                                  "change": round(_num(last["close"]) - _num(prev["close"]), 2) if prev is not last else None,
                                  "prev_date": _d(prev.get("date")) if prev is not last else None})
        except Exception as exc:  # noqa: BLE001
            snap["errors"].append(f"{sym}: {type(exc).__name__}")
    snap["ok"] = any(c.get("rate") is not None or c.get("next_meeting") for c in snap["countries"]) or bool(snap["bonds"])
    return snap


def _dmy(iso: str | None) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]}" if iso and len(iso) >= 10 else "–"


def _pct(v: float | None, digits: int = 2) -> str:
    return "–" if v is None else f"{v:.{digits}f} %".replace(".", ",")


def agent_brief(snap: dict | None) -> str:
    """Markdown-blok til alle agenters instruks. Tom, når intet blev hentet."""
    if not snap or not snap.get("ok"):
        return ""
    lines = [f"## Makro (Europa og Norden, EODHD) pr. {_dmy(snap.get('ref_date'))}",
             "Centralbanker og nøgletal for de valutaer, porteføljen handles i. Brug dem som facit for renteniveau og "
             "næste rentemøde; renteforventninger ud over mødets estimat er din egen vurdering.",
             "", "| Centralbank | Styringsrente | Næste møde | Seneste nøgletal |", "|---|---|---|---|"]
    for c in snap.get("countries", []):
        if c.get("error"):
            lines.append(f"| {c['bank']} ({c['currency']}) | – | – | kalender utilgængelig |")
            continue
        rate = _pct(c.get("rate")) + (f" ({_dmy(c.get('rate_date'))})" if c.get("rate_date") else "")
        nxt = _dmy(c.get("next_meeting")) if c.get("next_meeting") else "–"
        if c.get("next_meeting") and c.get("next_estimate") is not None:
            nxt += f", ventet {_pct(c['next_estimate'])}"
        series = "; ".join(f"{s['label']} {_pct(s['value'], 1)}" + (f" ({s['period']})" if s.get("period") else "")
                           for s in c.get("series", [])) or "–"
        lines.append(f"| {c['bank']} ({c['currency']}) | {rate} | {nxt} | {series} |")
    if snap.get("bonds"):
        lines.append("")
        lines.append("Statsrenter (10 år): " + " · ".join(
            f"{b['label']} {_pct(b['value'])}" + (f" ({b['change']:+.2f} pp siden {_dmy(b['prev_date'])})".replace(".", ",")
                                                  if b.get("change") is not None else "")
            for b in snap["bonds"]))
    return "\n".join(lines)


def status_line(snap: dict | None) -> str:
    """Én linje til GitHub-loggen (kun offentlige makrotal)."""
    if not snap:
        return "Makro EU/Norden: ikke hentet"
    if not snap.get("ok"):
        return "Makro EU/Norden: ingen data" + (f" ({'; '.join(snap.get('errors', [])[:3])})" if snap.get("errors") else "")
    parts = []
    for c in snap.get("countries", []):
        if c.get("rate") is not None:
            parts.append(f"{c['bank']} {_pct(c['rate'])}" + (f" → {_dmy(c['next_meeting'])}" if c.get("next_meeting") else ""))
    for b in snap.get("bonds", [])[:1]:
        parts.append(f"{b['label']} {_pct(b['value'])}")
    return "Makro EU/Norden: ja (" + " · ".join(parts) + ")" if parts else "Makro EU/Norden: ja"
