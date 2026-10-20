"""EODHD som anden datakilde ved siden af Vibe-Tradings egne kilder.

Tre ting:

1. ``EodhdClient``: en lille klient til EODHD's REST-API (kun standardbiblioteket).
   Nøglen læses fra ``EODHD_API_KEY`` (eller ``EODHD_API_TOKEN``) og fjernes fra
   alle fejlbeskeder, så den aldrig kan nå GitHub-loggen.
2. ``install_into_vibe_trading()``: kobler EODHD ind i Vibe-Trading 0.1.16, mens
   processen kører (ingen ændring af den installerede pakke):
   - ny datakilde ``eodhd`` i loader-registret
   - nordiske og europæiske tickers (NOVO-B.CO, INVE-B.ST, EQNR.OL, SAP.DE …)
     sendes til EODHD med yfinance som reserve. I 0.1.16 ender de ellers hos de
     kinesiske A-aktie-kilder og giver ingen data.
   - EODHD som reserve nr. 2 for amerikanske aktier (efter Yahoo).
   - agent-værktøjet ``get_eodhd_data`` (kurser, nøgletal, regnskabsdatoer,
     nyheder med sentiment, makro, økonomisk kalender, screener, udbytter/splits).
3. Symbol-oversættelse mellem Vibe-Tradings/Yahoos format og EODHD's.
"""
from __future__ import annotations

import contextlib
import datetime as dt
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

# Yahoo/Vibe-Trading-suffiks → EODHD-børskode (resten er ens: CO, ST, OL, HE, PA, AS …)
SUFFIX_TO_EODHD = {"DE": "XETRA", "L": "LSE", "WA": "WAR", "IL": "LSE"}
# Suffikser som Vibe-Trading 0.1.16 ikke kan route (de havner hos tushare/A-aktier).
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
def rows_to_frame(rows: list[dict], start: str, end: str, *, adjusted: bool = True):
    """EODHD eod-rækker → DataFrame som Vibe-Tradings loaders (index trade_date, float OHLCV).

    Med ``adjusted`` skaleres OHLC med adjusted_close/close, så niveauet svarer til
    Yahoo/yfinance (split- og udbyttejusteret).
    """
    import pandas as pd

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "date" not in df or "close" not in df:
        return pd.DataFrame()
    df.index = pd.DatetimeIndex(pd.to_datetime(df["date"]).dt.normalize(), name="trade_date")
    for col in ("open", "high", "low", "close", "volume", "adjusted_close"):
        if col not in df:
            df[col] = 0.0 if col == "volume" else df.get("close")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if adjusted:
        factor = (df["adjusted_close"] / df["close"]).where(df["close"] > 0).fillna(1.0)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col] * factor
    df["volume"] = df["volume"].fillna(0.0)
    df = df[["open", "high", "low", "close", "volume"]].dropna(subset=["open", "high", "low", "close"])
    lo, hi = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize() + pd.Timedelta(days=1)
    return df[(df.index >= lo) & (df.index < hi)].astype(float).sort_index()


# ---------------------------------------------------------------------------
# Integration i Vibe-Trading (0.1.16)
# ---------------------------------------------------------------------------
_SHARED: dict[str, Any] = {"client": None}


def shared_client() -> EodhdClient:
    if _SHARED["client"] is None or _SHARED["client"].key != api_key():
        _SHARED["client"] = EodhdClient()
    return _SHARED["client"]


def _make_loader_class(client_factory: Callable[[], EodhdClient]):
    class EodhdLoader:
        """EODHD end-of-day (split- og udbyttejusteret), nordiske/europæiske og amerikanske aktier, valuta."""

        name = "eodhd"
        markets = {"eu_equity"}
        volume_units = {"us_equity": "shares", "eu_equity": "shares", "a_share": "shares"}
        requires_auth = True

        def is_available(self) -> bool:
            return bool(api_key())

        def fetch(self, codes: list[str], start_date: str, end_date: str, *, interval: str = "1D",
                  fields: list[str] | None = None) -> dict:
            del fields
            if not codes or str(interval).upper() not in ("1D", "D", "1W", "1M"):
                return {}
            client = client_factory()
            out = {}
            for code in codes:
                try:
                    frame = rows_to_frame(client.eod(code, start_date, end_date), start_date, end_date)
                except Exception:
                    continue
                if frame is not None and not frame.empty:
                    ccy = currency_of(code)
                    if ccy:
                        frame.attrs["quote_currency"] = ccy
                    out[code] = frame
            return out

    return EodhdLoader


def _compact(obj: Any, limit: int = 9000) -> str:
    text = json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(text) <= limit:
        return text
    return json.dumps({"ok": True, "truncated": True, "data": text[:limit]}, ensure_ascii=False)


