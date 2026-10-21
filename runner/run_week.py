#!/usr/bin/env python3
"""Vibetrad ugekørsel – kører det rigtige HKUDS/Vibe-Trading og krypterer resultatet.

Kører i GitHub Actions på knaptryk (se .github/workflows/vibetrad-weekly.yml). Flowet:

1. Afgør om der skal køres (knaptryk kører altid; lørdagskørsel kun med VT_SCHEDULE=on;
   kanariefuglen – den daglige gratis tørkørsel i kanarie.py – kører hver nat).
2. Læser porteføljen fra secret VT_PORTFOLIO (JSON, genereres af Vibetrad-siden).
3. Kobler EODHD ind i Vibe-Trading og krydstjekker porteføljens kurser, valuta,
   regnskabsdatoer og værdi mellem Vibe-Tradings egne kilder og EODHD (datatjek.py).
4. Lægger en plan efter spillebogen (risiko, regnskab, månedens team, rotation, CIO).
5. Kører swarms med Vibe-Tradings egen SwarmRuntime (samme motor som web-UI og CLI).
   Alle agenter får datatjekkets facit; CIO'en også afvigelserne.
6. Pakker rapporter, datatjek og agent-opsummeringer, gzip'er og krypterer med AES-256-GCM
   (nøgle afledt af VT_RESULTS_PASSPHRASE via PBKDF2-SHA256), og skriver
   results/latest.enc.json + results/weeks/<run-id>.enc.json + results/index.json.

Repoet er offentligt, så intet personligt må nå loggen: al output fra
Vibe-Trading omdirigeres til en midlertidig fil, og kun status-linjer uden
porteføljeindhold skrives til GitHub-loggen. Selve indholdet findes kun
krypteret.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import gzip
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import datatjek as dtj  # noqa: E402  (krydstjek Vibe-Trading × EODHD)
import eodhd as eo  # noqa: E402  (EODHD-klient og -integration i Vibe-Trading)
import fred as fr  # noqa: E402  (FRED: nøgletjek og makro-facit)
import kanarie as kn  # noqa: E402  (kanariefugl: daglig gratis tørkørsel af hele kæden)
import makro as mk  # noqa: E402  (EODHD: centralbanker og statsrenter for Europa og Norden)

STO = ZoneInfo("Europe/Stockholm")
SCHEMA = 1
PBKDF2_ITER = 600_000
MIN_PASSPHRASE = 16
MAX_ENVELOPE_BYTES = 240_000  # artifact-db dokumenter må højst være 256 KiB

PRESET_LABELS = {
    "portfolio_review_board": "Porteføljegennemgang (CIO)",
    "risk_committee": "Risikoudvalg",
    "earnings_research_desk": "Regnskabsdesk",
    "investment_committee": "Investeringskomité",
    "etf_allocation_desk": "ETF-allokering",
    "sentiment_intelligence_team": "Sentiment",
    "technical_analysis_panel": "Teknisk analyse",
    "macro_rates_fx_desk": "Makro, renter og valuta",
    "fund_selection_panel": "Fondsvalg (ISK-kerne)",
    "value_investing_committee": "Værdikomité (Buffett/Munger m.fl.)",
    "equity_research_team": "Idéjagt uden for tech",
    "fundamental_research_team": "Fundamental analyse",
    "macro_strategy_forum": "Makrostrategi (5 år)",
    "sector_rotation_team": "Sektorer og temaer",
    "visser_panel": "Visser-panelet (trend mod tese)",
}
# Vibetrads egne presets (fx Visser-panelet) ligger i repoet og kopieres med de indbyggede.
VIBETRAD_PRESETS_DIR = Path(__file__).resolve().parent / "presets"

# Ekstra Vibe-Trading-skills, der lægges på bestemte agenter (agent-id → skills).
# "*last*" er det sidste (syntetiserende) agent-trin i hvert preset.
# Få, relevante skills: hvert load_skill koster et kald og 10-20 000 tegn kontekst. thesis-tracker, risk-analysis og
# correlation-regime er Python-/filopskrifter, agenterne ikke kan følge uden shell, og udelades (19/10/2026).
EXTRA_SKILLS = {
    "*": {"*last*": ["research-discipline"]},
    "portfolio_review_board": {"chief_investment_officer": ["investor-lenses", "research-discipline"]},
    "investment_committee": {"*last*": ["research-discipline"]},
    "value_investing_committee": {"*last*": ["management-deep-dive", "private-company-research"]},
    # SpaceX er nyligt børsnoteret: rammen for pre-IPO/private selskaber (6 analytiker-vinkler) hjælper, hvor historikken er kort.
    "earnings_research_desk": {"*last*": ["private-company-research"]},
    "fundamental_research_team": {"*last*": ["private-company-research"]},
}
SPACEX_HINT = (" Selskabet er nyligt børsnoteret med kort kurs- og regnskabshistorik: behandl det som et nyligt privat "
               "selskab (ejerstruktur og udvanding, kapitalrunder og gæld, kundekoncentration, lock-up), og sig tydeligt, "
               "hvor historikken er for kort til et tal. Agenter med skill'en private-company-research kan bruge den.")
# Ekstra værktøjer pr. preset → agent-id. "*" = alle agenter i presettet, "*data*" = agenter med get_market_data.
# Preset-nøglen "*" gælder alle presets. Værktøjer, der ikke er tilgængelige (fx get_macro_series uden
# FRED_API_KEY, get_eodhd_data uden EODHD_API_KEY), udelades stille af Vibe-Trading.
EXTRA_TOOLS = {
    "*": {"*data*": ["get_eodhd_data"]},
    "portfolio_review_board": {"chief_investment_officer": ["report_audit", "financial_rigor", "get_market_data",
                                                            "get_eodhd_data", "etf_holdings", "get_macro_series"]},
    "risk_committee": {"*data*": ["portfolio_risk_xray"], "regime_detector": ["get_macro_series"]},
    "earnings_research_desk": {"*": ["get_stock_profile", "get_sec_filings", "get_financial_statements",
                                     "get_stock_news", "get_eodhd_data"]},
    "macro_rates_fx_desk": {"*": ["get_macro_series", "get_eodhd_data"]},
    "sentiment_intelligence_team": {"*": ["get_stock_news", "sentiment", "web_search", "get_eodhd_data"]},
    "value_investing_committee": {"*": ["get_stock_profile", "get_sec_filings", "get_eodhd_data"]},
    "fundamental_research_team": {"*": ["get_sec_filings", "get_fundamentals", "get_eodhd_data"]},
    "investment_committee": {"*": ["get_stock_profile", "get_sec_filings", "get_eodhd_data"]},
    "equity_research_team": {"*": ["get_stock_profile", "get_financial_statements", "get_eodhd_data"]},
    "etf_allocation_desk": {"*": ["etf_holdings", "get_eodhd_data"]},
    "fund_selection_panel": {"*": ["get_eodhd_data"]},
    "technical_analysis_panel": {"*": ["get_eodhd_data"]},
    "macro_strategy_forum": {"*": ["get_macro_series", "get_eodhd_data"]},
    "sector_rotation_team": {"*": ["get_eodhd_data", "etf_holdings"]},
    "visser_panel": {"*": ["get_eodhd_data"]},
}
DATA_GUIDANCE = """

## Data sources (Vibetrad)
Two independent sources are available: Vibe-Trading's own loaders (`get_market_data`, Yahoo first) and EODHD
(`get_eodhd_data`). Nordic and European listings (e.g. NOVO-B.CO, INVE-B.ST, EQNR.OL, SAP.DE) are served through EODHD.
Use `get_eodhd_data` for fundamentals, earnings dates, news sentiment, dividends/splits, macro series, the economic
calendar (Fed/Riksbank), government bond yields (US10Y.GBOND, SE10Y.GBOND), FX (USDSEK.FOREX) and screening of Nordic and
European stocks. If two sources disagree by more than 1 %, report both numbers and the date.
The `backtest` tool does not model Nordic/European markets in this Vibe-Trading version (it would treat them as China
A-shares): never backtest such symbols; use their US listing/ETF or compute returns from `get_eodhd_data` eod data.
EODHD coverage by market: prices, FX, fundamentals and the earnings calendar cover US, European and Nordic listings;
news and daily sentiment are rich for US names and thin for Nordic/European ones (few articles, often no sentiment
score); earnings_trends (analyst revisions) covers US and large European names. Use news with a query (e.g.
'earnings date', 'guidance') to find a specific announcement. European and Nordic macro (Riksbanken, Nationalbanken,
ECB, Fed, 10-year yields) is in the facit; economic_events gives the full calendar per country (SE, DK, EU, US).
"""
FRED_GUIDANCE = """

## Macro data (FRED, Vibetrad)
`get_macro_series` returns official US macro series from the St. Louis Fed. Useful series_id values: DGS10 and DGS2
(Treasury yields), T10Y2Y (curve), DFF (effective fed funds), VIXCLS (equity volatility), BAMLH0A0HYM2 (US high-yield
credit spread), DEXSDUS (SEK per USD, weekly noon-NY release: macro context only - never compare it with or use it
instead of the facit USD/SEK rate), CPIAUCSL (CPI), UNRATE (unemployment). Always pass start_date
(e.g. two years back) to keep the output small. FRED is US data: for Swedish or German rates use get_eodhd_data
(SE10Y.GBOND, DE10Y.GBOND). The "Makro (FRED)" table in your instructions is this run's reference snapshot.
"""
SYMBOL_GUIDANCE = """

## Symbols (Vibetrad)
Always write US stocks and ETFs with the .US suffix (ACWI.US, IEF.US, GLD.US, VNQ.US, SPCX.US). Without a suffix
Vibe-Trading treats the symbol as a China A-share and the call fails ("no price data"). Nordic listings: NOVO-B.CO,
INVE-B.ST. Danish and other Nordic stocks keep their exchange suffix and are never .US (Novo Nordisk B = NOVO-B.CO,
not NOVO.US). Currency pairs are FX symbols (USDSEK=X, DKKSEK=X), never DKK.US or USD.US.
If a tool call fails or returns no data, say so in the report and never present a number from that call.
portfolio_risk_xray needs at least 30 daily bars per symbol ("no symbol has at least 30 valid bars" otherwise): leave
start_date empty (one year) or set it at least 3 months back. Never use the short ground-truth price window as the
start date. It aligns all symbols on their common history, so with a recently listed stock (short history in the
facit) the whole window shrinks to that listing period: run it once with and once without that stock and report the
window (first and last date) of each.
`etf_holdings` is the exception to the suffix rule: it takes the bare US ticker (ACWI, not ACWI.US) and covers US- and
China-listed funds only; use the US sibling as a look-through proxy for a UCITS ETF and say so.
Never state a brokerage fee (courtage) or FX fee unless it is given in your instructions.
Market capitalisation: use the whole-company figure from the Vibetrad facit. Never compute it from one share class's
share count (Alphabet has A, B and C shares, so GOOGL alone is far too low).

## Source rules (Vibetrad)
- Report dates: the facit's verified date combines the company's own announcement (news), Yahoo and EODHD, and lists
  the other dates. Never replace it with a single source; a calendar feed alone is often an estimate.
- Insider transactions: `get_sec_filings` (Form 4) for US filers. Data-vendor insider feeds lag months.
- `get_financial_statements` and `get_sec_filings` cover US filers only; Nordic and European statements come from
  `get_eodhd_data` fundamentals when that tool is available.
- Nordic/European news coverage in data feeds is thin: if you have `web_search`, search the company's own
  investor-relations site and Nasdaq Nordic/Cision and cite the URL; otherwise say the news base is thin rather than
  inferring sentiment from a few articles.
"""
# Alle agenter får investorens rammer: ISK hos Nordnet som privatkunde (kørslen 19/10 anbefalede ACWI.US, som ikke kan købes)
ISK_GUIDANCE = """

## Investor constraints (Vibetrad)
The investor is a retail client who invests only through a Swedish ISK account at Nordnet.
- US- and Canada-domiciled ETFs (ACWI.US, EFV.US, VSS.US, IEUR.US, SPY.US, IEF.US, GLD.US ...) cannot be bought by EU
  retail investors (PRIIPs/MiFID II, since 2018). Use them only as data proxies. Whenever you suggest a fund or ETF, name
  an EU-domiciled UCITS equivalent (e.g. "iShares MSCI ACWI UCITS ETF" instead of ACWI.US) or a Nordnet index fund, and
  say that the exact instrument must be confirmed on Nordnet. Never tell the investor to buy a US-domiciled ETF.
- No options, futures, short selling or leverage: do not propose option strategies or derivative hedges. Single US,
  Nordic and European stocks are fine.
- Cash that the portfolio text marks as reserved (e.g. for an open order) is not available for new purchases.
- Currency risk is handled only through weights, diversification or currency-hedged UCITS share classes, never with
  derivatives.
- UCITS ETFs can be fetched through `get_eodhd_data` with their European listing, e.g. IUSQ.DE or SPYY.DE (MSCI ACWI),
  IWDA.AS or EUNL.DE (MSCI World), VWCE.DE (FTSE All-World), SXR8.DE (S&P 500), EXSA.DE (STOXX Europe 600), XDEV.DE
  (World Value), IUSN.DE (World Small Cap). Check a candidate with get_eodhd_data (fundamentals or eod) before naming
  it, and quantify diversification with `portfolio_risk_xray` on the holdings plus the candidate.
- Write the whole report in Danish even though the task template is English. Dates DD/MM/YYYY, amounts in SEK (local
  currency in brackets when useful). Institutional sections that do not exist for this investor (Northbound flows,
  margin balances, dragon-tiger boards, CNY/HKD, broker blotters, 13F data) are replaced by one line saying so.
"""
# Agenter med disse værktøjer henter kurser og får symbol-instruksen.
SYMBOL_TOOLS = {"get_market_data", "portfolio_risk_xray", "backtest", "technical_indicators", "quantlib_call", "etf_holdings",
                "get_eodhd_data"}
# Instrukser pr. preset → agent-id ("*" = alle agenter i presettet). Rettet efter kørslen 19/10/2026, hvor
# optionsstrategier, Northbound-flow, mæglerblotters og en fuld Brinson-attribution blev efterspurgt af præsets
# skrevet til institutionelle/kinesiske forhold.
EXTRA_PROMPT = {
    "risk_committee": {"*": """

## Vibetrad risk brief
The audit target is the investor's whole portfolio (facit table). Historical VaR/ES 95/99 % come from
`portfolio_risk_xray` (label them statistically weak when the window is short). Always compute the four loss scenarios
in SEK with `financial_rigor` on the facit values: 1) largest position -30 %, 2) largest position -50 %, 3) USD/SEK
-10 %, 4) US tech -25 %. For a stock listed under a year, replace historical crisis windows with ACWI.US or a long-listed
holding as proxy and say so. Protection ideas are weights, UCITS funds and the monthly deposit - never options or futures.
"""},
    "earnings_research_desk": {
        "event_options_analyst": """

## Vibetrad
Report the implied move (%) and implied vs realised volatility as information only, translated into SEK on the investor's
position. Propose no option trades: the account cannot hold options. If the option chain is unavailable, say so in one
line and use the realised earnings-day moves instead.
""",
        "earnings_strategist": """

## Vibetrad
The recommendation is one of: hold through the report, trim before, add after - sized in SEK and in months of the monthly
deposit, with the investor's weight target in mind. No option structures.
"""},
    "sentiment_intelligence_team": {"flow_analyst": """

## Vibetrad
Northbound/Stock Connect, margin balances and dragon-tiger boards do not exist for US and Nordic stocks: say so in one
line. Use `get_eodhd_data` (news sentiment) and `technical_indicators` (20-bar volume ratio, price/volume divergence)
instead, at most 5 tool calls.
"""},
    "macro_rates_fx_desk": {"*": """

## Vibetrad
The investor's base currency is SEK with mostly USD assets: replace China/CNY/HKD sections with the Riksbank, the Fed,
USD/SEK and EUR/SEK. FX exposure can only be changed through weights, diversification or currency-hedged UCITS share
classes, never derivatives.
"""},
    "macro_strategy_forum": {
        "domestic_economist": """

## Vibetrad
The investor's "domestic" economy is Sweden inside the EU, not China: replace PBOC/property-cycle sections with the
Riksbank, the ECB, Swedish inflation (CPIF), SEK (USD/SEK, EUR/SEK) and the Nordic cycle. Use `get_eodhd_data`
(economic calendar, SE10Y.GBOND/DE10Y.GBOND, FX) and `get_macro_series` (FRED) with at most 6 tool calls. Never mention
A-shares, Stock Connect or CNY.
""",
        "*": """

## Vibetrad
Horizon is strategic (years), not monthly tactics: the deliverable is which asset classes, regions, factors and sectors a
long-only equity investor in SEK should overweight for the horizon in the goal, with the two or three scenarios that
would change that view. No derivatives, no leverage, no China A-share framing.
"""},
    "sector_rotation_team": {
        "flow_analyst": """

## Vibetrad
Northbound/Stock Connect, main-force and margin data do not exist for US, European and Nordic markets: say so in one
line and use `get_eodhd_data` (sector ETF prices and news sentiment) and `etf_holdings` on US sector ETFs as proxies
(XLK, XLV, XLF, XLI, XLE, XLY, SMH, IWM as look-through only). At most 6 tool calls.
""",
        "rotation_strategist": """

## Vibetrad
The investor rebalances a few times a year, never monthly: deliver strategic sector and theme tilts for the horizon in
the goal with UCITS ETFs buyable at Nordnet as the vehicle (US tickers only as proxies), and a short backtest only on
US proxies (never Nordic/European symbols in `backtest`).
"""},
    "fund_selection_panel": {"*": """

## Vibetrad
Ignore CNY AUM thresholds and Wind/Tushare data: candidates are UCITS ETFs and index funds buyable at Nordnet (see
"Investor constraints"), checked with `get_eodhd_data` and `portfolio_risk_xray`. No backtest is required; a
comparison of volatility, drawdown and correlation to the existing holdings is the deliverable.
"""},
    "etf_allocation_desk": {"*": """

## Vibetrad
Every ETF you recommend must be a UCITS ETF buyable at Nordnet (European listing, see "Investor constraints"). US-listed
tickers may be used as look-through proxies in `etf_holdings` (bare ticker) only.
"""},
    "portfolio_review_board": {
        "attribution_analyst": """

## Vibetrad
Skip Brinson sector attribution and factor models: no benchmark sector series exist here. Compute each holding's weekly
contribution in SEK and in percentage points from the facit table (weekly returns and weights) plus the FX contribution,
compared with ACWI.US. At most 3 tool calls. Deliver the numbers, never a plan.
""",
        "execution_analyst": """

## Vibetrad
No broker blotter exists: write "ikke vurderbart" for realised VWAP/slippage in five lines at most. Then check any open
order in the portfolio text (limit price vs the facit price, distance in %, earnings date before a likely fill) and
give practical execution rules for a retail account (limit orders, avoid earnings days). At most 2 tool calls.
""",
        "chief_investment_officer": """

## Faktatjek før aflevering (Vibetrad)
Before you finish, audit your own report: run `report_audit` with command='extract' on your draft, verify at most five
sampled prices/weights against the facit table (no tool call needed) or with `financial_rigor` (arithmetic), then run
command='verdict'. End the report with a line "Faktatjek: PASS" or "Faktatjek: FAIL – <what was wrong and how you
corrected it>". Never present an inference (e.g. whether an order 'should have filled') as a fact; label it as an
inference and say what would confirm it. Compare your prices, weights and dates with the "Vibetrad datatjek" table in
your instructions (cross-checked between Vibe-Trading and EODHD) and add a short section "Datakvalitet" saying which
data gaps or disagreements matter.

## Syntese af ugens teams (Vibetrad)
The section "Konklusioner fra ugens øvrige Vibe-Trading-teams" in your instructions (or in your goal) lists the other
teams' conclusions. Use all of them:
- Add a table "Uenigheder mellem teams" (team | deres anbefaling | din beslutning | hvorfor) for every point where a team
  recommends something different from you (selling vs holding, timing, target weights, an order). Write "Ingen" only if
  no team disagrees. Never write that all teams agree without checking each team's recommendation.
- Reproduce ALL FOUR of the risk committee's loss scenarios in SEK, one row each, including USD/SEK -10 % (recompute
  from the facit values if the committee's number is missing), and use them in the risk budget.
