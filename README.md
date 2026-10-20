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
| **Porteføljen** | Regnskabsdatoer | `next_reports` og noter på overvågningslisten i `VT_PORTFOLIO` ("Regnskab 04/11" uden år betyder næste gang datoen kommer). `next_reports_source` siger, hvor datoen kommer fra; en dato, Vibetrad har hentet fra EODHD, tæller ikke som egen kilde, når EODHD selv svarer |
| **Porteføljen** | Opgørelsesdato | `as_of` i `VT_PORTFOLIO`: ADVARSEL, når den er over 7 dage gammel |
| **EODHD** | Kort kurshistorik | IPO-dato under et år siden: antal handelsdage og største fald fra top til bund kommer i agenternes facit, så risikotal for papiret omtales som usikre |
| **Nyheder (EODHD)** | Regnskabsdato fra selskabets egen meddelelse | Overskrifter fra de seneste 45 dage scannes for regnskabsord og en dato på engelsk, dansk og svensk ("SpaceX to Post Third Quarter 2026 Results … on October 20, 2026", "Novo Nordisk offentliggør regnskab … den 4. november", "Delårsrapport … publiceras den 17 oktober"). Selskabet skal være overskriftens emne, så "Meta and SpaceX report this week: Meta on Oct. 28" ikke giver SpaceX Metas dato. En meddelelses-ordlyd (to post/report, sets, announces, publiceras, offentliggør) tæller som bekræftet kilde på linje med en dato skrevet ind i Vibetrad. (19/10/2026: EODHD's kalender sagde 03/11 for SpaceX, mens selskabet selv havde meldt 20/10 i en pressemeddelelse, som lå i EODHD's nyhedsstrøm.) |

| Kontrol | PASS | ADVARSEL | FEJL |
|---|---|---|---|
| Lukkekurs samme handelsdag | ≤ 0,5 % | ≤ 2 % | > 2 % |
| Ugeafkast | ≤ 0,5 pp | ≤ 2 pp | > 2 pp |
| Valutakurs (USD/SEK, DKK/SEK …) | ≤ 0,5 % | ≤ 1 % | > 1 % |
| Porteføljens værdi i SEK | ≤ 0,5 % | ≤ 2 % | > 2 % |
| Seneste handelsdag | ens | forskellig | > 6 dage gammel |
| Valuta | som børsen | | anden valuta |
| Navn | fælles ord | intet fælles | |
| Markedsværdi | ≤ 5 % | > 20 % (info: aktieklasser) | |
| Regnskabsdato | kilderne er enige (±1 dag) | uenige eller ukendt (se reglen herunder) | |
| Kun én kilde har data | | ADVARSEL | ingen kilde: FEJL |

Der sammenlignes på seneste afsluttede handelsdag (Yahoo leverer også dagens ufærdige bar, EODHD ikke). Yahoo daterer valutakurser en dag for tidligt (UTC-tidsstempel ved London-midnat): det opdages og justeres automatisk, og porteføljens værdi regnes for begge kilder med samme valutakurs, så kun kursforskelle tæller. Yahoos regnskabsvindue (fx 26/10–30/10, når datoen ikke er bekræftet) tæller som enigt, hvis facit ligger i vinduet. London-kurser fra EODHD i pence omregnes til pund. Mangler antal for en beholdning, beregnes vægtene ikke, og det står som ADVARSEL.

