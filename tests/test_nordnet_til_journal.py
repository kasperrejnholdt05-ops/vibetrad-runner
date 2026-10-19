"""Tests for tools/nordnet_til_journal.py"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import nordnet_til_journal as nj  # noqa: E402

SE_HEADER = ["Id", "Bokföringsdag", "Affärsdag", "Likviddag", "Depå", "Transaktionstyp", "Värdepapper", "ISIN",
             "Antal", "Kurs", "Ränta", "Total Avgift", "Valuta", "Belopp", "Valuta", "Växlingskurs"]
SE_ROWS = [
    ["1", "2026-08-21", "2026-08-19", "2026-08-21", "ISK", "KÖPT", "Alphabet A", "US02079K3059", "3", "301,40", "0", "49", "SEK", "-9 315,00", "SEK", "10,25"],
    ["2", "2026-09-02", "2026-08-31", "2026-09-02", "ISK", "SÅLT", "Demo Robotics", "SE0012345678", "-100", "2,40", "0", "39", "SEK", "201,00", "SEK", "1"],
    ["3", "2026-08-25", "2026-08-25", "2026-08-25", "ISK", "INSÄTTNING", "", "", "", "", "", "", "SEK", "7 500,00", "SEK", ""],
    ["4", "2026-08-26", "2026-08-26", "2026-08-28", "ISK", "KÖPT", "SpaceX (Space Exploration Technologies)", "", "13", "161,10", "0", "69", "SEK", "-21 480,00", "SEK", "10,22"],
    ["5", "2026-08-27", "2026-08-27", "2026-08-27", "ISK", "UTDELNING", "Broadcom", "US11135F1012", "2", "0,59", "", "0", "SEK", "12,06", "SEK", "10,21"],
]


def write(path: Path, header, rows, enc="utf-16", delim="\t"):
    with path.open("w", newline="", encoding=enc) as fh:
        w = csv.writer(fh, delimiter=delim)
        w.writerow(header)
        w.writerows(rows)


def read_out(path: Path):
    return list(csv.DictReader(path.open(encoding="utf-8")))


def test_swedish_utf16_tab_export(tmp_path):
    src = tmp_path / "transaktioner.csv"
    write(src, SE_HEADER, SE_ROWS)
    out = tmp_path / "out.csv"
    res = nj.convert(src, out, nj.build_map(None, None))
    rows = read_out(out)
    assert res["written"] == 3 and res["skipped"] == {"INSÄTTNING": 1} and "Demo Robotics (SE0012345678)" in res["unmapped"]
    googl = rows[0]
    assert googl["symbol"] == "GOOGL" and googl["side"] == "buy" and googl["datetime"] == "2026-08-19 00:00:00"
    assert float(googl["price"]) == 301.40 and float(googl["quantity"]) == 3
    assert abs(float(googl["fee"]) - 49 / 10.25) < 0.01  # kurtage omregnet til aktiens valuta
    assert rows[1]["symbol"] == "SPCX"  # navnematch på "spacex"
    div = rows[2]
    assert div["side"] == "dividend" and abs(float(div["amount"]) - 12.06 / 10.21) < 0.01


def test_danish_semicolon_utf8_with_map_and_portfolio(tmp_path):
    hdr = ["Id", "Bogføringsdag", "Handelsdag", "Transaktionstype", "Værdipapirer", "ISIN", "Antal", "Kurs",
           "Total omkostninger", "Beløb", "Vekslingskurs"]
    rows = [["1", "13-10-2026", "13-10-2026", "KØBT", "Investor B", "SE0015811963", "3", "301,50", "29", "-904,50", "1"],
            ["2", "13-10-2026", "13-10-2026", "SOLGT", "NVIDIA Corp", "", "1", "185,20", "39", "1 840,00", "9,95"]]
    src = tmp_path / "dk.csv"
    write(src, hdr, rows, enc="utf-8-sig", delim=";")
    mp = tmp_path / "map.csv"
    mp.write_text("navn,symbol\nSE0015811963,INVE-B.ST\n", "utf-8")
    pj = tmp_path / "p.json"
    pj.write_text('{"holdings":[{"ticker":"NVDA.US","name":"NVIDIA"}]}', "utf-8")
    out = tmp_path / "o.csv"
    res = nj.convert(src, out, nj.build_map(str(pj), str(mp)))
    got = read_out(out)
    assert res["written"] == 2 and [r["symbol"] for r in got] == ["INVE-B.ST", "NVDA"]
    assert got[0]["datetime"] == "2026-10-13 00:00:00" and got[1]["side"] == "sell"


def test_known_names_and_isins():
    m = nj.build_map(None, None)
    assert nj.lookup(m, "Alphabet Inc. Class C", "") == nj.lookup(m, "Alphabet C", "") == "GOOG.US" and nj.lookup(m, "Alphabet A", "") == "GOOGL.US"
    assert nj.lookup(m, "Apple", "US0378331005") == "AAPL.US" and nj.lookup(m, "Volvo B", "SE0000115446") == "VOLV-B.ST"
    assert nj.lookup(m, "Apple Hospitality REIT", "") is None  # korte, tvetydige navne matches ikke


def test_number_parsing():
    assert nj.num("1 234,56") == 1234.56 and nj.num("-21 480,00") == -21480.0
    assert nj.num("1.234,5") == 1234.5 and nj.num("1,234.5") == 1234.5 and nj.num("") == 0.0


def test_missing_columns_message(tmp_path):
    src = tmp_path / "x.csv"
    src.write_text("a;b\n1;2\n", "utf-8")
    try:
        nj.convert(src, tmp_path / "o.csv", {})
    except ValueError as exc:
        assert "Mangler kolonner" in str(exc)
    else:
        raise AssertionError("burde fejle")
