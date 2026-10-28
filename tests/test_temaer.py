"""Risikotemaer og scenarier: regimet i oktober 2026, tab af tillid, sort svane på tværs af temaer, AI-signaler."""
import datetime as dt

import makrovagt as mv
import temaer as tm

REF = "2026-10-26"


def days(n, end=REF):
    e = dt.date.fromisoformat(end)
    return [(e - dt.timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def ramp(a, b, n=430):
    return [(d, a + (b - a) * i / (n - 1)) for i, d in enumerate(days(n))]


def flat(v, n=430):
    return [(d, v) for d in days(n)]


def monthly(start, step, n=14):
    e = dt.date.fromisoformat(REF)
    out = []
    for i in range(n):
        m = (e.month - (n - i)) % 12 + 1
        y = e.year + ((e.month - (n - i) - 1) // 12)
        out.append((dt.date(y, m, 1).isoformat(), start * (1 + step) ** i))
    return out


def eod(values):
    return [{"date": d, "close": v, "adjusted_close": v} for d, v in values]


def regime_oktober_2026():
    """Tal fra 26/10/2026 (EODHD/Treasury, Fortune, Wikipedia): 10-årig 5,00 %, 30-årig 5,31 %, realrente 2,66 %,
    2-årig 4,68 % mod Fed-loft 4,00 %, BEI 2,34 %, SOFR 3,93 mod IORB 3,90, Brent ca. 120 (65 for et år siden),
    HY-spænd ca. 3,3 %, aktierne tæt på toppen."""
    f = {"VIXCLS": flat(17.0), "BAMLH0A0HYM2": flat(3.3), "BAMLC0A0CM": flat(0.95), "STLFSI4": flat(-0.4),
         "NFCI": flat(-0.3), "T10Y3M": flat(0.78), "ICSA": [(d, 225000.0) for d in days(430)[::7]],
         "SAHMREALTIME": flat(0.2), "DCOILBRENTEU": [(d, 64.0 if i < 240 else 64.0 + 55.5 * (i - 240) / 189) for i, (d, _) in enumerate(flat(0))], "DTWEXBGS": flat(121.0),
         "DGS10": ramp(4.2, 5.0), "DGS30": ramp(4.8, 5.31), "DGS2": ramp(3.5, 4.68), "DFII10": ramp(1.9, 2.66),
         "DFEDTARU": flat(4.0), "T10YIE": flat(2.34), "SOFR": flat(3.93), "IORB": flat(3.90), "RRPONTSYD": flat(30.0),
         "BAMLH0A3HYC": flat(7.5), "BAMLC0A4CBBB": flat(1.2), "DHHNGSP": flat(4.1), "USEPUINDXD": flat(180.0),
         "CPIAUCSL": monthly(320.0, 0.0025), "PCU33443344": monthly(100.0, 0.004), "THREEFYTP10": flat(0.55), "DRTSCILM": [("2026-07-01", 12.0)],
         "A091RC1Q027SBEA": [("2026-04-01", 1280.0)], "FGRECPT": [("2026-04-01", 5600.0)]}
    up = ramp(100.0, 140.0)
    e = {s: eod(up) for s, *_ in mv.EOD_SERIES}
    e["VIX.INDX"] = eod(flat(17.0))
    e["USDSEK.FOREX"] = eod(flat(9.96))
    e["XAUUSD.FOREX"] = eod(ramp(3000.0, 4000.0))
    for s, _ in tm.EOD_EXTRA:
        e[s] = eod(up)
    e["USDTWD.FOREX"] = eod(flat(32.0))
    return f, e


def by(m):
    return {s["id"]: s for s in m["signals"]}


def test_regime_today_energy_red_rates_yellow_and_complacency():
    f, e = regime_oktober_2026()
    m = mv.build(f, e, None, REF)
    s = by(m)
    assert s["brent_lvl"]["level"] == "red" and s["brent_yoy"]["level"] == "red"
    assert s["dgs10"]["level"] == "yellow" and s["dgs30"]["level"] == "yellow" and s["real10"]["level"] == "yellow"
    assert s["hikes"]["level"] == "yellow" and "renteforhøjelser" in s["hikes"]["note"]
    assert s["sofr_iorb"]["level"] == "yellow" and s["rrp"]["level"] == "yellow"
    assert s["bei10"]["level"] == "green" and s["hy"]["level"] == "green"
    assert s["int_rev"]["level"] == "yellow" and 22 < s["int_rev"]["value"] < 24 and "samlede" in s["int_rev"]["note"]
    th = {t["id"]: t for t in m["themes"]}
    assert th["energi"]["level"] == "red" and th["renter"]["level"] == "yellow" and th["kredit"]["level"] == "green"
    assert m["level"] == "red" and not m["black_swan"]  # rødt i ét tema er ikke en sort svane
    sc = {x["id"]: x for x in m["scenarios"]}
    bad = [x for x in m["scenarios"] if not x["good"]]
    assert bad[0]["id"] == "stagflation" and bad[0]["score"] >= 50
    assert {"Olie (Brent), niveau", "10-årig statsrente (USA)", "Markedet venter renteforhøjelser"} <= set(sc["blod"]["missing"])
    assert m["complacency"]["flag"] and "energi" in m["complacency"]["text"]
    assert all(t["question"].endswith("?") for t in m["themes"]) and len(m["themes"]) == 10


def test_vigilante_pattern_rates_up_dollar_down_is_a_shock():
    f, e = regime_oktober_2026()
    f["DGS10"] = [(d, 4.6 if i < 400 else 5.0) for i, (d, _) in enumerate(f["DGS10"])]
    f["DTWEXBGS"] = [(d, 124.0 if i < 400 else 120.0) for i, (d, _) in enumerate(f["DTWEXBGS"])]
    m = mv.build(f, e, None, REF)
    v = by(m)["vigilante"]
    assert v["level"] == "red" and v["shock"] and m["black_swan"]
    assert by(m)["dgs10"]["shock"]  # +0,40 pp på en måned


def test_black_swan_needs_reds_in_three_themes():
    f, e = regime_oktober_2026()
    f["DGS10"] = flat(5.4)                       # renter rød
    m = mv.build(f, e, None, REF)
    assert set(m["red_themes"]) == {"energi", "renter"} and not m["black_swan"]
    f["BAMLH0A3HYC"] = flat(12.5)               # kredit rød → tre temaer
    m = mv.build(f, e, None, REF, swan_rule="tre_temaer")
    assert m["black_swan"] and set(m["red_themes"]) >= {"energi", "renter", "kredit"}
    live = mv.build(f, e, None, REF)            # live-reglen (kun chok): rødt niveau, ingen sort svane
    assert not live["black_swan"] and live["level"] == "red"


def test_ai_signals_below_falling_200d_are_red_and_feed_the_ai_scenario():
    f, e = regime_oktober_2026()
    down = ramp(140.0, 90.0)
    for s in ("SOXX.US", "NVDA.US", "NDX.INDX"):
        e[s] = eod(down)
    e["CRWV.US"] = eod(ramp(180.0, 60.0))
    m = mv.build(f, e, None, REF)
    s = by(m)
    assert s["soxx"]["level"] == "red" and s["nvda"]["level"] == "red" and s["crwv"]["level"] == "red"
    assert s["ndx_rel"]["level"] == "yellow"
    sc = {x["id"]: x for x in m["scenarios"]}
    assert sc["ai"]["score"] >= 50 and "Halvledere (SOXX)" in sc["ai"]["hits"]


def test_cpi_and_chip_prices_year_over_year():
    f, e = regime_oktober_2026()
    f["CPIAUCSL"] = monthly(320.0, 0.004)       # ca. 4,9 % om året
    f["PCU33443344"] = monthly(100.0, 0.013)    # ca. 17 % om året
    s = by(mv.build(f, e, None, REF))
    assert s["cpi"]["level"] == "yellow" and 4.5 < s["cpi"]["value"] < 5.2
    assert s["semi_ppi"]["level"] == "red"


def test_every_signal_has_a_known_theme_and_scenarios_reference_real_signals():
    f, e = regime_oktober_2026()
    m = mv.build(f, e, None, REF)
    ids = {t[0] for t in tm.THEMES}
    assert all(s["theme"] in ids for s in m["signals"])
    known = {s["id"] for s in m["signals"]} | {"buffett", "dd_ndx"}
    for sc in tm.SCENARIOS:
        assert {sid for sid, _ in sc["checks"]} <= known, sc["id"]


def test_missing_new_data_never_breaks_the_old_watch():
    f, e = regime_oktober_2026()
    for k in [x[0] for x in tm.FRED_EXTRA]:
        f.pop(k, None)
    for k, _ in tm.EOD_EXTRA:
        e.pop(k, None)
    m = mv.build(f, e, None, REF)
    assert m["themes"] and m["scenarios"] and m["level"] in ("green", "yellow", "red")


def test_background_signals_never_make_a_theme_red():
    """Statens rentebyrde (kvartalsdata) må gerne stå rødt som tal, men temaet, svanen og selvtilfredsheden er
    stress nu: før 28/10/2026 farvede den Statsgæld rødt og kom med i "selvtilfredshed"."""
    f, e = regime_oktober_2026()
    f["A091RC1Q027SBEA"] = [("2026-04-01", 1280.0)]
    f["FGRECPT"] = [("2026-04-01", 5000.0)]          # 25,6 % → rød på NIPA-basis
    m = mv.build(f, e, {"value": 255.0, "percentile": 100, "date": "2026-04-01", "quarter": "Q2 2026", "years": 10}, REF)
    s = by(m)
    assert s["int_rev"]["level"] == "red"                  # tallet vises ærligt
    th = {t["id"]: t for t in m["themes"]}
    assert th["gaeld"]["level"] == "yellow" and th["gaeld"]["red"] == 0
    assert "gaeld" not in m["red_themes"] and "Statsgæld" not in m["complacency"]["themes"]
    assert m["counts"]["red"] == sum(1 for x in m["signals"] if tm.counted_level(x) == "red" and not x["stale"])
    assert tm.counted_level({"id": "buffett", "level": "red"}) == "yellow"
    assert tm.counted_level({"id": "ccc", "level": "red"}) == "red"


def test_live_run_28_oktober_ccc_shock_is_the_swan_not_chip_prices():
    """Første rigtige kørsel 28/10/2026: CCC 13,45 % (+2,17 pp på en måned), halvlederpriser +26 % år til år,
    Brent 115 (+90 %). Chipprisernes signal står rødt, men det er CCC-chokket, der udløser sort svane."""
    f, e = regime_oktober_2026()
    f["PCU33443344"] = monthly(100.0, 0.0195)                       # ca. +26 % om året
    m = mv.build(f, e, None, REF)
    s, th = by(m), {t["id"]: t for t in m["themes"]}
    assert s["semi_ppi"]["level"] == "red" and th["chips"]["level"] == "yellow"   # baggrund: temaet højst gult
    assert "Chips og forsyning" not in m["complacency"]["themes"]
    assert m["red_themes"] == ["energi"] and not m["black_swan"]   # chippriser alene er baggrund
    f["BAMLH0A3HYC"] = [(d, 11.28 if i < 400 else 13.45) for i, (d, _) in enumerate(flat(0))]
    m = mv.build(f, e, None, REF)
    assert by(m)["ccc"]["shock"] and m["black_swan"] and m["headline"].startswith("Sort svane-varsel: Kreditspænd CCC")


def test_background_signals_never_turn_the_macro_red_on_their_own():
    f, e = regime_oktober_2026()
    f["DCOILBRENTEU"] = flat(70.0)
    f["PCU33443344"] = monthly(100.0, 0.03)
    f["A091RC1Q027SBEA"] = [("2026-04-01", 1600.0)]                 # 28,6 % → rød
    m = mv.build(f, e, None, REF)
    s = by(m)
    assert s["semi_ppi"]["level"] == "red" and s["int_rev"]["level"] == "red"
    assert m["level"] != "red" and m["red_themes"] == [] and not m["black_swan"]
