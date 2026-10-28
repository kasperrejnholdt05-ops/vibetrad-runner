"""Risikotemaer og udfaldsbarometer: vagthunden bygget til de omstændigheder, vi står i (oktober 2026).

Makrovagten ser på frygt, kredit, stress, job og aktiefald. Dette modul tilføjer de risici, der præger netop nu,
og samler alle signaler i ti temaer og seks scenarier, så man kan se, hvilken vej det trækker:

- **Renter og inflation**: 10-årig rente over 5 %, 30-årig over 5,3 %, realrente omkring 2,7 %, og markedet
  prisfastsætter renteforhøjelser (2-årig rente over Fed-loftet). Inflationsforventninger og CPI.
- **Statsgæld**: rentebetalinger på ca. 1.000 mia. USD (ca. 19 % af indtægterne), terminspræmie, og et "tab af
  tillid"-mønster: lange renter op, mens dollaren falder.
- **Kredit**: high yield-spændet er lavt (ca. 3,3 %) trods alt det andet; CCC- og BBB-spænd, bankernes
  lånestandarder, private credit (BDC'er) og neocloud-kredit (CoreWeave).
- **Likviditet**: SOFR over IORB (pengemarkedet strammer), reverse repo og bankreserver, stressindeks.
- **AI og tech-priser**: halvledere og NVIDIA mod 200-dages-linjen, bredden (lige vægtet mod markedsvægtet),
  Nasdaqs momentum mod S&P 500 og Buffett-indikatoren.
- **Energi**: Brent omkring 120 USD efter Hormuz-krisen (+80 % på et år), naturgas.
- **Chips og forsyning**: halvlederpriser (PPI), Taiwan (TSMC og den taiwanske dollar).
- **Geopolitik og politisk uro**: politisk usikkerhed (EPU-indekset), guld, dollaren.
- **Økonomi og job**: Sahm-reglen, nye ledige, rentekurven.
- **Aktiemarkederne**: VIX, fald fra top og dagschok.

Grænserne er sat, så de skelner i det nuværende regime (flere står allerede gult) – ikke så de altid er grønne.
Scenarierne tæller, hvor stor en del af hvert scenaries kendetegn der er til stede i dag. Det er en tælling, ikke
en sandsynlighed. "Selvtilfredshed" markerer, at aktier og kredit ikke prissætter risici, som temaerne viser.
"""
from __future__ import annotations

import datetime as dt
# (FRED-id, dansk navn, maks. alder i dage)
FRED_EXTRA: tuple[tuple[str, str, int], ...] = (
    ("DGS10", "10-årig statsrente (USA)", 7),
    ("DGS30", "30-årig statsrente (USA)", 7),
    ("DGS2", "2-årig statsrente (USA)", 7),
    ("DFII10", "10-årig realrente (TIPS)", 7),
    ("DFEDTARU", "Fed-rentens øvre grænse", 10),
    ("T10YIE", "Inflationsforventning 10 år", 7),
    ("CPIAUCSL", "Forbrugerpriser (CPI)", 75),
    ("THREEFYTP10", "Terminspræmie 10 år", 21),
    ("A091RC1Q027SBEA", "Statens rentebetalinger", 300),
    ("W006RC1Q027SBEA", "Statens indtægter", 300),
    ("BAMLH0A3HYC", "Kreditspænd CCC (de svageste lån)", 7),
    ("BAMLC0A4CBBB", "Kreditspænd BBB (laveste investment grade)", 7),
    ("DRTSCILM", "Bankernes lånestandarder (strammere = højere)", 200),
    ("SOFR", "SOFR (pengemarkedsrente)", 7),
    ("IORB", "Rente på bankreserver (IORB)", 10),
    ("RRPONTSYD", "Reverse repo hos Fed", 10),
    ("DHHNGSP", "Naturgas (Henry Hub)", 10),
    ("PCU33443344", "Halvlederpriser (PPI)", 75),
    ("USEPUINDXD", "Politisk usikkerhed (EPU, dagligt)", 10),
)
# (EODHD-symbol, dansk navn)
EOD_EXTRA: tuple[tuple[str, str], ...] = (
    ("SOXX.US", "Halvledere (SOXX)"),
    ("NVDA.US", "NVIDIA"),
    ("CRWV.US", "CoreWeave (neocloud-kredit)"),
    ("BIZD.US", "Private credit (BDC-indeks)"),
    ("RSP.US", "S&P 500 lige vægtet"),
    ("SPY.US", "S&P 500"),
    ("TSM.US", "TSMC (Taiwan)"),
    ("USDTWD.FOREX", "USD/TWD"),
)

