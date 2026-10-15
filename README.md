# vibetrad-runner

Kører det rigtige [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) på min Nordnet-ISK, **når jeg trykker "Start ny kørsel"** i Vibetrad (eller Actions → Vibetrad ugekørsel → Run workflow), og lægger en krypteret rapport klar til Vibetrad-siden på claude.ai.

Den automatiske lørdagskørsel (kl. 08:00 svensk tid) er slået fra. Slå den til igen med repo-variablen `VT_SCHEDULE=on`.

```
knaptryk   GitHub Actions ── vibe-trading-ai (SwarmRuntime) ── Anthropic API
               │  0. Datatjek: porteføljens tal krydstjekkes mellem Vibe-Tradings egne kilder og EODHD
               │  1. Risikoudvalg (hver uge)
               │  2. Regnskabsdesk (hver position med regnskab ≤ 14 dage, op til 2)
               │  3. Månedens team (efter hvilken lørdag i måneden det er):
               │       1. ETF-allokering · 2. Makro, renter og USD/SEK · 3. Fondsvalg til ISK-kernen
               │       4. Værdikomité (Buffett/Munger/Duan/Li Lu) på én position · 5. Idéjagt uden for tech
               │  4. Rotation hver 3. uge: Investeringskomité (overvågningslisten) → Sentiment → Teknisk analyse
               │  5. CIO-porteføljegennemgang med de øvrige teams' konklusioner og eget faktatjek (report_audit)
               ▼
           results/latest.enc.json   (AES-256-GCM, kun ciffertekst i repoet)
               ▼
07:40/19:40 planlagt Claude-opgave henter nye kørsler ind i Vibetrad (eller straks med "Hent nyeste rapport")
               ▼
           Vibetrad → Ugerapport: dekrypteres i browseren med min adgangsfrase
```

Hver kørsel gemmes for sig (`results/weeks/<uge>_<tidspunkt>_<run-id>.enc.json`), så flere kørsler i samme uge ikke overskriver hinanden. Ved knaptryk vælges månedens team og rotationsteamet automatisk: det team, der længst tid ikke har kørt (tørkørsler tæller ikke). Input `monthly_slot` tvinger et bestemt månedsteam. Ved planlagte lørdagskørsler vælges efter dato.

Budget-vagten starter teams i rækkefølgen ovenfor og springer de sidste ekstra teams over, før CIO'ens reserve rammes.

## Datatjek: Vibe-Trading × EODHD

Før agenterne starter, henter `runner/datatjek.py` de samme tal fra to uafhængige kæder og sammenligner dem:

| Kilde | Hvad | Hvordan |
|---|---|---|
| **Vibe-Trading (HKUDS)** | Kurser, valutakurser | Præcis den loader-kæde, agenternes `get_market_data` bruger (Yahoo → Stooq → … → yfinance). EODHD holdes ude af kæden, så sammenligningen er ægte. |
| | Navn, valuta, markedsværdi, P/E, regnskabsdato | Vibe-Tradings egen Yahoo-klient (`quoteSummary`), med yfinance som reserve |
| **EODHD** | Kurser (justeret), valutakurser, nøgletal, IPO-dato, regnskabskalender (før/efter åbning), udbytter og splits | EODHD's REST-API (secret `EODHD_API_KEY`) |
| **Porteføljen** | Regnskabsdatoer | `next_reports` og noter på overvågningslisten i `VT_PORTFOLIO` |

| Kontrol | PASS | ADVARSEL | FEJL |
|---|---|---|---|
| Lukkekurs samme handelsdag | ≤ 0,5 % | ≤ 2 % | > 2 % |
| Ugeafkast | ≤ 0,5 pp | ≤ 2 pp | > 2 pp |
| Valutakurs (USD/SEK, DKK/SEK …) | ≤ 0,3 % | ≤ 1 % | > 1 % |
| Porteføljens værdi i SEK | ≤ 0,5 % | ≤ 2 % | > 2 % |
| Seneste handelsdag | ens | forskellig | > 6 dage gammel |
| Valuta | som børsen | | anden valuta |
| Navn | fælles ord | intet fælles | |
| Markedsværdi | ≤ 5 % | > 20 % (info: aktieklasser) | |
| Regnskabsdato | kilderne er enige (±1 dag) | uenige eller ukendt (flertallet bliver facit) | |
| Kun én kilde har data | | ADVARSEL | ingen kilde: FEJL |

