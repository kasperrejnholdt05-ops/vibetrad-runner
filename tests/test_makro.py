"""Makro for Europa og Norden (EODHD-kalender og statsrenter). Alle tal er opdigtede."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))

import makro as mk  # noqa: E402

RATE = "Interest Rate Decision"


class FakeClient:
    def __init__(self, fail: set[str] | None = None):
        self.calls: list[tuple] = []
        self.fail = fail or set()

    def economic_events(self, iso2, start, end, limit, event_type=None):
        self.calls.append(("events", iso2, event_type))
        if iso2 in self.fail:
            raise RuntimeError("boom")
        if event_type == RATE:
            return {"SE": [{"type": RATE, "date": "2026-11-04 08:30:00", "actual": None, "previous": 1.75, "estimate": 2.0},
                           {"type": RATE, "date": "2026-09-23 08:30:00", "actual": 1.75, "previous": 1.75, "estimate": 1.75}],
                    "DK": [{"type": RATE, "date": "2026-10-29 15:00:00", "actual": None, "previous": 1.6, "estimate": None}],
                    "EU": [{"type": RATE, "date": "2026-10-29 13:15:00", "actual": None, "previous": 2.65},
                           {"type": RATE, "date": "2026-09-10 12:15:00", "actual": 2.65, "previous": 2.4}],
                    "US": [{"type": RATE, "date": "2026-10-28 18:00:00", "actual": None, "previous": 4.0, "estimate": 4.0}]}[iso2]
        assert iso2 != "US", "USA's nøgletal kommer fra FRED"
        return {"SE": [{"type": "CPIF", "comparison": "yoy", "period": "Sep", "date": "2026-10-14 06:00:00", "actual": 1.5},
                       {"type": "CPIF", "comparison": "mom", "period": "Sep", "date": "2026-10-14 06:00:00", "actual": 0.9},
                       {"type": "Inflation Rate", "comparison": "yoy", "period": "Sep", "date": "2026-10-14", "actual": 1.1},
                       {"type": "Inflation Rate", "comparison": "yoy", "period": "Oct", "date": "2026-11-05", "actual": None},
                       {"type": "Unemployment Rate", "period": "Sep", "date": "2026-10-16", "actual": 8.1},
                       {"type": RATE, "date": "2026-11-04", "actual": None, "previous": 1.75}],
                "DK": [{"type": "Inflation Rate", "comparison": "yoy", "period": "Sep", "date": "2026-10-10", "actual": 1.9}],
                "EU": []}[iso2]

    def eod(self, sym, start, end):
        self.calls.append(("eod", sym))
        if sym == "DK10Y.GBOND":
            return []
        base = {"SE10Y.GBOND": 3.0, "DE10Y.GBOND": 2.7}[sym]
        return [{"date": "2026-09-19", "close": base}, {"date": "2026-10-19", "close": round(base + 0.16, 4)}]


def test_snapshot_reads_rates_meetings_series_and_bonds():
    c = FakeClient()
    snap = mk.snapshot(c, "2026-10-19")
    assert snap["ok"] and not snap["errors"]
    se = next(x for x in snap["countries"] if x["country"] == "SE")
    assert (se["rate"], se["rate_date"], se["next_meeting"], se["next_estimate"]) == (1.75, "2026-09-23", "2026-11-04", 2.0)
    assert [(s["type"], s["value"], s["period"]) for s in se["series"]] == [("Inflation Rate", 1.1, "Sep"), ("CPIF", 1.5, "Sep"),
                                                                           ("Unemployment Rate", 8.1, "Sep")]
    dk = next(x for x in snap["countries"] if x["country"] == "DK")
    assert dk["rate"] == 1.6 and dk["rate_date"] is None  # ingen beslutning i vinduet: "previous" fra næste møde
    us = next(x for x in snap["countries"] if x["country"] == "US")
    assert us["series"] == [] and ("events", "US", None) not in c.calls  # kun rentebeslutninger for USA
    assert [b["symbol"] for b in snap["bonds"]] == ["SE10Y.GBOND", "DE10Y.GBOND"]  # DK uden data udelades
    assert snap["bonds"][0]["value"] == 3.16 and snap["bonds"][0]["change"] == 0.16 and snap["bonds"][0]["prev_date"] == "2026-09-19"
    assert len([x for x in c.calls if x[0] == "events"]) == 7 and len([x for x in c.calls if x[0] == "eod"]) == 3


def test_brief_and_status_line_are_danish_and_safe():
    snap = mk.snapshot(FakeClient(), "2026-10-19")
    b = mk.agent_brief(snap)
    assert b.startswith("## Makro (Europa og Norden, EODHD) pr. 19/10/2026")
    assert "| Riksbanken (SEK) | 1,75 % (23/09/2026) | 04/11/2026, ventet 2,00 % | inflation (år/år) 1,1 % (Sep); CPIF (år/år) 1,5 % (Sep); ledighed 8,1 % (Sep) |" in b
    assert "| ECB (EUR) | 2,65 % (10/09/2026) | 29/10/2026 | – |" in b
    assert "Statsrenter (10 år): Sverige 10 år 3,16 % (+0,16 pp siden 19/09/2026) · Tyskland 10 år 2,86 % (+0,16 pp siden 19/09/2026)" in b
    assert mk.status_line(snap) == ("Makro EU/Norden: ja (Riksbanken 1,75 % → 04/11/2026 · Nationalbanken 1,60 % → 29/10/2026 · "
                                    "ECB 2,65 % → 29/10/2026 · Fed 4,00 % → 28/10/2026 · Sverige 10 år 3,16 %)")
    assert mk.agent_brief(None) == "" and mk.status_line(None) == "Makro EU/Norden: ikke hentet"


def test_failures_degrade_per_country_without_stopping():
    snap = mk.snapshot(FakeClient(fail={"SE", "EU"}), "2026-10-19")
    assert snap["ok"] and snap["errors"] == ["SE: RuntimeError", "EU: RuntimeError"]
    assert "| Riksbanken (SEK) | – | – | kalender utilgængelig |" in mk.agent_brief(snap)
    empty = mk.snapshot(FakeClient(fail={"SE", "DK", "EU", "US"}), "2026-10-19")
    assert empty["ok"] is True and empty["bonds"]  # statsrenter alene giver stadig en blok