**Regnskabsdato ved uenighed.** Flest kilder vinder, og datoer højst 1 dag fra hinanden tæller som enige (EODHD og Yahoo daterer ofte i hver sin tidszone). Står det lige, vinder en bekræftet dato: Yahoo siger eksplicit "ikke et estimat", datoen er skrevet ind i Vibetrad (`next_reports_source: "manuel"`, typisk fra selskabets IR-side), eller selskabets egen meddelelse står i nyhederne. Dernæst vinder en dateret nyhedsomtale fra de seneste 21 dage over EODHD-kalenderen, som er et estimat (for selskaber noteret inden for et år et skabelon-estimat, mærket `eodhd_estimate`). Ellers vinder den tidligste dato: hellere forberedt for tidligt end overrasket. Nyhederne læses dybt: op til 200 artikler fra 45 dage, både overskrift og brødtekst ("is scheduled to announce Q3 earnings results on Tuesday, October 20th" tæller som bekræftet); antallet læste artikler og en evt. hentefejl står i rækken (`news_scanned`, `news_error`), så et hul kan ses. (Kørslen 19/10/2026 kl. 18:43: EODHD sagde 03/11 for SpaceX, nyhederne havde haft 20/10 hele dagen, men kun 50 overskrifter blev læst, og den rigtige dato kom kun med, fordi Yahoo og din egen dato slog EODHD.) De andre datoer står i facit ("anden kilde: 03/11/2026") og i `alternatives`. Ligger facit uden for de 14 dage, men en anden kilde siger en dato inden for, kører regnskabsteamet alligevel, og dets mål siger, at kilderne er uenige, og at datoen skal bekræftes på selskabets investor-side. (Indtil 19/10/2026 vandt EODHD ved uafgjort. Derfor fik SpaceX EODHD's estimat 03/11 i stedet for den rigtige dato 20/10, og regnskabsteamet kørte ikke.)

Begivenheder, der ikke er datafejl, vises for sig: estimeret udløb af lock-up for nyligt børsnoterede selskaber (IPO + 180 dage, fx SpaceX ca. 09/12/2026) samt udbytter og splits.

Resultatet bruges tre steder:
- **Alle agenter** får en kompakt facit-tabel i deres instruks: kurs, ugeafkast i lokal valuta og i SEK, værdi i SEK, vægt efter markedsværdi og verificeret regnskabsdato. Under tabellen står porteføljens uge i SEK inkl. valutaeffekt (fra fredagens lukkekurs og valutakurs), resultatet siden køb for aktierne alene (kontanten er ikke med) og, når et månedligt indskud er oplyst, hvor mange måneders indskud hver overvægtet position kræver for at nå sit mål ved uændrede kurser og uden salg. Til sidst står markedsværdien for hele selskabet (alle aktieklasser, fra EODHD med Vibe-Trading som reserve), så ingen agent regner Alphabets værdi ud fra A-aktierne alene. Agenterne skal altså ikke selv regne disse tal. Facit er højst 5 600 tegn og afkortes ved et linjeskift.
- **CIO'en** får afvigelserne og skal skrive et afsnit "Datakvalitet".
- **Valuta-afvigelser** forklares i selve advarslen (begge kilders kurs, og at kilderne lukker valutadøgnet på forskellige tidspunkter). FRED's ugentlige DEXSDUS indgår ikke i sammenligningen og er mærket "kun makrokontekst".
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

**EODHD-dækning pr. marked** (står også i agenternes instruks): kurser, valuta, fundamentals og regnskabskalender dækker US, Europa og Norden; nyheder og daglig sentiment er rige for amerikanske papirer og tynde for nordiske/europæiske (få artikler, ofte ingen sentiment-score); analytikernes revisioner (`earnings_trends`) dækker US og store europæiske selskaber. `get_eodhd_data` har derfor fået `news` med `query` (søgeord i overskrift og tekst, op til 50 artikler), `sentiment` og `earnings_trends`, mens insider-handler ikke tilbydes (EODHD's feed er måneder bagud; agenterne bruger `get_sec_filings` for Form 4) og optioner heller ikke (handles ikke på kontoen).

**Ekstra Vibe-Trading-værktøjer pr. team** (tilføjet ud over presets): regnskabsdesken får `get_stock_profile`, `get_sec_filings`, `get_financial_statements` og `get_stock_news`. Makro-desken, risikoudvalgets regime-agent og CIO'en får `get_macro_series` (FRED, kræver gratis `FRED_API_KEY`). Sentiment får `get_stock_news`, `sentiment` og `web_search`. Værdi-, fundamental- og investeringskomitéen får `get_stock_profile`/`get_sec_filings`/`get_fundamentals`. ETF-desken og CIO'en får `etf_holdings` (overlap mellem fonde og enkeltaktier). Alle data-agenter får `get_eodhd_data`. Værktøjer, der kræver en nøgle, tilføjes kun, når nøglen findes.

**I codespacet:**
```bash
python ~/vibetrad-runner/tools/vt_med_eodhd.py serve --port 8899      # web-UI med EODHD
python ~/vibetrad-runner/tools/vt_med_eodhd.py status                 # hvad er koblet på?
python ~/vibetrad-runner/tools/vt_med_eodhd.py nøgle                  # virker EODHD-nøglen? (viser abonnement og dagens kald, aldrig nøglen)
claude mcp add vibe-trading -- python ~/vibetrad-runner/tools/vt_med_eodhd.py mcp   # Claude Code
```
Nøglen gives som Codespaces-secret `EODHD_API_KEY` (github.com → Settings → Codespaces → Secrets, med adgang til codespacets repo, og genstart codespacet). Alternativt gemmes den i en fil, kun du kan læse: `read -rs -p 'EODHD-nøgle: ' K && mkdir -p ~/.config/vibetrad && printf %s "$K" > ~/.config/vibetrad/eodhd_key && chmod 600 ~/.config/vibetrad/eodhd_key && unset K && echo` (nøglen vises ikke, mens du indsætter den).

## Makro for Europa og Norden (EODHD)

FRED dækker kun USA, og investoren handler i SEK, DKK og EUR. `runner/makro.py` henter før agenterne starter Riksbankens, Nationalbankens, ECB's og Feds seneste rentebeslutning og næste rentemøde (med markedets forventning), seneste inflation (CPIF for Sverige), ledighed og BNP-vækst fra EODHD's økonomiske kalender samt 10-årige statsrenter for Sverige, Danmark og Tyskland (EODHD GBOND-serier). Omkring 10 EODHD-kald, ingen LLM-kald. Tabellen "Makro (Europa og Norden, EODHD)" står i alle agenters instruks ved siden af FRED-tabellen, i rapporten (`makro_eu`) og i Vibetrad under Datatjek. GitHub-loggen viser fx `Makro EU/Norden: ja (Riksbanken 1,75 % → 04/11/2026 · …)`. Fejler kalenderen for et land, står det i tabellen, og kørslen fortsætter.

## FRED (makrotal fra St. Louis Fed)

`runner/fred.py` henter før agenterne starter en fast pakke officielle amerikanske makroserier og tester dermed `FRED_API_KEY` i hver kørsel, også i en tørkørsel:

| Serie | Hvad | Brugt til |
|---|---|---|
| `DGS10`, `DGS2` | US 10- og 2-årig statsrente | Renteniveau, diskontering |
| `T10Y2Y` | Rentekurven 10 år − 2 år | Recessionssignal (negativ = inverteret) |
| `DFF` | Effektiv Fed funds-rente | Pengepolitik |
| `VIXCLS` | VIX | Markedets forventede volatilitet |
| `BAMLH0A0HYM2` | US high yield-kreditspænd | Risikoappetit, stress |
| `DEXSDUS` | USD/SEK (Fed H.10, ugentlig udgivelse) | Valutarisiko på de amerikanske aktier |

- Seneste værdi, datoen og ændringen over ca. en måned kommer som tabellen **"Makro (FRED)"** i alle agenters instruks, ved siden af datatjekkets facit. En serie markeres "forældet", hvis den er ældre end 7 dage (USD/SEK: 16 dage).
- `get_macro_series` (Vibe-Tradings eget FRED-værktøj) gives til makro-desken, risikoudvalgets regime-agent og CIO'en med en kort guide til nyttige serier. Afviser FRED nøglen i nøgletjekket, tilføjes værktøjet ikke.
- GitHub-loggen viser fx `FRED: ja, nøglen virker (7 af 7 serier, 7 kald · US 10-årig statsrente 4,12 % pr. 14/10/2026)`, `FRED: nej (secret FRED_API_KEY mangler)` eller `FRED: nøglen virker ikke (forkert nøgle)`.
- En afvist nøgle stopper efter første kald, et nedbrud efter to serier i træk. Nøglen fjernes fra alle fejlbeskeder og logs. Snapshot og fejl ligger krypteret i rapporten (`fred`).

## Uger og datoer

Rapporten følger kalenderen (ISO-uger, Europe/Stockholm). Lørdag dækker hele ugen mandag–fredag. En manuel kørsel midt i ugen dækker ugen til og med seneste amerikanske lukkekurs og mærkes "ugen til dato". Den blokerer ikke lørdagens fulde kørsel.

## Kvant-værktøjer uden shell

Alle Vibe-Tradings presets bruger normalt `bash` til at skrive og køre Python. Det er slået fra her, fordi agenterne ellers kunne køre vilkårlig kode i et miljø med API-nøglen. I stedet kopierer runneren presets til `~/.vibe-trading/swarm/presets/` (som Vibe-Trading læser før de indbyggede) og giver agenterne Vibe-Tradings egne regneværktøjer: `portfolio_risk_xray` (volatilitet, drawdown, VaR/ES, korrelation, beta), `quantlib_call` (VaR/CVaR/EVT, GARCH, regimeskift, Brinson, DCF), `technical_indicators`, `financial_rigor` og `get_market_data`, plus en instruks om at levere tal frem for planer.

Fjernet er også `write_file`, `read_file` og `edit_file` (uden dem viser Vibe-Trading sin instruks for agenter uden kode, i stedet for "skriv ét Python-script og report.md") og `read_url`: en agent med hele porteføljen i sin instruks kunne ellers sendes til en vilkårlig adresse af en plantet tekst i en nyhed og dermed lække den. Nyheder og kurser hentes stadig via `get_eodhd_data` og `get_stock_news`, som kun kalder faste værter. Værktøjer uden mening for en ISK (optionskæder og optionspriser, kinesisk margin og fund flow, `analyze_trade_journal` uden fil) og skills om kinesiske/krypto-kilder, optioner, afdækning med derivater og Python-opskrifter fjernes fra alle presets (`DROP_TOOLS`/`DROP_SKILLS` i `runner/run_week.py`). I kørslen 19/10/2026 brugte regnskabsdeskens optionsagent sine kald på en optionskæde, investoren ikke kan handle.

Før hvert team skrives dets preset igen med ugens værdier: Vibe-Trading 0.1.16 erstatter kun `{goal}`-pladsholdere i opgaveteksten, ikke i agenternes systeminstruks, så `{portfolio}` og `{review_period}` stod ordret i instruksen. Runneren indsætter alle variabler (ikke `upstream_context`) og lægger teamets ekstra regler til.

Samtidig får agenterne ekstra Vibe-Trading-skills: `research-discipline` (bias-tjekliste) på hvert teams sidste, syntetiserende agent, `investor-lenses` på CIO'en, `management-deep-dive` og `private-company-research` (rammen for nyligt børsnoterede selskaber som SpaceX) i værdikomitéen, og `private-company-research` hos regnskabsdesken og fundamental-teamet. Når målet er SPCX, får agenterne en linje om at behandle selskabet som et nyligt privat selskab (ejerstruktur, udvanding, lock-up); kun den sidste agent har selve skill'en, så de andre bedes ikke indlæse den (19/10/2026 kl. 18:43 fik tre agenter "afvist af allowlist" på det). CIO'en får også `report_audit`, så den faktatjekker højst fem stikprøver af sine egne tal før aflevering og afslutter med "Faktatjek: PASS/FAIL". (Indtil 19/10/2026 fik agenterne flere skills, end de kunne nå at læse: i den kørsel brugte CIO'en sine første kald på at indlæse fire skills.)

## CIO'ens input fra de andre teams

CIO'en kører sidst og får de andre teams' rapporter som afsnittet "Konklusioner fra ugens øvrige Vibe-Trading-teams" i sin systeminstruks (målet henviser til det). Afsnittet begynder med en optælling: hvor mange teams der leverede rapport, og hvilke der ikke gjorde, så CIO'en tæller rigtigt (i kørslen 19/10/2026 skrev CIO'en "alle fire teams", selv om antallet var et andet). Hvert team får lige meget plads (i alt 16 000 tegn), og pladsen går til indledningen og afsnittene med konklusioner, beslutninger, planer og scenarier (fundet via overskrifterne), mens kilder og forbehold kommer sidst. Teams, der blev sprunget over eller fejlede, nævnes ("ingen rapport"), og det samme gør agenter, der ikke leverede en rapport. CIO'en skal lave en tabel "Uenigheder mellem teams", gengive risikoudvalgets scenarier i SEK, skrive "Jeg tager fejl, hvis …" ved hver hovedbeslutning og regne overgangsplanen i måneders indskud.