Der sammenlignes på seneste afsluttede handelsdag (Yahoo leverer også dagens ufærdige bar, EODHD ikke). Yahoo daterer valutakurser en dag for tidligt (UTC-tidsstempel ved London-midnat): det opdages og justeres automatisk, og porteføljens værdi regnes for begge kilder med samme valutakurs, så kun kursforskelle tæller. Yahoos regnskabsvindue (fx 26/10–30/10, når datoen ikke er bekræftet) tæller som enigt, hvis facit ligger i vinduet. London-kurser fra EODHD i pence omregnes til pund. Mangler antal for en beholdning, beregnes vægtene ikke, og det står som ADVARSEL.

Begivenheder, der ikke er datafejl, vises for sig: estimeret udløb af lock-up for nyligt børsnoterede selskaber (IPO + 180 dage, fx SpaceX ca. 09/12/2026) samt udbytter og splits.

Resultatet bruges tre steder:
- **Alle agenter** får en kompakt facit-tabel i deres instruks: kurs, uge, værdi i SEK, vægt efter markedsværdi og verificeret regnskabsdato.
- **CIO'en** får afvigelserne og skal skrive et afsnit "Datakvalitet".
- **Planen**: regnskabsdesken bruger de krydstjekkede datoer.

Hele tjekket ligger krypteret i rapporten. Vibetrad viser det under Ugerapport → **Datatjek**, og knappen **Krydstjek nu med EODHD** sammenligner rapportens kurser med EODHD igen og viser kursen nu. Den kører gennem EODHD-connectoren i Claude, ikke på API-kreditten. GitHub-loggen viser kun status og antal (fx `Datatjek: PASS · 17 ok, 0 advarsler, 0 fejl`).

Datatjekket koster ingen API-kredit (ingen LLM-kald) og cirka 100 EODHD-kald pr. kørsel. Slå det fra med repo-variablen `VT_DATATJEK=off`. Uden `EODHD_API_KEY` køres det kun med Vibe-Tradings kilder og giver ADVARSEL.

Kør det selv i codespacet (gratis):
```bash
python tools/vt_med_eodhd.py tjek --portfolio-file ~/portefolje.json   # samme JSON som VT_PORTFOLIO
```

## EODHD i Vibe-Trading

`runner/eodhd.py` kobler EODHD ind i den kørende Vibe-Trading-proces. Den installerede pakke ændres ikke.
- **Nordiske og europæiske aktier virker.** I 0.1.16 sendes symboler som `NOVO-B.CO`, `INVE-B.ST`, `EQNR.OL` og `SAP.DE` til de kinesiske A-aktie-kilder (tushare/tencent) og giver ingen data. Nu går de til EODHD med yfinance som reserve. Det gælder `get_market_data`, `portfolio_risk_xray` og `technical_indicators`.
- **EODHD er reserve nr. 2 for amerikanske aktier** (efter Yahoo), så en Yahoo-blokering af GitHubs IP-adresser ikke giver huller.
- **Agent-værktøjet `get_eodhd_data`** giver kurser, nøgletal, regnskabskalender, nyheder med sentiment, udbytter/splits, makroserier, økonomisk kalender (Fed/Riksbank), statsobligationsrenter (`US10Y.GBOND`, `SE10Y.GBOND`) og screener for nordiske og europæiske børser.
- **Aktieklasser** som `BRK-B.US` routes nu til Yahoo (i 0.1.16 endte de hos A-aktie-kilderne), og bare tickers (`NVDA`) tolkes som amerikanske i datatjekket.
- **Kendt begrænsning:** Vibe-Tradings backtest-motor modellerer ikke nordiske/europæiske markeder i 0.1.16 (den ville behandle dem som kinesiske A-aktier). Agenterne får besked på ikke at backteste dem.

Nøglen fjernes fra alle fejlbeskeder og logs. En afvist nøgle (HTTP 401) stopper EODHD for resten af kørslen. Et endpoint, abonnementet ikke dækker (HTTP 403), springes over, mens resten virker. Efter 4 mislykkede kald i træk holdes 5 minutters pause, så et nedbrud ikke koster minutter. Vibe-Trading bevarer EODHD som US-reserve, også hvis kilderækkefølgen ændres i web-UI'ets Settings (medmindre du selv har valgt rækkefølgen for US-aktier).