T = {
    "dgs10": (4.75, 5.25), "dgs10_jump21": 0.40,
    "dgs30": (5.0, 5.5),
    "real10": (2.25, 2.75),
    "hikes": (0.5, 1.0),            # 2-årig rente minus Fed-loftet, pp
    "bei10": (2.6, 3.0),
    "cpi_yoy": (3.5, 5.0),
    "tp10": (0.75, 1.25),
    "int_rev": (15.0, 20.0),        # rentebetalinger i % af indtægterne
    "ccc": (9.0, 12.0), "ccc_widen30": 1.5,
    "bbb": (1.6, 2.2),
    "lending": (20.0, 40.0),
    "sofr_iorb": (0.0, 10.0),       # basispoint
    "rrp_low": 50.0,                # mia. USD
    "natgas": (5.0, 7.0), "natgas_m": 40.0,
    "brent": (95.0, 115.0), "brent_yoy": (40.0, 75.0),
    "semi_ppi_yoy": (8.0, 15.0),
    "epu": (200.0, 350.0),
    "gold_yoy": (30.0, 50.0),
    "ai_dd": (15.0, 30.0),          # fald fra 52-ugers top for SOXX/NVDA
    "crwv_dd": (40.0, 60.0),
    "bizd_dd": (10.0, 20.0),
    "breadth3m": (-5.0, -10.0),     # RSP/SPY på 3 måneder, %
    "ndx_rel1m": -5.0,
    "twd_m": 3.0,
}

THEMES: tuple[tuple[str, str, str], ...] = (
    ("renter", "Renter og inflation", "Strammer renterne og inflationen grebet om økonomien?"),
    ("gaeld", "Statsgæld", "Mister investorerne tilliden til USA's gæld?"),
    ("kredit", "Kredit", "Begynder långiverne at trække sig?"),
    ("likviditet", "Likviditet og stress", "Strammer pengemarkedet til?"),
    ("ai", "AI og tech-priser", "Knækker AI-optimismen i aktiekurserne?"),
    ("energi", "Energi", "Presser olie og gas økonomien?"),
    ("chips", "Chips og forsyning", "Bliver mangel på chips og strøm en bremse?"),
    ("geo", "Geopolitik og politisk uro", "Stiger den politiske og geopolitiske uro?"),
    ("job", "Økonomi og job", "Er en recession på vej?"),
    ("marked", "Aktiemarkederne", "Er der allerede panik i aktierne?"),
)
THEME_OF = {
    "vix": "marked", "hy": "kredit", "ig": "kredit", "stlfsi": "likviditet", "nfci": "likviditet", "curve": "job",
    "claims": "job", "sahm": "job", "oil": "energi", "usd": "geo", "usdsek": "geo", "gold": "geo",
    "buffett": "ai",
}
LEVELS = ("green", "yellow", "red")
DA = {"green": "grøn", "yellow": "gul", "red": "rød"}


def theme_of(sid: str) -> str:
    if sid.startswith("dd_"):
        return "marked"
    return THEME_OF.get(sid) or SIGNAL_THEME.get(sid, "marked")


# ---------------------------------------------------------------------------
def _nf(v: float | None, d: int = 1) -> str:
    if v is None:
        return "–"
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _age(date: str | None, ref_day: str) -> int:
    try:
        return (dt.date.fromisoformat(ref_day) - dt.date.fromisoformat(str(date)[:10])).days
    except (TypeError, ValueError):
        return 10 ** 6


def _back(obs: list[tuple[str, float]], days: int) -> float | None:
    if not obs:
        return None
    cut = (dt.date.fromisoformat(obs[-1][0]) - dt.timedelta(days=days)).isoformat()
    before = [v for d, v in obs if d <= cut]
    return before[-1] if before else None


def _pct(a: float | None, b: float | None) -> float | None:
    return None if a is None or not b else (a / b - 1) * 100


def _lvl(v: float | None, lim: tuple[float, float]) -> str:
    if v is None:
        return "green"
    return "red" if v >= lim[1] else "yellow" if v >= lim[0] else "green"


def _sig(sid: str, label: str, value: float | None, date: str | None, level: str, note: str, *, unit: str = "",
         shock: bool = False, stale: bool = False, rule: str = "") -> dict:
    return {"id": sid, "label": label, "value": None if value is None else round(value, 3), "unit": unit,
            "date": date, "level": level if not stale else "green", "shock": bool(shock and not stale), "stale": stale,
            "note": note + (" (data er forældet – tæller ikke med)" if stale else ""), "rule": rule,
            "theme": SIGNAL_THEME.get(sid, "marked")}


