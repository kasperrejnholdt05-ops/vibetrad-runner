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
