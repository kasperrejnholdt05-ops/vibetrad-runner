"""Makrovagt: vagthundens øjne på markeder og økonomi (ingen LLM, ingen porteføljedata).

Makrovagten kigger efter stress, før den rammer aktiemarkederne: frygt (VIX), kreditspænd, finansiel stress,
rentekurve, arbejdsmarked (Sahm-reglen, nye ledige), aktiefald og dagschok, olie, dollar, krone og guld –
plus Buffett-indikatoren som baggrund for, hvor hårdt et fald kan blive.

Hvert signal får grøn, gul eller rød. Et "chok" er en usædvanlig hurtig bevægelse (fx VIX +40 % på 5 dage
eller et dagsfald på 3 %). Sort svane-varsel = mindst ét chok eller mindst tre røde signaler på én gang.
Sorte svaner kan ikke forudsiges; vagthunden kan kun opdage stressen tidligt, mens der er tid til at tænke.

Fra 27/10/2026 (runner/temaer.py) er vagthunden bygget til de omstændigheder, vi står i: renter over 5 %, en
statsgæld med rentebetalinger på ca. 1.000 mia. USD om året, AI-priser bygget på forventet omsætning, mangel på
chips og strøm, olie omkring 120 USD efter Hormuz-krisen, krig og politisk uro. Alle signaler hører til ét af ti
temaer, og seks scenarier viser, hvilken vej det trækker (blød landing, stagflation, rente- og gældschok, AI-brud,
kreditkrise, geopolitisk chok). Sort svane-varsel = et chok, eller røde signaler i mindst tre temaer.

Grænserne er den følsomme profil (Kasper 23/10/2026: "tidlig og følsom"): hellere en gul for meget end en
rød for sent. Resultatet ligger offentligt i results/vagt.json (kun markedsdata) og vises øverst på Vagt.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import temaer as tm

PROFILE = "følsom"

# (FRED-id, dansk navn, enhed, maks. alder i dage)
FRED_SERIES: tuple[tuple[str, str, str, int], ...] = (
    ("VIXCLS", "VIX (frygtindeks)", "", 7),
    ("BAMLH0A0HYM2", "Kreditspænd, risikable selskabslån (high yield)", "%", 7),
    ("BAMLC0A0CM", "Kreditspænd, sikre selskabslån (investment grade)", "%", 7),
    ("STLFSI4", "Finansiel stress (St. Louis Fed)", "", 14),
    ("NFCI", "Finansielle forhold (Chicago Fed)", "", 14),
    ("T10Y3M", "Rentekurve 10 år − 3 mdr.", "pp", 7),
    ("ICSA", "Nye ledige i USA (ugentlig)", "", 14),
    ("SAHMREALTIME", "Sahm-reglen (recessionssignal)", "pp", 75),
    ("DCOILBRENTEU", "Olie (Brent)", "USD", 10),
    ("DTWEXBGS", "Dollaren mod andre valutaer", "", 10),
)
# (EODHD-symbol, dansk navn, slags)
EOD_SERIES: tuple[tuple[str, str, str], ...] = (
    ("GSPC.INDX", "S&P 500", "index"),
    ("NDX.INDX", "Nasdaq 100", "index"),
    ("STOXX50E.INDX", "Euro Stoxx 50", "index"),
    ("OMXS30.INDX", "OMXS30", "index"),
    ("OMXC25.INDX", "OMXC25", "index"),
    ("VIX.INDX", "VIX (EODHD)", "vix"),
    ("USDSEK.FOREX", "USD/SEK", "fx"),
    ("XAUUSD.FOREX", "Guld", "gold"),
)

# Grænser (følsom profil). (gul, rød) – for "faldende er værre" er tallene fald i procent.
T = {
    "vix": (22.0, 30.0), "vix_spike5": 40.0,
    "hy": (4.0, 5.5), "hy_widen21": 0.75,
    "ig": (1.3, 1.8),
    "stlfsi": (0.0, 1.0),
    "nfci": (0.0, 0.5),
    "sahm": (0.3, 0.5),
    "claims": (15.0, 30.0),            # 4-ugers gennemsnit over årets laveste, %
    "dd": (7.0, 15.0),                 # fald fra 52-ugers top, %
    "day_shock": -3.0, "week_shock": -6.0,
    "oil_up": (20.0, 35.0), "oil_down": -30.0,
    "usd_up": 4.0,
    "sek_move": 5.0,
    "gold_up": 10.0,
    "buffett_pct": 90,
}
LEVELS = ("green", "yellow", "red")
DA = {"green": "grøn", "yellow": "gul", "red": "rød"}


def _f(v: Any) -> float | None:
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _nf(v: float | None, d: int = 1) -> str:
    if v is None:
        return "–"
    s = f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return s


def _dmy(d: str | None) -> str:
    try:
        return dt.date.fromisoformat(str(d)[:10]).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return "–"


def _lvl(value: float | None, limits: tuple[float, float]) -> str:
    if value is None:
        return "green"
    return "red" if value >= limits[1] else "yellow" if value >= limits[0] else "green"


def _worst(levels: list[str]) -> str:
    return max(levels or ["green"], key=LEVELS.index)


def _back(obs: list[tuple[str, float]], days: int) -> float | None:
    """Værdien ca. ``days`` kalenderdage før den seneste observation."""
    if not obs:
        return None
    last = dt.date.fromisoformat(obs[-1][0])
    cut = (last - dt.timedelta(days=days)).isoformat()
    before = [v for d, v in obs if d <= cut]
    return before[-1] if before else None


def _pct(a: float | None, b: float | None) -> float | None:
    return None if a is None or not b else (a / b - 1) * 100


def _sig(sid: str, label: str, value: float | None, date: str | None, level: str, note: str, *,
         unit: str = "", shock: bool = False, stale: bool = False, rule: str = "") -> dict:
    return {"id": sid, "label": label, "value": None if value is None else round(value, 3), "unit": unit,
            "date": date, "level": level if not stale else "green", "shock": bool(shock and not stale),
            "stale": stale, "note": note + (" (data er forældet – tæller ikke med)" if stale else ""), "rule": rule}


def _age(date: str | None, ref_day: str) -> int:
    try:
        return (dt.date.fromisoformat(ref_day) - dt.date.fromisoformat(str(date)[:10])).days
    except (TypeError, ValueError):
        return 10 ** 6


# ---------------------------------------------------------------------------
# Regler pr. signal
def fred_signals(series: dict[str, list[tuple[str, float]]], ref_day: str) -> list[dict]:
    out: list[dict] = []
    maxage = {sid: a for sid, _, _, a in FRED_SERIES}
    names = {sid: n for sid, n, _, _ in FRED_SERIES}

    def last(sid: str) -> tuple[str | None, float | None, bool]:
        obs = series.get(sid) or []
        if not obs:
            return None, None, True
        d, v = obs[-1]
        return d, v, _age(d, ref_day) > maxage[sid]

    d, v, st = last("VIXCLS")
    if v is not None:
        b5 = _back(series["VIXCLS"], 7)
        spike = _pct(v, b5)
        lv = _lvl(v, T["vix"])
        shock = spike is not None and spike >= T["vix_spike5"]
        if shock:
            lv = "red"
        note = (f"VIX er {_nf(v)}. Over {_nf(T['vix'][0], 0)} er markedet nervøst, over {_nf(T['vix'][1], 0)} bange"
                + (f"; steget {_nf(spike, 0)} % på en uge (chok)" if shock else ""))
        out.append(_sig("vix", names["VIXCLS"], v, d, lv, note, shock=shock, stale=st,
                        rule=f"gul ≥ {T['vix'][0]:g} · rød ≥ {T['vix'][1]:g} · chok +{T['vix_spike5']:g} % på 5 dage"))

    d, v, st = last("BAMLH0A0HYM2")
    if v is not None:
        b21 = _back(series["BAMLH0A0HYM2"], 30)
        widen = None if b21 is None else v - b21
        lv = _lvl(v, T["hy"])
        shock = widen is not None and widen >= T["hy_widen21"]
        if shock:
            lv = "red"
        note = (f"Långivere kræver {_nf(v, 2)} procentpoint ekstra for at låne til risikable selskaber"
                + (f"; steget {_nf(widen, 2)} pp på en måned (kreditchok)" if shock else
                   f" ({'+' if (widen or 0) >= 0 else ''}{_nf(widen, 2)} pp på en måned)" if widen is not None else ""))
        out.append(_sig("hy", names["BAMLH0A0HYM2"], v, d, lv, note, unit="%", shock=shock, stale=st,
                        rule=f"gul ≥ {T['hy'][0]:g} · rød ≥ {T['hy'][1]:g} · chok +{T['hy_widen21']:g} pp på en måned"))

    d, v, st = last("BAMLC0A0CM")
    if v is not None:
        out.append(_sig("ig", names["BAMLC0A0CM"], v, d, _lvl(v, T["ig"]),
                        f"Ekstra rente for sikre selskaber: {_nf(v, 2)} pp", unit="%", stale=st,
                        rule=f"gul ≥ {T['ig'][0]:g} · rød ≥ {T['ig'][1]:g}"))

    for sid, key, txt in (("STLFSI4", "stlfsi", "0 = normalt, over 0 = mere stress end normalt"),
                          ("NFCI", "nfci", "0 = gennemsnit, over 0 = strammere end normalt")):
        d, v, st = last(sid)
        if v is not None:
            out.append(_sig(key, names[sid], v, d, _lvl(v, T[key]), f"{_nf(v, 2)} ({txt})", stale=st,
                            rule=f"gul ≥ {T[key][0]:g} · rød ≥ {T[key][1]:g}"))

    obs = series.get("T10Y3M") or []
    if obs:
        d, v = obs[-1]
        st = _age(d, ref_day) > maxage["T10Y3M"]
        year = [x for dd, x in obs if dd >= (dt.date.fromisoformat(d) - dt.timedelta(days=365)).isoformat()]
        inverted_days = sum(1 for x in year if x < 0)
        recent = [x for dd, x in obs if dd >= (dt.date.fromisoformat(d) - dt.timedelta(days=90)).isoformat()]
        uninvert = v > 0 and inverted_days >= 60 and any(x < 0 for x in recent)
        lv = "yellow" if (v < 0 or uninvert) else "green"
        note = ("Rentekurven er vendt (korte renter over lange) – historisk et tidligt recessionsvarsel" if v < 0 else
                "Rentekurven retter sig op efter en lang inversion – historisk kommer recessioner ofte her" if uninvert else
                f"Normal rentekurve ({_nf(v, 2)} pp)")
        out.append(_sig("curve", names["T10Y3M"], v, d, lv, note, unit="pp", stale=st,
                        rule="gul: inverteret, eller retter sig op efter ≥ 60 dages inversion"))

    d, v, st = last("SAHMREALTIME")
    if v is not None:
        out.append(_sig("sahm", names["SAHMREALTIME"], v, d, _lvl(v, T["sahm"]),
                        f"{_nf(v, 2)} pp (0,5 eller mere har før hver recession siden 1970 betydet, at den var i gang)",
                        unit="pp", stale=st, rule=f"gul ≥ {T['sahm'][0]:g} · rød ≥ {T['sahm'][1]:g}"))

    obs = series.get("ICSA") or []
    if len(obs) >= 8:
        d = obs[-1][0]
        st = _age(d, ref_day) > maxage["ICSA"]
        avg4 = sum(v for _, v in obs[-4:]) / 4
        year = [v for dd, v in obs if dd >= (dt.date.fromisoformat(d) - dt.timedelta(days=365)).isoformat()]
        low = min(year) if year else None
        rise = _pct(avg4, low)
        out.append(_sig("claims", names["ICSA"], avg4, d, _lvl(rise, T["claims"]),
                        f"4-ugers gennemsnit {_nf(avg4 / 1000, 0)}.000 – {_nf(rise, 0)} % over årets laveste uge",
                        stale=st, rule=f"gul ≥ +{T['claims'][0]:g} % · rød ≥ +{T['claims'][1]:g} % over årets laveste"))

    obs = series.get("DCOILBRENTEU") or []
    if obs:
        d, v = obs[-1]
        st = _age(d, ref_day) > maxage["DCOILBRENTEU"]
        m = _pct(v, _back(obs, 30))
        lv = _lvl(m, T["oil_up"]) if m is not None and m >= 0 else ("yellow" if m is not None and m <= T["oil_down"] else "green")
        shock = m is not None and m >= T["oil_up"][1]
        out.append(_sig("oil", names["DCOILBRENTEU"], v, d, lv,
                        f"{_nf(v, 1)} USD pr. tønde, {'+' if (m or 0) >= 0 else ''}{_nf(m, 0)} % på en måned"
                        + (" (oliechok)" if shock else " (kraftigt fald – svag efterspørgsel?)" if lv == "yellow" and (m or 0) < 0 else ""),
                        unit="USD", shock=shock, stale=st,
                        rule=f"gul ≥ +{T['oil_up'][0]:g} % eller ≤ {T['oil_down']:g} % · rød ≥ +{T['oil_up'][1]:g} % på en måned"))

    obs = series.get("DTWEXBGS") or []
    if obs:
        d, v = obs[-1]
        st = _age(d, ref_day) > maxage["DTWEXBGS"]
        m = _pct(v, _back(obs, 30))
        out.append(_sig("usd", names["DTWEXBGS"], v, d, "yellow" if m is not None and m >= T["usd_up"] else "green",
                        f"{'+' if (m or 0) >= 0 else ''}{_nf(m, 1)} % på en måned"
                        + (" – flugt til dollar er et klassisk stresstegn" if m is not None and m >= T["usd_up"] else ""),
                        stale=st, rule=f"gul ≥ +{T['usd_up']:g} % på en måned"))
    return out


def eod_signals(rows: dict[str, list[dict]], ref_day: str) -> list[dict]:
    out: list[dict] = []
    for sym, name, kind in EOD_SERIES:
        data = [(str(r.get("date"))[:10], _f(r.get("adjusted_close") if r.get("adjusted_close") is not None else r.get("close")))
                for r in rows.get(sym) or []]
        data = [(d, v) for d, v in data if v]
        if len(data) < 6:
            continue
        d, v = data[-1]
        st = _age(d, ref_day) > 7
        prev = data[-2][1]
        day = _pct(v, prev)
        wk = _pct(v, data[-6][1])
        m = _pct(v, _back(data, 30))
        if kind == "index":
            year = [x for dd, x in data if dd >= (dt.date.fromisoformat(d) - dt.timedelta(days=365)).isoformat()]
            top = max(year)
            dd_ = (1 - v / top) * 100
            lv = _lvl(dd_, T["dd"])
            shock = (day is not None and day <= T["day_shock"]) or (wk is not None and wk <= T["week_shock"])
            if shock:
                lv = "red"
            note = (f"{_nf(dd_, 1)} % under toppen det seneste år · i går {'+' if (day or 0) >= 0 else ''}{_nf(day, 1)} % · "
                    f"5 dage {'+' if (wk or 0) >= 0 else ''}{_nf(wk, 1)} %"
                    + (" (dagschok)" if day is not None and day <= T["day_shock"] else
                       " (ugechok)" if wk is not None and wk <= T["week_shock"] else ""))
            out.append(_sig("dd_" + sym.split(".")[0].lower(), name, v, d, lv, note, shock=shock, stale=st,
                            rule=f"gul ≥ {T['dd'][0]:g} % under top · rød ≥ {T['dd'][1]:g} % · chok ≤ {T['day_shock']:g} % på en dag "
                                 f"eller ≤ {T['week_shock']:g} % på 5 dage"))
        elif kind == "vix":
            continue  # FRED's VIX er hovedkilden; EODHD's bruges kun, hvis FRED mangler (se build)
        elif kind == "fx":
            lv = "yellow" if m is not None and abs(m) >= T["sek_move"] else "green"
            out.append(_sig("usdsek", name, v, d, lv,
                            f"1 USD = {_nf(v, 2)} SEK, {'+' if (m or 0) >= 0 else ''}{_nf(m, 1)} % på en måned"
                            + (" – stor bevægelse: amerikanske papirer svinger ekstra i kroner" if lv == "yellow" else ""),
                            unit="SEK", stale=st, rule=f"gul ved ± {T['sek_move']:g} % på en måned"))
        elif kind == "gold":
            lv = "yellow" if m is not None and m >= T["gold_up"] else "green"
            out.append(_sig("gold", name, v, d, lv,
                            f"{_nf(v, 0)} USD pr. ounce, {'+' if (m or 0) >= 0 else ''}{_nf(m, 1)} % på en måned"
                            + (" – investorer søger sikkerhed" if lv == "yellow" else ""),
                            unit="USD", stale=st, rule=f"gul ≥ +{T['gold_up']:g} % på en måned"))
    return out


def vix_from_eod(rows: dict[str, list[dict]], ref_day: str) -> list[dict]:
    """Reserve: VIX fra EODHD, når FRED ikke gav en."""
    data = [(str(r.get("date"))[:10], _f(r.get("close"))) for r in rows.get("VIX.INDX") or []]
    data = [(d, v) for d, v in data if v]
    if not data:
        return []
    return [s for s in fred_signals({"VIXCLS": data}, ref_day) if s["id"] == "vix"]


def buffett_signal(b: dict | None) -> dict | None:
    if not b or b.get("value") is None:
        return None
    pct = b.get("percentile")
    lv = "yellow" if pct is not None and pct >= T["buffett_pct"] else "green"
    return _sig("buffett", "Buffett-indikator (aktier i forhold til økonomien)", b["value"], b.get("date"), lv,
                f"{_nf(b['value'], 0)} % af BNP ({b.get('quarter')}), højere end {pct} % af kvartalerne de sidste "
                f"{b.get('years')} år. Ikke et salgssignal – men jo højere, jo hårdere kan et fald blive", unit="%",
                rule=f"gul ≥ {T['buffett_pct']}. percentil (kun baggrund, aldrig rød alene)")


# ---------------------------------------------------------------------------
PLAN = {
    "green": ("Ingen tegn på makrostress. Følg planen: halvdelen af indskuddet til kernen, halvdelen til reserven. "
              "Reserven bruges kun, når en zone udløser en tranche."),
    "yellow": ("Hold øje. Tjek Zoner: ligger de liggende ordrer klar (eller kursalarmerne, hvor Nordnet ikke tager "
               "ordren endnu)? Køb kun det, en udløst tranche siger – aldrig mere."),
    "red": ("Alvorlig stress. Følg trancherne og intet andet: én tranche ad gangen, aldrig hele reserven på én dag. "
            "Sælg ikke i panik – kun aktier med et ramt kill-kriterie sælges."),
    "swan": ("Sort svane-varsel: noget usædvanligt sker hurtigt. Stop op i dag: læs Log og regler, og handl kun "
             "efter de trancher, zonerne udløser. Alt andet venter 24 timer, og sælg ikke i panik. Bunden kommer "
             "sjældent ved første signal."),
}


def build(series: dict[str, list[tuple[str, float]]], eod_rows: dict[str, list[dict]], buffett: dict | None,
          ref_day: str, *, generated_at: str | None = None, sources_ok: dict | None = None) -> dict:
    sig = fred_signals(series, ref_day)
    if not any(s["id"] == "vix" for s in sig):
        sig += vix_from_eod(eod_rows, ref_day)
    sig += eod_signals(eod_rows, ref_day)
    b = buffett_signal(buffett)
    if b:
        sig.append(b)
    sig += tm.fred_extra_signals(series, ref_day) + tm.eod_extra_signals(eod_rows, ref_day)
    for s_ in sig:
        s_.setdefault("theme", tm.theme_of(s_["id"]))
    live = [s for s in sig if not s["stale"]]
    reds = [s for s in live if s["level"] == "red" and s["id"] not in ("buffett", "int_rev")]
    yellows = [s for s in live if s["level"] == "yellow"]
    shocks = [s for s in live if s["shock"]]
    # Sort svane = et chok, eller røde signaler i mindst tre forskellige temaer på én gang. Flere røde i samme tema
    # (fx olie både i niveau og år til år) er ét problem, ikke tre.
    red_themes = sorted({s["theme"] for s in reds})
    swan = bool(shocks) or len(red_themes) >= 3
    level = "red" if reds else "yellow" if yellows else "green"
    if swan:
        level = "red"
    # Høj værdiansættelse + rødt = hårdere fald muligt: nævnes i overskriften
    rich = bool(b and b["level"] == "yellow")
    if swan:
        head = "Sort svane-varsel: " + ", ".join(s["label"] for s in (shocks or reds)[:3])
    elif level == "red":
        tl = {t[0]: t[1] for t in tm.THEMES}
        head = "Rød: alvorlig stress i " + ", ".join(
            f"{tl.get(t, t).lower()} ({next(s['label'] for s in reds if s['theme'] == t)})" for t in red_themes[:3])
    elif level == "yellow":
        ys = [s for s in yellows if s["id"] != "buffett"] or yellows  # Buffett nævnes i halen, ikke to gange
        head = "Gul: tidlige tegn i " + ", ".join(s["label"] for s in ys[:3]) + (" m.fl." if len(ys) > 3 else "")
    else:
        head = "Grøn: ingen tegn på makrostress"
    if rich and level != "green" and not (level == "yellow" and all(s["id"] == "buffett" for s in yellows)):
        head += " – og aktier er dyre (Buffett-indikatoren)"
    th = tm.themes(sig)
    return {"schema": 2, "profile": PROFILE, "ref_date": ref_day, "generated_at": generated_at,
            "level": level, "black_swan": swan, "headline": head, "red_themes": red_themes,
            "themes": th, "scenarios": tm.scenarios(sig), "complacency": tm.complacency(sig, th),
            "plan": PLAN["swan" if swan else level],
            "counts": {"red": len(reds), "yellow": len(yellows), "shocks": len(shocks), "signals": len(live),
                       "stale": len(sig) - len(live)},
            "signals": sorted(sig, key=lambda s: (-LEVELS.index(s["level"]), not s["shock"], s["id"] == "buffett", s["label"])),
            "sources": sources_ok or {},
            "note": "Kun markedsdata (FRED og EODHD). Sorte svaner kan ikke forudsiges – vagthunden opdager stressen "
                    "tidligt. Analyse, ikke rådgivning."}


def collect(ref_day: str, fred_client: Any = None, eod_client: Any = None, buffett: dict | None = None,
            *, days: int = 430) -> dict:
    """Henter data og bygger makrovagten. Fejler aldrig: manglende kilder giver færre signaler."""
    start = (dt.date.fromisoformat(ref_day) - dt.timedelta(days=days)).isoformat()
    series: dict[str, list[tuple[str, float]]] = {}
    ok = {"fred": None, "eodhd": None}
    if fred_client is not None and getattr(fred_client, "available", False):
        got = 0
        for sid in dict.fromkeys([x[0] for x in FRED_SERIES] + [x[0] for x in tm.FRED_EXTRA]):
            try:
                series[sid] = fred_client.observations(sid, start, ref_day)
                got += bool(series[sid])
            except Exception:  # noqa: BLE001 - én serie må aldrig stoppe vagten
                series[sid] = []
            if getattr(fred_client, "dead", False):
                break
        ok["fred"] = f"{got} af {len(set([x[0] for x in FRED_SERIES] + [x[0] for x in tm.FRED_EXTRA]))} serier"
    rows: dict[str, list[dict]] = {}
    if eod_client is not None and getattr(eod_client, "available", False):
        got = 0
        for sym in dict.fromkeys([x[0] for x in EOD_SERIES] + [x[0] for x in tm.EOD_EXTRA]):
            try:
                rows[sym] = eod_client.eod(sym, start, ref_day)
                got += bool(rows[sym])
            except Exception:  # noqa: BLE001
                rows[sym] = []
        ok["eodhd"] = f"{got} af {len(EOD_SERIES) + len(tm.EOD_EXTRA)} serier"
    return build(series, rows, buffett, ref_day, sources_ok=ok)


def status_line(m: dict | None) -> str:
    if not m:
        return "Makrovagt: ikke kørt"
    c = m.get("counts", {})
    return (f"Makrovagt: {DA.get(m.get('level'), m.get('level'))}{' · SORT SVANE-VARSEL' if m.get('black_swan') else ''} · "
            f"{c.get('red', 0)} røde, {c.get('yellow', 0)} gule, {c.get('shocks', 0)} chok af {c.get('signals', 0)} signaler")


def agent_brief(m: dict | None) -> str | None:
    if not m:
        return None
    lines = [f"**Makrovagt ({DA.get(m['level'])}{', sort svane-varsel' if m.get('black_swan') else ''}):** {m['headline']}."]
    for sc in m.get("scenarios", []):
        if sc.get("score") is not None:
            lines.append(f"- Scenarie {sc['label']}: {sc['score']} % af kendetegnene til stede")
    for s in m.get("signals", []):
        if s["level"] != "green" and not s["stale"]:
            lines.append(f"- {DA[s['level']]}{' · chok' if s['shock'] else ''}: {s['label']} – {s['note']} ({_dmy(s['date'])})")
    return "\n".join(lines)