- For every main decision add one line "Jeg tager fejl, hvis …" with a measurable condition.
- Transition plan: use the monthly deposit in the portfolio text and the "Indskud" line in the datatjek facit. For each
  step state how many months of deposits it takes at unchanged prices and whether it needs any sale.
- If a team or agent delivered no report, say so in "Datakvalitet" instead of relying on its conclusions. This includes
  the analysts in your own team: if a teammate's output is only a plan or empty, name it. Never write that there are no
  gaps when one exists.
- Name the teams exactly as they appear in your goal and count them correctly.
- Fund and ETF suggestions must be buyable at Nordnet (see "Investor constraints"): name UCITS funds/ETFs, and treat
  US-listed tickers only as proxies.
"""}}

# Rækkefølge i rapporten og som input til CIO'en (CIO kører altid sidst).
CORE = ("risk_committee", "portfolio_review_board")
# Shell og filer fjernes: uden write_file/read_file/edit_file viser Vibe-Trading sine instrukser for "agent uden
# kode" (ingen "write ONE Python script", intet report.md), og read_url fjernes, fordi en agent med hele
# porteføljen i sin instruks ellers kunne sendes til en vilkårlig adresse af en plantet tekst i en nyhed
# (dataudsivning). Nyheder og kurser hentes stadig via get_eodhd_data/get_stock_news, som kun kalder faste værter.
SHELL_TOOLS = {"bash", "background_run", "cancel_background", "write_file", "read_file", "edit_file", "read_url"}
# Værktøjer, der ikke giver mening for en ISK hos Nordnet (optioner, kinesisk margin/flow, handelsjournal uden fil).
DROP_TOOLS = {"get_options_chain", "options_pricing", "options_payoff", "get_fund_flow", "get_margin_trading",
              "get_research_reports", "analyze_trade_journal"}
# Skills, der handler om kinesiske/krypto-datakilder, options eller Python-opskrifter, agenterne ikke kan køre.
DROP_SKILLS = {"tushare", "yfinance", "okx-market", "options-strategy", "options-advanced", "hedging-strategy",
               "social-media-intelligence", "strategy-generate", "minute-analysis"}
QUANT_TOOLS = ["quantlib_call", "portfolio_risk_xray", "financial_rigor", "technical_indicators", "get_market_data"]
NO_SHELL_GUIDANCE = """

## Execution environment (Vibetrad)
Shell and file tools are DISABLED here: never try to write or run Python or bash scripts, never write report.md, and
never answer with a plan to compute something later. Do every calculation with the built-in tools:
- `portfolio_risk_xray` - portfolio volatility, max drawdown, historical VaR / expected shortfall, correlation, beta,
  concentration (pass symbols and weights; it fetches the prices itself).
- `technical_indicators` (fetches prices itself), `financial_rigor` (exact arithmetic, bull/base/bear targets) and
  `get_market_data` / `get_eodhd_data` for prices and fundamentals.
- `quantlib_call` fetches nothing and needs typed inputs: use it only for small inputs (parametric VaR from the x-ray's
  volatility, Monte Carlo, DCF/comps, Brinson with a few rows). Never retype a daily return series into it, and do not
  spend calls on action='list'/'describe' unless you really need a function you have not used before.
Budget: the "HARD LIMIT of 20 tool calls" is not enforced, but context is: each tool result is cut at 10 000 characters
and only the most recent results stay fully in context. Plan 8-15 calls, fetch one symbol per call with the shortest
window that answers the question, and note the numbers you need as soon as they arrive. A report with fewer numbers
beats no report: never end with a plan or a promise.
Report the numbers you computed, the inputs and the data window. If a figure truly cannot be computed, say exactly why
and continue with what you have.
Call `load_skill` only for skills in your own allowlist (listed in your skill descriptions); if your role text names
another skill, skip it and continue. A synthesis from upstream reports and the verified facts needs no tool calls;
a figure you compute yourself does.
"""

DEFAULT_GOAL = (
    "Gør porteføljen mere robust: maks. 10 % pr. enkeltaktie, mindre korrelation "
    "mellem positionerne og ingen unødige handler. Afslut med konkrete målvægte "
    "og hvad nye indskud bør gå til."
)
# weight_policy "target" (Kaspers valg 16/10/2026): 10 % er det langsigtede mål, som nås gradvist.
TRANSITION_GOAL = (
    "Gør porteføljen mere robust. Langsigtet mål: højst 10 % pr. enkeltaktie, men nå det gradvist: "
    "foreslå en overgangsplan med trin og tidshorisont (fx nye indskud til en bred kerne og trinvise "
    "justeringer) i stedet for at sælge ned til 10 % med det samme. Mindre korrelation mellem positionerne "
    "og ingen unødige handler. Afslut med målvægte for hvert trin, hvad nye indskud bør gå til, hvor mange "
    "måneders indskud hvert trin kræver ved uændrede kurser, og hvilke begivenheder (regnskaber, lock-up-udløb, "
    "store kursudsving) der bør udløse en revurdering."
)
WEIGHT_POLICIES = ("limit", "target")
# Strategikørsel (Kaspers valg 20/10/2026): alt er solgt, og agenterne skal bygge porteføljen op fra kontant.
STRATEGY_GOAL = (
    "Byg én samlet investeringsstrategi fra bunden: alt står kontant. Mandatet (horisont, tålt fald, univers, loft pr. "
    "aktie) står i porteføljeteksten og skal overholdes. Lever: 1) målallokering i procent pr. byggesten (kerne, "
    "satellitter, evt. kontant) med ét konkret instrument pr. byggesten, der kan købes hos Nordnet på en svensk ISK "
    "(UCITS-fonde/ETF'er med ISIN eller navn; enkeltaktier med ticker), 2) hvad der købes for kontanten nu, og hvad der "
    "venter – med begrundelse, hvis noget skal spredes over flere måneder, 3) hvordan hvert månedligt indskud fordeles, "
    "4) forventet afkastinterval og største realistiske fald for hele porteføljen over horisonten (med historiske "
    "kriser som reference – ingen løfter), 5) regler: rebalancering, hvornår en satellit sælges, og hvad der får dig til "
    "at ændre strategien, 6) en tabel 'Uenigheder mellem teams'. Ingen gearing, ingen derivater, ingen produkter, en "
    "privatkunde ikke kan købe hos Nordnet. Foretræk instrumenter med en børsticker (ETF'er og aktier), så de kan følges "
    "dagligt; en fond uden børsticker skal have ISIN og en børsnoteret ETF som kursproxy. Analytikerne leverer hver "
    "deres del kort til CIO'en; kun CIO'en skriver den endelige målportefølje."
)
# Kun til CIO'en (system-prompt). 20/10/2026 fik alle fire agenter i porteføljegennemgangen målportefølje-kravet via
# {goal}: tre forskellige tabeller, og CIO'ens egen blev aldrig skrevet, fordi rapporten ramte loftet for svarlængde
# midt i "Datakvalitet" ("hentet direkte fra E"). Tabellen står derfor nu øverst.
TARGET_TABLE_HEAD = "| Instrument | Ticker | ISIN | Type | Målvægt % | Køb fra | Kursproxy |"
# Kun til CIO'en: kort resumé og en beslutningstabel øverst, som Vibetrad viser på siden "I dag" med knapper
# (Handelstjek, Hold, Udsæt). Kasper skal kunne handle på konklusionen uden at læse hele rapporten (20/10/2026).
DECISION_TABLE_HEAD = "| Handling | Papir | Beløb SEK | Hvorfor | Hvornår | Jeg tager fejl, hvis |"
DECISION_RULES = (
    "Handling er én af: køb, øg, reducér, sælg, hold, afvent, revurdér. Papir er tickeren (SYMBOL.BØRS). Beløb SEK er et "
    "tal uden mellemrum eller tomt. Hvorfor er én kort sætning på almindeligt dansk med det tal, der afgør det. Hvornår er "
    "DD/MM/YYYY eller 'nu'. Jeg tager fejl, hvis er en målbar betingelse. Højst 6 rækker og kun det, investoren skal tage "
    "stilling til før næste kørsel; er der intet, så én række: | hold | – | | Ingen handling nødvendig | | |. Investoren er "
    "ikke aktieekspert: skriv uden fagord, eller forklar dem kort i parentes (fx 'under trendlinjen (200-dages gennemsnit)')."
)
CIO_DECISION_EXTRA = (
    "## Rapportens start (Vibetrad)\n"
    "Din rapport har et loft for svarlængde; skriv det vigtigste først. Start med '## Resumé' (højst 5 linjer, almindeligt "
    "dansk, ingen fagudtryk uden forklaring) og straks derefter '## Beslutninger til Kasper' med præcis denne tabel – "
    "Vibetrad læser den automatisk og viser hver række som en beslutning med knapper:\n"
    + DECISION_TABLE_HEAD + "\n|---|---|---|---|---|---|\n" + DECISION_RULES + "\nDerefter resten af rapporten."
)
STRATEGY_CIO_EXTRA = (
    "## Strategikørsel (Vibetrad) – din rapport\n"
    "Din rapport har et loft for svarlængde; tekst efter loftet går tabt. Byg den derfor i denne rækkefølge:\n"
    "1. '## Resumé' på højst 6 linjer.\n"
    "2. Straks derefter '## Målportefølje' med præcis denne tabel – Vibetrad læser den automatisk:\n"
    + TARGET_TABLE_HEAD + "\n|---|---|---|---|---|---|---|\n"
    "Én række pr. instrument (også kontant, hvis noget skal stå kontant). Ticker i formatet SYMBOL.BØRS "
    "(fx NVDA.US, NOVO-B.CO, VWCE.XETRA, IWDA.AS); tom for en fond uden børsticker. Type er aktie, etf, fond eller kontant. "
    "Målvægt % er et tal uden %-tegn, og vægtene summerer til 100. Køb fra er DD/MM/YYYY for en position, der først skal "
    "købes senere (fx dagen efter et regnskab), ellers tom. Kursproxy er kun udfyldt for fonde uden ticker.\n"
    "3. Straks derefter '## Beslutninger til Kasper' med præcis denne tabel (Vibetrad viser hver række som en beslutning):\n"
    + DECISION_TABLE_HEAD + "\n|---|---|---|---|---|---|\n" + DECISION_RULES + "\n"
    "4. Derefter resten. Hold hele rapporten under ca. 3 000 ord: tabeller frem for gentagelser, og gentag ikke teamenes "
    "tal, som du ikke bruger.\n"
    "Køb nu: regn i hele stk (ETF'er og aktier handles i hele stk hos Nordnet). Angiv antal, beløb i SEK og den vægt, "
    "det faktisk giver, og læg kurtage og valutaveksling til efter kurtageklassen i porteføljeteksten.\n"
    "Stresstest på din endelige vægt: brug for hver enkeltaktie dens egen målte fald i samme kriseperiode (hent data), "
    "ikke indeksets fald gange aktieandelen, og sig, om resultatet bryder mandatets tålte fald.\n"
    "Geografi: oplys den samlede vægt i Norden og i Europa inkl. kernefondens andel, og sig om det er en bevidst overvægt."
)
STRATEGY_ANALYST_EXTRA = (
    "## Strategikørsel (Vibetrad)\n"
    "I strategikørslen gælder dette i stedet for afsnittet 'Vibetrad' ovenfor. Alt står kontant. Kun CIO'en får de øvrige "
    "teams' konklusioner og skriver den endelige målportefølje: lav ikke din egen tabel 'Målportefølje', og skriv ikke, "
    "at teamkonklusionerne mangler. Lever din del til CIO'en på højst ca. 1 500 ord: {task}"
)
STRATEGY_ANALYST_TASKS = {
    "attribution_analyst": "dit bud på målallokeringen pr. byggesten med ét Nordnet-købbart instrument hver og begrundelsen "
                           "(hvilke byggesten der bærer væksten, og hvad der er overlap).",
    "risk_inspector": "risikotjek af den portefølje, teamenes forslag peger på: koncentration, korrelation mellem byggestenene "
                      "og stress, hvor hver enkeltaktie regnes med sit eget historiske kursfald.",
    "execution_analyst": "købsplanen: hele stk, beløb i SEK, kurtage og valutaveksling efter kurtageklassen i porteføljeteksten, "
                         "ordretype og rækkefølge, og hvad der bør vente på et regnskab.",
}


LANG_SUFFIX = (
    " Skriv hele rapporten på dansk. Brug datoformat DD/MM/YYYY og angiv beløb i SEK, "
    "med lokal valuta i parentes hvor relevant. Dette er research, ikke personlig rådgivning, "
    "og der må ikke afgives ordrer."
)


# ---------------------------------------------------------------------------
# Output-hygiejne
# ---------------------------------------------------------------------------
class Console:
    """Skriver status-linjer til den oprindelige stdout, også når fd 1/2 er omdirigeret."""

    def __init__(self) -> None:
        self._fd = os.dup(1)

    def say(self, msg: str) -> None:
        os.write(self._fd, (msg.rstrip() + "\n").encode("utf-8", "replace"))


@contextlib.contextmanager
def quiet_fds(log_path: Path):
    """Omdiriger alt på fd 1 og 2 (også fra tråde og C-kode) til log_path."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(1), os.dup(2)
    with open(log_path, "ab", buffering=0) as fh:
        os.dup2(fh.fileno(), 1)
        os.dup2(fh.fileno(), 2)
        try:
            yield
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            os.close(saved[0])
            os.close(saved[1])