def _closes(rows: list[dict] | None) -> list[tuple[str, float]]:
    out = []
    for r in rows or []:
        try:
            v = float(r.get("adjusted_close") if r.get("adjusted_close") is not None else r.get("close"))
        except (TypeError, ValueError):
            continue
        if v > 0:
            out.append((str(r.get("date"))[:10], v))
    return sorted(out)


SIGNAL_THEME = {
    "dgs10": "renter", "dgs30": "renter", "real10": "renter", "hikes": "renter", "bei10": "renter", "cpi": "renter",
    "tp10": "gaeld", "int_rev": "gaeld", "vigilante": "gaeld",
    "ccc": "kredit", "bbb": "kredit", "lending": "kredit", "bizd": "kredit",
    "sofr_iorb": "likviditet", "rrp": "likviditet",
    "soxx": "ai", "nvda": "ai", "crwv": "ai", "breadth": "ai", "ndx_rel": "ai",
    "brent_lvl": "energi", "brent_yoy": "energi", "natgas": "energi",
    "semi_ppi": "chips", "tsm": "chips", "twd": "chips",
    "epu": "geo", "gold_yoy": "geo",
}


# ---------------------------------------------------------------------------
def fred_extra_signals(s: dict[str, list[tuple[str, float]]], ref_day: str) -> list[dict]:
    out: list[dict] = []
    age = {sid: a for sid, _, a in FRED_EXTRA}

    def last(sid: str, max_age: int | None = None) -> tuple[str | None, float | None, bool]:
        obs = s.get(sid) or []
        if not obs:
            return None, None, True
        d, v = obs[-1]
        return d, v, _age(d, ref_day) > (max_age if max_age is not None else age.get(sid, 10))

    d, v, st = last("DGS10")
    if v is not None:
        b = _back(s["DGS10"], 30)
        jump = None if b is None else v - b
        shock = jump is not None and jump >= T["dgs10_jump21"]
        lv = "red" if shock else _lvl(v, T["dgs10"])
        out.append(_sig("dgs10", "10-årig statsrente (USA)", v, d, lv,
                        f"{_nf(v, 2)} %" + (f", {'+' if jump >= 0 else ''}{_nf(jump, 2)} pp på en måned" if jump is not None else "")
                        + (" (rentechok)" if shock else "") + ". Over 5 % presser det boligmarked, selskabsgæld og aktiernes værdi",
                        unit="%", shock=shock, stale=st,
                        rule=f"gul ≥ {T['dgs10'][0]:g} % · rød ≥ {T['dgs10'][1]:g} % · chok +{T['dgs10_jump21']:g} pp på en måned"))
    d, v, st = last("DGS30")
    if v is not None:
        out.append(_sig("dgs30", "30-årig statsrente (USA)", v, d, _lvl(v, T["dgs30"]),
                        f"{_nf(v, 2)} % – de lange renter viser tilliden til statsgælden", unit="%", stale=st,
                        rule=f"gul ≥ {T['dgs30'][0]:g} % · rød ≥ {T['dgs30'][1]:g} %"))
    d, v, st = last("DFII10")
    if v is not None:
        out.append(_sig("real10", "10-årig realrente (TIPS)", v, d, _lvl(v, T["real10"]),
                        f"{_nf(v, 2)} % efter inflation – det højeste i årtier gør dyre vækstaktier sårbare", unit="%",
                        stale=st, rule=f"gul ≥ {T['real10'][0]:g} % · rød ≥ {T['real10'][1]:g} %"))
    d2, v2, st2 = last("DGS2")
    du, vu, stu = last("DFEDTARU")
    if v2 is not None and vu is not None:
        gap = v2 - vu
        out.append(_sig("hikes", "Markedet venter renteforhøjelser", gap, d2, _lvl(gap, T["hikes"]),
                        f"2-årig rente {_nf(v2, 2)} % mod Fed-loftet {_nf(vu, 2)} %: {'+' if gap >= 0 else ''}{_nf(gap, 2)} pp"
                        + (" – markedet prissætter flere renteforhøjelser" if gap >= T["hikes"][0] else ""),
                        unit="pp", stale=st2 or stu, rule=f"gul ≥ +{T['hikes'][0]:g} pp · rød ≥ +{T['hikes'][1]:g} pp over Fed-loftet"))
    d, v, st = last("T10YIE")
    if v is not None:
        out.append(_sig("bei10", "Inflationsforventning 10 år", v, d, _lvl(v, T["bei10"]),
                        f"{_nf(v, 2)} % om året – over 2,6 % er inflationen ved at slippe løs", unit="%", stale=st,
                        rule=f"gul ≥ {T['bei10'][0]:g} % · rød ≥ {T['bei10'][1]:g} %"))
    obs = s.get("CPIAUCSL") or []
    if len(obs) >= 13:
        d, v = obs[-1]
        yoy = _pct(v, obs[-13][1])
        out.append(_sig("cpi", "Forbrugerpriser (CPI), år til år", yoy, d, _lvl(yoy, T["cpi_yoy"]),
                        f"{_nf(yoy, 1)} % på et år", unit="%", stale=_age(d, ref_day) > age["CPIAUCSL"],
                        rule=f"gul ≥ {T['cpi_yoy'][0]:g} % · rød ≥ {T['cpi_yoy'][1]:g} %"))
    d, v, st = last("THREEFYTP10")
    if v is not None:
        out.append(_sig("tp10", "Terminspræmie 10 år", v, d, _lvl(v, T["tp10"]),
                        f"{_nf(v, 2)} pp ekstra betaling for at eje lange statsobligationer – stiger, når tilliden til gælden falder",
                        unit="pp", stale=st, rule=f"gul ≥ {T['tp10'][0]:g} · rød ≥ {T['tp10'][1]:g}"))
    di, vi, sti = last("A091RC1Q027SBEA")
    dr, vr, strr = last("W006RC1Q027SBEA")
    if vi is not None and vr:
        share = vi / vr * 100
        out.append(_sig("int_rev", "Rentebetalinger i % af statens indtægter", share, di, _lvl(share, T["int_rev"]),
                        f"{_nf(share, 1)} % af indtægterne går til renter ({_nf(vi, 0)} mia. USD om året) – en snebold, der vokser med renten",
                        unit="%", stale=sti or strr, rule=f"gul ≥ {T['int_rev'][0]:g} % · rød ≥ {T['int_rev'][1]:g} % (langsom: baggrund)"))
    d, v, st = last("BAMLH0A3HYC")
    if v is not None:
        b = _back(s["BAMLH0A3HYC"], 30)
        widen = None if b is None else v - b
        shock = widen is not None and widen >= T["ccc_widen30"]
        out.append(_sig("ccc", "Kreditspænd CCC (de svageste lån)", v, d, "red" if shock else _lvl(v, T["ccc"]),
                        f"{_nf(v, 2)} pp" + (f", {'+' if widen >= 0 else ''}{_nf(widen, 2)} pp på en måned" if widen is not None else "")
                        + (" (de svageste låntagere presses først)" if shock else ""), unit="%", shock=shock, stale=st,
                        rule=f"gul ≥ {T['ccc'][0]:g} · rød ≥ {T['ccc'][1]:g} · chok +{T['ccc_widen30']:g} pp på en måned"))
    d, v, st = last("BAMLC0A4CBBB")
    if v is not None:
        out.append(_sig("bbb", "Kreditspænd BBB", v, d, _lvl(v, T["bbb"]), f"{_nf(v, 2)} pp – den største og svageste del af investment grade",
                        unit="%", stale=st, rule=f"gul ≥ {T['bbb'][0]:g} · rød ≥ {T['bbb'][1]:g}"))
    d, v, st = last("DRTSCILM")
    if v is not None:
        out.append(_sig("lending", "Bankernes lånestandarder", v, d, _lvl(v, T["lending"]),
                        f"{_nf(v, 0)} % netto af bankerne strammer kravene til virksomhedslån (kvartalsvis)", unit="%",
                        stale=st, rule=f"gul ≥ {T['lending'][0]:g} · rød ≥ {T['lending'][1]:g}"))
    ds, vs, sts = last("SOFR")
    dr2, vr2, str2 = last("IORB")
    if vs is not None and vr2 is not None:
        bp = (vs - vr2) * 100
        out.append(_sig("sofr_iorb", "Pengemarkedet (SOFR mod IORB)", bp, ds,
                        "red" if bp >= T["sofr_iorb"][1] else "yellow" if bp > T["sofr_iorb"][0] else "green",
                        f"SOFR {_nf(vs, 2)} % mod IORB {_nf(vr2, 2)} %: {'+' if bp >= 0 else ''}{_nf(bp, 0)} bp"
                        + (" – der er knap så mange penge i systemet (som september 2019)" if bp > 0 else ""), unit="bp",
                        stale=sts or str2, rule=f"gul > {T['sofr_iorb'][0]:g} bp · rød ≥ {T['sofr_iorb'][1]:g} bp"))
    d, v, st = last("RRPONTSYD")
    if v is not None:
        out.append(_sig("rrp", "Reverse repo hos Fed (likviditetsbuffer)", v, d, "yellow" if v < T["rrp_low"] else "green",
                        f"{_nf(v, 0)} mia. USD" + (" – bufferen er næsten brugt op" if v < T["rrp_low"] else ""), stale=st,
                        rule=f"gul < {T['rrp_low']:g} mia. USD"))
    obs = s.get("DHHNGSP") or []
    if obs:
        d, v = obs[-1]
        m = _pct(v, _back(obs, 30))
        shock = m is not None and m >= T["natgas_m"]
        out.append(_sig("natgas", "Naturgas (Henry Hub)", v, d, "red" if shock else _lvl(v, T["natgas"]),
                        f"{_nf(v, 2)} USD/MMBtu" + (f", {'+' if (m or 0) >= 0 else ''}{_nf(m, 0)} % på en måned" if m is not None else "")
                        + " – gas er strøm til datacentrene", unit="USD", shock=shock, stale=_age(d, ref_day) > age["DHHNGSP"],
                        rule=f"gul ≥ {T['natgas'][0]:g} · rød ≥ {T['natgas'][1]:g} · chok +{T['natgas_m']:g} % på en måned"))
    obs = s.get("DCOILBRENTEU") or []
    if obs:
        d, v = obs[-1]
        st = _age(d, ref_day) > 10
        out.append(_sig("brent_lvl", "Olie (Brent), niveau", v, d, _lvl(v, T["brent"]),
                        f"{_nf(v, 1)} USD pr. tønde – over 115 har historisk bremset økonomien", unit="USD", stale=st,
                        rule=f"gul ≥ {T['brent'][0]:g} · rød ≥ {T['brent'][1]:g} USD"))
        y = _back(obs, 365)
        yoy = _pct(v, y)
        if yoy is not None:
            out.append(_sig("brent_yoy", "Olie (Brent), år til år", yoy, d, _lvl(yoy, T["brent_yoy"]),
                            f"{'+' if yoy >= 0 else ''}{_nf(yoy, 0)} % på et år – et oliechok af den størrelse har gået forud for de fleste recessioner",
                            unit="%", stale=st, rule=f"gul ≥ +{T['brent_yoy'][0]:g} % · rød ≥ +{T['brent_yoy'][1]:g} %"))
    obs = s.get("PCU33443344") or []
    if len(obs) >= 13:
        d, v = obs[-1]
        yoy = _pct(v, obs[-13][1])
        out.append(_sig("semi_ppi", "Halvlederpriser (PPI), år til år", yoy, d, _lvl(yoy, T["semi_ppi_yoy"]),
                        f"{'+' if (yoy or 0) >= 0 else ''}{_nf(yoy, 1)} % – stigende chippriser er tegn på mangel (hukommelse, HBM)",
                        unit="%", stale=_age(d, ref_day) > age["PCU33443344"], rule=f"gul ≥ +{T['semi_ppi_yoy'][0]:g} % · rød ≥ +{T['semi_ppi_yoy'][1]:g} %"))
    obs = s.get("USEPUINDXD") or []
    if len(obs) >= 7:
        d = obs[-1][0]
        avg7 = sum(v for _, v in obs[-7:]) / 7
        out.append(_sig("epu", "Politisk usikkerhed (EPU)", avg7, d, _lvl(avg7, T["epu"]),
                        f"7-dages gennemsnit {_nf(avg7, 0)} (normalt omkring 100) – måler politik, told, budget og krig i avisernes dækning",
                        stale=_age(d, ref_day) > age["USEPUINDXD"], rule=f"gul ≥ {T['epu'][0]:g} · rød ≥ {T['epu'][1]:g}"))
    # Tab af tillid: lange renter op, dollaren ned (som Storbritannien 2022 og USA april 2025)
    d10 = s.get("DGS10") or []
    usd = s.get("DTWEXBGS") or []
    if d10 and usd:
        r_now, r_b = d10[-1][1], _back(d10, 30)
        u_now, u_b = usd[-1][1], _back(usd, 30)
        if r_b is not None and u_b:
            dr_, du_ = r_now - r_b, _pct(u_now, u_b)
            hit = dr_ >= 0.25 and (du_ or 0) <= -1.5
            out.append(_sig("vigilante", "Renter op, dollar ned (tab af tillid)", dr_, d10[-1][0], "red" if hit else "green",
                            f"10-årig rente {'+' if dr_ >= 0 else ''}{_nf(dr_, 2)} pp og dollaren {'+' if (du_ or 0) >= 0 else ''}{_nf(du_, 1)} % på en måned"
                            + (" – investorerne sælger både obligationer og dollar" if hit else ""), shock=hit,
                            stale=_age(d10[-1][0], ref_day) > 10, rule="chok: rente +0,25 pp og dollar −1,5 % på en måned"))
    return out


