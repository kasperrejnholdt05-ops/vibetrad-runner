"""Trendvagten: 200-dages-reglen og ROC-nulstilling (Jordi Visser-filteret) for beholdning, overvågningsliste og
målportefølje.

Visser (Weiss Multi-Strategy, 2026) beskriver sit filter sådan: aktien skal ligge over sit 200-dages gennemsnit, og
gennemsnittet skal pege opad; han leder efter en "rate of change" (ROC), der er nulstillet tæt på nul efter at have
været høj – konsolidering med trenden intakt – og holder ca. tre måneder.

Vibetrad bruger det ikke til at handle tit, men som et ekstra lag i analysen: før et køb (er trenden med eller imod
tesen?), for positioner man ejer (er trenden brudt?) og som spørgsmål til agenterne. Tallene er Vibetrads egne
præciseringer af en mundtlig beskrivelse:

- gennemsnittets hældning måles over 20 handelsdage (ca. én måned)
- ROC er kursændringen over 63 handelsdage (ca. tre måneder, Vissers holdeperiode)
- "nulstillet": ROC inden for ±3 % nu, efter at have været mindst +10 % på et tidspunkt inden for de forrige 63 dage
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Sequence

SMA_DAYS = 200
SLOPE_DAYS = 20
ROC_DAYS = 63
RESET_BAND = 3.0      # |ROC| under denne grænse = nulstillet
HOT_PEAK = 10.0       # ROC skal have været mindst så høj inden for de forrige ROC_DAYS
BREAK_DAYS = 5        # kursen er gået under gennemsnittet inden for så mange dage = "brud"
MIN_ROWS = SMA_DAYS + SLOPE_DAYS

LABELS = {
    "reset": "Købsvindue: trend intakt og ROC nulstillet",
    "intact": "Trend intakt",
    "hot": "Trend intakt, men varm (ROC højt)",
    "flat": "Over 200 dage, men gennemsnittet falder eller er fladt",
    "break": "Brud: lige gået under 200 dage",
    "under": "Under 200 dage",
    "short": "For kort historik",
}
# Hvad reglen betyder for den, der ejer / overvejer papiret (ingen handelsanbefaling)
MEANING = {
    "reset": "Vissers indgangssituation: konsolidering i en stigende trend.",
    "intact": "Trenden er med tesen.",
    "hot": "Trenden er med tesen, men kursen er løbet langt på kort tid; Visser venter på en nulstilling.",
    "flat": "Trenden er svækket; reglen kræver et stigende gennemsnit.",
    "break": "Trendbrud: revurdér tesen, hvis du ejer papiret; reglen køber ikke her.",
    "under": "Reglen køber ikke under et 200-dages gennemsnit; en køber her går imod trenden og skal have en grund.",
    "short": "Under 220 handelsdage: reglen kan ikke beregnes.",
}


def _sma(c: Sequence[float], end: int, n: int) -> float:
    return sum(c[end - n + 1:end + 1]) / n


def _roc(c: Sequence[float], i: int, n: int = ROC_DAYS) -> float | None:
    return (c[i] / c[i - n] - 1) * 100 if i - n >= 0 and c[i - n] > 0 else None


def status(rows: Sequence[tuple[str, float]]) -> dict[str, Any]:
    """Trendstatus fra daglige (justerede) lukkekurser, ældste først."""
    rows = [(d, float(c)) for d, c in rows if c and c > 0]
    if len(rows) < MIN_ROWS:
        return {"state": "short", "label": LABELS["short"], "meaning": MEANING["short"], "rows": len(rows)}
    c = [x[1] for x in rows]
    i = len(c) - 1
    sma = _sma(c, i, SMA_DAYS)
    sma_prev = _sma(c, i - SLOPE_DAYS, SMA_DAYS)
    slope = (sma / sma_prev - 1) * 100
    dist = (c[i] / sma - 1) * 100
    roc = _roc(c, i)
    prior = [r for r in (_roc(c, j) for j in range(max(i - ROC_DAYS, 0), i)) if r is not None]
    peak = max(prior) if prior else None
    # Brud: under gennemsnittet nu, men over det for højst BREAK_DAYS dage siden
    broke = c[i] < sma and any(c[j] >= _sma(c, j, SMA_DAYS) for j in range(i - BREAK_DAYS, i))
    if c[i] < sma:
        state = "break" if broke else "under"
    elif slope <= 0:
        state = "flat"
    elif roc is not None and abs(roc) <= RESET_BAND and peak is not None and peak >= HOT_PEAK:
        state = "reset"
    elif roc is not None and roc >= HOT_PEAK:
        state = "hot"
    else:
        state = "intact"
    r2 = lambda v: round(v, 2) if v is not None else None  # noqa: E731
    return {"state": state, "label": LABELS[state], "meaning": MEANING[state], "date": rows[-1][0],
            "close": r2(c[i]), "sma200": r2(sma), "dist_pct": r2(dist), "slope_pct": r2(slope),
            "roc63_pct": r2(roc), "roc63_peak_pct": r2(peak), "rows": len(rows)}


def rule_text() -> str:
    return (f"over {SMA_DAYS}-dages gennemsnit, gennemsnittet stigende over {SLOPE_DAYS} dage; "
            f"købsvindue når ROC({ROC_DAYS} dage) er inden for ±{RESET_BAND:g} % efter at have været mindst "
            f"+{HOT_PEAK:g} % inden for de forrige {ROC_DAYS} dage")


# ---------------------------------------------------------------------------------------------------------------
# 5-års-testen af reglen og spredningen – samme regnemåde som Vibetrads side (Trendvagt, Handelstjek, beslutningskort),
# så agenterne og Kasper ser de samme tal. Begge bruger de dagskurser, trendvagten allerede har hentet (ingen ekstra kald).
# ---------------------------------------------------------------------------------------------------------------
HIST_DAYS = 5 * 365 + 320   # ca. 5 år efter de første 200 dage, der går til gennemsnittet
HIST_MIN_ROWS = SMA_DAYS + 260  # mindst ca. ét års test
HIST_LABELS = {"good": "reglen dæmpede faldene", "cost": "reglen kostede afkast",
               "mixed": "mindre fald, men lavere afkast", "none": "ingen klar forskel", "short": "for kort historik"}
SPREAD_LABELS = {"low": "spreder kun lidt", "some": "spreder noget", "good": "spreder godt"}


def _max_dd(eq: Sequence[float]) -> float:
    pk, dd = float("-inf"), 0.0
    for v in eq:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return dd * 100


def history(rows: Sequence[tuple[str, float]], cost_pct: float = 0.5) -> dict[str, Any]:
    """Forenklet backtest: ejer papiret, når lukkekursen ligger over 200-dages-gennemsnittet, ellers kontanter (0 %).
    Positionen fra dagens lukkekurs gælder næste dags afkast; ``cost_pct`` trækkes ved hvert skift ind eller ud."""
    rows = [(d, float(c)) for d, c in rows if c and c > 0]
    if len(rows) < HIST_MIN_ROWS:
        return {"verdict": "short", "label": HIST_LABELS["short"], "rows": len(rows)}
    c = [x[1] for x in rows]
    n = len(c)
    pos = [0] * n
    s = sum(c[:SMA_DAYS])
    for i in range(SMA_DAYS - 1, n):
        if i >= SMA_DAYS:
            s += c[i] - c[i - SMA_DAYS]
        pos[i] = 1 if c[i] > s / SMA_DAYS else 0
    start = SMA_DAYS - 1
    eq, bh, trades, inside = [1.0], [1.0], 0, 0
    for i in range(start + 1, n):
        r = c[i] / c[i - 1] - 1
        e = eq[-1] * (1 + pos[i - 1] * r)
        prev = pos[i - 2] if i - 2 >= start else 0
        if pos[i - 1] != prev:
            e *= 1 - cost_pct / 100
            trades += 1
        eq.append(e)
        bh.append(bh[-1] * (1 + r))
        inside += pos[i - 1]
    yrs = (dt.date.fromisoformat(rows[-1][0][:10]) - dt.date.fromisoformat(rows[start][0][:10])).days / 365.25

    def cagr(a: list[float]) -> float:
        return (a[-1] ** (1 / yrs) - 1) * 100 if yrs > 0 else 0.0

    out = {"from": rows[start][0][:10], "to": rows[-1][0][:10], "years": round(yrs, 1), "trades": trades,
           "exposure_pct": round(inside / max(len(eq) - 1, 1) * 100, 1),
           "rule_cagr_pct": round(cagr(eq), 2), "rule_max_dd_pct": round(_max_dd(eq), 1),
           "hold_cagr_pct": round(cagr(bh), 2), "hold_max_dd_pct": round(_max_dd(bh), 1), "rows": n}
    dd = out["rule_max_dd_pct"] - out["hold_max_dd_pct"]  # > 0: reglen faldt mindre
    ret = out["rule_cagr_pct"] - out["hold_cagr_pct"]
    v = "good" if dd >= 10 and ret >= -3 else "cost" if ret <= -3 and dd < 10 else "mixed" if dd >= 10 else "none"
    out.update(verdict=v, label=HIST_LABELS[v])
    return out


def _iso_week(day: str) -> str:
    y, w, _ = dt.date.fromisoformat(day[:10]).isocalendar()
    return f"{y}-{w:02d}"


def weekly_returns(rows: Sequence[tuple[str, float]], weeks: int = 104) -> dict[str, float]:
    """Ugeafkast fra dagskurser (sidste lukkekurs i hver ISO-uge), nøglet på uge, så børser med forskellige
    helligdage kan sammenlignes."""
    last: dict[str, float] = {}
    for d, c in rows:
        if c and c > 0:
            last[_iso_week(d)] = float(c)
    ks = sorted(last)[-(weeks + 1):]
    return {ks[i]: last[ks[i]] / last[ks[i - 1]] - 1 for i in range(1, len(ks))}


def correlation(a: dict[str, float], b: dict[str, float], min_weeks: int = 30) -> float | None:
    ks = [k for k in a if k in b]
    if len(ks) < min_weeks:
        return None
    x, y = [a[k] for k in ks], [b[k] for k in ks]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((p - mx) * (q - my) for p, q in zip(x, y))
    sxx = sum((p - mx) ** 2 for p in x)
    syy = sum((q - my) ** 2 for q in y)
    return sxy / (sxx * syy) ** 0.5 if sxx > 0 and syy > 0 else None


def spread_kind(c: float | None) -> str | None:
    return None if c is None else "low" if c >= 0.8 else "some" if c >= 0.6 else "good"