def silence_rest(log_path: Path) -> None:
    """Send fd 1 og 2 til log_path resten af processen.

    Swarm-tråde, der fortsætter efter en tidsgrænse, kan ellers skrive til den offentlige
    GitHub-log, når quiet_fds er forladt. Console skriver stadig til den oprindelige stdout.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    fd = os.open(str(log_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)


# ---------------------------------------------------------------------------
# Tid og uge
# ---------------------------------------------------------------------------
def now_sto(override: str | None = None) -> dt.datetime:
    if override:
        t = dt.datetime.fromisoformat(override)
        return t.astimezone(STO) if t.tzinfo else t.replace(tzinfo=STO)
    return dt.datetime.now(STO)


def dmy(d: dt.date) -> str:
    return d.strftime("%d/%m/%Y")


def last_us_close(ref: dt.datetime) -> dt.date:
    """Seneste amerikanske handelsdag med en afsluttet lukkekurs (NYSE lukker 22:00 svensk tid)."""
    d = ref.date()
    if ref.weekday() >= 5 or ref.hour < 22:
        d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def week_window(ref: dt.datetime) -> dict:
    """Den aktuelle ISO-uge set fra ref, med de handelsdage der er afsluttet.

    Lørdag/søndag: hele ugen mandag–fredag. Tirsdag–fredag: mandag til seneste lukkekurs
    (ugen til dato). Mandag før lukning har ugen ingen handelsdage endnu, så forrige uge bruges.
    """
    end = last_us_close(ref)
    monday = end - dt.timedelta(days=end.weekday())
    friday = monday + dt.timedelta(days=4)
    iso = monday.isocalendar()
    full = end >= friday
    label = f"Uge {iso.week} · {dmy(monday)}–{dmy(end)}" + ("" if full else " (ugen til dato)")
    return {"id": f"{iso.year}-W{iso.week:02d}", "from": monday.isoformat(), "to": end.isoformat(),
            "full": full, "label": label}


def should_run(ref: dt.datetime, latest_meta: dict | None, event: str, force: bool, canary: bool = False) -> tuple[bool, str]:
    week = week_window(ref)
    if canary:  # den daglige tørkørsel kører altid, uanset ugedag og VT_SCHEDULE; den rører ikke ugerapporterne
        return True, "kanariefugl (daglig gratis tørkørsel af hele kæden)"
    if force or event == "workflow_dispatch":
        return True, "manuel kørsel"
    if event == "schedule" and os.environ.get("VT_SCHEDULE", "off").strip().lower() != "on":
        return False, "automatisk lørdagskørsel er slået fra (repo-variablen VT_SCHEDULE er ikke 'on') – kør på knaptryk"
    if ref.weekday() != 5:
        return False, "ikke lørdag i Stockholm"
    if ref.hour < 8:
        return False, f"kun {ref:%H:%M} i Stockholm – venter til 08:00"
    if (latest_meta and latest_meta.get("week_id") == week["id"] and latest_meta.get("status") in ("ok", "partial")
            and str(latest_meta.get("week_to") or "") >= week["to"] and not latest_meta.get("dry_run")):
        return False, f"{week['id']} er allerede kørt for hele ugen"
    return True, "planlagt lørdagskørsel"


# ---------------------------------------------------------------------------
# Portefølje
# ---------------------------------------------------------------------------
class PortfolioError(ValueError):
    pass


NEXT_REPORT_SOURCES = ("eodhd", "manuel", "brief")


def load_portfolio(raw: str | None) -> dict:
    if not raw or not raw.strip():
        raise PortfolioError("VT_PORTFOLIO mangler – opret secret'en med JSON fra Vibetrad-siden")
    try:
        p = json.loads(raw)
    except json.JSONDecodeError as exc:  # beskeden må ikke citere indholdet
        raise PortfolioError(f"VT_PORTFOLIO er ikke gyldig JSON (linje {exc.lineno}, kolonne {exc.colno})") from None
    if not isinstance(p, dict) or not isinstance(p.get("holdings"), list):
        raise PortfolioError("VT_PORTFOLIO skal have en liste 'holdings'")
    if not p["holdings"] and not (isinstance(p.get("cash_sek"), (int, float)) and p["cash_sek"] > 0):
        raise PortfolioError("VT_PORTFOLIO skal have en ikke-tom liste 'holdings' eller kontant (cash_sek) at investere")
    try:
        return _clean_portfolio(p)
    except PortfolioError:
        raise
    except (TypeError, ValueError, AttributeError, KeyError) as exc:  # aldrig selve værdien i beskeden
        raise PortfolioError(f"VT_PORTFOLIO har et felt med forkert type ({type(exc).__name__}); ret det i Vibetrad "
                             "og kopiér VT_PORTFOLIO igen") from None


def _num(value: Any, field: str, *, default: float | None = None, allow_none: bool = False) -> float | None:
    """Tal fra VT_PORTFOLIO. Tekst med mellemrum/komma accepteres ("2 615", "10,5"); alt andet er en fejl uden værdi."""
    if value is None or isinstance(value, bool):
        if allow_none or value is None:
            return default
        raise PortfolioError(f"{field} skal være et tal")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d[\d \u00a0]*(?:[.,]\d+)?\s*", value):
        return float(re.sub(r"[ \u00a0]", "", value).replace(",", "."))
    raise PortfolioError(f"{field} skal være et tal")


def _clean_portfolio(p: dict) -> dict:
    clean = []
    for i, h in enumerate(p["holdings"]):
        if not isinstance(h, dict) or not str(h.get("ticker") or "").strip():
            raise PortfolioError(f"holdings[{i}] mangler 'ticker'")
        qty = _num(h.get("qty"), f"holdings[{i}].qty", allow_none=True)
        ticker = dtj.norm_ticker(h["ticker"])  # bare amerikanske tickers får .US, så datatjek og plan taler om det samme
        clean.append({
            "ticker": ticker,
            "name": str(h.get("name") or h["ticker"]).strip()[:120],
            "qty": qty,
            "cost_sek": _num(h.get("cost_sek"), f"holdings[{i}].cost_sek", allow_none=True),
            "max_weight": _num(h.get("max_weight"), f"holdings[{i}].max_weight", default=10.0, allow_none=True) or 10.0,
            "kind": str(h.get("kind") or "stock").strip().lower()[:20],
        })
    p["holdings"] = clean
    policy = str(p.get("weight_policy") or "limit").strip().lower()
    p["weight_policy"] = policy if policy in WEIGHT_POLICIES else "limit"
    p["account"] = str(p.get("account") or "ISK Nordnet").strip()[:60]
    p["currency"] = str(p.get("currency") or "SEK").strip().upper()[:3] or "SEK"
    for key in ("goal", "framework", "cash_note", "as_of"):
        if p.get(key) is not None and not isinstance(p[key], str):
            p[key] = str(p[key])
    wl = p.get("watchlist")
    p["watchlist"] = [dict(w, ticker=dtj.norm_ticker(w.get("ticker"))) for w in wl
                      if isinstance(w, dict) and str(w.get("ticker") or "").strip()] if isinstance(wl, list) else []
    nr = p.get("next_reports")
    p["next_reports"] = ({dtj.norm_ticker(k): str(v) for k, v in nr.items() if str(k or "").strip() and v}
                         if isinstance(nr, dict) else {})
    p["cash_sek"] = _num(p.get("cash_sek"), "cash_sek", default=0.0, allow_none=True) or 0.0
    reserved = p.get("cash_reserved_sek")
    if isinstance(reserved, str) and re.fullmatch(r"\s*\d+(?:[.,]\d+)?\s*", reserved):
        reserved = float(reserved.replace(",", "."))
    if not isinstance(reserved, (int, float)) or isinstance(reserved, bool):
        reserved = reserved_from_note(p.get("cash_note"))
    p["cash_reserved_sek"] = round(min(max(float(reserved or 0), 0.0), p["cash_sek"]), 2)
    dep = p.get("monthly_deposit_sek")
    p["monthly_deposit_sek"] = float(dep) if isinstance(dep, (int, float)) and not isinstance(dep, bool) and 0 < dep < 10_000_000 else None
    cc = re.sub(r"[^\w .,/()+-]", "", str(p.get("courtage_class") or "")).strip()[:40]
    p["courtage_class"] = cc or None
    # Kurtagesatserne fra Vibetrad (Nordnets prisliste for kontoens klasse); kun tekst, ingen tal i loggen.
    ct = re.sub(r"[^\w .,/()+%:;=–-]", "", str(p.get("courtage_terms") or "")).strip()[:300]
    p["courtage_terms"] = ct or None
    # Målporteføljen fra strategikørslen (Vibetrad → Plan): trendvagten og agenterne kender også de papirer,
    # der endnu ikke er købt, og hvornår strategien vil købe dem.
    tg = p.get("target")
    clean_tg = []
    for g in (tg if isinstance(tg, list) else [])[:20]:
        if not isinstance(g, dict):
            continue
        typ = str(g.get("type") or "").strip().lower()
        typ = typ if typ in ("etf", "aktie", "fond", "kontant") else "etf"
        tk = dtj.norm_ticker(g.get("ticker")) if str(g.get("ticker") or "").strip() and typ != "kontant" else None
        wait = str(g.get("wait") or "")[:10]
        clean_tg.append({"name": re.sub(r"\s+", " ", str(g.get("name") or tk or typ)).strip()[:80], "ticker": tk, "type": typ,
                         "weight": _num(g.get("weight"), "target.weight", default=0.0, allow_none=True) or 0.0,
                         "wait": wait if re.fullmatch(r"\d{4}-\d{2}-\d{2}", wait) else None})
    p["target"] = clean_tg
    # Hvor hver regnskabsdato i next_reports kommer fra (Vibetrad fra 19/10/2026): "eodhd" = hentet fra EODHD,
    # "manuel" = skrevet ind af Kasper, "brief" = Vibetrads kalender. En kopi af EODHD er ikke en egen kilde.
    src = p.get("next_reports_source")
    p["next_reports_source"] = ({dtj.norm_ticker(k): str(v).strip().lower() for k, v in src.items()
                                 if str(v).strip().lower() in NEXT_REPORT_SOURCES} if isinstance(src, dict) else {})
    # Journal (Vibetrad → Journal/Handelstjek): teser og kill-kriterier, så CIO'en kan følge op efter handlen.
    jr = p.get("journal")
    clean_jr = []
    for j in (jr if isinstance(jr, list) else []):
        if len(clean_jr) >= 40:
            break
        if not isinstance(j, dict) or not str(j.get("ticker") or "").strip():
            continue
        txt = lambda k, n: re.sub(r"\s+", " ", str(j.get(k) or "")).strip()[:n] or None  # noqa: E731
        date = str(j.get("date") or "")[:10]
        clean_jr.append({"ticker": dtj.norm_ticker(j.get("ticker")), "date": date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else None,
                         "action": txt("action", 20), "thesis": txt("thesis", 300), "kill": txt("kill", 300),
                         "review": (str(j.get("review") or "")[:10] or None), "status": txt("status", 20),
                         "amount_sek": _num(j.get("amount_sek"), "journal.amount_sek", default=None, allow_none=True)})
    p["journal"] = clean_jr
    # Regler fra Vibetrad → Plan (exit, loft, rebalancering): tekst, som agenterne skal respektere.
    rules = p.get("rules")
    p["rules"] = re.sub(r"\s+", " ", str(rules)).strip()[:1500] if isinstance(rules, str) and rules.strip() else None
    # Mandat (Vibetrad → Plan): horisont, tålt fald, univers og loft pr. aktie – styrer strategikørslen.
    m = p.get("mandate")
    if isinstance(m, dict):
        uni = str(m.get("universe") or "").strip().lower()
        p["mandate"] = {
            "horizon_years": int(min(max(_num(m.get("horizon_years"), "mandate.horizon_years", default=5.0, allow_none=True) or 5.0, 1), 40)),
            "max_drawdown_pct": int(min(max(_num(m.get("max_drawdown_pct"), "mandate.max_drawdown_pct", default=30.0, allow_none=True) or 30.0, 5), 90)),
            "universe": uni if uni in MANDATE_UNIVERSES else "kerne+satellit",
            "single_stock_cap_pct": int(min(max(_num(m.get("single_stock_cap_pct"), "mandate.single_stock_cap_pct", default=10.0, allow_none=True) or 10.0, 1), 100)),
            "objective": re.sub(r"\s+", " ", str(m.get("objective") or "")).strip()[:300] or None,
        }
    else:
        p["mandate"] = None
    return p


MANDATE_UNIVERSES = {
    "kerne+satellit": "UCITS-fonde/ETF'er som kerne og enkeltaktier som satellitter inden for loftet pr. aktie",
    "kun fonde": "kun UCITS-fonde og -ETF'er, ingen enkeltaktier",
    "frit": "frit: fonde, ETF'er og enkeltaktier, også større positioner inden for loftet pr. aktie",
}


def mandate_text(p: dict) -> str:
    m = p.get("mandate")
    if not m:
        return ""
    return (f"Mandat: horisont {m['horizon_years']} år; investoren tåler et midlertidigt fald på op til {m['max_drawdown_pct']} % "
            f"af porteføljen uden at sælge; univers: {MANDATE_UNIVERSES[m['universe']]}; loft {m['single_stock_cap_pct']} % "
            f"pr. enkeltaktie" + (f"; mål: {m['objective']}" if m.get("objective") else "") + ".")


_AMOUNT = r"(?P<amt>(?:\d{1,3}(?:[ .\u00a0\u202f]\d{3})+|\d+)(?:[,.]\d{1,2})?)(?!\d)"
_HELD = r"(?:reserv\w*|bundet|bunden|øremærket|öronmärkt|afsat|avsatt|spærret|spärrat)"
_RESERVED_PATTERNS = (
    # "Heraf 2 400 SEK reserveret til åben limitordre", "2.400 kr. er bundet i ordren"
    re.compile(_AMOUNT + r"\s*(?:SEK|kr\b\.?)(?P<gap>[^\d.;,]{0,30}?)\b(?P<held>" + _HELD + ")", re.I),
    # "Reserveret til ordre: 2 400 SEK", "bundet ca. 2 400 SEK"
    re.compile(r"\b(?P<held>" + _HELD + r")(?P<gap>[^\d.;,]{0,30}?(?:ca\.\s*)?)" + _AMOUNT + r"\s*(?:SEK|kr\b)", re.I),
)
# "ikke længere reserveret", "Intet reserveret", "inte spärrat": ingen reservation
_NOT_HELD = re.compile(r"\b(?:ikke|intet|ingen|inte|ej|not|no|længere|längre|ophævet|frigivet|annulleret)\b", re.I)


def _amount(raw: str) -> float:
    s = re.sub(r"[ \u00a0\u202f]", "", raw)
    dec = re.search(r"[,.](\d{1,2})$", s)
    whole = s[:dec.start()] if dec else s
    return float(re.sub(r"[.,]", "", whole) + ("." + dec.group(1) if dec else ""))


def reserved_from_note(note: object) -> float:
    """Beløb, som kontantnoten siger er reserveret (fx 'Heraf 2 400 SEK reserveret til åben limitordre').

    Kun SEK/kr tæller; beløb i andre valutaer (fx limitkursen i DKK) ignoreres. En nægtelse lige før eller
    mellem beløb og ordet ("ikke længere reserveret", "Intet reserveret") tæller ikke. Samme beløb nævnt
    flere gange tælles én gang.
    """
    text = str(note or "")
    found: dict[tuple[int, int], float] = {}
    for rx in _RESERVED_PATTERNS:
        for m in rx.finditer(text):
            before = text[max(0, m.start("held") - 20):m.start("held")]
            if _NOT_HELD.search(m.group("gap")) or _NOT_HELD.search(before):
                continue
            found[m.span("amt")] = _amount(m.group("amt"))
    return float(sum(set(found.values())))


def free_cash(p: dict) -> float:
    return max(float(p.get("cash_sek") or 0) - float(p.get("cash_reserved_sek") or 0), 0.0)


def cost_weights(p: dict) -> list[dict]:
    total = sum(h["cost_sek"] or 0 for h in p["holdings"]) + (p.get("cash_sek") or 0)
    out = []
    for h in p["holdings"]:
        w = (h["cost_sek"] or 0) / total * 100 if total else None
        out.append({"ticker": h["ticker"], "name": h["name"], "weight_cost_pct": round(w, 1) if w is not None else None,
                    "max_weight": h["max_weight"], "over_max": bool(w is not None and w > h["max_weight"])})
    return out


def portfolio_text(p: dict) -> str:
    lines = [f"Konto: {p['account']} (svensk ISK, basisvaluta {p['currency']}). Opgjort {p.get('as_of') or 'seneste'}."]
    if not p["holdings"]:
        lines.append("Ingen værdipapirer – alt står kontant og skal investeres efter mandatet.")
    for h in p["holdings"]:
        bits = [f"{h['name']} ({h['ticker']})"]
        if h["qty"] is not None:
            bits.append(f"{h['qty']:g} stk")
        if h["cost_sek"]:
            bits.append(f"anskaffelse {h['cost_sek']:,.0f} SEK".replace(",", " "))
        bits.append(f"langsigtet mål {h['max_weight']:g} %" if p.get("weight_policy") == "target"
                    else f"maks. vægt {h['max_weight']:g} %")
        lines.append("- " + ", ".join(bits))
    if p.get("cash_sek"):
        note = f" ({p['cash_note']})" if p.get("cash_note") else ""
        # Kun beløbet får mellemrum som tusindtalsskilletegn; noten beholder sine kommaer (fx 98,40 DKK)
        line = f"- Kontant: {p['cash_sek']:,.0f}".replace(",", " ") + f" SEK{note}"
        if p.get("cash_reserved_sek"):
            line += (f". Ledig til nye køb: {free_cash(p):,.0f} SEK ({p['cash_reserved_sek']:,.0f} SEK er reserveret "
                     "og kan ikke bruges)").replace(",", " ")
        lines.append(line)
    if p.get("watchlist"):
        wl = "; ".join(f"{w.get('ticker')}: {w.get('note', '')}".strip(": ") for w in p["watchlist"] if isinstance(w, dict))
        lines.append(f"Overvågning/åbne ordrer: {wl}")
    if p.get("monthly_deposit_sek"):
        lines.append(f"Månedligt indskud: {p['monthly_deposit_sek']:,.0f} SEK. Nye indskud kan sprede porteføljen uden salg; "
                     "regn i måneder ved uændrede kurser.".replace(",", " "))
    else:
        lines.append("Månedligt indskud: ikke oplyst. Antag ikke et beløb; sig, hvad planen kræver pr. måned.")
    broker = f"Nordnet, kurtageklasse {p['courtage_class']}" if p.get("courtage_class") else "Nordnet, kurtageklasse ikke oplyst"
    if p.get("courtage_terms"):
        lines.append(f"Kurtage: {broker}. Satser: {p['courtage_terms']}. Brug dem, når du regner en handel.")
    else:
        lines.append(f"Kurtage: {broker}. Satsen og valutapåslaget kendes ikke her: angiv ingen kurtagesats, "
                     "men skriv 'kurtage og valutaveksling efter din Nordnet-klasse'.")
    if p.get("target"):
        parts = [f"{g['name']} ({g['ticker']}) {g['weight']:g} %" if g.get("ticker") else f"{g['name']} {g['weight']:g} %"
                 for g in p["target"]]
        waits = [f"{g['ticker'] or g['name']} købes tidligst {fr._dmy(g['wait'])}" for g in p["target"] if g.get("wait")]
        lines.append("Målportefølje (fra strategikørslen, godkendt i Vibetrad): " + " · ".join(parts)
                     + (". " + "; ".join(waits) if waits else "") + ".")
    if p.get("framework"):
        lines.append(f"Investeringsramme: {p['framework']}")
    if mandate_text(p):
        lines.append(mandate_text(p))
    if p.get("rules"):
        lines.append(f"Investorens egne regler (Vibetrad → Plan), som alle forslag skal respektere: {p['rules']}")
    if p.get("journal"):
        lines.append("Investorens journal (tese og kill-kriterie pr. beslutning; 'planlagt' = endnu ikke handlet):")
        for j in p["journal"]:
            bits = [f"{fr._dmy(j['date']) if j.get('date') else 'dato ukendt'} {j.get('action') or ''} {j['ticker']}".strip()]
            if j.get("status"):
                bits.append(j["status"])
            if j.get("amount_sek"):
                bits.append(f"{j['amount_sek']:,.0f} SEK".replace(",", " "))
            if j.get("thesis"):
                bits.append(f"tese: {j['thesis']}")
            if j.get("kill"):
                bits.append(f"tager fejl hvis: {j['kill']}")
            if j.get("review"):
                bits.append(f"opfølgning {fr._dmy(j['review'])}")
            lines.append("- " + " · ".join(bits))
    return "\n".join(lines)


def journal_instruction(p: dict) -> str:
    """CIO'en følger op efter handlen: holder tesen, er kill-kriteriet ramt, hvad er næste skridt."""
    if not p.get("journal"):
        return ""
    return ("\n\nOpfølgning efter handel: Investorens journal (se porteføljeteksten) har en tese og et kill-kriterie pr. "
            "beslutning. Medtag et afsnit 'Opfølgning på journalen' med én linje pr. post: holder tesen ud fra ugens data "
            "(ja/delvist/nej), er kill-kriteriet ramt, nær eller langt fra, og hvad der bør ske (hold, revurdér på "
            "opfølgningsdatoen, reducér) med begrundelse i tal. Skeln mellem god beslutning og godt udfald. Poster med "
            "status 'planlagt' vurderes som: bør handlen stadig gennemføres? Poster med status 'papir' er papirhandler "
            "(ingen rigtige penge): regn, hvordan de har klaret sig siden datoen, og hvad processen lærer af dem. "
            "Poster, hvis opfølgningsdato er passeret eller ligger inden for 14 dage, får en egentlig 3-måneders-"
            "gennemgang: holder tesen, og hvad siger trendvagten (200-dages-reglen)?")


# ---------------------------------------------------------------------------
# Plan efter spillebogen
# ---------------------------------------------------------------------------
def yahoo_symbol(ticker: str) -> str:
    t = ticker.upper()
    if t.endswith(".US"):
        return t[:-3]
    return t


def yf_next_report(ticker: str) -> dt.date | None:
    try:
        import yfinance as yf  # afhængighed af vibe-trading-ai

        cal = yf.Ticker(yahoo_symbol(ticker)).calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if dates:
            d = dates[0]
            return d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d)[:10])
    except Exception:
        return None
    return None


def target_holdings(p: dict) -> list[dict]:
    """Målporteføljens papirer (uden kontant) som planlagte positioner – bruges, når der endnu intet er købt."""
    kind = {"aktie": "stock", "etf": "etf", "fond": "fund"}
    return [{"ticker": g["ticker"], "name": g.get("name") or g["ticker"], "kind": kind.get(g.get("type"), "stock"),
             "weight": g.get("weight")} for g in p.get("target") or [] if g.get("ticker") and g.get("type") != "kontant"]


def upcoming_reports(p: dict, ref: dt.datetime, horizon_days: int = 14,
                     lookup: Callable[[str], dt.date | None] | None = yf_next_report,
                     verified: dict[str, str] | None = None,
                     alternatives: dict[str, list[str]] | None = None) -> list[dict]:
    """Regnskaber inden for horisonten. ``verified`` er datatjekkets krydstjekkede datoer og vinder.

    ``alternatives`` er datoer, som en anden kilde giver. Ligger den valgte dato uden for horisonten, men en
    anden kilde siger en dato inden for, tages regnskabet med på den tidligste dato (risiko først), og den
    valgte dato står i ``alt_dates``, så regnskabsteamet ved, at datoen er usikker.
    """
    today = ref.date()
    end = today + dt.timedelta(days=horizon_days)
    found = []
    seen: set[str] = set()
    for h in list(p["holdings"]) + target_holdings(p):  # også målporteføljens aktier, før de er købt
        if h["kind"] in ("cash", "fund", "etf") or h["ticker"] in seen:  # samme papir to gange → ét regnskab
            continue
        seen.add(h["ticker"])
        raw = (p.get("next_reports") or {}).get(h["ticker"])
        d, src = None, None
        if verified and verified.get(h["ticker"]):
            with contextlib.suppress(ValueError):
                d, src = dt.date.fromisoformat(verified[h["ticker"]][:10]), "datatjek"
        if d is None and raw:
            try:
                d, src = dt.date.fromisoformat(str(raw)[:10]), "portefølje"
            except ValueError:
                d = None
        if (d is None or d < today) and lookup:
            d2 = lookup(h["ticker"])
            if d2:
                d, src = d2, "yahoo"
        alts: list[dt.date] = []
        for a in (alternatives or {}).get(h["ticker"], []) if src == "datatjek" else []:
            with contextlib.suppress(ValueError, TypeError):
                alts.append(dt.date.fromisoformat(str(a)[:10]))
        alts = sorted(a for a in set(alts) if a >= today and a != d)
        if d and not today <= d <= end and alts and alts[0] <= end:
            alts, d, src = sorted(set(alts[1:] + [d])), alts[0], "datatjek (tidligste kilde)"
        if d and today <= d <= end:
            row = {"ticker": h["ticker"], "name": h["name"], "date": d.isoformat(), "source": src}
            if alts:
                row["alt_dates"] = [a.isoformat() for a in alts]
            found.append(row)
    return sorted(found, key=lambda x: x["date"])


def market_of(ticker: str) -> str:
    t = ticker.upper()
    return {"CO": "Danmark (Nasdaq Copenhagen)", "ST": "Sverige (Nasdaq Stockholm)", "HE": "Finland",
            "OL": "Norge", "US": "USA"}.get(t.rsplit(".", 1)[-1], "global")


def last_done(history: list[dict] | None, preset: str) -> str:
    """Tidspunkt for seneste rigtige kørsel, hvor preset blev gennemført ('' = aldrig)."""
    return max((str(w.get("generated_at", "")) for w in history or []
                if preset in (w.get("presets") or []) and not w.get("dry_run")), default="")


def visser_names(p: dict, limit: int = 8) -> str:
    """Navnene til Visser-panelet: enkeltaktier i beholdningen, aktier i målporteføljen og på overvågningslisten."""
    w = {x["ticker"]: x["weight_cost_pct"] for x in cost_weights(p)}
    out: list[str] = []
    seen: set[str] = set()
    for h in p["holdings"]:
        if h["kind"] in ("cash", "fund", "etf") or h["ticker"] in seen:
            continue
        seen.add(h["ticker"])
        share = f", {w[h['ticker']]:g} % af anskaffelsen" if w.get(h["ticker"]) is not None else ""
        out.append(f"{h['name']} ({h['ticker']}, beholdning{share})")
    for g in p.get("target") or []:
        if g.get("type") != "aktie" or not g.get("ticker") or g["ticker"] in seen:
            continue
        seen.add(g["ticker"])
        wait = f", købes tidligst {fr._dmy(g['wait'])}" if g.get("wait") else ""
        out.append(f"{g['name']} ({g['ticker']}, målportefølje {g['weight']:g} %{wait})")
    for x in p.get("watchlist") or []:
        t = x.get("ticker") if isinstance(x, dict) else None
        if not t or t in seen or str(x.get("kind") or "stock") in ("etf", "fund"):
            continue
        seen.add(t)
        note = re.sub(r"\s+", " ", str(x.get("note") or "")).strip()[:80]
        out.append(f"{x['name']} ({t}, overvågning" if x.get("name") else f"{t} (overvågning")
        out[-1] += f": {note})" if note else ")"
    return "; ".join(out[:limit])