def eod_extra_signals(rows: dict[str, list[dict]], ref_day: str) -> list[dict]:
    out: list[dict] = []

    def dd52(sym: str) -> tuple[str | None, float | None, float | None, bool, list[tuple[str, float]]]:
        c = _closes(rows.get(sym))
        if len(c) < 30:
            return None, None, None, True, c
        d, v = c[-1]
        year = [x for dd, x in c if dd >= (dt.date.fromisoformat(d) - dt.timedelta(days=365)).isoformat()]
        return d, v, (1 - v / max(year)) * 100, _age(d, ref_day) > 7, c

    def sma(c: list[tuple[str, float]], n: int, back: int = 0) -> float | None:
        vals = [v for _, v in c[: len(c) - back]] if back else [v for _, v in c]
        return sum(vals[-n:]) / n if len(vals) >= n else None

    for sym, sid, label in (("SOXX.US", "soxx", "Halvledere (SOXX)"), ("NVDA.US", "nvda", "NVIDIA")):
        d, v, dd, st, c = dd52(sym)
        if v is None:
            continue
        m200, m200b = sma(c, 200), sma(c, 200, 20)
        below = m200 is not None and v < m200
        falling = m200 is not None and m200b is not None and m200 < m200b
        lv = "red" if (below and falling) or (dd or 0) >= T["ai_dd"][1] else "yellow" if below or (dd or 0) >= T["ai_dd"][0] else "green"
        out.append(_sig(sid, label, dd, d, lv,
                        f"{_nf(dd, 1)} % under toppen det seneste år · " + (
                            f"{'under' if below else 'over'} 200-dages-linjen ({_nf((v / m200 - 1) * 100, 1)} %)" if m200 else "for kort historik")
                        + (", og linjen falder" if falling else ""), unit="%", stale=st,
                        rule=f"gul: under 200-dages-linjen eller ≥ {T['ai_dd'][0]:g} % fra top · rød: under en faldende linje eller ≥ {T['ai_dd'][1]:g} %"))
    d, v, dd, st, c = dd52("CRWV.US")
    if v is not None:
        out.append(_sig("crwv", "CoreWeave (neocloud-kredit)", dd, d, _lvl(dd, T["crwv_dd"]),
                        f"{_nf(dd, 0)} % under toppen – AI-datacentre bygget på lån; første sted, et kreditbrud i AI viser sig",
                        unit="%", stale=st, rule=f"gul ≥ {T['crwv_dd'][0]:g} % · rød ≥ {T['crwv_dd'][1]:g} % fra top"))
    d, v, dd, st, c = dd52("BIZD.US")
    if v is not None:
        out.append(_sig("bizd", "Private credit (BDC'er)", dd, d, _lvl(dd, T["bizd_dd"]),
                        f"{_nf(dd, 1)} % under toppen – långiverne uden for bankerne", unit="%", stale=st,
                        rule=f"gul ≥ {T['bizd_dd'][0]:g} % · rød ≥ {T['bizd_dd'][1]:g} % fra top"))
    rsp, spy = _closes(rows.get("RSP.US")), _closes(rows.get("SPY.US"))
    if len(rsp) > 70 and len(spy) > 70:
        sd = dict(spy)
        ratio = [(d, x / sd[d]) for d, x in rsp if d in sd]
        if len(ratio) > 64:
            ch = _pct(ratio[-1][1], ratio[-64][1])
            lv = "red" if ch is not None and ch <= T["breadth3m"][1] else "yellow" if ch is not None and ch <= T["breadth3m"][0] else "green"
            out.append(_sig("breadth", "Markedets bredde (lige vægt mod S&P 500)", ch, ratio[-1][0], lv,
                            f"{'+' if (ch or 0) >= 0 else ''}{_nf(ch, 1)} % på 3 måneder – negativt betyder, at få store aktier bærer markedet",
                            unit="%", stale=_age(ratio[-1][0], ref_day) > 7,
                            rule=f"gul ≤ {T['breadth3m'][0]:g} % · rød ≤ {T['breadth3m'][1]:g} % på 3 måneder"))
    ndx, spx = _closes(rows.get("NDX.INDX")), _closes(rows.get("GSPC.INDX"))
    if len(ndx) > 30 and len(spx) > 30:
        r_n = _pct(ndx[-1][1], _back(ndx, 30))
        r_s = _pct(spx[-1][1], _back(spx, 30))
        if r_n is not None and r_s is not None:
            rel = r_n - r_s
            out.append(_sig("ndx_rel", "Nasdaq 100 mod S&P 500 (1 måned)", rel, ndx[-1][0], "yellow" if rel <= T["ndx_rel1m"] else "green",
                            f"{'+' if rel >= 0 else ''}{_nf(rel, 1)} pp – tech, der taber terræn, er ofte det første tegn på, at AI-handlen vender",
                            unit="pp", stale=_age(ndx[-1][0], ref_day) > 7, rule=f"gul ≤ {T['ndx_rel1m']:g} pp på en måned"))
    d, v, dd, st, c = dd52("TSM.US")
    if v is not None:
        out.append(_sig("tsm", "TSMC (Taiwan)", dd, d, _lvl(dd, (20.0, 35.0)),
                        f"{_nf(dd, 1)} % under toppen – næsten alle avancerede AI-chips laves her", unit="%", stale=st,
                        rule="gul ≥ 20 % · rød ≥ 35 % fra top"))
    twd = _closes(rows.get("USDTWD.FOREX"))
    if len(twd) > 30:
        m = _pct(twd[-1][1], _back(twd, 30))
        out.append(_sig("twd", "Taiwanske dollar", m, twd[-1][0], "yellow" if m is not None and m >= T["twd_m"] else "green",
                        f"USD/TWD {'+' if (m or 0) >= 0 else ''}{_nf(m, 1)} % på en måned – kapitalflugt fra Taiwan viser sig her",
                        unit="%", stale=_age(twd[-1][0], ref_day) > 7, rule=f"gul ≥ +{T['twd_m']:g} % på en måned"))
    gold = _closes(rows.get("XAUUSD.FOREX"))
    if len(gold) > 250:
        yoy = _pct(gold[-1][1], _back(gold, 365))
        out.append(_sig("gold_yoy", "Guld, år til år", yoy, gold[-1][0], _lvl(yoy, T["gold_yoy"]),
                        f"{'+' if (yoy or 0) >= 0 else ''}{_nf(yoy, 0)} % på et år – centralbanker og investorer søger væk fra dollar og obligationer",
                        unit="%", stale=_age(gold[-1][0], ref_day) > 7, rule=f"gul ≥ +{T['gold_yoy'][0]:g} % · rød ≥ +{T['gold_yoy'][1]:g} %"))
    return out