**Ekstra Vibe-Trading-værktøjer pr. team** (tilføjet ud over presets): regnskabsdesken får `get_stock_profile`, `get_sec_filings`, `get_financial_statements` og `get_stock_news`. Makro-desken får `get_macro_series` (FRED, kræver gratis `FRED_API_KEY`). Sentiment får `get_stock_news`, `sentiment` og `web_search`. Værdi-, fundamental- og investeringskomitéen får `get_stock_profile`/`get_sec_filings`/`get_fundamentals`. ETF-desken og CIO'en får `etf_holdings` (overlap mellem fonde og enkeltaktier). Alle data-agenter får `get_eodhd_data`. Værktøjer, der kræver en nøgle, tilføjes kun, når nøglen findes.

**I codespacet:**
```bash
python ~/vibetrad-runner/tools/vt_med_eodhd.py serve --port 8899      # web-UI med EODHD
python ~/vibetrad-runner/tools/vt_med_eodhd.py status                 # hvad er koblet på?
python ~/vibetrad-runner/tools/vt_med_eodhd.py nøgle                  # virker EODHD-nøglen? (viser abonnement og dagens kald, aldrig nøglen)
claude mcp add vibe-trading -- python ~/vibetrad-runner/tools/vt_med_eodhd.py mcp   # Claude Code
```
Nøglen gives som Codespaces-secret `EODHD_API_KEY` (github.com → Settings → Codespaces → Secrets, med adgang til codespacets repo, og genstart codespacet). Alternativt gemmes den i en fil, kun du kan læse: `read -rs -p 'EODHD-nøgle: ' K && mkdir -p ~/.config/vibetrad && printf %s "$K" > ~/.config/vibetrad/eodhd_key && chmod 600 ~/.config/vibetrad/eodhd_key && unset K && echo` (nøglen vises ikke, mens du indsætter den).

## Uger og datoer

Rapporten følger kalenderen (ISO-uger, Europe/Stockholm). Lørdag dækker hele ugen mandag–fredag. En manuel kørsel midt i ugen dækker ugen til og med seneste amerikanske lukkekurs og mærkes "ugen til dato". Den blokerer ikke lørdagens fulde kørsel.

## Kvant-værktøjer uden shell

Alle Vibe-Tradings presets bruger normalt `bash` til at skrive og køre Python. Det er slået fra her, fordi agenterne ellers kunne køre vilkårlig kode i et miljø med API-nøglen. I stedet kopierer runneren presets til `~/.vibe-trading/swarm/presets/` (som Vibe-Trading læser før de indbyggede) og giver agenterne Vibe-Tradings egne regneværktøjer: `portfolio_risk_xray` (volatilitet, drawdown, VaR/ES, korrelation, beta), `quantlib_call` (VaR/CVaR/EVT, GARCH, regimeskift, Brinson, DCF), `technical_indicators`, `financial_rigor` og `get_market_data`, plus en instruks om at levere tal frem for planer.

Samtidig får agenterne ekstra Vibe-Trading-skills: `research-discipline` (bias-tjekliste) på alle syntetiserende agenter, `thesis-tracker` og `investor-lenses` på CIO'en, `risk-analysis`, `hedging-strategy` og `correlation-regime` i risikoudvalget, `management-deep-dive` i værdikomitéen og `private-company-research` (rammen for nyligt børsnoterede selskaber som SpaceX) hos CIO'en, værdikomitéen, regnskabsdesken og fundamental-teamet. Når målet er SPCX, får agenterne også en eksplicit henvisning til den. CIO'en får også `report_audit`, så den faktatjekker en stikprøve af sine egne tal før aflevering og afslutter med "Faktatjek: PASS/FAIL".

## Budget