def build_plan(p: dict, ref: dt.datetime, week: dict, *, max_swarms: int = 6, plan_kind: str = "fuld",
               reports: list[dict] | None = None, force_risk: bool = False,
               history: list[dict] | None = None, by_date: bool | None = None) -> list[dict]:
    """Ugens plan. Rækkefølge: risiko → dybdeanalyser → CIO (som får de andres konklusioner)."""
    if plan_kind == "alt":
        return alt_plan(p, ref, week, reports=reports, max_swarms=max_swarms)
    if plan_kind == "strategi" or not p["holdings"]:  # alt kontant: byg porteføljen fra bunden
        return strategy_plan(p, week, max_swarms=max_swarms)
    goal = (p.get("goal") or (TRANSITION_GOAL if p.get("weight_policy") == "target" else DEFAULT_GOAL)).strip()
    ptxt = portfolio_text(p)
    period = f"{dmy(dt.date.fromisoformat(week['from']))}–{dmy(dt.date.fromisoformat(week['to']))}"
    stocks = [h for h in p["holdings"] if h["kind"] not in ("cash", "fund", "etf")]
    biggest = max(stocks or p["holdings"], key=lambda h: h["cost_sek"] or 0)
    weights = cost_weights(p)
    wtxt = ", ".join(f"{w['ticker']} {w['weight_cost_pct']:g} %" for w in weights if w["weight_cost_pct"] is not None)
    iso_week = dt.date.fromisoformat(week["from"]).isocalendar().week
    dep = p.get("monthly_deposit_sek")
    cash_part = (f"den ledige kontant ({free_cash(p):,.0f} SEK; resten er reserveret)" if p.get("cash_reserved_sek")
                 else f"kontanten ({float(p.get('cash_sek') or 0):,.0f} SEK)").replace(",", " ")
    new_money = (f"nye indskud (ca. {dep:,.0f} SEK om måneden)".replace(",", " ") + f" og {cash_part}" if dep
                 else f"nye indskud og {cash_part}")
    scope = (f"Fordel kun {new_money}: sælg ikke de eksisterende aktier, og fordel ikke hele porteføljens værdi. "
             + ("Regn kurtage efter satserne i porteføljeteksten. " if p.get("courtage_terms") else "Angiv ingen kurtagesats. ")
             + "Foreslå kun UCITS-fonde og -ETF'er, der kan købes hos Nordnet (fx iShares MSCI "
             "ACWI UCITS ETF); amerikanske ETF'er som ACWI.US er kun dataproxy og kan ikke købes på en ISK. ")

    plan: list[dict] = [{
        "preset": "risk_committee", "reason": "Fast ugentligt risikoudvalg (spillebog)",
        "vars": {"goal": (
            f"Vurdér drawdown, halerisiko og markedsregime for denne portefølje (periode {period}):\n{ptxt}\n"
            f"Vægte efter anskaffelse: {wtxt}. Brug portfolio_risk_xray på disse symboler og vægte, og quantlib_call til "
            f"VaR/CVaR/EVT og regime. Regn tabet i SEK i fire scenarier: 1) {biggest['name']} ({biggest['ticker']}) −30 %, "
            f"2) {biggest['name']} −50 %, 3) USD/SEK −10 %, 4) aktiemarkedet samlet −25 % med hver positions egen historik "
            "(enkeltaktier falder ofte mere end indekset). Brug VaR kun som supplement, "
            "angiv datavinduet, og sig tydeligt, når et papir har kort kurshistorik (se datatjekkets facit): så er "
            "volatilitet, VaR, beta og korrelation statistisk usikre. Hvilke ændringer sænker risikoen mest?"
            + LANG_SUFFIX)},
    }]
    if plan_kind == "fuld":
        for r in (reports or [])[:2]:
            day = dmy(dt.date.fromisoformat(r["date"]))
            alt = ", ".join(dmy(dt.date.fromisoformat(a)) for a in r.get("alt_dates") or [])
            unsure = (f"; kilderne er uenige: en anden kilde siger {alt}. Bekræft datoen på selskabets investor-side, "
                      "før du planlægger omkring den") if alt else ""
            plan.append({
                "preset": "earnings_research_desk",
                "reason": f"Regnskab {day}" + (f" (eller {alt})" if alt else "") + " (spillebog: før hvert regnskab)",
                "vars": {"target": f"{r['ticker']} (regnskab {day}{unsure}; skriv på dansk, beløb også i SEK)"
                                   + (SPACEX_HINT if r["ticker"].startswith("SPCX") else "")},
                "context": r,
            })
        wl = [w for w in p.get("watchlist") or [] if isinstance(w, dict) and w.get("ticker")]
        nth = (ref.day - 1) // 7 + 1  # hvilken lørdag i måneden (1–5)
        month_idx = ref.year * 12 + ref.month
        focus = stocks[month_idx % len(stocks)] if stocks else biggest
        monthly = {
            1: {"preset": "etf_allocation_desk", "reason": "Månedens 1. lørdag: ETF-allokering (kerne og spredning)",
                "vars": {"risk_profile": "moderat, langsigtet, ingen gearing, svensk ISK i SEK hos Nordnet",
                         "market": (f"globale ETF'er, der kan købes via Nordnet. Find dem, der sænker korrelationen mest "
                                    f"til en portefølje med {wtxt}. {scope}Skriv på dansk, beløb i SEK.")}},
            2: {"preset": "macro_rates_fx_desk", "reason": "Månedens 2. lørdag: makro, renter og USD/SEK",
                "vars": {"goal": (f"Svensk investor med basisvaluta SEK og denne eksponering efter anskaffelse: {wtxt}. "
                                  "Vurdér Fed/Riksbanken, USD/SEK-retning og om valutarisikoen bør afdækkes eller spredes. "
                                  "Skriv på dansk, beløb i SEK."),
                         "timeframe": "1–6 måneder"}},
            3: {"preset": "fund_selection_panel", "reason": "Månedens 3. lørdag: konkrete kernefonde til ISK",
                "vars": {"fund_type": "globale indeksfonde og ETF'er, der kan købes på Nordnet ISK (globale, Europa, Norden, small cap, value)",
                         "goal": (f"Find 2–3 lavomkostnings-kernefonde, der sænker korrelationen til en portefølje med {wtxt}, "
                                  f"med omkostninger, tracking og risiko. {scope}Skriv på dansk, beløb i SEK.")}},
            4: {"preset": "value_investing_committee", "reason": f"Månedens 4. lørdag: værdikomité på {focus['name']}",
                "vars": {"company": focus["ticker"], "market": f"{market_of(focus['ticker'])}. Skriv på dansk, beløb også i SEK."
                                   + (SPACEX_HINT if focus["ticker"].startswith("SPCX") else "")}},
            5: {"preset": "equity_research_team", "reason": "Månedens 5. lørdag: idéjagt uden for tech",
                "vars": {"market": "Norden og Europa, ikke-tech (sundhed, forbrug, industri, finans)",
                         "goal": (f"Find 3–5 kvalitetsaktier, der spreder en portefølje med {wtxt}. "
                                  "Skriv på dansk, DD/MM/YYYY, beløb i SEK.")}},
        }
        if by_date is None:
            by_date = ref.weekday() == 5
        forced = os.environ.get("VT_MONTHLY_SLOT", "").strip()
        if forced.isdigit() and int(forced) in monthly:
            slot = int(forced)
        elif by_date:
            slot = nth
        else:  # knaptryk: det månedsteam, der er længst tid siden har kørt
            slot = min(monthly, key=lambda k: (last_done(history, monthly[k]["preset"]), k))
        if slot in monthly:
            plan.append(monthly[slot])
        # Rotation: fundamental/investeringskomité og Visser-panelet (fundamentals, nyheder og katalysatorer, trend,
        # risiko og porteføljechef pr. navn). Uden enkeltaktier eller kandidater: de gamle sentiment- og teknik-teams.
        vnames = visser_names(p)
        rot_presets = (["investment_committee" if wl else "fundamental_research_team"]
                       + (["visser_panel"] if vnames else ["sentiment_intelligence_team", "technical_analysis_panel"]))
        n_rot = len(rot_presets)
        rot = (iso_week % n_rot if by_date or not history
               else min(range(n_rot), key=lambda i: (last_done(history, rot_presets[i]), i)))
        if rot == 0:
            if wl:
                cand = wl[(iso_week // 3) % len(wl)]
                plan.append({
                    "preset": "investment_committee",
                    "reason": f"Købskandidat fra overvågningslisten: {cand['ticker']} (spillebog: før hvert køb)",
                    "vars": {"target": cand["ticker"], "market": (
                        f"{market_of(cand['ticker'])}. Vurdér som spredning til en portefølje med {wtxt}. "
                        f"Note: {cand.get('note', '')}. Skriv på dansk, DD/MM/YYYY, beløb også i SEK.")},
                    "context": {"ticker": cand["ticker"]},
                })
            else:
                plan.append({"preset": "fundamental_research_team", "reason": f"Fundamental analyse af {focus['name']}",
                             "vars": {"target": focus["ticker"], "market": f"{market_of(focus['ticker'])}. Skriv på dansk."
                                      + (SPACEX_HINT if focus["ticker"].startswith("SPCX") else "")}})
        elif rot_presets[rot] == "visser_panel":
            plan.append({
                "preset": "visser_panel",
                "reason": "Visser-panelet: trend mod tese for satellitter og kandidater (hver 2. uge)",
                "vars": {"names": vnames,
                         "focus": ("Langsigtet ISK, ingen daghandel: reglen er et lag i analysen. "
                                   + (f"Kommende regnskaber: {', '.join(r['ticker'] + ' ' + dmy(dt.date.fromisoformat(r['date'])) for r in (reports or []))}. "
                                      if reports else "")
                                   + "Skriv på dansk, DD/MM/YYYY, beløb også i SEK.")},
            })
        elif rot_presets[rot] == "sentiment_intelligence_team":
            plan.append({
                "preset": "sentiment_intelligence_team",
                "reason": "Sentiment (rotation)",
                "vars": {"market": "Porteføljens papirer: " + ", ".join(f"{h['name']} ({h['ticker']})" for h in p["holdings"])
                                   + ". Medtag en katalysatorkalender for de næste 90 dage. Skriv på dansk.",
                         "timeframe": "seneste 1–4 uger"},
            })
        else:
            plan.append({
                "preset": "technical_analysis_panel",
                "reason": f"Teknisk analyse af største position (rotation): {biggest['name']}",
                "vars": {"target": biggest["ticker"], "timeframe": "daglig og ugentlig, 6–12 måneder; start med "
                         "trendvagten i facit (200-dages-reglen). Skriv på dansk."},
            })
    plan.append({
        "preset": "portfolio_review_board", "reason": "Fast ugentlig CIO-gennemgang, der samler ugens agent-teams",
        "vars": {"portfolio": ptxt, "review_period": f"Uge {iso_week}: {period}", "goal": goal + LANG_SUFFIX},
        "cio": True,
    })
    skip = {x.strip() for x in os.environ.get("VT_SKIP_PRESETS", "").split(",") if x.strip()}
    if force_risk:
        skip.discard("risk_committee")
    plan = [s for s in plan if s["preset"] not in skip or s.get("cio")]
    if len(plan) > max(1, max_swarms):  # behold altid CIO'en
        plan = plan[:max(1, max_swarms) - 1] + [plan[-1]]
    return plan


ALT_GOAL = (
    "Ugerapporten: saml ALLE ugens teams (risiko og stresstest, regnskaber, aktieanalyse af hvert navn, strategi og makro) "
    "til én vurdering af den valgte portefølje. Hvad holder, hvad skal ændres, og hvad skal investoren gøre før næste "
    "rapport? Brug facit for kurser, trend (200-dages-reglen, 5 år, spredning), Buffett-indikatoren, kreditmarkedet og "
    "kæde-tjekket (kanariefuglen). Investoren er ikke aktieekspert: skriv kort og uden fagord."
)


def alt_plan(p: dict, ref: dt.datetime, week: dict, *, reports: list[dict] | None = None, max_swarms: int = 7) -> list[dict]:
    """Den ene ugerapport: alle teams på den valgte portefølje – beholdningen, eller målporteføljen, når alt står kontant.

    Rækkefølge: risiko → regnskaber (højst 2) → aktieanalyse (Visser-panelet: fundamentals, nyheder og katalysatorer,
    trend, risiko og porteføljechef pr. navn) → strategi og makro → CIO, der får alle de andres konklusioner.
    Uden beholdning OG uden målportefølje bygges porteføljen fra kontant (strategikørslen)."""
    held = [h for h in p["holdings"] if h["kind"] != "cash"]
    tgt = target_holdings(p)
    if not held and not tgt:
        return strategy_plan(p, week, max_swarms=max_swarms)
    m = p.get("mandate") or {"horizon_years": 5, "max_drawdown_pct": 30, "universe": "kerne+satellit", "single_stock_cap_pct": 10}
    H, DD, CAP = m["horizon_years"], m["max_drawdown_pct"], m["single_stock_cap_pct"]
    ptxt = portfolio_text(p)
    period = f"{dmy(dt.date.fromisoformat(week['from']))}–{dmy(dt.date.fromisoformat(week['to']))}"
    iso_week = dt.date.fromisoformat(week["from"]).isocalendar().week
    if held:
        basis = "den nuværende portefølje (beholdningen" + (" og målporteføljen, den bygges hen imod)" if tgt else ")")
        wtxt = ", ".join(f"{w['ticker']} {w['weight_cost_pct']:g} %" for w in cost_weights(p) if w["weight_cost_pct"] is not None)
        stocks = [h for h in held if h["kind"] not in ("fund", "etf")]
        biggest = max(stocks or held, key=lambda h: h.get("cost_sek") or 0)
    else:
        basis = ("målporteføljen (endnu ikke købt: alt står kontant og købes gradvist med de månedlige indskud – vurdér "
                 "den, som om den var købt, og sig hvad der bør ændres, før der købes)")
        wtxt = ", ".join(f"{g['ticker']} {g['weight']:g} %" for g in tgt if g.get("weight") is not None)
        stocks = [g for g in tgt if g["kind"] == "stock"]
        biggest = max(stocks or tgt, key=lambda g: g.get("weight") or 0)
    rules = (f"Mandat: horisont {H} år, tåler et midlertidigt fald på op til {DD} % uden at sælge, loft {CAP} % pr. enkeltaktie, "
             "svensk ISK hos Nordnet i SEK, ingen gearing eller derivater. ")
    plan: list[dict] = [{
        "preset": "risk_committee", "label": "Risiko og stresstest", "reason": "Ugerapport: risiko og stresstest",
        "vars": {"goal": (
            f"Vurdér drawdown, halerisiko og markedsregime for {basis} (periode {period}):\n{ptxt}\nVægte: {wtxt}. {rules}"
            "Brug portfolio_risk_xray på disse symboler og vægte, og quantlib_call til VaR/CVaR og regime. Regn tabet i SEK i fire "
            f"scenarier: 1) globalt aktiefald −30 %, 2) 2008-lignende fald −50 %, 3) USD/SEK −10 %, 4) {biggest['name']} "
            f"({biggest['ticker']}) −50 % med sin egen historik. Sig, om det tålte fald på {DD} % holder. Brug kreditmarkedet "
            "(high-yield-spænd) og Buffett-indikatoren fra facit som baggrund for regimet, og sig tydeligt, når et papir har "
            "kort kurshistorik. Hvilke ændringer sænker risikoen mest uden at opgive vækstmålet?" + LANG_SUFFIX)},
    }]
    for r in (reports or [])[:2]:
        day = dmy(dt.date.fromisoformat(r["date"]))
        alt = ", ".join(dmy(dt.date.fromisoformat(a)) for a in r.get("alt_dates") or [])
        unsure = (f"; kilderne er uenige: en anden kilde siger {alt}. Bekræft datoen på selskabets investor-side, "
                  "før du planlægger omkring den") if alt else ""
        plan.append({
            "preset": "earnings_research_desk", "label": "Regnskabsdesk",  # aldrig navne i labels: de står i den offentlige log
            "reason": f"Ugerapport: regnskab {day}" + (f" (eller {alt})" if alt else ""),
            "vars": {"target": f"{r['ticker']} (regnskab {day}{unsure}; skriv på dansk, beløb også i SEK)"
                               + (SPACEX_HINT if r["ticker"].startswith("SPCX") else "")},
            "context": r,
        })
    vnames = visser_names(p)
    if vnames:
        plan.append({
            "preset": "visser_panel", "label": "Aktieanalyse: fem agenter pr. aktie",
            "reason": "Ugerapport: aktieanalyse (fundamentals, nyheder og katalysatorer, trend, risiko, porteføljechef)",
            "vars": {"names": vnames,
                     "focus": ("Langsigtet ISK, ingen daghandel: 200-dages-reglen er et lag i analysen. "
                               + (f"Kommende regnskaber: {', '.join(r['ticker'] + ' ' + dmy(dt.date.fromisoformat(r['date'])) for r in (reports or []))}. "
                                  if reports else "")
                               + "Skriv på dansk, DD/MM/YYYY, beløb også i SEK.")},
        })
    plan.append({
        "preset": "macro_strategy_forum", "label": "Strategi og makro", "reason": "Ugerapport: strategi, makro og værdiansættelse",
        "vars": {"market": (f"globalt aktiemarked (USA, Europa, Norden, emerging markets) for en svensk investor i SEK. Vurdér {basis} "
                            f"({wtxt}) mod makro, renter, valuta, kreditmarkedet og værdiansættelse (Buffett-indikatoren og CAPE i "
                            f"facit) over {H} år: hvad bør justeres i målporteføljen, og hvad er de største trusler og muligheder "
                            f"de næste 3–12 måneder? {rules}Skriv på dansk, beløb i SEK."),
                 "horizon": f"{H} år (strategisk) med 3–12 måneders udsigt"},
    })
    plan.append({
        "preset": "portfolio_review_board", "label": "Porteføljegennemgang (CIO)",
        "reason": "Ugerapport: CIO'en samler alle teams til én vurdering og beslutninger",
        "vars": {"portfolio": ptxt, "review_period": f"Uge {iso_week}: {period}", "goal": ALT_GOAL + LANG_SUFFIX},
        "cio": True,
    })
    skip = {x.strip() for x in os.environ.get("VT_SKIP_PRESETS", "").split(",") if x.strip()}
    plan = [s for s in plan if s["preset"] not in skip or s.get("cio")]
    if len(plan) > max(1, max_swarms):  # behold altid CIO'en
        plan = plan[:max(1, max_swarms) - 1] + [plan[-1]]
    return plan


def strategy_plan(p: dict, week: dict, *, max_swarms: int = 7) -> list[dict]:
    """Strategikørsel: syv teams bygger én strategi fra kontant. CIO'en samler (kører sidst)."""
    m = p.get("mandate") or {"horizon_years": 5, "max_drawdown_pct": 30, "universe": "kerne+satellit", "single_stock_cap_pct": 10}
    ptxt = portfolio_text(p)
    period = f"{dmy(dt.date.fromisoformat(week['from']))}–{dmy(dt.date.fromisoformat(week['to']))}"
    iso_week = dt.date.fromisoformat(week["from"]).isocalendar().week
    cash = float(p.get("cash_sek") or 0)
    dep = p.get("monthly_deposit_sek")
    money = (f"{cash:,.0f} SEK kontant nu" + (f" og ca. {dep:,.0f} SEK i nye indskud om måneden" if dep else "")).replace(",", " ")
    H, DD, CAP = m["horizon_years"], m["max_drawdown_pct"], m["single_stock_cap_pct"]
    uni = MANDATE_UNIVERSES[m["universe"]]
    nordnet = ("Kun instrumenter, en privatkunde kan købe hos Nordnet på en svensk ISK: UCITS-fonde og -ETF'er (europæisk "
               "notering, ISIN) og enkeltaktier på nordiske, europæiske og amerikanske børser. Amerikanske ETF'er som ACWI.US "
               "er kun dataproxy. Ingen gearing, derivater eller certifikater. ")
    base = (f"Investoren har solgt alt og har {money}. Mandat: horisont {H} år, maksimér væksten, tåler et midlertidigt fald "
            f"på op til {DD} % uden at sælge, univers: {uni}, loft {CAP} % pr. enkeltaktie. Ved modstrid mellem investeringsrammen, "
            f"et ældre mål og dette mandat gælder mandatet. {nordnet}")
    plan: list[dict] = [
        {"preset": "macro_strategy_forum", "reason": "Strategi: makro og aktivklasser for horisonten",
         "vars": {"market": "globalt aktiemarked (USA, Europa, Norden, emerging markets) for en svensk investor i SEK. "
                            + base + "Skriv på dansk, beløb i SEK.",
                  "horizon": f"{H} år (strategisk), med 12-måneders udsigt som delmål"}},
        {"preset": "sector_rotation_team", "reason": "Strategi: sektorer og temaer med medvind over horisonten",
         "vars": {"market": "USA, Europa og Norden (UCITS-sektor- og tema-ETF'er som instrument)",
                  "goal": base + f"Find de 3–5 sektorer/temaer, der bør overvægtes de næste {H} år, med UCITS-ETF pr. tema. "
                                 "Skriv på dansk, beløb i SEK."}},
        {"preset": "etf_allocation_desk", "reason": "Strategi: kerneallokering i ETF'er",
         "vars": {"risk_profile": f"aggressiv vækst, {H} års horisont, tåler fald på {DD} %, ingen gearing, svensk ISK i SEK hos Nordnet",
                  "market": "globale UCITS-ETF'er, der kan købes via Nordnet. " + base
                            + "Byg kernen (region, størrelse, faktorer som momentum/kvalitet/small cap) med vægte i procent. Skriv på dansk, beløb i SEK."}},
        {"preset": "fund_selection_panel", "reason": "Strategi: konkrete fonde og ETF'er på Nordnet",
         "vars": {"fund_type": "aktiefonde og UCITS-ETF'er på Nordnet ISK: globale indeks, USA, Europa, Norden, small cap, emerging markets, tema/sektor",
                  "goal": base + "Vælg 3–6 konkrete fonde/ETF'er (navn og ISIN) med omkostninger, tracking og risiko, der passer til kernen. Skriv på dansk, beløb i SEK."}},
        {"preset": "equity_research_team", "label": "Idéjagt: vækstaktier" if m["universe"] != "kun fonde" else "Idéjagt: temaer til fondene",
         "reason": "Strategi: enkeltaktier som satellitter" if m["universe"] != "kun fonde" else "Strategi: idéjagt (kun fonde – aktier som reference)",
         "vars": {"market": "USA, Europa og Norden (Nasdaq, NYSE, Xetra, Stockholm, København, Oslo, Helsinki)",
                  "goal": base + (f"Find 5–8 vækstaktier med {H} års medvind, der kan udgøre satellitterne (hver højst {CAP} %), med tese, "
                                  "kill-kriterie og hvad der skal være sandt om 5 år. " if m["universe"] != "kun fonde" else
                                  "Mandatet tillader ikke enkeltaktier: find i stedet de temaer og selskaber, som fondene bør give eksponering til. ")
                          + "Skriv på dansk, DD/MM/YYYY, beløb i SEK."}},
        {"preset": "risk_committee", "reason": "Strategi: stresstest af den foreslåede portefølje",
         "vars": {"goal": (f"Stresstest en vækstportefølje for en svensk investor med {money} (periode {period}):\n{ptxt}\n"
                           f"Reference-portefølje (byg den selv af data-proxies med vægte): 60 % globalt indeks (ACWI.US), 15 % small cap "
                           f"(IWM.US), 10 % emerging markets (EEM.US), 15 % tema/enkeltaktier (QQQ.US som proxy). Brug portfolio_risk_xray og "
                           f"quantlib_call. Regn tabet i SEK i fire scenarier: 1) globalt aktiefald −30 %, 2) 2008-lignende fald −50 %, "
                           f"3) USD/SEK −10 %, 4) amerikansk tech −40 %. Regn desuden en variant, hvor 30–40 % ligger i 2–3 enkeltaktier, "
                           f"med aktiernes egne kursfald i 2008, 2020 og 2022 (fx NVDA.US og NOVO-B.CO), for enkeltaktier falder ofte "
                           f"mere end indekset. Sig, om mandatets tålte fald på {DD} % holder, og hvad der "
                           "sænker halerisikoen uden at opgive vækstmålet." + LANG_SUFFIX)}},
        {"preset": "portfolio_review_board", "reason": "Strategi: CIO'en samler teams til én strategi",
         "vars": {"portfolio": ptxt, "review_period": f"Uge {iso_week}: {period} (strategikørsel)", "goal": STRATEGY_GOAL + LANG_SUFFIX},
         "system_extra": {"chief_investment_officer": STRATEGY_CIO_EXTRA,
                          **{role: STRATEGY_ANALYST_EXTRA.format(task=t) for role, t in STRATEGY_ANALYST_TASKS.items()}},
         "cio": True},
    ]
    skip = {x.strip() for x in os.environ.get("VT_SKIP_PRESETS", "").split(",") if x.strip()}
    plan = [s for s in plan if s["preset"] not in skip or s.get("cio")]
    if len(plan) > max(1, max_swarms):
        plan = plan[:max(1, max_swarms) - 1] + [plan[-1]]
    return plan


# Afsnit i en teamrapport, som CIO'en især skal se (konklusioner og beslutninger), og afsnit den kan undvære.
_DIGEST_T1 = re.compile(r"(executive summary|sammenfatning|resum|konklusion|conclusion|beslutning|decision|anbefaling|"
                        r"recommendation|handlingspunkt|action item|samlet|summary|endelig|final|bottom line|verdict)", re.I)
_DIGEST_T2 = re.compile(r"(overgang|transition|målvægt|target|scenari|risikooversigt|risk overview|risikobudget|risk budget|"
                        r"trade|sizing|position|integr|uenig|disagree|katalysator|catalyst|begivenhed|trigger|plan|stop|hedge)", re.I)
_DIGEST_SKIP = re.compile(r"(datakilde|kilder\b|forbehold|metode|backtest-note|reproducer|data sources|sources|appendix|bilag|"
                          r"methodology)", re.I)
_MD_HEADING = re.compile(r"^#{1,4}\s+\S.*$", re.M)


def report_digest(text: str | None, budget: int) -> str:
    """Det vigtigste fra en teamrapport inden for ``budget`` tegn.

    Indledningen og afsnit med konklusioner og beslutninger først (de står ofte sidst), derefter
    planer, scenarier og risikobudget; kilder og forbehold til sidst. Afsnittene bevarer rækkefølgen.
    Uden overskrifter: begyndelsen og slutningen af rapporten.
    """
    text = (text or "").strip()
    if len(text) <= budget:
        return text
    marks = [m.start() for m in _MD_HEADING.finditer(text)]
    if not marks or len(marks) < 2:
        head = text[: budget // 3]
        tail = text[-(budget - len(head) - 8):]
        return f"{head} …\n[…]\n… {tail}"
    if marks[0] != 0:
        marks = [0] + marks
    secs = [text[a:b].strip() for a, b in zip(marks, marks[1:] + [len(text)])]
    head, rest = secs[0], secs[1:]
    titles = [sec.split("\n", 1)[0] for sec in rest]
    t1 = [i for i, t in enumerate(titles) if _DIGEST_T1.search(t) and not _DIGEST_SKIP.search(t)]
    t2 = [i for i, t in enumerate(titles) if i not in t1 and _DIGEST_T2.search(t) and not _DIGEST_SKIP.search(t)]
    t3 = [i for i, t in enumerate(titles) if i not in t1 and i not in t2 and not _DIGEST_SKIP.search(t)]
    room = budget - 20
    head = head if len(head) <= max(300, room // 6) else head[: max(300, room // 6)].rsplit(" ", 1)[0] + " …"
    room -= len(head) + 2
    take: dict[int, str] = {}
    cap = max(500, room // max(1, len(t1) + len(t2)))

    def cut(sec: str, n: int) -> str:
        return sec if n >= len(sec) else sec[:n].rsplit(" ", 1)[0] + " …"

    for i in t1 + t2:  # første runde: alle vigtige afsnit får en lige stor bid
        if room < 120:
            break
        take[i] = cut(rest[i], min(cap, room))
        room -= len(take[i]) + 2
    for i in t1 + t2:  # anden runde: resten af pladsen til afkortede konklusioner og planer
        if room < 120:
            break
        if i in take and len(take[i]) < len(rest[i]):
            piece = cut(rest[i], len(take[i]) + room)
            room -= len(piece) - len(take[i])
            take[i] = piece
    for i in t3:  # tredje runde: øvrige afsnit, hvis der er plads tilbage
        if room < 120:
            break
        take[i] = cut(rest[i], room)
        room -= len(take[i]) + 2
    body = "\n\n".join(take[i] for i in sorted(take))
    return f"{head}\n\n{body}".strip()


def cio_goal_with_inputs(base_goal: str, runs: list[dict], limit: int = 16000) -> str:
    """Giv CIO'en alle øvrige teams' konklusioner, så syntesen bygger på hele ugens arbejde."""
    return base_goal + cio_digest(runs, limit)


def cio_digest(runs: list[dict], limit: int = 16000) -> str:
    """Digest af de øvrige teams til CIO'en: hvert team får lige stor plads, og pladsen bruges på konklusioner
    og beslutninger (ikke indledningen). Teams uden rapport nævnes, så CIO'en ikke tror, at de er enige.
    Overskriften tæller teams, så CIO'en ikke tæller forkert (19/10/2026 skrev den "alle fire teams" om seks)."""
    done = [r for r in runs if r.get("status") == "completed" and r.get("final_report")]
    # Et team, der fejlede, blev stoppet på tid eller budget, kan stadig have færdige agenter med rapport
    # (Vibe-Trading markerer hele kørslen "failed", når én agent fejler, og synteseagenten blokeres). Deres
    # arbejde gives videre som delrapport, så CIO'en ikke mister fx regnskabsanalysen, fordi optionsagenten faldt.
    partial = [r for r in runs if r not in done and partial_reports(r)]
    missing = [r for r in runs if r not in done and r not in partial]
    if not done and not missing and not partial:
        return ""
    names = ", ".join(r.get("label", r["preset"]) for r in done) or "ingen"
    part_names = ", ".join(r.get("label", r["preset"]) for r in partial)
    miss_names = ", ".join(r.get("label", r["preset"]) for r in missing) or "ingen"
    intro = (f"\n\n## Konklusioner fra ugens øvrige Vibe-Trading-teams\n{len(done)} team(s) med rapport ({names}); "
             + (f"{len(partial)} med delrapport fra enkelte agenter ({part_names}); " if partial else "")
             + f"{len(missing)} uden rapport ({miss_names}). Brug disse tal, når du tæller teams. Brug alle konklusioner "
             "i syntesen, og sig hvor du er uenig.\n")
    notes = [f"### {r.get('label', r['preset'])}: ingen rapport ({r.get('status')}"
             + (f": {r['skip_reason']}" if r.get("skip_reason") else "") + ")" for r in missing]
    room = limit - len(intro) - sum(len(n) + 2 for n in notes)
    share = max(800, room // max(1, len(done) + len(partial)))
    parts = []
    for r in done:
        head = f"### {r.get('label', r['preset'])}\n"
        no_rep = [t.get("role") for t in r.get("tasks", []) if t.get("report") in ("empty", "plan_only")]
        cut = [t.get("role") for t in r.get("tasks", []) if t.get("cut") and t.get("report") not in ("empty", "plan_only")]
        flag = (f"(Agenter uden rapport i teamet: {', '.join(no_rep)})\n" if no_rep else "") + \
               (f"(Afkortet midt i teksten – resten mangler: {', '.join(cut)}{' og teamets slutrapport' if r.get('final_report_cut') else ''})\n"
                if cut or r.get("final_report_cut") else "")
        parts.append(head + flag + report_digest(r["final_report"], share - len(head) - len(flag)))
    for r in partial:
        reps = partial_reports(r)
        failed = [t.get("role") for t in r.get("tasks", []) if t.get("status") in ("failed", "blocked", "cancelled")]
        head = (f"### {r.get('label', r['preset'])}: delrapport ({r.get('status')}; ingen samlet rapport"
                + (f", fejlet/blokeret: {', '.join(failed)}" if failed else "") + ")\n")
        each = max(300, (share - len(head)) // len(reps))
        body = "\n\n".join(f"#### {role}\n" + report_digest(text, each - len(role) - 6) for role, text in reps)
        parts.append(head + body)
    return intro + "\n\n".join(parts + notes)


_ERROR_KINDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Blocked: upstream not completed", re.I), "blokeret af en fejlet agent"),
    (re.compile(r"output contract not met: data agent produced no tool calls", re.I),
     "svar uden værktøjskald afvist af Vibe-Trading (leverancekontrakt)"),
    (re.compile(r"output contract not met: plan-only", re.I), "kun en plan (leverancekontrakt)"),
    (re.compile(r"output contract not met: (?:empty|raw tool-result|unparsed|explicitly fabricated)", re.I),
     "tomt eller ubrugeligt svar (leverancekontrakt)"),
    (re.compile(r"output contract not met", re.I), "leverance afvist af Vibe-Trading (leverancekontrakt)"),
    (re.compile(r"Failed to render prompt template", re.I), "prompt-skabelon kunne ikke udfyldes"),
    (re.compile(r"content_filter_circuit_breaker", re.I), "svar blokeret af indholdsfilter"),
    (re.compile(r"LLM call failed at iteration (\d+).*?(prompt is too long|context.?length|too many tokens)", re.I | re.S),
     r"LLM-kald fejlede i iteration \1 (konteksten for lang)"),
    (re.compile(r"LLM call failed at iteration (\d+).*?(rate.?limit|overloaded|529)", re.I | re.S),
     r"LLM-kald fejlede i iteration \1 (overbelastning/rate limit)"),
    (re.compile(r"LLM call failed at iteration (\d+).*?(?:status[_ ]code|Error code|HTTP)[:= ]+(\d{3})", re.I | re.S),
     r"LLM-kald fejlede i iteration \1 (HTTP \2)"),
    (re.compile(r"LLM call failed at iteration (\d+)", re.I), r"LLM-kald fejlede i iteration \1"),
    (re.compile(r"exceeded layer deadline|timed out|timeout", re.I), "tidsgrænse"),
    (re.compile(r"worker did not complete \(status=(\w+)\)", re.I), r"agenten afsluttede ikke (\1)"),
]


def _error_kind(error: str | None) -> str:
    """Fejltypen uden fejlteksten (den kan rumme tickere eller beløb). Ukendte fejl bliver kun 'anden fejl'."""
    text = str(error or "")
    for pat, label in _ERROR_KINDS:
        m = pat.search(text)
        if m:
            return m.expand(label) if "\\" in label else label
    return "anden fejl"


def partial_reports(run: dict) -> list[tuple[str, str]]:
    """(rolle, tekst) for de agenter, der leverede en rapport i et team uden samlet rapport."""
    if run.get("status") == "skipped":
        return []
    return [(t.get("role") or t.get("agent_id") or "agent", t["summary"]) for t in run.get("tasks", [])
            if t.get("status") == "completed" and t.get("summary") and t.get("report") == "ok"]


# Værktøjer, der kun kan bruges med en nøgle. Uden nøglen tilføjes de ikke (så agenterne ikke lover data, de ikke kan hente).
TOOL_REQUIRES = {"get_eodhd_data": ("EODHD_API_KEY", "EODHD_API_TOKEN"), "get_macro_series": ("FRED_API_KEY",)}


def _tool_usable(name: str) -> bool:
    need = TOOL_REQUIRES.get(name)
    return not need or any(os.environ.get(k, "").strip() for k in need)


def _extra_tools_for(preset: str, agent: dict) -> list[str]:
    tools = set(agent.get("tools") or [])
    out: list[str] = []
    for key in ("*", preset):
        rules = EXTRA_TOOLS.get(key, {})
        out += rules.get("*", []) + rules.get(agent.get("id"), [])
        if "get_market_data" in tools:
            out += rules.get("*data*", [])
    return [t for t in dict.fromkeys(out) if t not in tools and _tool_usable(t)]


def prepare_shellfree_presets(dst: Path, src: Path | None = None, brief: str = "",
                              eodhd_ready: bool | None = None, fred_ready: bool | None = None,
                              own_dir: Path | None = None) -> list[str]:
    """Kopiér Vibe-Tradings presets til brugermappen med kvant-værktøjer i stedet for shell.

    Vibe-Trading læser brugermappen før de indbyggede presets, så samme navne bruges.
    Agenter, der var afhængige af bash, får quantlib_call, portfolio_risk_xray m.fl. og en
    instruks om at regne med dem. Agenter med data-værktøjer får EODHD som anden kilde, og alle
    agenter får datatjekkets facit (``brief``). Returnerer navnene på de presets der blev ændret.
    """
    import yaml

    default_src = src is None  # Vibetrads egne presets (runner/presets) følger kun med de indbyggede
    if src is None:
        from src.swarm import presets as _p

        src = Path(_p.PRESETS_DIR)
    dst.mkdir(parents=True, exist_ok=True)
    changed = []
    own_dir = VIBETRAD_PRESETS_DIR if own_dir is None and default_src else own_dir
    own = sorted(Path(own_dir).glob("*.yaml")) if own_dir and Path(own_dir).is_dir() else []
    for f in sorted(Path(src).glob("*.yaml")) + own:
        spec = yaml.safe_load(f.read_text("utf-8"))
        touched = False
        agents = spec.get("agents", [])
        for i, agent in enumerate(agents):
            tools = list(agent.get("tools") or [])
            if SHELL_TOOLS & set(tools):
                kept = [t for t in tools if t not in SHELL_TOOLS]
                agent["tools"] = kept + [t for t in QUANT_TOOLS if t not in kept]
                agent["system_prompt"] = str(agent.get("system_prompt", "")).rstrip() + NO_SHELL_GUIDANCE
                touched = True
            if DROP_TOOLS & set(agent.get("tools") or []):
                agent["tools"] = [t for t in agent["tools"] if t not in DROP_TOOLS]
                touched = True
            if DROP_SKILLS & set(agent.get("skills") or []):
                agent["skills"] = [s for s in agent["skills"] if s not in DROP_SKILLS]
                touched = True
            add_tools = _extra_tools_for(f.stem, agent)
            if eodhd_ready is False:  # EODHD blev ikke koblet på: lov ikke agenterne et værktøj, de ikke har
                add_tools = [t for t in add_tools if t != "get_eodhd_data"]
            if fred_ready is False:  # FRED afviste nøglen eller svarede ikke i nøgletjekket
                add_tools = [t for t in add_tools if t != "get_macro_series"]
            if add_tools:
                agent["tools"] = list(agent.get("tools") or []) + add_tools
                touched = True
            if "get_eodhd_data" in (agent.get("tools") or []):
                agent["system_prompt"] = str(agent.get("system_prompt", "")).rstrip() + DATA_GUIDANCE
                touched = True
            if "get_macro_series" in (agent.get("tools") or []):
                agent["system_prompt"] = str(agent.get("system_prompt", "")).rstrip() + FRED_GUIDANCE
                touched = True
            if SYMBOL_TOOLS & set(agent.get("tools") or []):
                agent["system_prompt"] = str(agent.get("system_prompt", "")).rstrip() + SYMBOL_GUIDANCE
                touched = True
            agent["system_prompt"] = str(agent.get("system_prompt", "")).rstrip() + ISK_GUIDANCE
            touched = True
            if brief:
                agent["system_prompt"] = str(agent.get("system_prompt", "")).rstrip() + "\n\n" + brief.strip() + "\n"
                touched = True
            rules_p = EXTRA_PROMPT.get(f.stem, {})
            add_prompt = rules_p.get("*", "") + rules_p.get(agent.get("id"), "")
            if add_prompt:
                agent["system_prompt"] = str(agent.get("system_prompt", "")).rstrip() + add_prompt
                touched = True
            extra: list[str] = []
            for key in ("*", f.stem):
                rules = EXTRA_SKILLS.get(key, {})
                extra += rules.get(agent.get("id"), []) + (rules.get("*last*", []) if i == len(agents) - 1 else [])
            new = [x for x in dict.fromkeys(extra) if x not in (agent.get("skills") or [])]
            if new:
                agent["skills"] = list(agent.get("skills") or []) + new
                if "load_skill" not in (agent.get("tools") or []):
                    agent["tools"] = list(agent.get("tools") or []) + ["load_skill"]
                touched = True
        if touched:
            (dst / f.name).write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), "utf-8")
            changed.append(f.stem)
    return changed


_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def prepare_step_preset(dst: Path, preset: str, user_vars: dict, system_extra: dict[str, str] | None = None) -> bool:
    """Tilpas det klargjorte preset til ét trin, lige før det køres.

    Vibe-Trading erstatter kun {upstream_context} i system-prompten, så {portfolio}, {goal}, {target} m.fl. stod
    som rå pladsholdere i hver agents instruks (set 19/10/2026). Her indsættes trinnets variabler, og tekst til én
    bestemt agent (fx CIO'ens digest af de andre teams) lægges i dens egen instruks i stedet for i {goal}, som alle
    agenter i presettet får. Returnerer True, når filen blev skrevet.
    """
    import yaml

    f = dst / f"{preset}.yaml"
    if not f.exists():
        return False
    spec = yaml.safe_load(f.read_text("utf-8"))
    values = {k: str(v) for k, v in (user_vars or {}).items() if isinstance(v, (str, int, float))}

    def fill(text: str) -> str:
        return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)) if m.group(1) != "upstream_context" else m.group(0), text)

    for agent in spec.get("agents", []):
        agent["system_prompt"] = fill(str(agent.get("system_prompt", "")))
        extra = (system_extra or {}).get(agent.get("id"))
        if extra:
            agent["system_prompt"] = agent["system_prompt"].rstrip() + "\n\n" + extra.strip() + "\n"
    f.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), "utf-8")
    return True