ETF_SECTIONS = tuple(f"ETF_Data::{k}" for k in ("ISIN", "Company_Name", "Domicile", "Index_Name", "Inception_Date",
                                                    "Ongoing_Charge", "TotalAssets", "Dividend_Paying_Frequency",
                                                    "Holdings_Count", "World_Regions", "Top_10_Holdings"))


def etf_brief(raw: dict) -> dict:
    """ETF_Data::X-nøgler → kort dict (regioner over 1 %, top-10 som navn → vægt)."""
    out: dict[str, Any] = {}
    for k, v in (raw or {}).items():
        if not k.startswith("ETF_Data::") or v in (None, "", "NA"):
            continue
        key = k.split("::", 1)[1]
        if key == "World_Regions" and isinstance(v, dict):
            v = {r: round(float(x.get("Equity_%") or 0), 1) for r, x in v.items()
                 if isinstance(x, dict) and float(x.get("Equity_%") or 0) >= 1}
        elif key == "Top_10_Holdings" and isinstance(v, dict):
            v = {f"{x.get('Name') or c} ({c})": round(float(x.get("Assets_%") or 0), 2) for c, x in v.items() if isinstance(x, dict)}
        elif key == "Ongoing_Charge":
            with contextlib.suppress(TypeError, ValueError):
                v = f"{float(v):.2f} %"
        out[key] = v
    return out


TOOL_DESCRIPTION = (
    "EODHD market data (second source next to Vibe-Trading's Yahoo/yfinance). Use it to cross-check prices and for "
    "Nordic/European listings. Symbols: AAPL.US, NOVO-B.CO, INVE-B.ST, EQNR.OL, SAP.XETRA (SAP.DE also works), "
    "USDSEK.FOREX, SE10Y.GBOND/US10Y.GBOND (government bond yields). Actions: "
    "quote (symbols: delayed live price), eod (symbol, start, end: daily OHLC with adjusted close), "
    "fundamentals (symbol: name, currency, sector, IPO date, market cap, P/E, EPS estimates, analyst target, shares; for "
    "ETFs/funds also ISIN, ongoing charge (TER), domicile, index, AUM, regions and top-10 holdings – XETRA listings as "
    "VWCE.XETRA or VWCE.DE), "
    "earnings_calendar (symbols, start, end: report dates, BMO/AMC, EPS estimate), "
    "news (symbol or tag, start, limit up to 50, optional query = keyword filter on title/text, e.g. 'results' or "
    "'earnings date': headlines with sentiment polarity and link), "
    "sentiment (symbols, start, end: daily news sentiment score per symbol; thin for Nordic names), "
    "earnings_trends (symbols: consensus EPS/revenue estimates with 7/30/90-day revisions), "
    "dividends / splits (symbol, start), "
    "macro (country ISO-3 + indicator, e.g. SWE inflation_consumer_prices_annual, USA real_interest_rate), "
    "economic_events (country ISO-2 e.g. US/SE, start, end: Fed/Riksbank decisions, CPI etc.), "
    "screener (filters as EODHD JSON list, e.g. [[\"exchange\",\"=\",\"ST\"],[\"market_capitalization\",\">\",1e10]], "
    "sort e.g. market_capitalization.desc, limit). Dates YYYY-MM-DD. Not offered: insider transactions (EODHD's feed "
    "lags months; use get_sec_filings for Form 4) and options (not tradable in this account)."
)