# ---------------------------------------------------------------------------
# Scenarier: (id, navn, godt/dårligt, [(signal-id, betingelse)], forklaring)
def _is(level: str):
    return lambda s: LEVELS.index(s["level"]) >= LEVELS.index(level)


def _calm(s: dict) -> bool:
    return s["level"] == "green"


SCENARIOS: tuple[dict, ...] = (
    {"id": "blod", "label": "Blød landing", "good": True,
     "text": "Inflationen falder, renterne topper, kredit og job holder. Det gode udfald: korrektioner bliver korte. "
             "Tallet er, hvor mange af forudsætningerne der er opfyldt – alle skal holde.",
     "checks": [("brent_lvl", _calm), ("dgs10", _calm), ("hikes", _calm), ("bei10", _calm), ("cpi", _calm), ("hy", _calm),
                ("sahm", _calm), ("claims", _calm)]},
    {"id": "stagflation", "label": "Stagflation (olie og renter)", "good": False,
     "text": "Dyr energi holder inflationen oppe, centralbankerne hæver renten ind i en svækket økonomi. Rammer alle aktier, mest de dyre.",
     "checks": [("brent_lvl", _is("yellow")), ("brent_yoy", _is("yellow")), ("hikes", _is("yellow")), ("real10", _is("yellow")),
                ("bei10", _is("yellow")), ("cpi", _is("yellow")), ("natgas", _is("yellow"))]},
    {"id": "gaeld", "label": "Rente- og gældschok", "good": False,
     "text": "Investorerne kræver mere for at låne USA penge: lange renter stiger, dollaren svækkes. Kan udløse et hurtigt fald i aktier og obligationer samtidig.",
     "checks": [("dgs10", _is("yellow")), ("dgs30", _is("yellow")), ("tp10", _is("yellow")), ("int_rev", _is("yellow")),
                ("vigilante", _is("red")), ("gold_yoy", _is("yellow"))]},
    {"id": "ai", "label": "AI-brud", "good": False,
     "text": "AI-indtjeningen kan ikke bære priserne eller gælden bag datacentrene. Tech og halvledere falder mest; Nasdaq kan miste 30–50 %.",
     "checks": [("soxx", _is("yellow")), ("nvda", _is("yellow")), ("crwv", _is("yellow")), ("breadth", _is("yellow")),
                ("ndx_rel", _is("yellow")), ("dd_ndx", _is("yellow")), ("buffett", _is("yellow"))]},
    {"id": "kredit", "label": "Kreditkrise og recession", "good": False,
     "text": "Långiverne trækker sig, virksomheder går konkurs, ledigheden stiger. Det klassiske bjørnemarked med fald på 30–50 %.",
     "checks": [("hy", _is("yellow")), ("ccc", _is("yellow")), ("bbb", _is("yellow")), ("lending", _is("yellow")), ("bizd", _is("yellow")),
                ("sahm", _is("yellow")), ("claims", _is("yellow")), ("sofr_iorb", _is("red")), ("stlfsi", _is("yellow"))]},
    {"id": "geo", "label": "Geopolitisk chok", "good": False,
     "text": "Krig og politik slår direkte igennem: oliepris, guld og usikkerhed eksploderer. Ofte et hurtigt fald, der vender, hvis økonomien holder.",
     "checks": [("oil", _is("yellow")), ("gold", _is("yellow")), ("epu", _is("yellow")), ("vix", _is("yellow")), ("twd", _is("yellow")),
                ("tsm", _is("yellow")), ("usd", _is("yellow"))]},
)