def budget_period_start(ref: dt.datetime, reset_day: int | None = None) -> dt.date:
    """Første dag i budgetperioden.

    Max-abonnementets API-kredit (100 USD) fornyes pr. faktureringsperiode og udløber ved periodens
    slutning, så månedsloftet følger samme dag (``VT_BUDGET_RESET_DAY``, fx 23). 1 = kalendermåned.
    Dage over 28 rundes ned til 28, så perioden findes i alle måneder.
    """
    if reset_day is None:
        try:
            reset_day = int(os.environ.get("VT_BUDGET_RESET_DAY", "1") or 1)
        except ValueError:
            reset_day = 1
    day = min(max(reset_day, 1), 28)
    d = ref.date()
    if d.day >= day:
        return d.replace(day=day)
    y, m = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    return dt.date(y, m, day)


def month_spent(results: Path, ref: dt.datetime, reset_day: int | None = None) -> float:
    """Samlet estimeret forbrug (USD) for rigtige kørsler i den aktuelle budgetperiode."""
    try:
        ledger = json.loads((results / "ledger.json").read_text("utf-8"))
    except Exception:
        return 0.0
    first = budget_period_start(ref, reset_day)
    nxt = dt.date(first.year + (first.month == 12), first.month % 12 + 1, first.day)
    start, end = first.isoformat(), nxt.isoformat()
    return round(sum(float(e.get("cost_usd") or 0) for e in ledger.get("runs", [])
                     if start <= str(e.get("generated_at", ""))[:10] < end and not e.get("dry_run")), 2)