| Variabel | Standard | Betydning |
|---|---|---|
| `VT_RUN_BUDGET_USD` | `35` | Maks. estimeret forbrug pr. kørsel (kan sættes pr. kørsel med input `budget_usd`). Ekstra teams springes over, før CIO'en mister sin reserve. |
| `VT_SCHEDULE` | `off` | `on` slår den automatiske lørdagskørsel til igen. |
| `VT_MONTHLY_BUDGET_USD` | `95` | Loft pr. kalendermåned (af API-kreditten på 100 USD). Læses fra `results/ledger.json`. |
| `VT_CIO_RESERVE_USD` | `8` | Holdes altid tilbage til CIO-gennemgangen. |
| `VT_PRICE_IN_PER_MTOK` / `VT_PRICE_OUT_PER_MTOK` | `2` / `10` | Pris pr. mio. tokens for claude-sonnet-5 (ind/ud) til estimatet. Tjek platform.claude.com → Usage. |
| `VT_MAX_SWARMS` | `6` | Højst så mange teams pr. kørsel (CIO'en bevares altid). |
| `VT_SWARM_TIMEOUT_MIN` | `40` | Et team, der kører længere, stoppes og markeres "Tidsgrænse". |
| `VT_EARNINGS_DAYS` | `14` | Regnskabsdesken kører, når et regnskab er højst så mange dage væk. |
| `VT_SKIP_PRESETS` | tom | Presets der skal springes over (kommasepareret). |
| `VT_PLAN` | `fuld` | `kerne` kører kun risikoudvalg og CIO. |
| `VT_MONTHLY_SLOT` | tom | Kun ved manuel kørsel (input "monthly_slot"): tving månedens team 1–5. |
| `VT_MODEL` | `claude-sonnet-5` | Model til alle agenter. |
| `VT_DATATJEK` | `on` | `off` springer datatjekket over. |

Sættes under Settings → Secrets and variables → Actions → **Variables** eller med `gh variable set NAVN --body VÆRDI --repo kasperrejnholdt05-ops/vibetrad-runner`.

## Sikkerhed

- Repoet er offentligt, fordi Claude kun kan hente filer fra offentlige repoer. Det indeholder kun kode og krypterede resultater.
- Porteføljen ligger i secret `VT_PORTFOLIO`, rapporterne krypteres med AES-256-GCM (PBKDF2-SHA256, 600 000 runder) fra secret `VT_RESULTS_PASSPHRASE`.
- GitHub-loggen viser kun status-linjer. Testene tjekker, at tickere, navne og beløb ikke slipper ud.
- Ukrypteret er kun: ugenummer, periode, tidspunkt, status, antal teams, preset-navne, estimeret forbrug og datatjekkets samlede status (PASS/ADVARSEL/FEJL).
- EODHD-nøglen fjernes fra alle fejlbeskeder og fra log-uddraget i rapporten. Testene tjekker, at den hverken når loggen eller resultatfilerne.

## Opsætning (én gang)

1. Opret repoet (Public) og push indholdet.
2. Secrets: `ANTHROPIC_API_KEY`, `VT_RESULTS_PASSPHRASE` (mindst 16 tegn, gem den i en adgangskodemanager) og `VT_PORTFOLIO` (én linje JSON fra Vibetrad → Ugerapport → Opsætning → Kopiér VT_PORTFOLIO).
   Anbefalet: `EODHD_API_KEY` (eodhd.com → Settings → API token) til datatjekket og agenterne. Valgfri: `FRED_API_KEY` (gratis på fred.stlouisfed.org) til makro-desken.
3. Actions → Vibetrad ugekørsel → Run workflow med *Tørkørsel* for at teste kæden gratis.

## Nordnet-handelshistorik til Shadow Account

`tools/nordnet_til_journal.py` oversætter Nordnets transaktionseksport til Vibe-Tradings handelsjournal:
- Den læser svenske, danske, norske og finske kolonner, UTF-16/UTF-8 og komma som decimaltegn.
- Navn og ISIN oversættes til ticker.
- Kurtage omregnes til aktiens valuta.

```bash
python tools/nordnet_til_journal.py ~/transaktioner.csv                  # → transaktioner_vibetrading.csv
python tools/nordnet_til_journal.py ~/transaktioner.csv --map tickers.csv  # egne navn/ISIN → ticker
```

Upload resultatet i Vibe-Tradings Agent-chat. Shadow-backtesten understøtter amerikanske aktier, og nordiske aktier indgår kun i adfærdsanalysen. Scriptet siger tydeligt, hvis Nordnets kolonnenavne afviger. Tjek første kørsel.

## Når du handler

Opdatér `VT_PORTFOLIO` (Kopiér VT_PORTFOLIO i Vibetrad → `gh secret set VT_PORTFOLIO --repo kasperrejnholdt05-ops/vibetrad-runner`). Ellers analyserer lørdagens kørsel den gamle beholdning.

## Udvikling

```bash
pip install "cryptography>=42" pyyaml pytest pandas
python -m pytest -q                      # 91 tests (EODHD-integrationstesten kører kun, hvor vibe-trading-ai er installeret)
VT_PORTFOLIO="$(cat runner/portfolio.example.json)" VT_RESULTS_PASSPHRASE=0123456789abcdef \
  python runner/run_week.py --dry-run --event workflow_dispatch --results /tmp/vt-results
```

Vibe-Trading er fastlåst til 0.1.16 i `requirements.txt`. Opgradér bevidst, og kør en tørkørsel bagefter.

Dette er research, ikke investeringsrådgivning. Intet her kan lægge ordrer.