(Indtil 16/10/2026 fik CIO'en kun de første 3 000 tegn pr. team og højst 9 000 i alt, så det fjerde team faldt helt ud og konklusionerne nåede ikke frem. Indtil 19/10/2026 lå uddraget i målet, hvor Vibe-Tradings grounding tolkede hvert stort ord i det som et amerikansk symbol.)

## Agenter uden rapport

En agent kan afslutte uden at levere andet end en plan ("Executing the data-gathering calls now") eller helt uden tekst. Runneren markerer dem (`report: plan_only/empty` pr. agent og `agents_without_report` pr. team), GitHub-loggen viser fx `· Risikoudvalg: completed · … · 1 af 4 agenter uden rapport: Tail Risk Analyst`, og Vibetrad viser "Ingen rapport" eller "Kun en plan" ved agenten (ældre rapporter vurderes i browseren efter samme regler).

Reglerne: en tekst med en resultat-overskrift (konklusion, vurdering, anbefaling, resultat, scenarier …) eller en tabel er en rapport. Uden det tæller teksten som en plan, hvis den begynder med overskriften "Plan" (eller "Approach", "Fremgangsmåde", "Metode"), eller hvis de sidste linjer er fremadrettede ("Executing … now", "Now let me …", "Jeg færdiggør nu rapporten"). Det fanger begge mønstre fra testkørslen 16/10, også en plan fuld af vægte i procent, som en simpel optælling af tal ville kalde en rapport. Testene bruger de mønstre (med ændrede tal).

## Symboler og gebyrer

Alle agenter med data-værktøjer får at vide, at amerikanske aktier og ETF'er skal skrives med `.US` (fx `ACWI.US`). Uden suffix tolker Vibe-Trading 0.1.16 symbolet som en kinesisk A-aktie, og kaldet fejler. Nordiske aktier beholder deres børs-suffix og er aldrig `.US` (Novo Nordisk B er `NOVO-B.CO`, ikke `NOVO.US`), og valutakurser skrives `USDSEK=X`/`DKKSEK=X`, aldrig `DKK.US` (begge fejl skete i kørslen 19/10/2026). De må heller ikke citere tal fra et kald, der fejlede, eller angive en kurtagesats, der ikke står i deres instruks. `portfolio_risk_xray` kræver mindst 30 handelsdage pr. papir og regner kun på de dage, alle papirerne har handlet: agenterne skal lade startdatoen stå tom eller gå mindst 3 måneder tilbage og helst holde nyligt børsnoterede papirer ude af selve x-rayet (i kørslen 19/10/2026 fejlede risikoudvalgets kald med "no symbol has at least 30 valid bars", fordi de brugte det korte prisvindue). Markedsværdi tages fra facit og regnes aldrig ud fra én aktieklasses antal aktier. Vibe-Tradings grounding (30 dages kurser for symboler i opgaveteksten) bruger kun symboler, der står ordret i teksten: i 0.1.16 blev hvert stort ord (SEK, ISK, NOVO, PASS …) til et amerikansk symbol og hentet forgæves.

## ISK og Nordnet-regler

Alle agenter får investorens rammer i deres instruks:
- Investoren er privatkunde og investerer kun via en svensk ISK hos Nordnet.
- ETF'er med hjemsted i USA eller Canada (ACWI.US, SPY.US, IEF.US …) kan ikke købes af private i EU (PRIIPs/MiFID II, siden 2018). De bruges kun som dataproxy. Foreslår en agent en fond eller ETF, skal den nævne en UCITS-udgave med hjemsted i EU (fx iShares MSCI ACWI UCITS ETF i stedet for ACWI.US) eller en indeksfond hos Nordnet, og sige at det præcise instrument skal bekræftes hos Nordnet.
- Ingen optioner, futures, short eller gearing. Afdækning med derivater foreslås ikke; valutarisiko håndteres kun med vægte, spredning eller valutasikrede UCITS-klasser.
- Reserveret kontant (fx til en åben ordre) kan ikke bruges til nye køb.
- UCITS-ETF'er kan hentes med deres europæiske notering i `get_eodhd_data` (IUSQ.DE/SPYY.DE for MSCI ACWI, IWDA.AS/EUNL.DE for MSCI World, VWCE.DE, SXR8.DE, EXSA.DE, XDEV.DE, IUSN.DE; `.DE` oversættes til EODHD's XETRA), og en kandidat tjekkes med værktøjet, før den nævnes.
- Hele rapporten skrives på dansk med DD/MM/YYYY og beløb i SEK. Institutionelle afsnit, der ikke findes for en privatkunde (Northbound-flow, margin, dragon-tiger, 13F …), erstattes af én linje.

