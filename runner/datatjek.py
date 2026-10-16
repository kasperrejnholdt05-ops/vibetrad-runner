#!/usr/bin/env python3
"""Datatjek: krydstjek af porteføljens data mellem Vibe-Tradings egne kilder og EODHD.

Før agenterne starter, hentes de samme tal fra to uafhængige kæder:

- **Vibe-Trading (HKUDS)**: præcis de loaders, agenternes ``get_market_data`` bruger
  (Yahoo → Stooq → … → yfinance), og Vibe-Tradings Yahoo-klient til nøgletal og
  regnskabsdatoer. EODHD holdes ude af denne kæde, så sammenligningen er ægte.
- **EODHD**: end-of-day-kurser, nøgletal, regnskabskalender, udbytter og splits.

Der sammenlignes lukkekurs, seneste handelsdag, ugeafkast, valuta, navn,
markedsværdi, regnskabsdatoer (også mod porteføljens egne datoer), valutakurser
og porteføljens værdi i SEK. Resultatet er PASS / ADVARSEL / FEJL med en kort
forklaring pr. afvigelse, plus et kompakt "facit" som alle agenter får.

Kan også køres alene i codespacet:
    python runner/datatjek.py --portfolio-file portefolje.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:  # runner/ ligger på stien, når run_week.py eller tests kører
    sys.path.insert(0, _HERE)
import eodhd as eo  # noqa: E402

STO = ZoneInfo("Europe/Stockholm")
BENCHMARK = "ACWI.US"

# Tolerancer (procent / procentpoint)
PRICE_OK, PRICE_WARN = 0.5, 2.0
WEEK_OK, WEEK_WARN = 0.5, 2.0
FX_OK, FX_WARN = 0.3, 1.0
VALUE_OK, VALUE_WARN = 0.5, 2.0
MCAP_OK, MCAP_WARN = 5.0, 20.0
STALE_DAYS = 6
LOCKUP_DAYS = 180
SHORT_HISTORY_DAYS = 365  # papirer børsnoteret for under et år siden: risikotal er statistisk usikre
PORTFOLIO_MAX_AGE_DAYS = 7  # VT_PORTFOLIO_MAX_AGE_DAYS kan ændre det

LABEL = {"ok": "PASS", "warn": "ADVARSEL", "fail": "FEJL"}
_STOP = {"inc", "corp", "corporation", "co", "class", "common", "stock", "shares", "share", "plc", "ab", "as", "a/s",
         "asa", "oyj", "nv", "sa", "se", "ag", "the", "ltd", "limited", "holding", "holdings", "group", "a", "b", "c",
         "ser", "series", "ordinary", "adr", "company", "incorporated"}


def _num(x: Any) -> float | None:
    if isinstance(x, dict):
        x = x.get("raw")
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def _date(x: Any) -> str | None:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return dt.datetime.fromtimestamp(float(x), tz=dt.timezone.utc).date().isoformat()
    if isinstance(x, dict):
        return _date(x.get("raw") if "raw" in x else x.get("fmt"))
    s = str(x).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    parts = (m.group(1), m.group(2), m.group(3)) if m else None
    if not parts:
        m = re.match(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", s)  # DD/MM/YYYY (europæisk)
        parts = (m.group(3), m.group(2), m.group(1)) if m else None
    if not parts:
        return None
    try:
        return dt.date(int(parts[0]), int(parts[1]), int(parts[2])).isoformat()
    except ValueError:  # fx 31/11 eller amerikansk 10/28/2026
        return None


def _weekdays(start: str, end: str) -> int:
    """Hverdage fra og med start til og med end (skøn for handelsdage, når kurserne ikke kan hentes)."""
    a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    return sum(1 for k in range((b - a).days + 1) if (a + dt.timedelta(days=k)).weekday() < 5) if b >= a else 0


def dmy(iso: str | None) -> str:
    if not iso:
        return "–"
    y, m, d = iso[:10].split("-")
    return f"{d}/{m}/{y}"


def dk(v: float, d: int = 2) -> str:
    """Dansk decimaltegn: 1.09 → '1,09'."""
    return f"{v:.{d}f}".replace(".", ",")


def pct_diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return abs(a - b) / abs(b) * 100


def grade(value: float | None, ok: float, warn: float) -> str:
    if value is None:
        return "warn"
    return "ok" if value <= ok else "warn" if value <= warn else "fail"


def worst(states: Iterable[str]) -> str:
    order = {"ok": 0, "info": 0, "warn": 1, "fail": 2}
    w = "ok"
    for s in states:
        if order.get(s, 0) > order[w]:
            w = s
    return w


def name_tokens(name: str | None) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", str(name or "").lower()) if t not in _STOP and len(t) > 1}


def names_match(a: str | None, b: str | None) -> bool | None:
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return None
    return bool(ta & tb) or "".join(sorted(ta)) == "".join(sorted(tb))


def week_return(rows: list[tuple[str, float]], week_from: str, week_to: str) -> float | None:
    before = [c for d, c in rows if d < week_from]
    inside = [c for d, c in rows if week_from <= d <= week_to]
    if not before or not inside or not before[-1]:
        return None
    return (inside[-1] / before[-1] - 1) * 100


def last_on_or_before(rows: list[tuple[str, float]], day: str) -> tuple[str, float] | None:
    hit = [(d, c) for d, c in rows if d <= day]
    return hit[-1] if hit else None


# ---------------------------------------------------------------------------
# Kilder
# ---------------------------------------------------------------------------
class VibeTradingSource:
    """Vibe-Tradings egne loaders og Yahoo-klient – uden EODHD i kæden."""

    label = "Vibe-Trading"

    def __init__(self) -> None:
        self.errors: list[str] = []
        try:
            import src.market_data  # noqa: F401
            from backtest.loaders import registry  # noqa: F401

            self.available = True
        except Exception as exc:  # noqa: BLE001
            self.available = False
            self.errors.append(f"Vibe-Trading kunne ikke indlæses ({type(exc).__name__})")

    @staticmethod
    def _provider(src: str) -> list[str]:
        from backtest.loaders.registry import FALLBACK_CHAINS

        for chain in FALLBACK_CHAINS.values():
            if src in chain:
                return [x for x in chain if x != "eodhd"]
        return [src]

    def prices(self, codes: list[str], start: str, end: str) -> dict[str, dict]:
        from src.market_data import fetch_market_data

        out: dict[str, dict] = {}
        eu = [c for c in codes if eo.is_eu_symbol(c)]
        rest = [c for c in codes if c not in eu]
        # Nordiske/europæiske symboler: Vibe-Tradings auto-routing sender dem til kinesiske kilder,
        # så her bruges Vibe-Tradings egen yfinance-loader direkte.
        for group, source in ((rest, "auto"), (eu, "yfinance")):
            if not group:
                continue
            try:
                res = fetch_market_data(codes=group, start_date=start, end_date=end, source=source, max_rows=0,
                                        fallback_chain_provider=self._provider, include_provenance=True)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"Kurser ({source}): {type(exc).__name__}")
                continue
            prov = res.get("_provenance") or {}
            for code in group:
                recs = res.get(code)
                if isinstance(recs, dict):
                    recs = recs.get("data")
                rows = []
                for r in recs or []:
                    d = _date(r.get("trade_date") or r.get("date") or r.get("datetime") or r.get("index"))
                    c = _num(r.get("close"))
                    if d and c:
                        rows.append((d, c))
                if rows:
                    p = prov.get(code) or {}
                    out[code] = {"rows": sorted(rows), "source": p.get("source") or source,
                                 "currency": p.get("quote_currency")}
        return out

    def profile(self, code: str) -> dict:
        info: dict[str, Any] = {}
        try:
            from backtest.loaders.yahoo_client import get_quote_summary

            s = get_quote_summary(code, ["price", "summaryDetail", "calendarEvents"]) or {}
            price, sd = s.get("price") or {}, s.get("summaryDetail") or {}
            ce = (s.get("calendarEvents") or {}).get("earnings") or {}
            info = {"name": price.get("longName") or price.get("shortName"), "currency": price.get("currency"),
                    "market_cap": _num(price.get("marketCap")) or _num(sd.get("marketCap")),
                    "trailing_pe": _num(sd.get("trailingPE")), "forward_pe": _num(sd.get("forwardPE")),
                    "earnings_dates": sorted(d for d in (_date(x) for x in ce.get("earningsDate") or []) if d),
                    "earnings_estimate": bool(_num(ce.get("isEarningsDateEstimate"))), "source": "yahoo"}
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"Profil (Yahoo) {type(exc).__name__}")
        if not info.get("earnings_dates"):
            try:  # reserve: yfinance (også en del af Vibe-Trading-installationen)
                import yfinance as yf

                cal = yf.Ticker(eo.to_yahoo(code)).calendar
                dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
                if dates:
                    info["earnings_dates"] = sorted(d for d in (_date(str(x)) for x in dates) if d)
                    info.setdefault("source", "yfinance")
            except Exception:  # noqa: BLE001
                pass
        return info


class DisabledSource:
    """En kilde, der er slået fra (fx VT_DATATJEK_VT=off i tests eller ved fejlsøgning)."""

    def __init__(self, label: str, why: str = "slået fra") -> None:
        self.label, self.available, self.errors = label, False, [f"{label} {why}"]


class EodhdSource:
    label = "EODHD"

    def __init__(self, client: eo.EodhdClient | None = None) -> None:
        self.client = client or eo.EodhdClient()
        self.available = self.client.available
        self.errors: list[str] = []

    def prices(self, codes: list[str], start: str, end: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for code in codes:
            try:
                rows = self.client.eod(code, start, end)
            except eo.EodhdError as exc:
                self.errors.append(str(exc))
                continue
            clean = []
            for r in rows:
                d, c = _date(r.get("date")), _num(r.get("adjusted_close")) or _num(r.get("close"))
                if d and c:
                    clean.append((d, c))
            if clean:
                out[code] = {"rows": sorted(clean), "source": "eodhd", "currency": eo.currency_of(code),
                             "raw_close": _num(rows[-1].get("close"))}
        return out

    def history(self, code: str, start: str, end: str) -> list[tuple[str, float]]:
        """Daglige lukkekurser (justerede) for ét papir, fx siden børsnoteringen."""
        return (self.prices([code], start, end).get(code) or {}).get("rows", [])

    def profile(self, code: str) -> dict:
        try:
            f = self.client.fundamentals(code, ("General", "Highlights", "Valuation"))
        except eo.EodhdError as exc:
            self.errors.append(str(exc))
            return {}
        g, h, v = f.get("General") or {}, f.get("Highlights") or {}, f.get("Valuation") or {}
        return {"name": g.get("Name"), "currency": g.get("CurrencyCode"), "isin": g.get("ISIN"),
                "ipo_date": _date(g.get("IPODate")), "sector": g.get("Sector"), "type": g.get("Type"),
                "market_cap": _num(h.get("MarketCapitalization")),
                "trailing_pe": _num(v.get("TrailingPE")) or _num(h.get("PERatio")),
                "forward_pe": _num(v.get("ForwardPE")), "target_price": _num(h.get("WallStreetTargetPrice"))}

    def earnings(self, codes: list[str], start: str, end: str) -> dict[str, list[dict]]:
        if not codes:
            return {}
        back = {eo.to_eodhd(c): c for c in codes}
        try:
            rows = self.client.earnings(codes, start, end)
        except eo.EodhdError as exc:
            self.errors.append(str(exc))
            return {}
        out: dict[str, list[dict]] = {}
        for r in rows:
            code = back.get(str(r.get("code", "")).upper())
            d = _date(r.get("report_date"))
            if code and d:
                out.setdefault(code, []).append({"date": d, "when": r.get("before_after_market"),
                                                 "estimate": _num(r.get("estimate"))})
        for v in out.values():
            v.sort(key=lambda x: x["date"])
        return out

    def actions(self, code: str, start: str) -> list[dict]:
        ev: list[dict] = []
        try:
            for d in self.client.dividends(code, start):
                if _date(d.get("date")):
                    ev.append({"kind": "dividend", "date": _date(d.get("date")), "value": _num(d.get("value")),
                               "currency": d.get("currency")})
            for s in self.client.splits(code, start):
                if _date(s.get("date")):
                    ev.append({"kind": "split", "date": _date(s.get("date")), "value": s.get("split")})
        except eo.EodhdError as exc:
            self.errors.append(str(exc))
        return ev


# ---------------------------------------------------------------------------
# Selve tjekket
# ---------------------------------------------------------------------------
def norm_ticker(t: Any) -> str:
    s = str(t or "").strip().upper()
    if s and "." not in s and not s.endswith("=X") and not s.startswith("^"):
        s += ".US"  # Vibe-Trading og EODHD forventer suffiks; bare tickers er amerikanske
    return s


def instruments(p: dict) -> list[dict]:
    seen, out = set(), []
    for h in p.get("holdings") or []:
        t = norm_ticker(h.get("ticker"))
        if not t or h.get("kind") == "cash" or t in seen:
            continue
        seen.add(t)
        out.append({"ticker": t, "name": h.get("name") or t, "role": "beholdning", "kind": h.get("kind") or "stock",
                    "qty": _num(h.get("qty")), "max_weight": _num(h.get("max_weight"))})
    for w in p.get("watchlist") or []:
        t = norm_ticker(w.get("ticker")) if isinstance(w, dict) else ""
        if t and t not in seen:
            seen.add(t)
            out.append({"ticker": t, "name": w.get("name") or t, "role": "overvågning", "kind": w.get("kind") or "stock",
                        "note": w.get("note")})
    if BENCHMARK not in seen:
        out.append({"ticker": BENCHMARK, "name": "iShares MSCI ACWI (sammenligning)", "role": "benchmark", "kind": "etf"})
    return out


def portfolio_dates(p: dict) -> dict[str, str]:
    dates: dict[str, str] = {}
    for t, d in (p.get("next_reports") or {}).items():
        if _date(d):
            dates[norm_ticker(t)] = _date(d)
    for w in p.get("watchlist") or []:
        if isinstance(w, dict) and w.get("ticker"):
            m = re.search(r"regnskab\w*\s*:?\s*(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4})", str(w.get("note") or ""), re.I)
            if m and _date(m.group(1)) and norm_ticker(w["ticker"]) not in dates:
                dates[norm_ticker(w["ticker"])] = _date(m.group(1))
    return dates


def fx_pairs(insts: list[dict], base: str = "SEK") -> list[str]:
    ccys = sorted({eo.currency_of(i["ticker"]) for i in insts} - {None, base})
    return [f"{c}{base}=X" for c in ccys]


def _compare_series(vt: dict | None, eod: dict | None, week: dict, ref_day: str, ok: float, warn: float,
                    cut: str | None = None, fx: bool = False) -> dict:
    """Sammenlign to kursrækker. ``cut`` er seneste afsluttede handelsdag: Yahoo/yfinance leverer også
    dagens ufærdige bar, EODHD kun afsluttede dage, så begge skæres her."""
    cut = min(cut or ref_day, ref_day)
    row: dict[str, Any] = {"status": "ok", "notes": []}
    for key, src in (("vt", vt), ("eodhd", eod)):
        if src:
            last = last_on_or_before(src["rows"], cut)
            row[key] = {"date": last[0] if last else None, "close": last[1] if last else None,
                        "week_pct": week_return(src["rows"], week["from"], week["to"]), "source": src.get("source"),
                        "currency": src.get("currency")}
    if not vt and not eod:
        row.update(status="fail")
        row["notes"].append("ingen kurs fra nogen kilde")
        return row
    if not vt or not eod:
        row["status"] = "warn"
        row["notes"].append(f"kun {'EODHD' if eod else 'Vibe-Trading'} har kurs")
    else:
        common = sorted(set(d for d, _ in vt["rows"]) & set(d for d, _ in eod["rows"]))
        common = [d for d in common if d <= cut]
        if not common:
            row["status"] = "warn"
            row["notes"].append("ingen fælles handelsdag")
        else:
            d = common[-1]
            a, b = dict(vt["rows"])[d], dict(eod["rows"])[d]
            diff = pct_diff(a, b)
            row.update(compare_date=d, diff_pct=round(diff, 3) if diff is not None else None)
            g = grade(diff, ok, warn)
            if g != "ok":
                row["notes"].append(f"lukkekurs {dmy(d)} afviger {dk(diff)} %")
            row["status"] = worst([row["status"], g])
            if row["vt"]["date"] != row["eodhd"]["date"]:
                gap = abs((dt.date.fromisoformat(row["vt"]["date"]) - dt.date.fromisoformat(row["eodhd"]["date"])).days)
                if fx and gap <= 1:  # valuta handles døgnet rundt; kilderne lukker dagen på forskellige tidspunkter
                    row["info"] = f"seneste dag: Vibe-Trading {dmy(row['vt']['date'])}, EODHD {dmy(row['eodhd']['date'])}"
                else:
                    row["status"] = worst([row["status"], "warn"])
                    row["notes"].append(f"seneste dag: Vibe-Trading {dmy(row['vt']['date'])}, EODHD {dmy(row['eodhd']['date'])}")
        wa, wb = row["vt"]["week_pct"], row["eodhd"]["week_pct"]
        if wa is not None and wb is not None and not fx:  # valuta: ugeafkast afhænger af lukketidspunkt, kurs tjekkes ovenfor
            row["week_diff_pp"] = round(abs(wa - wb), 3)
            g = grade(row["week_diff_pp"], WEEK_OK, WEEK_WARN)
            if g != "ok":
                row["notes"].append(f"ugeafkast afviger {dk(row['week_diff_pp'])} procentpoint")
            row["status"] = worst([row["status"], g])
    latest = max((x["date"] for x in (row.get("vt"), row.get("eodhd")) if x and x.get("date")), default=None)
    if latest is None:
        row["status"] = "fail"
        row["notes"].append("ingen kurs til og med i dag")
        return row
    age = (dt.date.fromisoformat(ref_day) - dt.date.fromisoformat(latest)).days
    if age > STALE_DAYS:
        row["status"] = "fail"
        row["notes"].append(f"forældede data: seneste kurs {dmy(latest)}")
    return row


def _align_fx(vt_px: dict, eod_px: dict, pairs: list[str]) -> dict[str, str]:
    """Yahoo stempler daglige valutabarer ved London-midnat (UTC), så Vibe-Trading kan datere dem en dag for tidligt.

    For hvert valutapar vælges den forskydning (0 eller +1 dag), der giver mindst median-afvigelse
    over de seneste fælles dage. Ved +1 flyttes Vibe-Tradings datoer, og det noteres.
    """
    out: dict[str, str] = {}
    for pair in pairs:
        v, e = vt_px.get(pair), eod_px.get(pair)
        if not v or not e:
            continue
        ed = dict(e["rows"])

        def median_diff(shift: int) -> float | None:
            diffs = []
            for d, c in v["rows"][-8:]:
                d2 = (dt.date.fromisoformat(d) + dt.timedelta(days=shift)).isoformat()
                if d2 in ed and ed[d2]:
                    diffs.append(abs(c - ed[d2]) / abs(ed[d2]) * 100)
            return sorted(diffs)[len(diffs) // 2] if len(diffs) >= 2 else None

        m0, m1 = median_diff(0), median_diff(1)
        if m1 is not None and (m0 is None or m1 < m0 * 0.5) and m1 <= FX_OK:
            v["rows"] = [((dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat(), c) for d, c in v["rows"]]
            out[pair] = "Vibe-Tradings Yahoo-valutakurser var dateret en dag for tidligt (UTC-tidsstempel) og er justeret"
    return out


def _fix_pence(vt_px: dict, eod_px: dict, cut: str) -> dict[str, str]:
    """EODHD noterer London-aktier i pence (GBX), Vibe-Trading i pund. Skalér EODHD til pund."""
    out: dict[str, str] = {}
    for code, e in eod_px.items():
        if eo.suffix_of(code) not in ("L", "LSE") or not e.get("rows"):
            continue
        v = vt_px.get(code)
        a = last_on_or_before(v["rows"], cut) if v else None
        b = last_on_or_before(e["rows"], cut)
        ratio = a[1] / b[1] if a and b and b[1] else None
        if ratio is None or 0.005 < ratio < 0.02:
            e["rows"] = [(d, c / 100) for d, c in e["rows"]]
            out[code] = "EODHD's kurs i pence (GBX) er omregnet til pund" + ("" if ratio else " (antaget)")
    return out


def run(p: dict, week: dict, ref: dt.datetime, *, vt: Any = None, eod: Any = None,
        horizon_days: int = 120, with_profiles: bool = True) -> dict:
    """Kør datatjekket. ``vt``/``eod`` kan erstattes i tests."""
    vt = vt if vt is not None else VibeTradingSource()
    eod = eod if eod is not None else EodhdSource()
    ref_day = ref.date().isoformat()
    cut = min(week["to"], ref_day)  # seneste afsluttede amerikanske handelsdag
    start = (dt.date.fromisoformat(week["from"]) - dt.timedelta(days=12)).isoformat()
    insts = instruments(p)
    codes = [i["ticker"] for i in insts]
    pairs = fx_pairs(insts, p.get("currency") or "SEK")
    issues: list[str] = []

    # Er beholdningen (VT_PORTFOLIO) for gammel? Den opdateres manuelt efter handler.
    as_of = _date(p.get("as_of"))
    try:
        max_age = int(os.environ.get("VT_PORTFOLIO_MAX_AGE_DAYS", PORTFOLIO_MAX_AGE_DAYS))
    except ValueError:
        max_age = PORTFOLIO_MAX_AGE_DAYS
    portfolio_age = (ref.date() - dt.date.fromisoformat(as_of)).days if as_of else None
    stale = portfolio_age is not None and portfolio_age > max_age
    if stale:
        issues.append(f"ADVARSEL portefølje: VT_PORTFOLIO er opgjort {dmy(as_of)} ({portfolio_age} dage gammel). "
                      "Opdatér beholdningen i Vibetrad og kopiér VT_PORTFOLIO til GitHub.")

    def fetch(src: Any, items: list[str]) -> dict:
        if not getattr(src, "available", False) or not items:
            return {}
        try:
            return src.prices(items, start, ref_day) or {}
        except Exception as exc:  # noqa: BLE001
            src.errors.append(f"kurser: {type(exc).__name__}")
            return {}

    account = eo.key_status(eod.client) if getattr(eod, "available", False) and getattr(eod, "client", None) else None
    vt_px, eod_px = fetch(vt, codes + pairs), fetch(eod, codes + pairs)
    pence = _fix_pence(vt_px, eod_px, cut)
    fx_shift = _align_fx(vt_px, eod_px, pairs)

    rows = []
    for i in insts:
        r = _compare_series(vt_px.get(i["ticker"]), eod_px.get(i["ticker"]), week, ref_day, PRICE_OK, PRICE_WARN, cut)
        r.update({k: i.get(k) for k in ("ticker", "name", "role", "kind")})
        r["currency"] = eo.currency_of(i["ticker"])
        if i["ticker"] in pence:
            r["notes"].append(pence[i["ticker"]])
        if eo.is_eu_symbol(i["ticker"]) and i["ticker"] in vt_px:
            r["info"] = "Vibe-Tradings automatiske routing kan ikke dette marked; Vibe-Tradings tal er hentet med yfinance-loaderen"
        rows.append(r)
    fx_rows = []
    for pair in pairs:
        r = _compare_series(vt_px.get(pair), eod_px.get(pair), week, ref_day, FX_OK, FX_WARN, cut, fx=True)
        r["pair"] = pair.replace("=X", "")
        if pair in fx_shift:
            r["info"] = fx_shift[pair]
        fx_rows.append(r)

    # Nøgletal, navn, valuta og IPO/lock-up (og kort kurshistorik)
    fundamentals, events = [], []
    short_history: list[dict] = []
    profiles: dict[str, dict] = {}
    for i in insts:
        if not with_profiles or i["role"] == "benchmark":
            continue
        a = vt.profile(i["ticker"]) if getattr(vt, "available", False) else {}
        b = eod.profile(i["ticker"]) if getattr(eod, "available", False) else {}
        profiles[i["ticker"]] = {"vt": a, "eodhd": b}
        if not a and not b:
            continue
        expected = eo.currency_of(i["ticker"])
        f: dict[str, Any] = {"ticker": i["ticker"], "name": i["name"], "status": "ok", "notes": [],
                             "vt": {k: a.get(k) for k in ("name", "currency", "market_cap", "trailing_pe", "forward_pe")},
                             "eodhd": {k: b.get(k) for k in ("name", "currency", "market_cap", "trailing_pe",
                                                             "forward_pe", "ipo_date", "sector", "target_price")}}
        allowed = {expected} | ({"GBX", "GBP"} if expected == "GBP" else set())
        for label, cur in (("Vibe-Trading", a.get("currency")), ("EODHD", b.get("currency"))):
            if cur and expected and cur.upper() not in allowed:
                f["status"] = "fail"
                f["notes"].append(f"{label} siger valuta {cur}, børsen handler i {expected}")
        nm = names_match(a.get("name"), b.get("name"))
        f["name_match"] = nm
        if nm is False:
            f["status"] = worst([f["status"], "warn"])
            f["notes"].append(f"navne afviger: '{a.get('name')}' / '{b.get('name')}'")
        md = pct_diff(a.get("market_cap"), b.get("market_cap"))
        f["mcap_diff_pct"] = round(md, 1) if md is not None else None
        if md is not None and md > MCAP_OK:
            f["status"] = worst([f["status"], "warn" if md > MCAP_WARN else "info"])
            f["notes"].append(f"markedsværdi afviger {dk(md, 0)} % (aktieklasser/tidspunkt kan forklare det)")
        fundamentals.append(f)
        ipo = b.get("ipo_date")
        if ipo:
            lock = (dt.date.fromisoformat(ipo) + dt.timedelta(days=LOCKUP_DAYS)).isoformat()
            if dt.date.fromisoformat(ipo) >= ref.date() - dt.timedelta(days=540):
                events.append({"ticker": i["ticker"], "kind": "lockup", "date": lock,
                               "text": f"{i['name']}: børsnoteret {dmy(ipo)}. Typisk lock-up udløber ca. {dmy(lock)} "
                                       f"(estimat: IPO + {LOCKUP_DAYS} dage, bekræft i prospektet)."})
        if ipo and dt.date.fromisoformat(ipo) > ref.date() - dt.timedelta(days=SHORT_HISTORY_DAYS):
            hist = []
            if getattr(eod, "available", False) and hasattr(eod, "history"):
                try:
                    hist = eod.history(i["ticker"], ipo, cut) or []
                except Exception as exc:  # noqa: BLE001
                    eod.errors.append(f"historik: {type(exc).__name__}")
            closes = [c for d, c in hist if d >= ipo]
            dd = None
            if len(closes) > 1:
                peak, dd = closes[0], 0.0
                for c in closes:
                    peak = max(peak, c)
                    dd = min(dd, c / peak - 1)
            days = len(closes) or _weekdays(ipo, cut)
            short_history.append({"ticker": i["ticker"], "name": i["name"], "ipo_date": ipo, "trading_days": days,
                                  "approx": not closes, "max_drawdown_pct": round(dd * 100, 1) if dd is not None else None})
        if getattr(eod, "available", False) and i["kind"] not in ("fund",):
            since = (dt.date.fromisoformat(week["from"]) - dt.timedelta(days=30)).isoformat()
            for e in eod.actions(i["ticker"], since):
                txt = (f"udbytte {str(e['value']).replace('.', ',')} {e.get('currency') or ''}".strip() if e["kind"] == "dividend" and e.get("value")
                       else f"split {e.get('value')}" if e["kind"] == "split" else e["kind"])
                events.append({"ticker": i["ticker"], "kind": e["kind"], "date": e["date"],
                               "text": f"{i['name']}: {txt}, ex-dato {dmy(e['date'])}"})

    # Papirer uden navn i porteføljen (fx overvågningslisten) får kildernes navn
    for i in insts:
        if i["name"] == i["ticker"]:
            pr = profiles.get(i["ticker"], {})
            nm = (pr.get("eodhd") or {}).get("name") or (pr.get("vt") or {}).get("name")
            if nm:
                i["name"] = nm
                for r in rows:
                    if r["ticker"] == i["ticker"]:
                        r["name"] = nm

    # Regnskabsdatoer: porteføljen × EODHD × Vibe-Trading (Yahoo)
    pdates = portfolio_dates(p)
    stock_codes = [i["ticker"] for i in insts if i["kind"] not in ("etf", "fund") and i["role"] != "benchmark"]
    cal = (eod.earnings(stock_codes, (ref.date() - dt.timedelta(days=3)).isoformat(),
                        (ref.date() + dt.timedelta(days=horizon_days)).isoformat())
           if getattr(eod, "available", False) else {})
    earnings = []
    for code in stock_codes:
        name = next(i["name"] for i in insts if i["ticker"] == code)
        e_rows = [x for x in cal.get(code, []) if x["date"] >= ref_day]
        e_next = e_rows[0] if e_rows else None
        vt_prof = (profiles.get(code, {}).get("vt", {}) or {})
        vt_all = sorted(d for d in vt_prof.get("earnings_dates", []) if d)
        if vt_all and vt_all[-1] < ref_day:  # kun datoer for et overstået regnskab
            vt_all = []
        # Yahoo giver et vindue [fra, til], når datoen ikke er bekræftet: det tæller som enigt, hvis datoen ligger i det
        window = (vt_all[0], vt_all[-1]) if len(vt_all) >= 2 and vt_all[-1] > vt_all[0] else None
        cand = {"portefølje": pdates.get(code) if (pdates.get(code) or "") >= ref_day else None,
                "EODHD": e_next["date"] if e_next else None,
                "Vibe-Trading": vt_all[0] if vt_all and not window else None}
        known = {k: v for k, v in cand.items() if v}
        r: dict[str, Any] = {"ticker": code, "name": name, "portfolio": cand["portefølje"], "eodhd": cand["EODHD"],
                             "eodhd_when": (e_next or {}).get("when"), "eps_estimate": (e_next or {}).get("estimate"),
                             "vibe_trading": vt_all[0] if vt_all else None,
                             "vibe_trading_to": window[1] if window else None,
                             "vt_estimate": bool(vt_prof.get("earnings_estimate") or window),
                             "status": "ok", "notes": []}

        def near(d: str, best: str) -> bool:
            return abs((dt.date.fromisoformat(d) - dt.date.fromisoformat(best)).days) <= 1

        if not known and not window:
            r.update(status="warn", verified=None)
            r["notes"].append("ingen kommende regnskabsdato i nogen kilde")
        elif not known:
            r.update(status="info", verified=window[0], agree=1)
            r["notes"].append(f"kun Vibe-Trading (Yahoo) har et estimat: {dmy(window[0])}–{dmy(window[1])}")
        else:
            counts: dict[str, int] = {}
            for v in known.values():
                counts[v] = counts.get(v, 0) + 1
            best = max(counts, key=lambda d: (counts[d], d == cand["EODHD"], d == cand["Vibe-Trading"]))
            in_window = bool(window and window[0] <= best <= window[1])
            r["verified"] = best
            r["agree"] = counts[best] + (1 if in_window else 0)
            off = [f"{k} {dmy(v)}" for k, v in known.items() if not near(v, best)]
            if window and not in_window:
                off.append(f"Vibe-Trading {dmy(window[0])}–{dmy(window[1])}")
            if off:
                r["status"] = "warn"
                r["notes"].append(f"datoer afviger: {', '.join(off)} (flest siger {dmy(best)})")
                if cand["portefølje"] and not near(cand["portefølje"], best) and r["agree"] >= 2:
                    r["notes"].append("ret datoen i VT_PORTFOLIO (Vibetrad → Overvågning/Beholdninger)")
            elif r["agree"] == 1:
                r["status"] = "info"
                r["notes"].append(f"kun {next(iter(known))} kender datoen")
        earnings.append(r)

    # Porteføljens værdi i SEK fra hver kilde
    def fx_close(src_px: dict, ccy: str | None) -> float | None:
        """Valutakurs til SEK. Begge kilder værdisættes med samme kurs (EODHD først), så valutaens
        lukketidspunkt ikke forveksles med kursafvigelser; valutaen tjekkes for sig ovenfor."""
        if ccy == (p.get("currency") or "SEK"):
            return 1.0
        if not ccy:  # ukendt børs → ingen gæt på valutaen
            return None
        key = f"{ccy}{p.get('currency') or 'SEK'}=X"
        for px in (eod_px, vt_px):
            s = px.get(key)
            last = last_on_or_before(s["rows"], cut) if s else None
            if last:
                return last[1]
        return None

    value: dict[str, Any] = {}
    prow = []
    no_qty = [i["ticker"] for i in insts if i["role"] == "beholdning" and not i.get("qty")]
    for key, src_px in (("vibe_trading", vt_px), ("eodhd", eod_px)):
        total, complete = p.get("cash_sek") or 0.0, not no_qty  # uden antal kan vægtene ikke regnes
        for i in insts:
            if i["role"] != "beholdning" or not i.get("qty"):
                continue
            s = src_px.get(i["ticker"])
            last = last_on_or_before(s["rows"], cut) if s else None
            fx = fx_close(src_px, eo.currency_of(i["ticker"]))
            if not last or fx is None:
                complete = False
                continue
            total += i["qty"] * last[1] * fx
        value[key] = round(total, 2) if complete else None
    chosen_px = eod_px if value.get("eodhd") is not None else vt_px
    chosen_key = "eodhd" if value.get("eodhd") is not None else "vibe_trading"
    total = value.get(chosen_key)
    for i in insts:
        if i["role"] != "beholdning":
            continue
        s = chosen_px.get(i["ticker"])
        last = last_on_or_before(s["rows"], cut) if s else None
        fx = fx_close(chosen_px, eo.currency_of(i["ticker"]))
        v = i["qty"] * last[1] * fx if last and fx is not None and i.get("qty") else None
        w = v / total * 100 if v is not None and total else None
        prow.append({"ticker": i["ticker"], "name": i["name"], "qty": i.get("qty"), "close": last[1] if last else None,
                     "date": last[0] if last else None, "currency": eo.currency_of(i["ticker"]), "fx": fx,
                     "value_sek": round(v, 2) if v is not None else None, "weight_pct": round(w, 1) if w is not None else None,
                     "max_weight": i.get("max_weight"),
                     "over_max": bool(w is not None and i.get("max_weight") and w > i["max_weight"])})
    vd = pct_diff(value.get("vibe_trading"), value.get("eodhd"))
    pstatus = grade(vd, VALUE_OK, VALUE_WARN) if vd is not None else "warn" if total is None else "info"
    portfolio = {"value_sek": value, "chosen": chosen_key if total is not None else None, "total_sek": total,
                 "diff_pct": round(vd, 2) if vd is not None else None, "cash_sek": p.get("cash_sek") or 0.0,
                 "rows": prow, "status": pstatus, "missing_qty": no_qty}
    if no_qty:
        issues.append(f"ADVARSEL porteføljeværdi: antal mangler for {', '.join(no_qty)} i VT_PORTFOLIO, så vægtene "
                      "ikke kan beregnes (opdatér beholdningen i Vibetrad og kopiér VT_PORTFOLIO igen)")

    if not getattr(eod, "available", False):
        issues.append("EODHD-nøglen mangler (secret EODHD_API_KEY): kun Vibe-Tradings egne kilder er brugt")
    if not getattr(vt, "available", False):
        why = (getattr(vt, "errors", None) or [""])[0]
        issues.append(f"Vibe-Trading var ikke tilgængelig{f' ({why})' if why else ''}: kun EODHD er brugt")
    one_source = not getattr(eod, "available", False) or not getattr(vt, "available", False)
    for r in rows + fx_rows:
        notes = [n for n in r["notes"] if not (one_source and n.startswith("kun "))]  # dækket af linjen ovenfor
        if r["status"] in ("warn", "fail") and notes:
            issues.append(f"{LABEL[r['status']]} {r.get('ticker') or r.get('pair')}: {'; '.join(notes)}")
    for f in fundamentals:
        if f["status"] in ("warn", "fail"):
            issues.append(f"{LABEL[f['status']]} {f['ticker']}: {'; '.join(f['notes'])}")
    for e in earnings:
        if e["status"] in ("warn", "fail"):
            issues.append(f"{LABEL[e['status']]} regnskab {e['ticker']}: {'; '.join(e['notes'])}")
    if pstatus in ("warn", "fail"):
        issues.append(f"{LABEL[pstatus]} porteføljeværdi: Vibe-Trading {_nf(value.get('vibe_trading'), 0)} SEK, "
                      f"EODHD {_nf(value.get('eodhd'), 0)} SEK")

    states = ([r["status"] for r in rows + fx_rows] + [f["status"] for f in fundamentals]
              + [e["status"] for e in earnings] + [pstatus] + (["warn"] if stale else []))
    if not getattr(eod, "available", False) or not getattr(vt, "available", False):
        states.append("warn")
    status = worst(states)
    served: dict[str, int] = {}
    for v in vt_px.values():
        served[v["source"]] = served.get(v["source"], 0) + 1
    count = {k: sum(1 for s in states if s == k) for k in ("ok", "warn", "fail")}
    return {
        "version": 1, "generated_at": dt.datetime.now(STO).isoformat(timespec="seconds"), "status": LABEL[status],
        "ref_date": ref_day, "window": {"from": start, "to": ref_day, "week_from": week["from"], "week_to": week["to"]},
        "sources": {"vibe_trading": {"available": bool(getattr(vt, "available", False)), "served_by": served,
                                     "errors": list(dict.fromkeys(getattr(vt, "errors", [])))[:10]},
                    "eodhd": {"available": bool(getattr(eod, "available", False)), "account": account,
                              "calls": getattr(getattr(eod, "client", None), "calls", 0),
                              "errors": list(dict.fromkeys(getattr(eod, "errors", [])))[:10]}},
        "counts": count, "instruments": rows, "fx": fx_rows, "fundamentals": fundamentals,
        "earnings": earnings, "events": sorted(events, key=lambda e: e["date"]), "portfolio": portfolio,
        "short_history": short_history, "portfolio_as_of": as_of, "portfolio_age_days": portfolio_age,
        "weight_policy": p.get("weight_policy", "limit"),
        "issues": issues[:40],
    }


# ---------------------------------------------------------------------------
# Facit til agenterne
# ---------------------------------------------------------------------------
def _nf(v: float | None, d: int = 2) -> str:
    if v is None:
        return "–"
    s = f"{v:,.{d}f}".replace(",", " ").replace(".", ",")
    return s


def _sp(v: float | None, d: int = 1) -> str:
    return "–" if v is None else ("+" if v > 0 else "") + _nf(v, d) + " %"


def verified_reports(dc: dict | None) -> dict[str, str]:
    return {e["ticker"]: e["verified"] for e in (dc or {}).get("earnings", []) if e.get("verified")}


def agent_brief(dc: dict | None, limit: int = 3800) -> str:
    """Kompakt facit (markdown), som alle agenter får i deres instruks."""
    if not dc:
        return ""
    srcs = []
    if dc["sources"]["vibe_trading"]["available"]:
        srcs.append("Vibe-Trading (" + ", ".join(sorted(dc["sources"]["vibe_trading"]["served_by"])) + ")")
    if dc["sources"]["eodhd"]["available"]:
        srcs.append("EODHD")
    lines = [f"## Vibetrad datatjek {dmy(dc['ref_date'])}: {dc['status']}",
             f"Kilder krydstjekket: {' og '.join(srcs) or 'ingen'}. Brug tallene herunder som facit for kurser, vægte og "
             "datoer. Hvis dine egne værktøjer giver mere end 1 % afvigelse, så sig det og forklar hvorfor.",
             "", "| Papir | Lukkekurs | Uge | Værdi SEK | Vægt | Næste regnskab |", "|---|---|---|---|---|---|"]
    by_t = {r["ticker"]: r for r in dc.get("portfolio", {}).get("rows", [])}
    er = {e["ticker"]: e for e in dc.get("earnings", [])}
    lim = "langsigtet mål" if dc.get("weight_policy") == "target" else "maks"
    for r in dc.get("instruments", []):
        src = r.get("eodhd") or r.get("vt") or {}
        pr = by_t.get(r["ticker"], {})
        e = er.get(r["ticker"]) or {}
        when = {"AfterMarket": " efter lukning", "BeforeMarket": " før åbning"}.get(e.get("eodhd_when") or "", "")
        rep = (dmy(e.get("verified")) + when + (f" ({e.get('agree')} kilder)" if e.get("agree") else "")) if e.get("verified") else "–"
        w = (f"{_nf(pr.get('weight_pct'), 1)} %" + (f" ({lim} {pr['max_weight']:g} %)" if pr.get("over_max") else "")) if pr.get("weight_pct") is not None else "–"
        lines.append(f"| {r['name']} ({r['ticker']}) | {_nf(src.get('close'))} {eo.currency_of(r['ticker']) or ''} "
                     f"({dmy(src.get('date'))}) | {_sp(src.get('week_pct'))} | {_nf(pr.get('value_sek'), 0)} | {w} | {rep} |")
    fx = " · ".join(f"{f['pair'][:3]}/{f['pair'][3:]} {_nf((f.get('eodhd') or f.get('vt') or {}).get('close'), 4)}"
                    for f in dc.get("fx", []))
    pf = dc.get("portfolio", {})
    lines.append("")
    lines.append(f"{fx} · Kontant {_nf(pf.get('cash_sek'), 0)} SEK · I alt {_nf(pf.get('total_sek'), 0)} SEK "
                 f"(markedsværdi, {('EODHD' if pf.get('chosen') == 'eodhd' else 'Vibe-Trading')}).")
    for s in dc.get("short_history", []):
        w = (by_t.get(s["ticker"]) or {}).get("weight_pct")
        lines.append(f"**Kort historik:** {s['name']} ({s['ticker']}) har kun {'ca. ' if s.get('approx') else ''}"
                     f"{s['trading_days']} handelsdage (børsnoteret {dmy(s['ipo_date'])})"
                     + (f", største fald fra top til bund {_nf(s['max_drawdown_pct'], 0)} %" if s.get("max_drawdown_pct") is not None else "")
                     + ". Volatilitet, VaR, beta og korrelation for papiret"
                     + (f" og for porteføljen (vægt {_nf(w, 0)} %)" if w is not None else "")
                     + " er statistisk usikre. Sig det, når du bruger dem.")
    ev = [e["text"] for e in dc.get("events", []) if e["date"] >= dc["ref_date"] or e["kind"] != "lockup"]
    if ev:
        lines.append("Begivenheder: " + " ".join(ev[:6]))
    if dc.get("issues"):
        lines.append("Afvigelser: " + " | ".join(dc["issues"][:6]))
    lines.append("Nordiske/europæiske symboler (fx NOVO-B.CO, INVE-B.ST) kan hentes med get_market_data (EODHD-loader) "
                 "eller get_eodhd_data.")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit] + "\n[… afkortet]"


def cio_instruction(dc: dict | None) -> str:
    if not dc:
        return ""
    extra = ("\n\nDatakvalitet: Vibetrads datatjek (Vibe-Trading × EODHD) gav status "
             f"{dc['status']}. Medtag et kort afsnit 'Datakvalitet' i rapporten: hvilke afvigelser påvirker "
             "konklusionen, og hvilke tal du har brugt.")
    if dc.get("issues"):
        extra += " Afvigelser: " + " | ".join(dc["issues"][:10])
    return extra


# ---------------------------------------------------------------------------
# CLI (codespacet)
# ---------------------------------------------------------------------------
def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Krydstjek porteføljens data: Vibe-Trading × EODHD")
    ap.add_argument("--portfolio-file", help="JSON som VT_PORTFOLIO (standard: miljøvariablen VT_PORTFOLIO)")
    ap.add_argument("--json", help="gem hele resultatet som JSON her")
    ap.add_argument("--no-profiles", action="store_true", help="spring nøgletal over (hurtigere)")
    args = ap.parse_args(argv)
    raw = Path(args.portfolio_file).read_text("utf-8") if args.portfolio_file else os.environ.get("VT_PORTFOLIO", "")
    from run_week import load_portfolio, now_sto, week_window

    p = load_portfolio(raw)
    ref = now_sto(os.environ.get("VT_NOW"))
    try:
        eo.install_into_vibe_trading(with_tool=False)
    except Exception:  # noqa: BLE001
        pass
    dc = run(p, week_window(ref), ref, with_profiles=not args.no_profiles)
    print(agent_brief(dc, limit=20000))
    print()
    for line in dc["issues"] or ["Ingen afvigelser."]:
        print("·", line)
    if args.json:
        Path(args.json).write_text(json.dumps(dc, ensure_ascii=False, indent=1), "utf-8")
        print(f"\nGemt: {args.json}")
    return 0 if dc["status"] != "FEJL" else 1


if __name__ == "__main__":
    sys.exit(_cli())