# ---------------------------------------------------------------------------
# Swarm-kørsel
# ---------------------------------------------------------------------------
TERMINAL = {"completed", "failed", "cancelled"}


def _status(x: Any) -> str:
    return getattr(x, "value", x) if x is not None else ""


def run_swarm(runtime: Any, store: Any, preset: str, user_vars: dict, *, timeout_s: int,
              poll_s: float = 2.0, clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep,
              budget_usd: float | None = None, price_in: float = 0.0, price_out: float = 0.0) -> tuple[Any, str | None]:
    """Kør et preset og vent. Returnerer (run, stop) hvor stop er None, "timeout" eller "budget".

    Stop-grunden holdes uden for Vibe-Tradings SwarmRun (en pydantic-model, der afviser nye felter: før
    19/10/2026 gav ``run.timed_out = True`` en ValueError, og en 40 minutters kørsel blev registreret som fejl
    med 0 USD). Budgettet tjekkes undervejs: overstiger de foreløbige tokens budgettet, afbrydes kørslen, og de
    færdige agenters rapporter bevares.
    """
    # Agenterne får ikke shell-adgang (bash-værktøjet udelades); de bruger
    # Vibe-Tradings egne data- og analyseværktøjer.
    run = runtime.start_run(preset, user_vars, include_shell_tools=False)
    deadline = clock() + timeout_s
    current = run

    def cost(r: Any) -> float:
        tin = float(getattr(r, "total_input_tokens", 0) or 0)
        tout = float(getattr(r, "total_output_tokens", 0) or 0)
        return tin / 1e6 * price_in + tout / 1e6 * price_out

    def stop(reason: str) -> tuple[Any, str]:
        with contextlib.suppress(Exception):
            runtime.cancel_run(run.id)
        sleep(min(5.0, poll_s * 2))
        return store.load_run(run.id) or current, reason

    while True:
        current = store.load_run(run.id) or current
        if _status(current.status) in TERMINAL:
            return current, None
        if clock() > deadline:
            return stop("timeout")
        if budget_usd is not None and cost(current) > budget_usd:
            return stop("budget")
        sleep(poll_s)


def _cap(text: str | None, n: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= n else text[:n] + f"\n\n[… afkortet, {len(text) - n} tegn udeladt]"


# Agent uden rapport. Testkørslen 16/10 viste to mønstre, som en simpel optælling ikke fanger:
#   1) en struktureret plan ("## Plan" + punkter med vægte i %) der slutter "Executing the data-gathering calls now."
#   2) en kort statusbesked der slutter fremadrettet ("Jeg færdiggør nu rapporten.", "Now let me get …").
# En rigtig rapport har en resultat-overskrift (konklusion, vurdering, anbefaling …) eller en tabel.
_PLAN_MARKERS = re.compile(r"\b(executing|i will|i'll|let me (?!know)|next,? i|now i|starting with|jeg vil|jeg starter|"
                           r"nu henter|planen er|først henter)(?![a-zæøå])", re.I)
_RESULT_WORDS = re.compile(r"(konklusion|conclusion|anbefal|recommend|vurdering|assessment)", re.I)
_FORWARD_TAIL = re.compile(r"\b(executing|now (?:i'?ll|i will|let me|i am going to)|let me (?:now |also |just )?(?:get|fetch|run|"
                           r"check|compute|calculate|pull|load|start|call|gather|verify)|i'?ll now|i will now|i'?m now|"
                           r"proceeding|starting (?:with|now)|then (?:write|run|compile)|next,? i|jeg færdiggør|"
                           r"færdiggør nu|nu (?:henter|skriver|laver|går|beregner|kører)|jeg går (?:nu )?i gang|"
                           r"jeg (?:skriver|henter|beregner|kører) nu|herefter (?:skriver|henter))(?![a-zæøå])", re.I)
_RESULT_HEADING = re.compile(r"^[ \t]*(?:#{1,6}[ \t]*|\*\*)(?:\d+[.)][ \t]*)?[^\n]{0,40}?(konklusion|conclusion|"
                             r"sammenfatning|resumé|summary|findings|anbefaling|recommendation|resultat|results|vurdering|"
                             r"assessment|beslutning|decision)", re.I | re.M)
_PLAN_HEADING = re.compile(r"[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?(plan|approach|fremgangsmåde|tilgang|metode)(?![a-zæøå])", re.I)


def classify_report(summary: str | None) -> str:
    """'ok', 'empty' (ingen tekst) eller 'plan_only' (agenten beskrev kun, hvad den ville gøre)."""
    text = (summary or "").strip()
    if not text or text.lower().strip("() ") in ("no summary", "ingen opsummering", "none"):
        return "empty"
    table_rows = sum(1 for line in text.splitlines() if line.lstrip().startswith("|"))
    if _RESULT_HEADING.search(text) or table_rows >= 3:
        return "ok"
    if _PLAN_HEADING.match(text) or _FORWARD_TAIL.search(text[-300:]):
        return "plan_only"
    if len(text) < 3000 and _PLAN_MARKERS.search(text) and len(_RESULT_WORDS.findall(text)) < 2:
        return "plan_only"
    return "ok"


def looks_cut(text: str | None) -> bool:
    """Slutter rapporten midt i en sætning? Agenterne skriver report.md i ét værktøjskald, og rammer svaret loftet for
    svarlængde, gemmes kun begyndelsen (CIO 20/10/2026: "… hentet direkte fra E"; Risk Budgeter: "| … | 29").
    Rapporter slutter normalt med "Faktatjek: …" eller et tegn som . ! ? ) | :."""
    t = (text or "").strip()
    if len(t) < 2000:
        return False
    last = next((x.strip() for x in reversed(t.splitlines()) if x.strip()), "")
    if last.startswith("|") and not last.endswith("|"):
        return True
    if re.search(r"faktatjek\s*:", t[-400:], re.I):
        return False
    return not re.search(r"[.!?)\]»\"'*`|:%]$", last)


def summarize_run(run: Any, preset: str, reason: str, started: dt.datetime, duration_s: float,
                  *, price_in: float, price_out: float, report_cap: int = 60_000, task_cap: int = 16_000,
                  stopped: str | None = None) -> dict:
    roles = {a.id: getattr(a, "role", a.id) for a in (getattr(run, "agents", None) or [])}
    tin = int(getattr(run, "total_input_tokens", 0) or 0)
    tout = int(getattr(run, "total_output_tokens", 0) or 0)
    tasks = []
    for t in getattr(run, "tasks", None) or []:
        tasks.append({
            "id": t.id, "agent_id": t.agent_id, "role": roles.get(t.agent_id, t.agent_id),
            "status": _status(t.status), "summary": _cap(t.summary, task_cap), "error": _cap(t.error, 2000),
            "report": classify_report(t.summary) if _status(t.status) == "completed" else None,
            "cut": looks_cut(t.summary),
            "iterations": int(getattr(t, "worker_iterations", 0) or 0),
            "started_at": getattr(t, "started_at", None), "completed_at": getattr(t, "completed_at", None),
        })
    status = stopped or _status(run.status)
    return {
        "preset": preset, "label": PRESET_LABELS.get(preset, preset), "reason": reason,
        "run_id": run.id, "status": status,
        "started_at": started.isoformat(timespec="seconds"),
        "completed_at": getattr(run, "completed_at", None),
        "duration_s": round(duration_s), "provider": getattr(run, "provider", None), "model": getattr(run, "model", None),
        "tokens_in": tin, "tokens_out": tout,
        "est_cost_usd": round(tin / 1e6 * price_in + tout / 1e6 * price_out, 2),
        "final_report": _cap(getattr(run, "final_report", None), report_cap),
        "final_report_cut": looks_cut(getattr(run, "final_report", None)),
        "tasks": tasks,
        "agents_without_report": [t["role"] for t in tasks if t["report"] in ("empty", "plan_only")],
        "agents_cut": [t["role"] for t in tasks if t["cut"] and t["report"] not in ("empty", "plan_only")],
    }


# ---------------------------------------------------------------------------
# Kryptering (kompatibel med WebCrypto i browseren)
# ---------------------------------------------------------------------------
def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _derive(passphrase: str, salt: bytes, iterations: int) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations).derive(passphrase.encode("utf-8"))


def _canonical(value: Any) -> Any:
    """Samme talform som JavaScript: 8.0 bliver 8, så AAD'en er ens i Python og i browseren."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("public metadata må ikke indeholde NaN/inf")
        if value.is_integer():
            return int(value)
        if "e" in repr(value):  # 1e-05 skrives anderledes i JavaScript (0.00001): undgå tallet i metadata
            raise ValueError("public metadata må ikke indeholde tal i eksponentform")
        return value
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def aad_bytes(public: dict) -> bytes:
    """Metadata bindes til krypteringen. Skal give samme bytes som JSON.stringify(sortKeys(public)) i browseren.

    Kørslen 19/10/2026: et beløb som 8.0 blev til "8.0" her og "8" i browseren, så rapporten kunne ikke låses
    op. Nu skrives tal som i JavaScript, og nøgler sorteres rekursivt (browseren sorterer det samme).
    """
    return json.dumps(_canonical(public), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def encrypt(payload: dict, passphrase: str, public: dict, *, iterations: int = PBKDF2_ITER) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, iv = secrets.token_bytes(16), secrets.token_bytes(12)
    key = _derive(passphrase, salt, iterations)
    plain = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0)
    aad = aad_bytes(public)
    ct = AESGCM(key).encrypt(iv, plain, aad)
    return {"schema": SCHEMA, "alg": "AES-256-GCM", "kdf": "PBKDF2-SHA256", "iter": iterations, "zip": "gzip",
            "salt": _b64(salt), "iv": _b64(iv), "ct": _b64(ct), "public": public}


def decrypt(envelope: dict, passphrase: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _derive(passphrase, base64.b64decode(envelope["salt"]), int(envelope["iter"]))
    aad = aad_bytes(envelope["public"])
    plain = AESGCM(key).decrypt(base64.b64decode(envelope["iv"]), base64.b64decode(envelope["ct"]), aad)
    return json.loads(gzip.decompress(plain))


def fit_payload(payload: dict, passphrase: str, public: dict, iterations: int) -> dict:
    """Krymp rapporterne indtil kuverten passer i ét artifact-db dokument."""
    caps = [(60_000, 16_000), (30_000, 8_000), (15_000, 4_000), (8_000, 1_500)]
    env = encrypt(payload, passphrase, public, iterations=iterations)
    for rc, tc in caps[1:]:
        if len(json.dumps(env)) <= MAX_ENVELOPE_BYTES:
            break
        for r in payload["runs"]:
            r["final_report"] = _cap(r.get("final_report"), rc)
            for t in r.get("tasks", []):
                t["summary"] = _cap(t.get("summary"), tc)
        payload["truncated"] = True
        env = encrypt(payload, passphrase, public, iterations=iterations)
    if len(json.dumps(env)) > MAX_ENVELOPE_BYTES:  # sidste udvej: log-udsnit og datatjek-detaljer
        payload["log_tail"] = _cap(payload.get("log_tail"), 1500)
        if isinstance(payload.get("datacheck"), dict):
            payload["datacheck"] = {k: v for k, v in payload["datacheck"].items()
                                    if k in ("status", "issues", "counts", "ref_date", "earnings", "portfolio", "sources")}
        payload["truncated"] = True
        env = encrypt(payload, passphrase, public, iterations=iterations)
    return env


# ---------------------------------------------------------------------------
# Resultatfiler
# ---------------------------------------------------------------------------
def read_latest_meta(results: Path) -> dict | None:
    f = results / "latest.enc.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text("utf-8")).get("public")
    except Exception:
        return None


def write_results(results: Path, envelope: dict) -> None:
    pub = envelope["public"]
    (results / "weeks").mkdir(parents=True, exist_ok=True)
    body = json.dumps(envelope, ensure_ascii=False, indent=1) + "\n"
    rid = pub.get("run_id") or pub["week_id"]
    (results / "weeks" / f"{rid}.enc.json").write_text(body, "utf-8")
    latest_f = results / "latest.enc.json"
    current = read_latest_meta(results) or {}
    if str(current.get("generated_at") or "") <= str(pub.get("generated_at") or ""):  # en ældre kørsel overskriver ikke nyeste
        latest_f.write_text(body, "utf-8")
    idx_f = results / "index.json"
    try:
        idx = json.loads(idx_f.read_text("utf-8"))
    except Exception:
        idx = {"schema": SCHEMA, "weeks": []}
    idx["weeks"] = [w for w in idx.get("weeks", []) if (w.get("run_id") or w.get("week_id")) != rid]
    idx["weeks"].insert(0, pub)
    idx["weeks"] = sorted(idx["weeks"], key=lambda w: w["generated_at"], reverse=True)[:156]
    idx["latest"] = idx["weeks"][0].get("run_id") or idx["weeks"][0]["week_id"]
    idx_f.write_text(json.dumps(idx, ensure_ascii=False, indent=1) + "\n", "utf-8")
    led_f = results / "ledger.json"
    try:
        led = json.loads(led_f.read_text("utf-8"))
    except Exception:
        led = {"schema": SCHEMA, "runs": []}
    led["runs"] = [e for e in led.get("runs", []) if (e.get("run_id") or e.get("week_id")) != rid]
    led["runs"].append({k: pub.get(k) for k in ("run_id", "generated_at", "week_id", "cost_usd", "status", "dry_run", "runs", "completed")})
    led["runs"] = sorted(led["runs"], key=lambda e: str(e.get("generated_at") or ""))[-400:]
    led_f.write_text(json.dumps(led, ensure_ascii=False, indent=1) + "\n", "utf-8")


def env_num(name: str, default: float, *, lo: float, hi: float) -> float:
    """Tal fra workflow-input eller repo-variabel. "35,5" accepteres; andet giver en kort fejl uden traceback."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        raise ValueError(f"{name} skal være et tal (fik tekst)") from None
    if not lo <= value <= hi:
        raise ValueError(f"{name} skal ligge mellem {lo:g} og {hi:g}")
    return value


def set_output(**kv: Any) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for k, v in kv.items():
            fh.write(f"{k}={v}\n")


# ---------------------------------------------------------------------------
# Falsk runtime til tørkørsel og test
# ---------------------------------------------------------------------------
class FakeRuntime:
    """Efterligner SwarmRuntime/SwarmStore uden LLM-kald (bruges af --dry-run og tests)."""

    def __init__(self, fail: Iterable[str] = (), empty: Iterable[str] = ()):
        self.fail = set(fail)
        self.empty = set(empty)  # presets, hvor agent 2 ikke leverer en rapport
        self.runs: dict[str, Any] = {}

    def start_run(self, preset: str, user_vars: dict, include_shell_tools: bool = False) -> Any:
        rid = f"dry-{preset}-{len(self.runs) + 1}"
        agents = [SimpleNamespace(id=f"{preset}_a{i}", role=f"Analytiker {i}") for i in (1, 2)]
        ok = preset not in self.fail
        empty = preset in self.empty
        tasks = [SimpleNamespace(id=f"task-{i}", agent_id=a.id, status="completed" if ok else "failed",
                                 summary=(None if empty and i == 2 else f"Tørkørsel: {a.role} for {preset}.") if ok else None,
                                 error=None if ok else "Simuleret fejl", worker_iterations=3,
                                 started_at=None, completed_at=None) for i, a in enumerate(agents, 1)]
        run = SimpleNamespace(id=rid, status="completed" if ok else "failed", agents=agents, tasks=tasks,
                              final_report=f"# {PRESET_LABELS.get(preset, preset)}\n\nTørkørsel – ingen LLM-kald.\n\n"
                                           f"Variabler: {', '.join(sorted(user_vars))}" if ok else None,
                              total_input_tokens=1200, total_output_tokens=300, completed_at=dt.datetime.now(STO).isoformat(),
                              provider="dry-run", model="none")
        self.runs[rid] = run
        return run

    def load_run(self, rid: str) -> Any:
        return self.runs.get(rid)

    def cancel_run(self, rid: str) -> bool:
        return True


# ---------------------------------------------------------------------------
# Rettelser i Vibe-Trading 0.1.16 (anvendes i processen, før SwarmRuntime kører)
# ---------------------------------------------------------------------------
# Fundet efter kørslen 19/10/2026: hver worker overskriver ALLE værktøjsresultater undtagen de 3 nyeste med
# "[cleared]" i hver iteration. En agent, der henter 5–6 kurser, har mistet de første, før den skriver rapporten,
# og må gentage kaldene eller skrive uden tallene. Vibetrad beholder resultaterne, indtil de samlet fylder mere end budgettet, og
# rydder så de ældste ned til halvdelen (få ændringer → prompt-cachen holder), med en markør der siger, at
# kaldet lykkedes.
VT_TOOL_CONTEXT_CHARS = 40_000
_VT_CLEARED = "[CLEARED FROM CONTEXT"
_VT_MICROCOMPACT = (
    '        tool_msgs = [m for m in messages if m.get("role") == "tool"]\n'
    "        if len(tool_msgs) > _KEEP_RECENT_TOOLS:\n"
    "            for msg in tool_msgs[:-_KEEP_RECENT_TOOLS]:\n"
    '                content = msg.get("content", "")\n'
    "                if isinstance(content, str) and len(content) > 100:\n"
    '                    msg["content"] = "[cleared]"\n'
)