def _tool_execute(client: EodhdClient, **kw: Any) -> str:
    action = str(kw.get("action") or "").strip().lower()
    today = dt.date.today()
    start = kw.get("start") or (today - dt.timedelta(days=30)).isoformat()
    end = kw.get("end") or today.isoformat()
    syms = kw.get("symbols") or []
    if isinstance(syms, str):
        syms = [s.strip() for s in syms.split(",") if s.strip()]
    sym = kw.get("symbol") or (syms[0] if syms else None)
    if not syms and sym:
        syms = [sym]
    try:
        if action == "quote":
            data: Any = [{k: r.get(k) for k in ("code", "timestamp", "close", "previousClose", "change_p", "volume")}
                         for r in client.real_time(syms)]
        elif action == "eod":
            data = client.eod(sym, start, end)[-260:]
        elif action == "fundamentals":
            f = client.fundamentals(sym, ("General", "Highlights", "Valuation", "SharesStats", "AnalystRatings"))
            g = f.get("General") or {}
            data = {"General": {k: g.get(k) for k in ("Code", "Name", "Exchange", "CurrencyCode", "CountryISO", "ISIN",
                                                     "Sector", "Industry", "IPODate", "FiscalYearEnd", "UpdatedAt")},
                    **{k: f.get(k) for k in ("Highlights", "Valuation", "SharesStats", "AnalystRatings") if f.get(k)}}
            if str(g.get("Type") or "").upper() in ("ETF", "FUND"):
                # ETF'er og fonde har ingen Highlights/Valuation; ISIN og omkostning står i ETF_Data. 20/10/2026 skrev
                # alle teams "ISIN/TER kunne ikke verificeres", selv om EODHD har dem (fx EXSA: DE0002635307, 0,19 %).
                data["General"]["Type"] = g.get("Type")
                data["ETF_Data"] = etf_brief(client.fundamentals(sym, ETF_SECTIONS))
        elif action in ("earnings", "earnings_calendar"):
            data = client.earnings(syms, start, kw.get("end") or (today + dt.timedelta(days=120)).isoformat())
        elif action == "news":
            query = str(kw.get("query") or "").strip().lower()
            words = [w for w in re.split(r"[\s,;]+", query) if w]
            items = client.news(sym, tag=kw.get("tag"), start=kw.get("start"), end=kw.get("end"),
                                limit=int(kw.get("limit") or (50 if words else 10)))
            if words:  # søgeord: alle ord skal findes i overskrift eller tekst
                items = [n for n in items if all(w in (str(n.get("title") or "") + " " + str(n.get("content") or "")).lower()
                                                 for w in words)]
            data = [{"date": n.get("date"), "title": n.get("title"), "symbols": (n.get("symbols") or [])[:8],
                     "sentiment": n.get("sentiment"), "link": n.get("link"),
                     **({"snippet": str(n.get("content") or "")[:240]} if words else {})}
                    for n in items[: int(kw.get("limit") or 10)]]
        elif action == "sentiment":
            data = client.sentiment(syms, kw.get("start"), kw.get("end"))
        elif action == "earnings_trends":
            data = client.earnings_trends(syms)[:12]
        elif action == "dividends":
            data = client.dividends(sym, kw.get("start"))[-40:]
        elif action == "splits":
            data = client.splits(sym, kw.get("start"))
        elif action == "macro":
            data = client.macro(str(kw.get("country") or "USA"), str(kw.get("indicator") or "inflation_consumer_prices_annual"))[-30:]
        elif action == "economic_events":
            data = client.economic_events(kw.get("country"), start, kw.get("end") or (today + dt.timedelta(days=45)).isoformat(),
                                          int(kw.get("limit") or 50))
        elif action == "screener":
            filters = kw.get("filters") or []
            if isinstance(filters, str):
                filters = json.loads(filters)
            data = client.screener(filters, kw.get("sort"), int(kw.get("limit") or 25))
        else:
            return json.dumps({"ok": False, "error": f"Unknown action {action!r}. See the tool description."})
    except EodhdError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - et værktøj må aldrig vælte en agent
        return json.dumps({"ok": False, "error": redact(f"{type(exc).__name__}: {exc}", client.key)}, ensure_ascii=False)
    return _compact({"ok": True, "source": "eodhd", "action": action, "data": data})


TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["quote", "eod", "fundamentals", "earnings_calendar", "news", "sentiment",
                                               "earnings_trends", "dividends", "splits", "macro", "economic_events",
                                               "screener"]},
        "symbol": {"type": "string", "description": "e.g. NVDA.US, NOVO-B.CO, USDSEK.FOREX"},
        "symbols": {"type": "array", "items": {"type": "string"}},
        "start": {"type": "string", "description": "YYYY-MM-DD"},
        "end": {"type": "string", "description": "YYYY-MM-DD"},
        "tag": {"type": "string"},
        "query": {"type": "string", "description": "news only: keywords that must all appear in the headline or text"},
        "country": {"type": "string"},
        "indicator": {"type": "string"},
        "filters": {"type": "array", "items": {"type": "array", "items": {}}},
        "sort": {"type": "string"},
        "limit": {"type": "integer"},
    },
    "required": ["action"],
}


def _make_tool_class(client_factory: Callable[[], EodhdClient]):
    from src.agent.tools import BaseTool

    class EodhdDataTool(BaseTool):
        name = "get_eodhd_data"
        description = TOOL_DESCRIPTION
        parameters = TOOL_PARAMETERS
        repeatable = True
        is_readonly = True

        @classmethod
        def check_available(cls) -> bool:
            return bool(api_key())

        def execute(self, **kwargs: Any) -> str:
            kwargs.pop("run_dir", None)
            return _tool_execute(client_factory(), **kwargs)

    return EodhdDataTool