def scenarios(signals: list[dict]) -> list[dict]:
    by = {s["id"]: s for s in signals if not s.get("stale")}
    out = []
    for sc in SCENARIOS:
        hits, missing, have = [], [], 0
        for sid, cond in sc["checks"]:
            s = by.get(sid)
            if not s:
                continue
            have += 1
            (hits if cond(s) else missing).append(s["label"])
        score = round(100 * len(hits) / have) if have else None
        out.append({"id": sc["id"], "label": sc["label"], "good": sc["good"], "text": sc["text"], "score": score,
                    "hits": hits, "missing": missing, "measured": have, "total": len(sc["checks"])})
    good = [x for x in out if x["good"]]
    bad = sorted([x for x in out if not x["good"]], key=lambda x: -(x["score"] or 0))
    return good + bad


def themes(signals: list[dict]) -> list[dict]:
    out = []
    for tid, label, q in THEMES:
        sig = [s for s in signals if s.get("theme") == tid and not s.get("stale")]
        lv = max([s["level"] for s in sig] or ["green"], key=LEVELS.index)
        bad = [s for s in sig if s["level"] != "green"]
        out.append({"id": tid, "label": label, "question": q, "level": lv, "shock": any(s["shock"] for s in sig),
                    "signals": len(sig), "red": sum(s["level"] == "red" for s in sig), "yellow": sum(s["level"] == "yellow" for s in sig),
                    "top": [s["label"] for s in sorted(bad, key=lambda s: -LEVELS.index(s["level"]))[:3]]})
    return out


def complacency(signals: list[dict], th: list[dict]) -> dict:
    """Selvtilfredshed: risikotemaerne er røde, men aktier og kredit prissætter det ikke."""
    by = {s["id"]: s for s in signals if not s.get("stale")}
    hot = [t for t in th if t["level"] == "red" and t["id"] not in ("marked",)]
    spx = by.get("dd_gspc")
    hy = by.get("hy")
    vix = by.get("vix")
    calm_mkt = spx is not None and spx["level"] == "green" and (hy is None or hy["level"] == "green") and (vix is None or vix["level"] == "green")
    flag = bool(hot) and calm_mkt
    return {"flag": flag, "themes": [t["label"] for t in hot],
            "text": ("Markedet er roligt, selv om " + ", ".join(t["label"].lower() for t in hot[:3]) + " står rødt. "
                     "Aktier tæt på toppen og lave kreditspænd betyder, at et chok ikke er prissat – faldet kan komme hurtigt. "
                     "For planen: hold reserven klar, og jag ikke stigninger.") if flag else ""}