CIO'en skal desuden kun foreslå fonde og ETF'er, der kan købes hos Nordnet. (Kørslen 19/10/2026 anbefalede ACWI.US, som en privatkunde ikke kan købe hos Nordnet, og flere teams efterspurgte optionsstrategier, Northbound-flow og mæglerblotters fra presets skrevet til institutionelle og kinesiske forhold. Hvert team har nu sine egne regler oven på presettet: risikoudvalget fire scenarier uden optioner, makro-desken renter og USD/SEK, fondsvalg og ETF-desken kun UCITS, regnskabsdesken uden optionsanalyse og CIO'en med afsnittet "Konklusioner fra ugens øvrige Vibe-Trading-teams".)

**Ledig kontant.** Runneren læser det reserverede beløb fra kontantnoten ("Heraf 2 400 SEK reserveret til åben limitordre", "Reserveret til ordre: 2 400 SEK", "2.400 kr. er bundet i ordren"). Kun beløb i SEK/kr tæller, så en limitkurs i DKK ignoreres. En nægtelse ("ikke længere reserveret", "Intet reserveret") tæller ikke, og samme beløb nævnt to gange tælles én gang. Et tal i `cash_reserved_sek` vinder over noten, og reservationen kan aldrig blive større end kontanten. Porteføljeteksten viser "Ledig til nye køb: … SEK", datatjekkets facit viser "heraf … reserveret, … ledig", og ETF- og fondsteamet får kun indskuddet og den ledige kontant at fordele.

## Vægtpolitik

`weight_policy` i `VT_PORTFOLIO` styrer, hvordan `max_weight` bruges. `limit` (standard) er en hård grænse. `target` (Kaspers valg 16/10/2026) gør 10 % pr. aktie til et langsigtet mål: CIO'en skal foreslå en overgangsplan med trin og tidshorisont i stedet for at sælge ned med det samme, og Vibetrads tjekliste viser "Over det langsigtede mål" som advarsel i stedet for en overskredet grænse. Knappen Kopiér VT_PORTFOLIO i Vibetrad sætter `target`.

Risikoudvalget regner altid tabet i SEK i fire scenarier: den største position −30 % og −50 %, USD/SEK −10 % og amerikansk tech samlet −25 %. VaR bruges kun som supplement.

`monthly_deposit_sek` (månedligt indskud) og `courtage_class` (kurtageklasse hos Nordnet) i `VT_PORTFOLIO` kommer med i porteføljeteksten. Indskuddet bruges til overgangsplanen; ETF- og fondsteamet fordeler kun nye indskud og den ledige kontant, aldrig hele porteføljens værdi. Kurtagesatsen kendes ikke af agenterne, så de må ikke angive en. Begge felter sættes under Opsætning i Vibetrad og kommer med, når du kopierer VT_PORTFOLIO. Ingen af dem vises i GitHub-loggen.

## Rettelser i Vibe-Trading 0.1.16

Runneren retter seks ting i Vibe-Trading, mens kørslen står på. Der ændres ikke i den installerede pakke. Status står i GitHub-loggen ("Vibe-Trading-rettelser: …"), i rapporten (`vibe_trading_patches`) og i Vibetrad under Model.

- **Værktøjsresultater bevares.** I 0.1.16 overskriver hver agent alle værktøjsresultater undtagen de 3 nyeste med `[cleared]`, i hver iteration. En agent, der henter seks kurser, har dermed mistet de tre første, før den skriver, og må gentage kaldene eller skrive uden tallene. I kørslen 19/10/2026 skrev flere agenter, at resultater var "ryddet fra konteksten", før de kunne læse dem (fx optionskæden, kvartalsregnskabet og quantlib-modulerne). Runneren erstatter den ene blok i `_run_worker_impl`: resultaterne bevares, indtil de tilsammen fylder over 40 000 tegn (`VT_TOOL_CONTEXT_CHARS`). Så ryddes de ældste ned til halvdelen, og de 3 nyeste røres aldrig. Ryddede resultater får en markør, der siger, at kaldet lykkedes, eller at det fejlede (fejl-kuverter), så ingen agent citerer tal fra et fejlet kald. Korte resultater, hvor markøren ville fylde mere, ryddes ikke. Linjenumrene i Vibe-Tradings tracebacks er de samme som før. Det giver få ændringer i beskederne, så prompt-cachen holder. Hvis Vibe-Tradings kode ændrer sig, så blokken ikke findes, bruges originalen, og loggen siger "ikke anvendt".
- **`temperature` udelades for Claude-modeller af 5. generation** (`claude-sonnet-5`, `claude-opus-5-5` …), som afviser feltet. Vibe-Trading opdager det ellers først ved den første fejl, og i kørslen 19/10/2026 fejlede de parallelle agenters første kald derfor. `VT_TEMPERATURE=on|off` tvinger valget.
- **Agentinstruksen uden falsk grænse.** 0.1.16 skriver "You have a HARD LIMIT of 20 tool calls" og "Plan (0 tool calls): state your plan" i hver agents instruks, selv om grænsen ikke håndhæves (`max_iterations` er 50). I kørslen 19/10/2026 stoppede agenter efter 20 kald eller afleverede planen som rapport. Runneren pakker `build_worker_prompt` ind og erstatter de to linjer med en kort besked om at arbejde effektivt og aldrig slutte med en plan.
- **Grounding kun på rigtige symboler.** `extract_symbols_from_user_vars` pakkes ind, så kun symboler, der står ordret i opgaveteksten, hentes (se Symboler og gebyrer).
- **Leverancekontrakten uden krav om værktøjskald.** 0.1.16 afviser en "data-agent" (ethvert værktøj ud over bash, filer og load_skill), der svarer uden et eneste værktøjskald og uden report.md ("output contract not met"). Uden filværktøjer findes report.md ikke, og de syntetiserende agenter (strateg, redaktør, dommer, attribution) har kvant-værktøjer fra runneren og tæller som data-agenter: svarede de ud fra de andre agenters rapporter og facit, blev de afvist, synteseagenten blokeret og hele teamet markeret "failed". I kørslen 19/10/2026 kl. 18:43 faldt fire af seks teams sådan, inkl. CIO'en. `_classify_deliverable` pakkes ind, så netop det krav slås fra; tomme svar, planer, rå værktøjsresultater og "mock data" afvises stadig af Vibe-Trading, og runneren markerer selv agenter uden rapport.
- **Flere forsøg ved midlertidige fejl fra Anthropic.** 0.1.16 prøver et LLM-kald igen præcis én gang ved 429/529 eller tabt forbindelse; derefter er agenten fejlet, og med `max_retries: 1` i de fleste presets starter den forfra én gang og fejler så teamet. `ChatLLM.stream_chat` pakkes ind, så midlertidige fejl prøves op til `VT_STREAM_RETRIES` (standard 4) gange mere med ventetid 5, 10, 20, 40 s (Retry-After respekteres). Endelige fejl (4xx undtagen 408/429) går straks videre.

Et team, der alligevel fejler eller stoppes på tid eller budget, mister ikke de færdige agenters arbejde: deres rapporter gives videre til CIO'en som *delrapport* ("N med delrapport fra enkelte agenter"), og GitHub-loggen viser pr. agent, hvad der skete (fx `Earnings Desk Strategist (failed: svar uden værktøjskald afvist af Vibe-Trading (leverancekontrakt))`), aldrig selve fejlteksten.

En integrationstest kører den rigtige ReAct-løkke i Vibe-Trading med en falsk LLM og otte kurskald: med rettelsen ser det sidste kald alle otte resultater, og i 0.1.16 uden rettelsen er fem af dem `[cleared]`. En anden test kører de to indpakninger mod den installerede Vibe-Trading og tjekker, at linjerne er væk, og at "NOVO" i en tekst ikke bliver til `NOVO.US`.

## Budget

| Variabel | Standard | Betydning |
|---|---|---|
| `VT_RUN_BUDGET_USD` | `35` | Maks. estimeret forbrug pr. kørsel (kan sættes pr. kørsel med input `budget_usd`). Ekstra teams springes over, før CIO'en mister sin reserve. |
| `VT_SCHEDULE` | `off` | `on` slår den automatiske lørdagskørsel til igen. |
| `VT_MONTHLY_BUDGET_USD` | `95` | Loft pr. budgetperiode (af Max-abonnementets API-kredit på 100 USD). Læses fra `results/ledger.json`. Også en tørkørsel viser periodens rigtige forbrug, så budgettet kan tjekkes gratis (tørkørsler tæller ikke med). |
| `VT_BUDGET_RESET_DAY` | `23` | Dag i måneden, hvor budgetperioden starter forfra. Max-kreditten fornyes på faktureringsdagen og udløber ved periodens slutning. `1` = kalendermåned. |
| `VT_CIO_RESERVE_USD` | `8` | Holdes altid tilbage til CIO-gennemgangen. |
| `VT_PRICE_IN_PER_MTOK` / `VT_PRICE_OUT_PER_MTOK` | `2` / `10` | Pris pr. mio. tokens for claude-sonnet-5 (ind/ud) til estimatet. Tjek platform.claude.com → Usage. |
| `VT_MAX_SWARMS` | `6` | Højst så mange teams pr. kørsel (CIO'en bevares altid). |
| `VT_SWARM_TIMEOUT_MIN` | `40` | Et team, der kører længere, stoppes og markeres "Tidsgrænse" (`timeout`). De agenter, der nåede at levere, kommer med i rapporten. (Runnerens egen standard uden workflowet er 45.) |
| `VT_MIN_SWARM_USD` | `0.5` | Et team startes ikke, hvis der er mindre end dette tilbage af kørslens budget. |
| `VT_EARNINGS_DAYS` | `14` | Regnskabsdesken kører, når et regnskab er højst så mange dage væk. |
| `VT_SKIP_PRESETS` | tom | Presets der skal springes over (kommasepareret). |
| `VT_PLAN` | `fuld` | `kerne` kører kun risikoudvalg og CIO. |
| `VT_MONTHLY_SLOT` | tom | Kun ved manuel kørsel (input "monthly_slot"): tving månedens team 1–5. |
| `VT_MODEL` | `claude-sonnet-5` | Model til alle agenter. |
| `VT_DATATJEK` | `on` | `off` springer datatjekket over. |
| `VT_PORTFOLIO_MAX_AGE_DAYS` | `7` | Datatjekket giver ADVARSEL, når VT_PORTFOLIO er opgjort for mere end så mange dage siden, og agenterne får samme besked. |

Sættes under Settings → Secrets and variables → Actions → **Variables** eller med `gh variable set NAVN --body VÆRDI --repo kasperrejnholdt05-ops/vibetrad-runner`. En variabel med en værdi, der ikke er et tal eller ligger uden for sit interval, stopper kørslen med `config_error`, før der bruges kredit (indtil 19/10/2026 gav fx `VT_RUN_BUDGET_USD=35 USD` et nedbrud med traceback i loggen).

Budgettet vogtes også, mens et team kører: hvert 5. sekund regnes teamets foreløbige tokens om til USD, og overstiger de det, der er tilbage af kørslens budget (minus CIO'ens reserve), afbrydes teamet og markeres `budget`; de agenter, der nåede at levere, kommer med. Estimatet er bevidst forsigtigt (listepriser, ingen cache-rabat), så det rigtige forbrug på platform.claude.com er lavere (19/10/2026: estimat 28,06 USD, faktisk 17,08 USD).

## Sikkerhed

- Repoet er offentligt, fordi Claude kun kan hente filer fra offentlige repoer. Det indeholder kun kode og krypterede resultater.
- Porteføljen ligger i secret `VT_PORTFOLIO`, rapporterne krypteres med AES-256-GCM (PBKDF2-SHA256, 600 000 runder) fra secret `VT_RESULTS_PASSPHRASE`. De ukrypterede felter er bundet til ciffertekstens tag (AAD), så de ikke kan ændres uden at dekrypteringen fejler. AAD'en bygges ens i Python og i browseren: nøgler sorteres rekursivt, og tal uden decimaler skrives uden `.0` (indtil 19/10/2026 skrev Python `8.0`, hvor JavaScript skriver `8`, så en rapport med et helt estimat ikke kunne dekrypteres i Vibetrad).
- `VT_PORTFOLIO` læses robust: tal med mellemrum eller komma ("2 615", "10,5") accepteres, tickere normaliseres, og et felt med forkert type stopper kørslen med en forklaring uden porteføljens indhold i loggen.
- GitHub-loggen viser kun status-linjer (FRED-linjen nævner den offentlige US 10-års rente). Testene tjekker, at tickere, navne og beløb ikke slipper ud. En uventet fejl i runneren skriver kun fejltypen og stedet (fil, linje, funktion i den offentlige kode) til loggen; fejlbeskeden, som kan rumme en ticker eller et beløb, går kun til den private logfil på runneren.
- Testene bruger kun opdigtede beholdninger, beløb, datoer og ordrer. Rigtige tal fra porteføljen, Nordnet-eksporter eller rapporter må aldrig committes, heller ikke som testdata.
- Workflowet tjekker altid nyeste `main` ud (også en kørsel, der har stået i kø bag en anden) og gemmer ikke GitHub-tokenet i `.git/config`, mens Vibe-Trading og dens afhængigheder kører. Tokenet bruges kun i push-trinnet.
- Resultatet pushes med op til 5 forsøg: er main ændret under kørslen, hentes nyeste main, og kuverten lægges ind igen med `run_week.py --merge-results` (index, ledger og latest flettes, så to kørslers resultater aldrig overskriver hinanden). Den krypterede rapport gemmes desuden som artifact på kørslen i 14 dage, så en betalt kørsel ikke går tabt, hvis push fejler. En simuleret konflikt mellem to kørsler er testet.
- Ukrypteret er kun: ugenummer, periode, tidspunkt, status, antal teams, preset-navne, estimeret forbrug og datatjekkets samlede status (PASS/ADVARSEL/FEJL).
- EODHD-nøglen fjernes fra alle fejlbeskeder og fra log-uddraget i rapporten. Testene tjekker, at den hverken når loggen eller resultatfilerne.
- Agenterne har hverken shell, filer eller `read_url` (se Kvant-værktøjer uden shell), og Vibe-Tradings `web_search` må ikke falde tilbage på kinesiske søgemaskiner (`VIBE_TRADING_SEARCH_BING_FALLBACK=0`).

## Opsætning (én gang)

1. Opret repoet (Public) og push indholdet.
2. Secrets: `ANTHROPIC_API_KEY`, `VT_RESULTS_PASSPHRASE` (mindst 16 tegn, gem den i en adgangskodemanager) og `VT_PORTFOLIO` (én linje JSON fra Vibetrad → Ugerapport → Opsætning → Kopiér VT_PORTFOLIO).
   Anbefalet: `EODHD_API_KEY` (eodhd.com → Settings → API token) til datatjekket og agenterne. Valgfri: `FRED_API_KEY` (gratis på fred.stlouisfed.org) til makro-facit og `get_macro_series`.
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
python -m pytest -q                      # 225 tests (integrationstestene for EODHD og Vibe-Trading-rettelserne kører kun, hvor vibe-trading-ai er installeret)
node tests/decrypt_webcrypto.mjs results/latest.enc.json 'adgangsfrase'   # dekrypterer en kuvert med WebCrypto præcis som Vibetrad-siden (testene bruger den, når node findes)
VT_PORTFOLIO="$(cat runner/portfolio.example.json)" VT_RESULTS_PASSPHRASE=0123456789abcdef \
  python runner/run_week.py --dry-run --event workflow_dispatch --results /tmp/vt-results
```

Vibe-Trading er fastlåst til 0.1.16 i `requirements.txt`. Opgradér bevidst, og kør en tørkørsel bagefter.

Dette er research, ikke investeringsrådgivning. Intet her kan lægge ordrer.