def key_status(client: EodhdClient | None = None) -> dict:
    """Test nøglen uden at vise den: {'ok': bool, 'subscription', 'requests_today', 'daily_limit', 'error'}."""
    c = client or EodhdClient()
    if not c.available:
        return {"ok": False, "error": "ingen nøgle (EODHD_API_KEY er ikke sat)"}
    try:
        u = c.user()
    except EodhdError as exc:
        return {"ok": False, "error": str(exc)}
    if not u:
        return {"ok": False, "error": "EODHD svarede uden kontooplysninger"}
    return {"ok": True, "subscription": " ".join(str(x) for x in (u.get("subscriptionType"), u.get("subscriptionMode")) if x),
            "requests_today": u.get("apiRequests"), "daily_limit": u.get("dailyRateLimit")}


def install_into_vibe_trading(client_factory: Callable[[], EodhdClient] = shared_client, *,
                              with_tool: bool = True) -> dict:
    """Kobl EODHD ind i den kørende Vibe-Trading-proces. Idempotent. Returnerer hvad der blev gjort."""
    done: dict[str, Any] = {"loader": False, "routing": False, "us_fallback": False, "tool": False,
                            "key": bool(api_key())}
    from backtest.loaders import registry as reg

    reg._ensure_registered()
    if "eodhd" not in reg.LOADER_REGISTRY:
        reg.register(_make_loader_class(client_factory))
    reg.VALID_SOURCES.add("eodhd")
    reg.PRICE_CALIBER_BY_SOURCE.setdefault("eodhd", "split_dividend")
    done["loader"] = True

    # Kæden for europæiske symboler skal ligge først i dict'en: Vibe-Trading slår en
    # kilde op i den første kæde, der indeholder den, når symbolets marked (her
    # fejlagtigt "a_share") ikke gør.
    if "eu_equity" not in reg.FALLBACK_CHAINS or next(iter(reg.FALLBACK_CHAINS)) != "eu_equity":
        items = [(k, v) for k, v in reg.FALLBACK_CHAINS.items() if k != "eu_equity"]
        reg.FALLBACK_CHAINS.clear()
        reg.FALLBACK_CHAINS["eu_equity"] = ["eodhd", "yfinance", "local"]
        reg.FALLBACK_CHAINS.update(items)
    _ensure_us_fallback(reg)
    if not getattr(reg.refresh_source_order_overrides, "_vibetrad", False):
        # Vibe-Trading genskaber standardkæderne, når en MARKET_DATA_ORDER_*-variabel ændres (fx fra Settings i
        # web-UI'et). Sæt EODHD ind igen bagefter, medmindre brugeren selv har valgt rækkefølgen for US-aktier.
        orig_refresh = reg.refresh_source_order_overrides

        def refresh_and_keep_eodhd(*a: Any, **kw: Any) -> Any:
            out = orig_refresh(*a, **kw)
            _ensure_us_fallback(reg)
            return out

        refresh_and_keep_eodhd._vibetrad = True  # type: ignore[attr-defined]
        reg.refresh_source_order_overrides = refresh_and_keep_eodhd
    done["us_fallback"] = "eodhd" in reg.FALLBACK_CHAINS.get("us_equity", [])

    import src.market_data as md

    if not any(src == "eodhd" for _, src in md._SOURCE_PATTERNS):
        md._SOURCE_PATTERNS.append((EU_PATTERN, "eodhd"))
        # Aktieklasser (BRK-B.US, BF.B.US) matcher ikke Vibe-Tradings US-mønster og havnede hos A-aktie-kilderne
        md._SOURCE_PATTERNS.append((re.compile(r"^[A-Z0-9&.\-]+\.US$", re.I), "yahoo"))
    done["routing"] = md.detect_source("NOVO-B.CO") == "eodhd"

    if with_tool:
        import src.tools as tools_pkg

        try:
            from src.agent.tools import BaseTool

            present = any(getattr(c, "name", "") == "get_eodhd_data" for c in _all_subclasses(BaseTool))
            if not present:
                _SHARED["tool_cls"] = _make_tool_class(client_factory)
            tools_pkg._SUBCLASSES_CACHE = None  # tving ny opdagelse, så værktøjet kommer med
            done["tool"] = True
        except Exception as exc:  # noqa: BLE001
            done["tool_error"] = f"{type(exc).__name__}: {exc}"
    return done


def _ensure_us_fallback(reg: Any) -> None:
    try:
        if reg.get_source_order_override("us_equity") is not None:
            return  # brugerens egen rækkefølge respekteres
    except Exception:  # noqa: BLE001
        pass
    us = reg.FALLBACK_CHAINS.get("us_equity")
    if isinstance(us, list) and "eodhd" not in us:
        us.insert(1 if us else 0, "eodhd")


def _all_subclasses(cls) -> list:
    out, stack = [], list(cls.__subclasses__())
    while stack:
        c = stack.pop()
        out.append(c)
        stack.extend(c.__subclasses__())
    return out