def _tool_context_budget() -> int:
    try:
        return max(5_000, int(os.environ.get("VT_TOOL_CONTEXT_CHARS", VT_TOOL_CONTEXT_CHARS)))
    except ValueError:
        return VT_TOOL_CONTEXT_CHARS


def _looks_like_error(text: str) -> bool:
    """Fejl-kuvert fra et værktøj ({"status": "error"} eller ok/success = false), som Vibe-Tradings egen regel."""
    t = text.strip()
    if not t.startswith("{"):
        return False
    try:
        data = json.loads(t)
    except ValueError:
        return bool(re.search(r'^\{\s*"(?:status)"\s*:\s*"error"', t[:200]))
    return isinstance(data, dict) and (data.get("status") == "error" or data.get("ok") is False
                                       or data.get("success") is False)


def _vibetrad_compact_tool_results(messages: list, keep_recent: int = 3, budget: int | None = None) -> int:
    """Ryd gamle værktøjsresultater først, når de tilsammen fylder mere end ``budget`` tegn.

    De ``keep_recent`` nyeste røres aldrig. Ellers ryddes de ældste (over 100 tegn), indtil resten er under
    halvdelen af budgettet. Returnerer antal ryddede resultater.
    """
    budget = _tool_context_budget() if budget is None else budget
    tools = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]

    def size(m: dict) -> int:
        c = m.get("content")
        return len(c) if isinstance(c, str) else 0

    total = sum(size(m) for m in tools)
    if total <= budget:
        return 0
    cleared = 0
    for m in tools[:-keep_recent] if keep_recent > 0 else tools:
        if total <= budget // 2:
            break
        c = m.get("content")
        if not isinstance(c, str) or len(c) <= 100 or c.startswith(_VT_CLEARED):
            continue
        if _looks_like_error(c):
            marker = (f"{_VT_CLEARED}: this tool call FAILED (error, {len(c)} characters). It was removed to keep "
                      "the context small. Never present a number from it.]")
        else:
            marker = (f"{_VT_CLEARED}: this tool call SUCCEEDED and returned {len(c)} characters. It was removed "
                      "to keep the context small. Use the numbers you already noted from it; repeat the call only "
                      "if you need exact figures you did not note.]")
        if len(marker) >= len(c):  # rydning ville ikke spare plads
            continue
        m["content"] = marker
        total += len(marker) - len(c)
        cleared += 1
    return cleared


def patch_vibe_trading() -> dict[str, str]:
    """Anvend Vibetrads rettelser i Vibe-Trading. Fejler aldrig: status pr. rettelse til rapporten."""
    status: dict[str, str] = {}
    try:
        import inspect

        from src.swarm import worker as w

        fn = w._run_worker_impl
        if "_vibetrad_compact_tool_results" in fn.__code__.co_names:
            status["tool_context"] = "ok (allerede anvendt)"
        else:
            src = inspect.getsource(fn)
            if src.count(_VT_MICROCOMPACT) != 1:
                status["tool_context"] = "ikke anvendt: Vibe-Tradings kode er ændret (opdatér rettelsen)"
            else:
                # Samme antal linjer og samme startlinje som originalen, så tracebacks peger det rigtige sted i worker.py
                repl = "        _vibetrad_compact_tool_results(messages, _KEEP_RECENT_TOOLS)\n"
                repl += "\n" * (_VT_MICROCOMPACT.count("\n") - 1)
                new = "\n" * (fn.__code__.co_firstlineno - 1) + src.replace(_VT_MICROCOMPACT, repl)
                import __future__

                code = compile(new, inspect.getsourcefile(fn) or "<vibetrad>", "exec",
                               flags=__future__.annotations.compiler_flag, dont_inherit=True)
                w.__dict__["_vibetrad_compact_tool_results"] = _vibetrad_compact_tool_results
                ns: dict[str, Any] = {}
                exec(code, w.__dict__, ns)  # noqa: S102  (vores egen, kendte erstatning af én blok)
                patched = ns["_run_worker_impl"]
                patched.__doc__, patched.__module__ = fn.__doc__, fn.__module__
                w._run_worker_impl = patched
                status["tool_context"] = f"ok (budget {_tool_context_budget()} tegn)"
    except Exception as exc:  # noqa: BLE001
        status["tool_context"] = f"ikke anvendt: {type(exc).__name__}"
    status["temperature"] = _patch_temperature()
    status["prompt"] = _patch_worker_prompt()
    status["grounding"] = _patch_grounding()
    status["retries"] = _patch_stream_retries()
    status["contract"] = _patch_deliverable_contract()
    return status


def _patch_deliverable_contract() -> str:
    """Vibe-Trading 0.1.16 afviser en "data-agent" (ethvert værktøj ud over bash/filer/load_skill), der svarer uden et
    eneste værktøjskald og uden report.md. Uden filværktøjer (fjernet 19/10/2026) findes report.md ikke, og de
    syntetiserende agenter (strateg, redaktør, dommer, attribution) fik kvant-værktøjer af runneren og blev dermed
    "data-agenter": svarede de ud fra de andre agenters rapporter og facit, blev de afvist, og hele teamet faldt
    (fire af seks teams 19/10/2026 om aftenen, inkl. CIO'en). Kravet om værktøjskald slås fra, som før
    rettelsen, hvor report.md altid opfyldte det; Vibe-Tradings øvrige kontrol (tomme svar, planer, rå
    værktøjsresultater, "mock data") gælder stadig, og runneren markerer selv agenter med plan eller uden tekst."""
    try:
        from src.swarm import worker as w

        if getattr(w._classify_deliverable, "_vibetrad", False):
            return "ok (allerede anvendt)"
        orig = w._classify_deliverable

        def classify(summary: str, *, is_data_agent: bool, report_written: bool, data_tool_calls: int) -> str | None:
            reason = orig(summary, is_data_agent=is_data_agent, report_written=report_written,
                          data_tool_calls=data_tool_calls)
            if reason and reason.startswith("data agent produced no tool calls"):
                return None
            return reason

        classify._vibetrad = True  # type: ignore[attr-defined]
        classify.__doc__ = orig.__doc__
        w._vibetrad_orig_classify_deliverable = orig
        w._classify_deliverable = classify
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"ikke anvendt: {type(exc).__name__}"


def _stream_retry_plan(retries: int | None = None) -> tuple[int, list[float]]:
    """Antal ekstra forsøg og ventetider (sekunder) for et LLM-kald, der fejler midlertidigt."""
    try:
        n = int(os.environ.get("VT_STREAM_RETRIES", "") or 4) if retries is None else retries
    except ValueError:
        n = 4
    n = max(0, min(n, 10))
    return n, [min(60.0, 5.0 * 2 ** i) for i in range(n)]


def _patch_stream_retries() -> str:
    """Vibe-Trading 0.1.16 prøver et LLM-kald igen præcis én gang, når udbyderen svarer 429/529 eller forbindelsen
    falder; derefter er agenten fejlet, og med max_retries 1 i de fleste presets starter den forfra én gang og
    fejler så hele teamet (fire af seks teams 19/10/2026 om aftenen, amerikansk spidsbelastning). Her prøves
    midlertidige fejl op til VT_STREAM_RETRIES (standard 4) gange mere med stigende ventetid (5, 10, 20, 40 s;
    Retry-After fra udbyderen respekteres), før fejlen gives videre. Endelige fejl (4xx undtagen 408/429) går
    straks videre som før."""
    try:
        from src.providers import chat as c

        if getattr(c.ChatLLM.stream_chat, "_vibetrad", False):
            return "ok (allerede anvendt)"
        orig = c.ChatLLM.stream_chat
        n, delays = _stream_retry_plan()
        if n == 0:
            return "slået fra"

        def stream_chat(self: Any, messages: Any, *args: Any, **kwargs: Any) -> Any:
            should_cancel = kwargs.get("should_cancel")
            for attempt in range(n + 1):
                try:
                    return orig(self, messages, *args, **kwargs)
                except c.ProviderStreamError as exc:
                    if not exc.retryable or attempt >= n or (should_cancel and should_cancel()):
                        raise
                    wait = delays[attempt]
                    if exc.retry_after_s is not None:
                        wait = min(60.0, max(wait, float(exc.retry_after_s)))
                    logging.getLogger("vibetrad").warning("LLM-kald fejlede midlertidigt (forsøg %d af %d), venter %.0f s: %s",
                                                          attempt + 1, n + 1, wait, type(exc.original).__name__)
                    time.sleep(wait)
            raise AssertionError("unreachable")  # pragma: no cover

        stream_chat._vibetrad = True  # type: ignore[attr-defined]
        stream_chat.__doc__ = orig.__doc__
        c.ChatLLM._vibetrad_orig_stream_chat = orig
        c.ChatLLM.stream_chat = stream_chat
        return f"ok ({n} ekstra forsøg)"
    except Exception as exc:  # noqa: BLE001
        return f"ikke anvendt: {type(exc).__name__}"


_VT_HARD_LIMIT = "You have a HARD LIMIT of 20 tool calls. After that you will be cut off. Work efficiently.\n\n"
_VT_PLAN_LINE = "**Plan (0 tool calls):** Before calling any tool, state your plan in 3-5 bullet points.\n\n"
_VT_PROMPT_FIX = ("Work efficiently: context is limited, so fetch only what you need, note the numbers as they arrive, "
                  "and never finish with a plan instead of a result.\n\n")


def _patch_worker_prompt() -> str:
    """Vibe-Trading 0.1.16 skriver "HARD LIMIT of 20 tool calls" og beder om en plan først, selv om grænsen ikke
    håndhæves (max_iterations er 50). Agenterne stoppede efter 20 kald eller afleverede planen (19/10/2026)."""
    try:
        from src.swarm import worker as w

        orig = getattr(w, "_vibetrad_orig_build_worker_prompt", None) or w.build_worker_prompt
        probe = orig.__doc__ or ""
        if getattr(w.build_worker_prompt, "_vibetrad", False):
            return "ok (allerede anvendt)"

        def build_worker_prompt(*args: Any, **kwargs: Any) -> str:
            text = orig(*args, **kwargs)
            return text.replace(_VT_HARD_LIMIT, _VT_PROMPT_FIX).replace(_VT_PLAN_LINE, "")

        build_worker_prompt._vibetrad = True  # type: ignore[attr-defined]
        build_worker_prompt.__doc__ = probe
        w._vibetrad_orig_build_worker_prompt = orig
        w.build_worker_prompt = build_worker_prompt
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"ikke anvendt: {type(exc).__name__}"


def _patch_grounding() -> str:
    """Grounding henter 30 dages kurser for symboler i opgaveteksten. Bare store ord (SEK, ISK, NOVO, PASS …)
    blev gjort til .US-symboler og hentet forgæves (kørslen 19/10/2026: NOVO.US, DKK.US). Vibetrad skriver altid
    .US eksplicit, så kun eksplicitte symboler bruges."""
    try:
        from src.swarm import grounding as g

        if getattr(g.extract_symbols_from_user_vars, "_vibetrad", False):
            return "ok (allerede anvendt)"
        orig = g.extract_symbols_from_user_vars

        def extract(user_vars: dict) -> list:
            found = orig(user_vars)
            text = "\n".join(str(v) for v in (user_vars or {}).values() if isinstance(v, str))
            return [s for s in found if s in text]  # kun symboler, der står ordret i teksten

        extract._vibetrad = True  # type: ignore[attr-defined]
        g.extract_symbols_from_user_vars = extract
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"ikke anvendt: {type(exc).__name__}"


# Claude-modeller af 5. generation afviser `temperature`. Vibe-Trading opdager det først ved første fejl, og i
# kørslen 19/10/2026 fejlede de parallelle agenters første kald derfor, før de blev forsøgt igen.
_NO_TEMPERATURE = re.compile(r"^claude-[a-z]+-5(?:$|[-.])")


def _patch_temperature() -> str:
    model = str(os.environ.get("LANGCHAIN_MODEL_NAME") or "").strip()
    mode = os.environ.get("VT_TEMPERATURE", "auto").strip().lower()
    if not model or mode in ("on", "send"):
        return "ikke ændret"
    if mode not in ("off", "omit") and not _NO_TEMPERATURE.match(model):
        return "ikke ændret (modellen accepterer temperature)"
    try:
        from src.providers import llm

        llm._ANTHROPIC_TEMPERATURE_UNSUPPORTED.add(model)
        return "udelades fra første kald"
    except Exception as exc:  # noqa: BLE001
        return f"ikke anvendt: {type(exc).__name__}"


def make_runtime(dry: bool) -> tuple[Any, Any, str]:
    if dry:
        fake = FakeRuntime(fail=filter(None, os.environ.get("VT_DRY_FAIL", "").split(",")),
                           empty=filter(None, os.environ.get("VT_DRY_EMPTY", "").split(",")))
        return fake, fake, "dry-run"
    from importlib.metadata import version

    from src.config import load_swarm_agent_config
    from src.config.paths import get_swarm_runs_dir
    from src.swarm.runtime import SwarmRuntime
    from src.swarm.store import SwarmStore

    store = SwarmStore(base_dir=get_swarm_runs_dir())
    runtime = SwarmRuntime(store=store, agent_config=load_swarm_agent_config())
    return runtime, store, version("vibe-trading-ai")


VT_PATCHES: dict[str, str] = {}


