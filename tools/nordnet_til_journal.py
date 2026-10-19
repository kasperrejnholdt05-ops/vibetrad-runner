#!/usr/bin/env python3
"""Nordnet-transaktioner → Vibe-Tradings generiske handelsjournal (til Shadow Account).

Nordnet eksporterer transaktioner som CSV (ofte UTF-16 med tabulator, svenske/danske/norske/
finske kolonnenavne, komma som decimaltegn og kun navn + ISIN, ingen ticker). Vibe-Tradings
trade-journal kender ikke det format. Dette script oversætter til de kolonner, Vibe-Trading
læser som "generic": datetime, symbol, name, side, quantity, price, amount, fee.

Brug (i codespacet):
    python tools/nordnet_til_journal.py ~/transaktioner.csv
    python tools/nordnet_til_journal.py ~/transaktioner.csv --portfolio-json "$VT_PORTFOLIO_FIL" --map mine_tickers.csv

Resultat: <navn>_vibetrading.csv ved siden af input-filen. Upload den i Vibe-Tradings Agent-chat
(clips-ikonet) og skriv fx "Analysér min handelsadfærd, udtræk min shadow-strategi og
sammenlign med mine faktiske handler".

Amerikanske aktier skrives som rene tickers (GOOGL), fordi Vibe-Tradings journal kun genkender
dem sådan. Shadow-backtesten understøtter USA, Hongkong, Kina og krypto. Nordiske aktier (fx
NOVO-B.CO) indgår i adfærdsanalysen, men ikke i shadow-backtesten.

Kun køb, salg og udbytte tages med. Indbetalinger, skat, renter, gebyrer og fondshandler uden
ticker springes over og tælles op til sidst.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

COLS = {
    "date": ["affärsdag", "handelsdag", "handelsdato", "kauppapäivä", "trade date", "bokföringsdag",
             "bogføringsdag", "bokføringsdag", "kirjauspäivä", "transaktionsdag"],
    "type": ["transaktionstyp", "transaktionstype", "tapahtumatyyppi", "transaction type", "type"],
    "name": ["värdepapper", "værdipapirer", "værdipapir", "verdipapir", "arvopaperi", "security", "instrument"],
    "isin": ["isin"],
    "qty": ["antal", "antall", "määrä", "quantity"],
    "price": ["kurs", "price", "hinta"],
    "amount": ["belopp", "beløb", "beløp", "summa", "amount"],
    "fee": ["total avgift", "courtage", "total omkostninger", "kurtage", "kurtasje", "kokonaiskulut",
            "välityspalkkio", "fee", "commission"],
    "fx": ["växlingskurs", "vekslingskurs", "vaihtokurssi", "exchange rate"],
}
SIDES = {
    "buy": ["köpt", "köp", "købt", "køb", "kjøpt", "kjøp", "osto", "buy", "bought"],
    "sell": ["sålt", "sälj", "solgt", "salg", "myynti", "sell", "sold"],
    "dividend": ["utdelning", "udbytte", "utbytte", "osinko", "dividend"],
}
# Kendte navne/ISIN → Vibe-Trading-symbol (amerikanske aktier skal have .US-suffiks i Vibe-Trading).
# ISIN matcher præcist og vinder. Navne matcher som hele ord i værdipapirets navn, og det længste navn vinder
# ("alphabet c" før "alphabet", men "alphabet c" matcher ikke "Alphabet Class A"). Alt andet kan tilføjes med --map eller --portfolio-json.
KNOWN = {
    # navne (kun entydige)
    "alphabet": "GOOGL.US", "alphabet c": "GOOG.US", "alphabet inc. class c": "GOOG.US",
    "alphabet inc class c": "GOOG.US", "nvidia": "NVDA.US", "broadcom": "AVGO.US",
    "spacex": "SPCX.US", "space exploration": "SPCX.US", "microsoft": "MSFT.US", "tesla": "TSLA.US",
    "meta platforms": "META.US", "amazon.com": "AMZN.US", "berkshire hathaway b": "BRK-B.US",
    "novo nordisk": "NOVO-B.CO", "hennes & mauritz": "HM-B.ST", "a.p. møller - mærsk b": "MAERSK-B.CO",
    # ISIN
    "US02079K3059": "GOOGL.US", "US02079K1079": "GOOG.US", "US67066G1040": "NVDA.US", "US11135F1012": "AVGO.US",
    "US0378331005": "AAPL.US", "US5949181045": "MSFT.US", "US0231351067": "AMZN.US", "US30303M1027": "META.US",
    "US88160R1014": "TSLA.US", "US0846707026": "BRK-B.US", "NL0010273215": "ASML.AS",
    "DK0062498333": "NOVO-B.CO", "DK0010244508": "MAERSK-B.CO", "DK0010181759": "CARL-B.CO",
    "DK0060094928": "ORSTED.CO", "DK0061539921": "VWS.CO",
    "SE0000115446": "VOLV-B.ST", "SE0000108656": "ERIC-B.ST", "SE0015811963": "INVE-B.ST", "SE0000106270": "HM-B.ST",
    "GB0009895292": "AZN.ST", "FI4000297767": "NDA-SE.ST", "NO0010096985": "EQNR.OL", "FI0009000681": "NOKIA.HE",
}


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-16", "utf-8-sig", "cp1252") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = raw.decode(enc)
            if "\x00" not in text:
                return text
        except UnicodeError:
            continue
    raise ValueError("Kunne ikke læse filens tegnsæt")


def sniff_rows(text: str) -> list[list[str]]:
    first = text.splitlines()[0] if text else ""
    delim = max(["\t", ";", ","], key=first.count)
    return [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any(c.strip() for c in r)]


def num(val: str) -> float:
    s = str(val or "").strip().replace(" ", "").replace(" ", "")
    if not s or s in ("-", "--"):
        return 0.0
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return float(re.sub(r"[^0-9.\-]", "", s) or 0)
    except ValueError:
        return 0.0


def to_iso(val: str) -> str:
    v = str(val or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(v[:10], fmt).strftime("%Y-%m-%d 00:00:00")
        except ValueError:
            continue
    return v


def side_of(val: str) -> str | None:
    v = str(val or "").strip().lower()
    for side, words in SIDES.items():
        if v in words or any(v.startswith(w + " ") for w in words):
            return side
    return None


def find_cols(header: list[str]) -> dict[str, int]:
    low = [h.strip().lower() for h in header]
    out: dict[str, int] = {}
    for key, names in COLS.items():
        for n in names:
            if n in low:
                out[key] = low.index(n)
                break
    return out


def build_map(portfolio_json: str | None, map_csv: str | None) -> dict[str, str]:
    m = {k.lower() if not re.match(r"^[A-Z]{2}[A-Z0-9]{9}\d$", k) else k: v for k, v in KNOWN.items()}
    if portfolio_json:
        p = json.loads(Path(portfolio_json).read_text("utf-8"))
        for h in p.get("holdings", []):
            if h.get("name") and h.get("ticker"):
                m[str(h["name"]).lower().split(" (")[0]] = str(h["ticker"]).upper()
    if map_csv:
        for row in csv.reader(Path(map_csv).read_text("utf-8").splitlines()):
            if len(row) >= 2 and row[0].strip() and not row[0].lower().startswith("navn"):
                key = row[0].strip()
                m[key if re.match(r"^[A-Z]{2}[A-Z0-9]{9}\d$", key) else key.lower()] = row[1].strip().upper()
    return m


def lookup(m: dict[str, str], name: str, isin: str) -> str | None:
    if isin and isin in m:
        return m[isin]
    n = name.lower()
    best = None
    for key, sym in m.items():
        if (not re.match(r"^[A-Z]{2}[A-Z0-9]{9}\d$", key) and key
                and re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", n)):  # hele ord: "alphabet c" ≠ "alphabet class a"
            if best is None or len(key) > len(best[0]):
                best = (key, sym)
    return best[1] if best else None


def convert(path: Path, out: Path, mapping: dict[str, str]) -> dict:
    rows = sniff_rows(read_text(path))
    if not rows:
        raise ValueError("Filen er tom")
    cols = find_cols(rows[0])
    missing = [k for k in ("date", "type", "name", "qty", "price") if k not in cols]
    if missing:
        raise ValueError(f"Mangler kolonner {missing}. Fundne kolonner: {rows[0]}")
    get = lambda r, k: r[cols[k]] if k in cols and cols[k] < len(r) else ""  # noqa: E731
    written, skipped, unmapped = 0, {}, {}
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["datetime", "symbol", "name", "side", "quantity", "price", "amount", "fee"])
        for r in rows[1:]:
            typ = get(r, "type")
            side = side_of(typ)
            if side is None:
                skipped[typ.strip() or "(tom)"] = skipped.get(typ.strip() or "(tom)", 0) + 1
                continue
            name, isin = get(r, "name").strip(), get(r, "isin").strip()
            sym = lookup(mapping, name, isin)
            if not sym:
                unmapped[f"{name} ({isin})" if isin else name] = unmapped.get(name, 0) + 1
                continue
            if sym.endswith(".US"):  # Vibe-Tradings journal genkender kun amerikanske aktier som rene tickers
                sym = sym[:-3]
            qty, price = abs(num(get(r, "qty"))), abs(num(get(r, "price")))
            fx = num(get(r, "fx")) or 1.0
            fee = abs(num(get(r, "fee"))) / (fx if fx > 0 else 1.0)
            amount = qty * price if side != "dividend" else abs(num(get(r, "amount"))) / (fx if fx > 0 else 1.0)
            w.writerow([to_iso(get(r, "date")), sym, name, side, f"{qty:g}", f"{price:.4f}", f"{amount:.2f}", f"{fee:.2f}"])
            written += 1
    return {"written": written, "skipped": skipped, "unmapped": unmapped, "out": str(out)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("csv", help="Nordnet-eksporten (Transaktioner → Eksportér)")
    ap.add_argument("--out", help="output-fil (standard: <input>_vibetrading.csv)")
    ap.add_argument("--portfolio-json", help="fil med VT_PORTFOLIO-JSON (navn → ticker)")
    ap.add_argument("--map", help="CSV med to kolonner: navn eller ISIN, Vibe-Trading-symbol (fx 'Investor B,INVE-B.ST')")
    a = ap.parse_args(argv)
    src = Path(a.csv).expanduser()
    out = Path(a.out).expanduser() if a.out else src.with_name(src.stem + "_vibetrading.csv")
    try:
        res = convert(src, out, build_map(a.portfolio_json, a.map))
    except ValueError as exc:
        print(f"FEJL: {exc}")
        return 2
    print(f"Skrev {res['written']} handler til {res['out']}")
    if res["skipped"]:
        print("Sprunget over (ikke køb/salg/udbytte): " + ", ".join(f"{k} ×{v}" for k, v in sorted(res["skipped"].items())))
    if res["unmapped"]:
        print("UDEN TICKER – tilføj dem i en --map-fil og kør igen:")
        for k in res["unmapped"]:
            print(f"  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
