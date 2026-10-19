"""FRED (St. Louis Fed) i Vibetrad: nøgletjek og makro-facit til hver kørsel.

Vibe-Tradings værktøj ``get_macro_series`` henter FRED-serier, men kun når en agent selv beder om
det, og kun hvis ``FRED_API_KEY`` findes. Dette modul gør FRED synligt og nyttigt i hver kørsel:

* ``snapshot()`` henter en lille, fast pakke serier (renter, rentekurve, Fed funds, VIX,
  kreditspænd og USD/SEK) og tester dermed nøglen. Det sker også i en tørkørsel, så nøglen kan
  kontrolleres gratis.
* ``agent_brief()`` laver en kort tabel, som alle agenter får i deres instruks (samme sted som
  datatjekkets facit).
* ``status_line()`` er linjen i GitHub-loggen.

Nøglen sendes som ``api_key`` i URL'en og fjernes fra alle fejlbeskeder (``redact``).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

BASE_URL = "https://api.stlouisfed.org/fred"
KEY_ENV = "FRED_API_KEY"
_TOKEN_RE = re.compile(r"(api_key|api_token|apikey|token)=[^&\s'\"]+", re.I)

# (serie-id, dansk navn, enhed, maks. alder i dage før den regnes som forældet)
SERIES: tuple[tuple[str, str, str, int], ...] = (
    ("DGS10", "US 10-årig statsrente", "%", 7),
    ("DGS2", "US 2-årig statsrente", "%", 7),
    ("T10Y2Y", "Rentekurve 10 år − 2 år", "pp", 7),
    ("DFF", "Fed funds-rente (effektiv)", "%", 7),
    ("VIXCLS", "VIX (forventet aktievolatilitet)", "", 7),
    ("BAMLH0A0HYM2", "US high yield-kreditspænd (OAS)", "%", 7),
    ("DEXSDUS", "USD/SEK (Fed H.10, ugentlig – kun makrokontekst, ikke til omregning)", "SEK", 16),  # H.10 udgives ugentligt
)
HEADLINE = "DGS10"


def api_key() -> str:
    return os.environ.get(KEY_ENV, "").strip()


def redact(text: Any, key: str | None = None) -> str:
    s = _TOKEN_RE.sub(r"\1=***", str(text))
    for k in {key or "", api_key()}:
        if k and len(k) >= 6:
            s = s.replace(k, "***")
    return s


class FredError(RuntimeError):
    """Fejl fra FRED. Beskeden er altid renset for nøglen."""


class FredClient:
    """Minimal FRED-klient til series/observations. ``calls`` tæller vellykkede kald."""

    def __init__(self, key: str | None = None, *, timeout: float = 15.0, retries: int | None = None,
                 opener: Callable[..., Any] | None = None, sleep: Callable[[float], None] = time.sleep,
                 base_url: str | None = None):
        self.key = (key if key is not None else api_key()).strip()
        self.timeout = timeout
        self.retries = int(os.environ.get("FRED_RETRIES", "1")) if retries is None else retries
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep
        self.base_url = (base_url or os.environ.get("FRED_BASE_URL") or BASE_URL).rstrip("/")
        self.calls = 0
        self.errors: list[str] = []
        self.dead = False  # nøglen afvist: resten af kørslen uden FRED
        self.net_fails = 0  # netværksfejl i træk (nedbrud: snapshot stopper efter 2)

    @property
    def available(self) -> bool:
        return bool(self.key)

    def observations(self, series_id: str, start: str | None = None, end: str | None = None) -> list[tuple[str, float]]:
        """Datoer og værdier (stigende dato). FRED's "." (manglende værdi) springes over."""
        if not self.key:
            raise FredError(f"{KEY_ENV} mangler")
        if self.dead:
            raise FredError("FRED afviste nøglen tidligere i kørslen")
        q = {"series_id": series_id, "file_type": "json", "sort_order": "asc"}
        if start:
            q["observation_start"] = start
        if end:
            q["observation_end"] = end
        url = f"{self.base_url}/series/observations?" + urllib.parse.urlencode({**q, "api_key": self.key})
        req = urllib.request.Request(url, headers={"User-Agent": "vibetrad-runner/1.0", "Accept": "application/json"})
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                self._sleep(1.5 * attempt)
            try:
                with self._open(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                self.calls += 1
                self.net_fails = 0
                data = json.loads(raw.decode("utf-8") or "null") if raw else None
                return _parse(data)
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in (429,) or exc.code >= 500:
                    continue
                detail = _error_message(exc)
                if exc.code in (400, 401, 403) and "api_key" in detail.lower():
                    self.dead = True
                    msg = f"FRED afviste nøglen (HTTP {exc.code}: forkert nøgle)"
                elif "series does not exist" in detail.lower():
                    msg = f"FRED kender ikke serien {series_id}"
                else:
                    msg = f"FRED HTTP {exc.code} ({series_id})"
                msg = redact(msg, self.key)
                self.errors.append(msg)
                raise FredError(msg) from None
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last = exc
        self.net_fails += 1
        msg = redact(f"FRED svarede ikke ({series_id}): {type(last).__name__}: {last}", self.key)
        self.errors.append(msg)
        raise FredError(msg)


def _error_message(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", "replace")
        return str(json.loads(body).get("error_message") or body)
    except Exception:  # noqa: BLE001 - kun til diagnose
        return ""


def _parse(data: Any) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for o in (data or {}).get("observations", []) if isinstance(data, dict) else []:
        try:
            v = o.get("value")
            if v in (None, "", "."):
                continue
            out.append((str(o["date"])[:10], float(v)))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort()
    return out


def _series_row(obs: list[tuple[str, float]], ref_day: str, max_age: int) -> dict:
    obs = [o for o in obs if o[0] <= ref_day]
    if not obs:
        return {"value": None, "date": None, "prev": None, "prev_date": None, "change": None, "stale": None}
    d, v = obs[-1]
    month = (dt.date.fromisoformat(d) - dt.timedelta(days=30)).isoformat()
    before = [o for o in obs if o[0] <= month]
    pd_, pv = before[-1] if before else (None, None)
    age = (dt.date.fromisoformat(ref_day) - dt.date.fromisoformat(d)).days
    return {"value": v, "date": d, "prev": pv, "prev_date": pd_,
            "change": round(v - pv, 4) if pv is not None else None, "stale": age > max_age}


def snapshot(ref_day: str, client: FredClient | None = None, *, window_days: int = 75) -> dict:
    """Seneste værdi, værdien ca. 1 måned før og ændringen for hver serie i ``SERIES``.

    Returnerer altid en ordbog (aldrig en undtagelse): ``key`` (findes nøglen), ``ok`` (mindst
    én serie hentet og nøglen ikke afvist), ``calls``, ``series`` og ``error``.
    """
    c = client or FredClient()
    out: dict[str, Any] = {"key": c.available, "ok": False, "calls": 0, "ref_date": ref_day,
                           "series": [], "error": None, "source": "FRED (St. Louis Fed)"}
    if not c.available:
        out["error"] = f"secret {KEY_ENV} mangler"
        return out
    start = (dt.date.fromisoformat(ref_day) - dt.timedelta(days=window_days)).isoformat()
    for sid, name, unit, max_age in SERIES:
        row: dict[str, Any] = {"id": sid, "name": name, "unit": unit}
        try:
            row.update(_series_row(c.observations(sid, start, ref_day), ref_day, max_age))
        except FredError as exc:
            row.update(value=None, error=str(exc))
        out["series"].append(row)
        if c.dead or c.net_fails >= 2:  # afvist nøgle eller nedbrud: ingen grund til at prøve resten
            break
    out["calls"] = c.calls
    got = [r for r in out["series"] if r.get("value") is not None]
    out["ok"] = bool(got) and not c.dead
    if c.errors:
        out["error"] = c.errors[0] if not got or c.dead else f"{len(c.errors)} serie(r) fejlede: {c.errors[0]}"
    return out


def _nf(v: float | None, d: int = 2) -> str:
    if v is None:
        return "–"
    return f"{v:,.{d}f}".replace(",", " ").replace(".", ",")


def _dmy(d: str | None) -> str:
    return f"{d[8:10]}/{d[5:7]}/{d[:4]}" if d and len(d) >= 10 else "–"


def _fmt(row: dict) -> str:
    unit = row.get("unit") or ""
    return _nf(row.get("value"), 4 if unit == "SEK" else 2) + (f" {unit}" if unit else "")


def _chg(row: dict) -> str:
    c = row.get("change")
    if c is None:
        return "–"
    unit = row.get("unit")
    d = 4 if unit == "SEK" else 2
    suffix = " pp" if unit in ("%", "pp") else (" SEK" if unit == "SEK" else "")
    return ("+" if c > 0 else "") + _nf(c, d) + suffix


def status_line(snap: dict | None) -> str:
    if not snap:
        return "FRED: ikke kørt"
    if not snap.get("key"):
        return f"FRED: nej (secret {KEY_ENV} mangler)"
    if not snap.get("ok"):
        err = str(snap.get("error") or "ingen data")
        if "forkert nøgle" in err:
            return "FRED: nøglen virker ikke (forkert nøgle)"
        return f"FRED: ingen data ({err[:140]})"
    head = next((r for r in snap["series"] if r["id"] == HEADLINE and r.get("value") is not None),
                next(r for r in snap["series"] if r.get("value") is not None))
    n = sum(1 for r in snap["series"] if r.get("value") is not None)
    return (f"FRED: ja, nøglen virker ({n} af {len(SERIES)} serier, {snap.get('calls', 0)} kald · "
            f"{head['name']} {_fmt(head)} pr. {_dmy(head['date'])})")


def agent_brief(snap: dict | None) -> str:
    """Kort makro-tabel til agenternes instruks. Tom, hvis FRED ikke gav data."""
    if not snap or not snap.get("ok"):
        return ""
    rows = [r for r in snap["series"] if r.get("value") is not None]
    lines = [f"## Makro (FRED, St. Louis Fed) pr. {_dmy(snap.get('ref_date'))}",
             "Officielle amerikanske makrotal hentet før agenterne startede. Brug dem som facit for renter, "
             "volatilitet og kreditspænd; USD/SEK til omregning står i datatjekkets facit (DEXSDUS er kun makrokontekst). "
             "Hent længere historik med get_macro_series, hvis du har værktøjet.",
             "", "| Serie | Seneste | Dato | Ændring ca. 1 md |", "|---|---|---|---|"]
    for r in rows:
        stale = " (forældet)" if r.get("stale") else ""
        lines.append(f"| {r['name']} ({r['id']}) | {_fmt(r)} | {_dmy(r['date'])}{stale} | {_chg(r)} |")
    return "\n".join(lines)