def validate_vars(preset: str, user_vars: dict, dry: bool) -> None:
    if dry:
        return
    from src.swarm.presets import load_preset

    spec = load_preset(preset)
    missing = [v["name"] for v in spec.get("variables", []) if v.get("required") and not str(user_vars.get(v["name"], "")).strip()]
    if missing:
        raise ValueError(f"{preset} mangler variabler: {', '.join(missing)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", default="results")
    ap.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", "local"))
    ap.add_argument("--force", action="store_true", default=os.environ.get("VT_FORCE", "") == "true")
    ap.add_argument("--force-risk", action="store_true", default=os.environ.get("VT_FORCE_RISK", "") == "true")
    ap.add_argument("--dry-run", action="store_true", default=os.environ.get("VT_DRY_RUN", "") == "true")
    ap.add_argument("--canary", action="store_true", default=kn.env_flag(),
                    help="kanariefugl: tørkørsel, der tester nøgler, Vibe-Trading, datakilder og kryptering (results/canary.json)")
    ap.add_argument("--now", default=os.environ.get("VT_NOW"))
    ap.add_argument("--gate-only", action="store_true", help="afgør kun om der skal køres (ingen secrets)")
    ap.add_argument("--merge-results", metavar="FIL",
                    help="læg en gemt kuvert (results/weeks/*.enc.json) ind i index, ledger og latest igen (push-konflikt)")
    args = ap.parse_args(argv)
    if args.canary:
        args.dry_run = True  # kanariefuglen bruger aldrig API-kredit

    con = Console()
    results = Path(args.results)
    results.mkdir(parents=True, exist_ok=True)
    if args.merge_results:
        try:
            envelope = json.loads(Path(args.merge_results).read_text("utf-8"))
            assert envelope.get("schema") == SCHEMA and envelope.get("public", {}).get("generated_at"), "ikke en resultat-kuvert"
            if kn.is_canary_envelope(envelope):
                kn.write(results, envelope)
            else:
                write_results(results, envelope)
        except Exception as exc:  # noqa: BLE001
            con.say(f"FEJL: kuverten kunne ikke lægges ind ({type(exc).__name__}).")
            return 3
        con.say(f"Kuvert {envelope['public'].get('run_id') or envelope['public']['week_id']} lagt ind i resultaterne igen.")
        return 0
    ref = now_sto(args.now)
    week = week_window(ref)

    ok, why = should_run(ref, read_latest_meta(results), args.event, args.force, canary=args.canary)
    con.say(f"Stockholm {ref:%d/%m/%Y %H:%M} · {week['label']} · {'KØRER' if ok else 'springer over'}: {why}")
    if not ok or args.gate_only:
        set_output(ran="true" if ok else "false", status="gate" if ok else "skipped", week=week["id"])
        return 0

    passphrase = os.environ.get("VT_RESULTS_PASSPHRASE", "")
    if len(passphrase) < MIN_PASSPHRASE:
        con.say(f"FEJL: secret VT_RESULTS_PASSPHRASE mangler eller er kortere end {MIN_PASSPHRASE} tegn.")
        set_output(ran="false", status="config_error", week=week["id"])
        return 2
    try:
        portfolio = load_portfolio(os.environ.get("VT_PORTFOLIO"))
    except PortfolioError as exc:
        con.say(f"FEJL: {exc}")
        set_output(ran="false", status="config_error", week=week["id"])
        return 2
    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        con.say("FEJL: secret ANTHROPIC_API_KEY mangler.")
        set_output(ran="false", status="config_error", week=week["id"])
        return 2

    try:
        max_swarms = int(env_num("VT_MAX_SWARMS", 6, lo=1, hi=12))
        run_budget = env_num("VT_RUN_BUDGET_USD", 35, lo=0, hi=1000)
        month_budget = env_num("VT_MONTHLY_BUDGET_USD", 95, lo=0, hi=10000)
        cio_reserve = env_num("VT_CIO_RESERVE_USD", 8, lo=0, hi=100)
        min_swarm = env_num("VT_MIN_SWARM_USD", 0.5, lo=0, hi=100)
        timeout_s = int(env_num("VT_SWARM_TIMEOUT_MIN", 45, lo=1, hi=300) * 60)
        price_in = env_num("VT_PRICE_IN_PER_MTOK", 2, lo=0, hi=1000)
        price_out = env_num("VT_PRICE_OUT_PER_MTOK", 10, lo=0, hi=1000)
        iterations = int(env_num("VT_PBKDF2_ITER", PBKDF2_ITER, lo=1000, hi=5_000_000))
    except ValueError as exc:
        con.say(f"FEJL: {exc}")
        set_output(ran="false", status="config_error", week=week["id"])
        return 2
    plan_kind = os.environ.get("VT_PLAN", "alt").strip().lower() or "alt"

    period_start = budget_period_start(ref)
    spent_before = month_spent(results, ref)  # også i en tørkørsel, så budgettet kan tjekkes gratis
    budget = max(0.0, min(run_budget, month_budget - spent_before))
    con.say(f"Budget: {budget:.2f} USD til denne kørsel (brugt siden {dmy(period_start)}: "
            f"{spent_before:.2f} af {month_budget:.0f} USD)")

    log_path = Path(tempfile.gettempdir()) / "vibetrad-vt.log"
    log_path.write_bytes(b"")
    errors: list[dict] = []
    runs: list[dict] = []
    t0 = time.monotonic()

    # EODHD kobles ind i Vibe-Trading (datakilde, routing af nordiske symboler, agent-værktøj),
    # og datatjekket krydstjekker porteføljens tal mellem de to kilder, før agenterne starter.
    eodhd_info: dict = {"key": bool(eo.api_key())}
    if not args.dry_run:
        with quiet_fds(log_path):
            try:
                eodhd_info = eo.install_into_vibe_trading()
            except Exception as exc:  # noqa: BLE001 - uden EODHD kører Vibe-Trading som før
                eodhd_info = {"key": bool(eo.api_key()), "error": f"{type(exc).__name__}: {eo.redact(exc)}"[:500]}
                errors.append({"stage": "eodhd", "type": type(exc).__name__, "message": eo.redact(str(exc))[:2000],
                               "trace": eo.redact(traceback.format_exc())[-4000:]})
        if eodhd_info.get("error"):
            con.say("EODHD kunne ikke kobles på Vibe-Trading (detaljer krypteret i rapporten)")
    datacheck: dict | None = None
    vt_why = ""
    if os.environ.get("VT_DATATJEK", "on").strip().lower() not in ("off", "0", "false", "nej"):
        with quiet_fds(log_path):  # kilderne skriver advarsler, der kan nævne symboler
            try:
                vt_src = None
                if os.environ.get("VT_DATATJEK_VT", "on").strip().lower() == "off":
                    vt_why = "slået fra med VT_DATATJEK_VT=off"
                    vt_src = dtj.DisabledSource("Vibe-Trading")
                elif args.dry_run and not dtj.VibeTradingSource().available:
                    vt_why = "installeres ikke i en tørkørsel"
                    vt_src = dtj.DisabledSource("Vibe-Trading", vt_why)
                datacheck = dtj.run(portfolio, week, ref, vt=vt_src)
            except Exception as exc:  # noqa: BLE001 - datatjekket må aldrig stoppe kørslen
                errors.append({"stage": "datatjek", "type": type(exc).__name__, "message": eo.redact(str(exc))[:2000],
                               "trace": eo.redact(traceback.format_exc())[-4000:]})
        if datacheck:
            c = datacheck["counts"]
            s = datacheck["sources"]
            acc = s["eodhd"].get("account") or {}
            if not s["eodhd"]["available"]:
                eod_txt = "nej (secret EODHD_API_KEY mangler)"
            elif acc and not acc.get("ok") and not s["eodhd"]["calls"]:
                eod_txt = "nøglen virker ikke" + (" (HTTP 401: forkert nøgle)" if "401" in str(acc.get("error")) else "")
            else:
                eod_txt = f"ja, nøglen virker ({s['eodhd']['calls']} kald)"
            vt_txt = ("ja" if s["vibe_trading"]["available"]
                      else f"nej ({vt_why})" if vt_why else "nej")
            con.say(f"Datatjek: {datacheck['status']} · {c['ok']} ok, {c['warn']} advarsler, {c['fail']} fejl · "
                    f"Vibe-Trading {vt_txt} · EODHD {eod_txt}")
        else:
            con.say("Datatjek: kunne ikke gennemføres (detaljer krypteret i rapporten)")

    # FRED: tester nøglen (også i en tørkørsel) og giver alle agenter et makro-facit.
    fred_snap: dict | None = None
    with quiet_fds(log_path):
        try:
            fred_snap = fr.snapshot(ref.date().isoformat())
        except Exception as exc:  # noqa: BLE001 - FRED må aldrig stoppe kørslen
            errors.append({"stage": "fred", "type": type(exc).__name__, "message": fr.redact(str(exc))[:2000],
                           "trace": fr.redact(traceback.format_exc())[-4000:]})
    con.say(fr.status_line(fred_snap))
    fred_ready = (bool(fred_snap.get("ok")) if fred_snap.get("key") else None) if fred_snap else None

    # Europa og Norden: Riksbanken, Nationalbanken, ECB og Fed samt 10-årige statsrenter fra EODHD (FRED er kun USA).
    makro_snap: dict | None = None
    if eo.api_key():
        with quiet_fds(log_path):
            try:
                makro_snap = mk.snapshot(eo.EodhdClient(), ref.date().isoformat())
            except Exception as exc:  # noqa: BLE001 - makro må aldrig stoppe kørslen
                errors.append({"stage": "makro", "type": type(exc).__name__, "message": eo.redact(str(exc))[:2000],
                               "trace": eo.redact(traceback.format_exc())[-4000:]})
        con.say(mk.status_line(makro_snap))

    horizon = int(os.environ.get("VT_EARNINGS_DAYS", "14"))
    with quiet_fds(log_path):  # yfinance skriver advarsler, der kan nævne symboler
        verified = dtj.verified_reports(datacheck)
        reports = upcoming_reports(portfolio, ref, horizon_days=horizon,
                                   lookup=None if args.dry_run or verified else yf_next_report, verified=verified,
                                   alternatives=dtj.alternative_reports(datacheck))
    try:
        history = json.loads((results / "index.json").read_text("utf-8")).get("weeks", [])
    except Exception:
        history = []
    if plan_kind in ("strategi", "alt") or not portfolio["holdings"]:  # strategi og ugerapporten har op til syv teams
        max_swarms = max(max_swarms, 7)
    plan = build_plan(portfolio, ref, week, max_swarms=max_swarms, plan_kind=plan_kind,
                      force_risk=args.force_risk, reports=reports, history=history,
                      by_date=(args.event == "schedule"))
    con.say("Plan: " + ", ".join(s.get("label") or PRESET_LABELS.get(s["preset"], s["preset"]) for s in plan))

    vt_version = "?"
    shellfree: list[str] = []
    with quiet_fds(log_path):
        try:
            runtime, store, vt_version = make_runtime(args.dry_run)
            if not args.dry_run:
                VT_PATCHES.update(patch_vibe_trading())
                brief = "\n\n".join(b for b in (dtj.agent_brief(datacheck), fr.agent_brief(fred_snap),
                                              mk.agent_brief(makro_snap)) if b)
                shellfree = prepare_shellfree_presets(Path.home() / ".vibe-trading" / "swarm" / "presets", brief=brief,
                                                      eodhd_ready=bool(eodhd_info.get("tool") and eodhd_info.get("key")),
                                                      fred_ready=fred_ready)
        except Exception as exc:
            errors.append({"stage": "init", "type": type(exc).__name__, "message": str(exc)[:2000],
                           "trace": traceback.format_exc()[-4000:]})
            runtime = store = None
    if shellfree:
        con.say(f"Presets tilpasset (kvant-værktøjer i stedet for shell, facit, ISK-rammer): {len(shellfree)}")
    if VT_PATCHES:
        con.say("Vibe-Trading-rettelser: " + "; ".join(f"{k} {v}" for k, v in VT_PATCHES.items()))
    if runtime is None:
        con.say(f"FEJL ved opstart af Vibe-Trading ({errors[-1]['type']}). Detaljer ligger krypteret i rapporten.")

    canary: dict | None = None
    health: dict | None = None  # kanariefuglens tjek af kæden – også i hver rigtig rapport (gratis, ingen porteføljedata)
    with contextlib.suppress(Exception):
        health = kn.summary(canary_checks(portfolio, datacheck, fred_snap, makro_snap, runtime is not None, vt_why))
    if args.canary:
        canary = health or kn.summary(canary_checks(portfolio, datacheck, fred_snap, makro_snap, runtime is not None, vt_why))
        for line in kn.console_lines(canary["checks"]):
            con.say(line)
    elif health:
        con.say(f"Kæde-tjek (kanariefuglen): {health['status']} · {health['ok']} ok, {health['warn']} advarsler, {health['fail']} fejl")

    used_usd = 0.0
    if not args.dry_run:
        silence_rest(log_path)  # Vibe-Tradings tråde (også efter en tidsgrænse) må aldrig nå den offentlige log
    for step in plan if runtime else []:
        label = step.get("label") or PRESET_LABELS.get(step["preset"], step["preset"])
        left = budget - used_usd
        reserve = 0.0 if step.get("cio") else cio_reserve
        if left < reserve + min_swarm:
            why = ("Månedsbudget brugt" if budget < run_budget and not used_usd else "Budget for denne kørsel brugt")
            runs.append({"preset": step["preset"], "label": label, "reason": step["reason"], "status": "skipped",
                         "skip_reason": f"{why} ({used_usd:.2f} af {budget:.2f} USD; {cio_reserve:.0f} USD holdes til CIO'en)",
                         "tasks": [], "tokens_in": 0, "tokens_out": 0, "est_cost_usd": 0})
            con.say(f"· {label}: sprunget over (budget)")
            continue
        system_extra: dict[str, str] = dict(step.get("system_extra") or {})
        if step.get("cio"):
            system_extra.setdefault("chief_investment_officer", CIO_DECISION_EXTRA)
            step["vars"]["goal"] = (step["vars"]["goal"] + dtj.cio_instruction(datacheck) + journal_instruction(portfolio)
                                    + health_note(health))
            digest = cio_digest(runs)
            if args.dry_run:
                step["vars"]["goal"] += digest
            elif digest:
                # Kun CIO'en får digesten – henvisningen må derfor heller ikke stå i {goal}, som hele teamet ser
                # (20/10/2026 skrev to analytikere, at afsnittet "indeholdt ingen tekst").
                system_extra["chief_investment_officer"] = "\n\n".join(
                    x for x in (system_extra.get("chief_investment_officer") or CIO_DECISION_EXTRA, digest.strip(),
                                "De øvrige teams' konklusioner står i afsnittet 'Konklusioner fra ugens øvrige "
                                "Vibe-Trading-teams' ovenfor.") if x)
        if not args.dry_run:
            with contextlib.suppress(Exception):  # kun kosmetik for agenterne: må aldrig stoppe kørslen
                prepare_step_preset(Path.home() / ".vibe-trading" / "swarm" / "presets", step["preset"], step["vars"],
                                    system_extra)
        started, m0 = dt.datetime.now(STO), time.monotonic()
        con.say(f"· {label}: starter")
        with quiet_fds(log_path):
            try:
                validate_vars(step["preset"], step["vars"], args.dry_run)
                run, stopped = run_swarm(runtime, store, step["preset"], step["vars"], timeout_s=timeout_s,
                                         poll_s=0.01 if args.dry_run else 5.0, budget_usd=left - reserve,
                                         price_in=price_in, price_out=price_out)
                summary = summarize_run(run, step["preset"], step["reason"], started, time.monotonic() - m0,
                                        price_in=price_in, price_out=price_out, stopped=stopped)
                if step.get("context"):
                    summary["context"] = step["context"]
                summary["label"] = label  # et trin kan have sit eget navn (strategikørslen)
                runs.append(summary)
            except Exception as exc:
                errors.append({"stage": step["preset"], "type": type(exc).__name__, "message": str(exc)[:2000],
                               "trace": traceback.format_exc()[-4000:]})
                runs.append({"preset": step["preset"], "label": label, "reason": step["reason"], "status": "error",
                             "error": f"{type(exc).__name__}: {str(exc)[:500]}", "tasks": [],
                             "tokens_in": 0, "tokens_out": 0, "est_cost_usd": 0,
                             "duration_s": round(time.monotonic() - m0)})
        r = runs[-1]
        used_usd += float(r.get("est_cost_usd") or 0)
        nr = r.get("agents_without_report") or []
        miss = f" · {len(nr)} af {len(r.get('tasks') or [])} agenter uden rapport: {', '.join(nr)}" if nr else ""
        cut = (r.get("agents_cut") or []) + (["slutrapporten"] if r.get("final_report_cut") else [])
        miss += f" · afkortet midt i teksten: {', '.join(cut)}" if cut else ""
        if r["status"] not in ("completed", "skipped") and r.get("tasks"):
            # Hvilke agenter faldt, og hvilke nåede at levere (kun roller og fejltype, aldrig fejlteksten)
            bad = [f"{t.get('role')} ({t.get('status')}" + (f": {_error_kind(t.get('error'))}" if t.get("error") else "") + ")"
                   for t in r["tasks"] if t.get("status") not in ("completed", None)]
            ok = [t.get("role") for t in r["tasks"] if t.get("status") == "completed"]
            miss += f" · agenter: {', '.join(bad) or 'ingen fejl registreret'}" + (f" · leverede: {', '.join(ok)}" if ok else "")
        con.say(f"· {label}: {r['status']} · {r.get('duration_s', 0)} s · {r.get('tokens_in', 0) + r.get('tokens_out', 0):,} tokens · {r.get('est_cost_usd', 0):.2f} USD".replace(",", " ") + miss)

    silence_rest(log_path)  # herfra når intet fra Vibe-Tradings tråde GitHub-loggen
    done = [r for r in runs if r["status"] == "completed"]
    started_runs = [r for r in runs if r["status"] != "skipped"]
    status = "ok" if started_runs and len(done) == len(started_runs) else "partial" if done else "failed"
    if canary:  # kanariefuglens status er tjekkene, ikke pladsholder-swarmene
        status = kn.STATUS_TO_RUN[canary["status"]]
    tin = sum(r.get("tokens_in", 0) for r in runs)
    tout = sum(r.get("tokens_out", 0) for r in runs)
    generated = dt.datetime.now(STO).isoformat(timespec="seconds")
    log_tail = fr.redact(eo.redact(log_path.read_text("utf-8", "replace")[-6000:])) if log_path.exists() else ""

    payload = {
        "schema": SCHEMA, "generated_at": generated, "week": week, "status": status,
        "vibe_trading_version": vt_version, "vibe_trading_patches": dict(VT_PATCHES),
        "model": os.environ.get("LANGCHAIN_MODEL_NAME"), "provider": os.environ.get("LANGCHAIN_PROVIDER"),
        "dry_run": args.dry_run, "trigger": args.event,
        "portfolio": {"account": portfolio["account"], "as_of": portfolio.get("as_of"),
                      "weight_policy": portfolio.get("weight_policy", "limit"),
                      "holdings": portfolio["holdings"], "cash_sek": portfolio["cash_sek"],
                      "cash_reserved_sek": portfolio.get("cash_reserved_sek") or 0.0,
                      "weights_cost": cost_weights(portfolio)},
        "upcoming_reports": reports, "plan": [{"preset": s["preset"], "reason": s["reason"]} for s in plan],
        "datacheck": datacheck, "eodhd": eodhd_info, "fred": fred_snap, "makro_eu": makro_snap, "health": health,
        "runs": runs,
        "totals": {"tokens_in": tin, "tokens_out": tout, "duration_s": round(time.monotonic() - t0),
                   "est_cost_usd": round(sum(r.get("est_cost_usd", 0) for r in runs), 2),
                   "price_basis": f"{price_in:g}/{price_out:g} USD pr. mio. tokens (ind/ud) – estimat",
                   "budget_usd": round(budget, 2), "month_spent_before_usd": spent_before,
                   "month_budget_usd": month_budget, "budget_period_start": period_start.isoformat(),
                   "plan_kind": plan_kind, "shellfree_presets": shellfree},
        "errors": errors, "log_tail": log_tail,
        "github": {"repo": os.environ.get("GITHUB_REPOSITORY"), "run_id": os.environ.get("GITHUB_RUN_ID"),
                   "server": os.environ.get("GITHUB_SERVER_URL", "https://github.com")},
    }
    # Én fil pr. kørsel. GITHUB_RUN_ID er ens ved "Re-run", så forsøgsnummeret kommer med fra forsøg 2.
    gh_run, attempt = os.environ.get("GITHUB_RUN_ID"), os.environ.get("GITHUB_RUN_ATTEMPT", "1").strip()
    suffix = (gh_run if attempt in ("", "1") else f"{gh_run}-{attempt}") if gh_run else secrets.token_hex(3)
    run_id = f"{week['id']}_{dt.datetime.fromisoformat(generated):%Y%m%d-%H%M}_{suffix}"
    public = {"run_id": run_id, "week_id": week["id"], "week_label": week["label"], "generated_at": generated, "status": status,
              "runs": len(runs), "completed": len(done), "dry_run": args.dry_run,
              "presets": [r["preset"] for r in done], "week_to": week["to"],
              "cost_usd": round(sum(r.get("est_cost_usd", 0) for r in runs), 2),
              "datacheck": datacheck["status"] if datacheck else "ikke kørt"}
    if canary:
        public["canary"] = {**canary, "duration_s": round(time.monotonic() - t0), "github_run_id": gh_run}
    try:
        envelope = fit_payload(payload, passphrase, public, iterations)
        assert decrypt(envelope, passphrase)["week"]["id"] == week["id"], "kryptering kunne ikke verificeres"
        result_file = kn.write(results, envelope) if canary else write_results(results, envelope)
    except Exception as exc:  # noqa: BLE001 - traceback går til den private log, ikke til GitHub
        traceback.print_exc()
        con.say(f"FEJL: resultatet kunne ikke krypteres eller skrives ({type(exc).__name__}).")
        set_output(ran="false", status="error", week=week["id"])
        return 3
    kb = len(json.dumps(envelope)) / 1024
    con.say(f"Resultat: {status} · {len(done)}/{len(runs)} swarms · {tin + tout:,} tokens · {kb:.0f} KB krypteret".replace(",", " "))
    if canary:
        con.say(f"Kanariefugl: {canary['status']} · {canary['ok']} ok, {canary['warn']} advarsler, {canary['fail']} fejl · "
                f"{round(time.monotonic() - t0)} s · results/canary.json")
    set_output(ran="true", status=status, week=week["id"], run_id=run_id, day=kn.day_label(generated),
               result_file=str(result_file or results / "weeks" / f"{run_id}.enc.json"))
    return 0


def health_note(health: dict | None) -> str:
    """Kæde-tjekket (samme tjek som kanariefuglen) som én linje til CIO'en."""
    if not health:
        return ""
    bad = [f"{c['label']}: {c['note']}" for c in health.get("checks") or [] if c.get("ok") is not True]
    return (f"\n\nKæde-tjek (kanariefuglen) i denne kørsel: {health['status']} ({health['ok']} ok, {health['warn']} advarsler, "
            f"{health['fail']} fejl)" + (". Afvigelser: " + " | ".join(bad[:6]) if bad else "") +
            ". Nævn det kort under Datakvalitet, hvis det påvirker konklusionen.")


def canary_checks(portfolio: dict, datacheck: dict | None, fred_snap: dict | None, makro_snap: dict | None,
                  runtime_ok: bool, vt_why: str = "") -> list[dict]:
    """Kanariefuglens tjek. Noterne må aldrig indeholde porteføljedata: kun status, antal og tekst om kilderne."""
    checks = [kn.check("secrets", "Secrets", True, "VT_RESULTS_PASSPHRASE og VT_PORTFOLIO kunne læses")]
    checks.append(kn.check("anthropic", "Anthropic-nøgle og model",
                           **kn.anthropic_probe(os.environ.get("LANGCHAIN_MODEL_NAME", ""), os.environ.get("ANTHROPIC_API_KEY", ""))))
    checks.append(kn.check("vibe_trading", "Vibe-Trading installeret", **kn.vibe_trading_probe()))
    if datacheck:
        s, c = datacheck["sources"], datacheck["counts"]
        eod = s["eodhd"]
        acc = eod.get("account") or {}
        if not eod["available"]:
            checks.append(kn.check("eodhd", "EODHD-nøgle", False, "secret EODHD_API_KEY mangler"))
        elif acc and not acc.get("ok") and not eod["calls"]:
            checks.append(kn.check("eodhd", "EODHD-nøgle", False, "nøglen virker ikke" + (" (HTTP 401)" if "401" in str(acc.get("error")) else "")))
        else:
            checks.append(kn.check("eodhd", "EODHD-nøgle", True, f"nøglen virker ({eod['calls']} kald)"))
        st = datacheck["status"]
        checks.append(kn.check("datatjek", "Datatjek", True if st == "PASS" else None if st == "ADVARSEL" else False,
                               f"{st} · {c['ok']} ok, {c['warn']} advarsler, {c['fail']} fejl"
                               + (f" · Vibe-Trading {vt_why}" if vt_why else "")))
        stocks = {h["ticker"] for h in portfolio["holdings"] if h["kind"] not in ("cash", "fund", "etf")}
        found = {e["ticker"] for e in datacheck.get("earnings", []) if e.get("verified")}
        missing = len(stocks - found)
        checks.append(kn.check("regnskab", "Regnskabsdatoer", True if not missing else None,
                               "fundet for alle aktier" if not missing else f"mangler for {missing} aktie(r)"))
    else:
        checks.append(kn.check("datatjek", "Datatjek", False, "kunne ikke gennemføres (detaljer krypteret i kuverten)"))
    if fred_snap and fred_snap.get("key"):
        checks.append(kn.check("fred", "FRED", bool(fred_snap.get("ok")), fr.status_line(fred_snap)))
    else:
        checks.append(kn.check("fred", "FRED", None, fr.status_line(fred_snap)))
    if makro_snap is not None:
        checks.append(kn.check("makro", "Makro EU/Norden (EODHD)", bool(makro_snap.get("ok")), mk.status_line(makro_snap)))
    checks.append(kn.check("runtime", "Runner-opstart", runtime_ok, "plan lagt og tørkørsels-motor startet" if runtime_ok
                           else "opstart fejlede (detaljer krypteret i kuverten)"))
    return checks


def _frames(tb: Any, limit: int = 6) -> str:
    """Stien til fejlen uden fejlbeskeden: fil, linje og funktion (offentlig kode), aldrig værdier eller locals."""
    frames = traceback.extract_tb(tb)[-limit:]
    return " ← ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames)) or "ukendt sted"


def _main_guarded() -> int:
    """Uventede fejl må aldrig skrive en traceback (med porteføljedata) i den offentlige GitHub-log.

    Loggen får kun fejltypen og stien til fejlen (filnavne, linjenumre og funktionsnavne fra den offentlige
    kode), så fejlen kan findes. Selve beskeden (som kan indeholde en ticker eller et beløb) går kun til den
    private logfil på runneren."""
    try:
        return main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            log = Path(tempfile.gettempdir()) / "vibetrad-vt.log"
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(traceback.format_exc())
        Console().say(f"FEJL: uventet {type(exc).__name__} i runneren ved {_frames(exc.__traceback__)}. "
                      "Fejlbeskeden står kun i den private log.")
        set_output(ran="false", status="error")
        return 3


if __name__ == "__main__":
    sys.exit(_main_guarded())
