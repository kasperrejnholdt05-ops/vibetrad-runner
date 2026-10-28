"""EODHD-klient til vagthunden (kun standardbiblioteket).

``EodhdClient`` henter dagskurser, realtidskurser m.m. fra EODHD's REST-API. Nøglen læses fra
``EODHD_API_KEY`` (eller ``EODHD_API_TOKEN``) og fjernes fra alle fejlbeskeder, så den aldrig kan nå
GitHub-loggen. Symboler i Yahoo-format oversættes til EODHD's (SAP.DE → SAP.XETRA, ^GSPC → GSPC.INDX).
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

BASE_URL = "https://eodhd.com/api"
KEY_ENVS = ("EODHD_API_KEY", "EODHD_API_TOKEN")

# Yahoo-suffiks → EODHD-børskode (resten er ens: CO, ST, OL, HE, PA, AS …)
SUFFIX_TO_EODHD = {"DE": "XETRA", "L": "LSE", "WA": "WAR", "IL": "LSE"}
# Europæiske/nordiske suffikser.
EU_SUFFIXES = ("CO", "ST", "OL", "HE", "IC", "DE", "XETRA", "F", "PA", "AS", "BR", "LS", "MI", "MC", "SW", "VI",
               "IR", "WA")
EU_PATTERN = re.compile(r"^[A-Z0-9&\-]+(?:\.[A-Z])?\.(%s)$" % "|".join(EU_SUFFIXES), re.I)
CURRENCY_BY_SUFFIX = {
    "US": "USD", "CO": "DKK", "ST": "SEK", "OL": "NOK", "HE": "EUR", "IC": "ISK", "DE": "EUR", "XETRA": "EUR",
    "F": "EUR", "PA": "EUR", "AS": "EUR", "BR": "EUR", "LS": "EUR", "MI": "EUR", "MC": "EUR", "SW": "CHF",
    "VI": "EUR", "IR": "EUR", "WA": "PLN", "L": "GBP", "LSE": "GBP", "TO": "CAD", "V": "CAD", "HK": "HKD",
}
_TOKEN_RE = re.compile(r"(api_token|apikey|api_key|token)=[^&\s'\"]+", re.I)


def key_file() -> str:
    """Reserve til codespacet: en fil kun brugeren kan læse (chmod 600), hvis der ikke er en Codespaces-secret."""
    return os.environ.get("EODHD_KEY_FILE") or os.path.join(os.path.expanduser("~"), ".config", "vibetrad", "eodhd_key")


def api_key() -> str:
    for name in KEY_ENVS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    try:
        with open(key_file(), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def redact(text: Any, key: str | None = None) -> str:
    s = _TOKEN_RE.sub(r"\1=***", str(text))
    for k in {key or "", api_key()}:
        if k and len(k) >= 6:
            s = s.replace(k, "***")
    return s


class EodhdError(RuntimeError):
    """Fejl fra EODHD. Beskeden er altid renset for nøglen."""


# ---------------------------------------------------------------------------
# Symboler
# ---------------------------------------------------------------------------
def suffix_of(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    if s.endswith("=X"):
        return "FOREX"
    if s.startswith("^"):
        return "INDX"
    return s.rsplit(".", 1)[-1] if "." in s else "US"


def to_eodhd(symbol: str) -> str:
    """AAPL.US → AAPL.US · NOVO-B.CO → NOVO-B.CO · SAP.DE → SAP.XETRA · USDSEK=X → USDSEK.FOREX · ^GSPC → GSPC.INDX."""
    s = str(symbol or "").strip().upper()
    if not s:
        return s
    if s.endswith("=X"):
        return s[:-2] + ".FOREX"
    m = re.fullmatch(r"([A-Z]{3})/([A-Z]{3})", s)
    if m:
        return m.group(1) + m.group(2) + ".FOREX"
    if s.endswith(".FX"):
        return s[:-3] + ".FOREX"
    if s.startswith("^"):
        return s[1:] + ".INDX"
    if "." not in s:
        return s + ".US"
    base, suf = s.rsplit(".", 1)
    if suf == "HK" and base.isdigit():
        base = base.lstrip("0").zfill(4)
    return f"{base}.{SUFFIX_TO_EODHD.get(suf, suf)}"


def to_yahoo(symbol: str) -> str:
    """Vibe-Trading/EODHD-symbol → Yahoo/yfinance (GOOGL.US → GOOGL, SAP.XETRA → SAP.DE)."""
    s = str(symbol or "").strip().upper()
    if s.endswith(".US"):
        return s[:-3].replace(".", "-")
    if s.endswith(".FOREX"):
        return s[:-6] + "=X"
    if s.endswith(".INDX"):
        return "^" + s[:-5]
    back = {"XETRA": "DE", "LSE": "L", "WAR": "WA"}
    if "." in s:
        base, suf = s.rsplit(".", 1)
        return f"{base}.{back.get(suf, suf)}"
    return s


def currency_of(symbol: str) -> str | None:
    s = str(symbol or "").strip().upper()
    if s.endswith("=X") or s.endswith(".FOREX"):
        pair = s.split("=")[0].split(".")[0]
        return pair[3:6] if len(pair) >= 6 else None
    return CURRENCY_BY_SUFFIX.get(suffix_of(s))


def is_eu_symbol(symbol: str) -> bool:
    return bool(EU_PATTERN.match(str(symbol or "").strip()))


# ---------------------------------------------------------------------------
# Klient
# ---------------------------------------------------------------------------
class EodhdClient:
    """Minimal EODHD-klient. ``calls`` tæller API-kald efter EODHD's vægte."""

    MAX_FAILS = 4  # så mange mislykkede kald i træk → pause, så et nedbrud ikke koster minutter
    PAUSE_S = 300.0

    def __init__(self, key: str | None = None, *, timeout: float = 20.0, retries: int | None = None,
                 opener: Callable[..., Any] | None = None, sleep: Callable[[float], None] = time.sleep,
                 base_url: str | None = None, clock: Callable[[], float] = time.monotonic):
        self.key = (key if key is not None else api_key()).strip()
        self.timeout = timeout
        self.retries = int(os.environ.get("EODHD_RETRIES", "2")) if retries is None else retries
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep
        self.base_url = (base_url or os.environ.get("EODHD_BASE_URL") or BASE_URL).rstrip("/")
        self.calls = 0
        self.requests = 0
        self.errors: list[str] = []
        self._fails = 0
        self._clock = clock
        self._paused_until = 0.0
        self._dead = False  # nøglen afvist (401): resten af kørslen uden EODHD
        self._blocked: set[str] = set()  # endpoints abonnementet ikke dækker (403)
        self._cache: dict[str, Any] = {}

    @property
    def available(self) -> bool:
        return bool(self.key)

    def _get(self, path: str, params: dict | None = None, *, weight: int = 1) -> Any:
        if not self.key:
            raise EodhdError("EODHD_API_KEY mangler")
        q = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        q.setdefault("fmt", "json")
        cache_key = path + "?" + urllib.parse.urlencode(sorted(q.items()))
        if cache_key in self._cache:
            return self._cache[cache_key]
        url = f"{self.base_url}/{path.lstrip('/')}?" + urllib.parse.urlencode({**q, "api_token": self.key})
        req = urllib.request.Request(url, headers={"User-Agent": "vibetrad-runner/1.0", "Accept": "application/json"})
        endpoint = path.split("/", 1)[0]
        if self._dead:
            raise EodhdError("EODHD afviste nøglen tidligere i kørslen (HTTP 401)")
        if endpoint in self._blocked:
            raise EodhdError(f"EODHD-abonnementet dækker ikke {endpoint} (HTTP 403)")
        if self._clock() < self._paused_until:
            raise EodhdError("EODHD svarer ikke lige nu (pause efter gentagne fejl)")
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                self._sleep(1.5 * attempt)
            try:
                self.requests += 1
                with self._open(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                self.calls += weight
                self._fails = 0
                data = json.loads(raw.decode("utf-8") or "null") if raw else None
                self._cache[cache_key] = data
                return data
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code == 404:
                    self._cache[cache_key] = None
                    return None
                if exc.code == 401:
                    self._dead = True
                    msg = f"EODHD afviste nøglen (HTTP 401, {endpoint})"
                    self.errors.append(msg)
                    raise EodhdError(msg) from None
                if exc.code == 403:  # typisk et endpoint, abonnementet ikke dækker – resten virker stadig
                    self._blocked.add(endpoint)
                    msg = f"EODHD-abonnementet dækker ikke {endpoint} (HTTP 403)"
                    self.errors.append(msg)
                    raise EodhdError(msg) from None
                if exc.code == 429 or exc.code >= 500:
                    continue
                raise EodhdError(f"EODHD HTTP {exc.code} ({endpoint})") from None
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last = exc
        self._fails += 1
        if self._fails >= self.MAX_FAILS:
            self._paused_until = self._clock() + self.PAUSE_S
            self._fails = 0
        msg = redact(f"EODHD svarede ikke ({endpoint}): {type(last).__name__}: {last}", self.key)
        self.errors.append(msg)
        raise EodhdError(msg)

    # --- endpoints -------------------------------------------------------
    def eod(self, symbol: str, start: str | None = None, end: str | None = None, period: str = "d") -> list[dict]:
        data = self._get(f"eod/{to_eodhd(symbol)}", {"from": start, "to": end, "period": period})
        return data if isinstance(data, list) else []

    def real_time(self, symbols: list[str]) -> list[dict]:
        syms = [to_eodhd(s) for s in symbols if s]
        if not syms:
            return []
        data = self._get(f"real-time/{syms[0]}", {"s": ",".join(syms[1:]) or None})
        return data if isinstance(data, list) else [data] if isinstance(data, dict) else []

    def fundamentals(self, symbol: str, sections: tuple[str, ...] = ("General", "Highlights", "Valuation")) -> dict:
        data = self._get(f"fundamentals/{to_eodhd(symbol)}", {"filter": ",".join(sections) if sections else None},
                         weight=10)
        if not isinstance(data, dict):
            return {}
        if len(sections) == 1 and sections[0] not in data:
            return {sections[0]: data}
        return data

    def earnings(self, symbols: list[str], start: str, end: str) -> list[dict]:
        data = self._get("calendar/earnings", {"symbols": ",".join(to_eodhd(s) for s in symbols), "from": start,
                                                "to": end})
        return (data or {}).get("earnings", []) if isinstance(data, dict) else []

    def dividends(self, symbol: str, start: str | None = None) -> list[dict]:
        data = self._get(f"div/{to_eodhd(symbol)}", {"from": start})
        return data if isinstance(data, list) else []

    def splits(self, symbol: str, start: str | None = None) -> list[dict]:
        data = self._get(f"splits/{to_eodhd(symbol)}", {"from": start})
        return data if isinstance(data, list) else []

    def news(self, symbol: str | None = None, *, tag: str | None = None, start: str | None = None,
             end: str | None = None, limit: int = 10) -> list[dict]:
        data = self._get("news", {"s": to_eodhd(symbol) if symbol else None, "t": tag, "from": start, "to": end,
                                  "limit": max(1, min(int(limit), 1000)), "offset": 0}, weight=5)
        return data if isinstance(data, list) else []

    def macro(self, country_iso3: str, indicator: str) -> list[dict]:
        data = self._get(f"macro-indicator/{country_iso3.upper()}", {"indicator": indicator})
        return data if isinstance(data, list) else []

    def economic_events(self, country_iso2: str | None, start: str, end: str, limit: int = 50,
                        event_type: str | None = None) -> list[dict]:
        data = self._get("economic-events", {"country": country_iso2, "from": start, "to": end, "type": event_type,
                                             "limit": max(1, min(int(limit), 200))})
        return data if isinstance(data, list) else []

    def sentiment(self, symbols: list[str], start: str | None = None, end: str | None = None) -> dict:
        """Daglig nyhedsstemning pr. symbol (EODHD /sentiments). Tynd for nordiske aktier."""
        data = self._get("sentiments", {"s": ",".join(to_eodhd(s) for s in symbols if s), "from": start, "to": end})
        return data if isinstance(data, dict) else {}

    def earnings_trends(self, symbols: list[str]) -> list[dict]:
        """Analytikernes EPS-/omsætningsestimater og revisioner (EODHD /calendar/trends, vægt 10)."""
        data = self._get("calendar/trends", {"symbols": ",".join(to_eodhd(s) for s in symbols if s)}, weight=10)
        if isinstance(data, dict):
            data = data.get("trends") or []
        return data if isinstance(data, list) else []

    def user(self) -> dict:
        """Kontooplysninger for nøglen (abonnement og dagens forbrug). Bruges til at teste nøglen."""
        data = self._get("user", {})
        return data if isinstance(data, dict) else {}

    def screener(self, filters: list, sort: str | None = None, limit: int = 25) -> list[dict]:
        data = self._get("screener", {"filters": json.dumps(filters), "sort": sort,
                                      "limit": max(1, min(int(limit), 100)), "offset": 0}, weight=5)
        return (data or {}).get("data", []) if isinstance(data, dict) else []


# ---------------------------------------------------------------------------
# Kursrækker i Vibe-Tradings format
# ---------------------------------------------------------------------------
