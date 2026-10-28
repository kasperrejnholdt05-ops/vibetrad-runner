# Vibetrad vagthund

Vagthund og købstiming for de amerikanske og skandinaviske aktiemarkeder. Værktøjet siger, **hvornår** der skal
købes under en korrektion eller et kollaps, i trancher og efter faste regler. Det giver ingen
investeringsrådgivning.

- **Makrovagt** (`runner/makrovagt.py`): 18 signaler fra FRED og EODHD (VIX, kreditspænd, finansiel stress,
  rentekurve, Sahm-reglen, nye ledige, olie, dollar, krone, guld, aktiefald og dagschok) plus Buffett-indikatoren.
  Hvert signal er grøn, gul eller rød. Sort svane-varsel = mindst ét chok eller mindst tre røde.
- **Risikotemaer og scenarier** (`runner/temaer.py`): yderligere ca. 27 signaler for netop de risici, der præger nu, i ti
  temaer:
  - renter og inflation (10- og 30-årig rente, realrente, prissatte renteforhøjelser, inflationsforventning, CPI)
  - statsgæld (terminspræmie, renter i % af indtægterne, "renter op og dollar ned")
  - kredit (CCC, BBB, bankernes lånestandarder, private credit)
  - likviditet (SOFR mod IORB, reverse repo)
  - AI og tech-priser (SOXX, NVIDIA, CoreWeave, markedets bredde, Nasdaq mod S&P)
  - energi (Brent i niveau og år til år, naturgas)
  - chips (halvlederpriser, TSMC, taiwanske dollar)
  - geopolitik og politisk uro (EPU-indekset, guld)
  - økonomi og job
  - aktiemarkederne

  Seks scenarier viser, hvilken vej det trækker: blød landing, stagflation, rente- og gældschok, AI-brud, kreditkrise
  og geopolitisk chok. "Selvtilfredshed" markerer røde temaer i et roligt marked. Sort svane-varsel = et chok, eller
  røde signaler i mindst tre temaer.
- **Zonemotor** (`runner/zoner.py` og den identiske `web/zoner.js`): faldet fra toppen for S&P 500, Nasdaq 100,
  OMXS30, OMXC25 og OBX. Hver zone har tre trancher.
- **Backtest og afspilning**: reglerne er kørt igennem al historik (S&P 500 siden 1960) og uge for uge gennem
  2008, 2020 og 2022.

Ingen LLM, ingen API-kredit, ingen porteføljedata. GitHub skriver kun markedsdata til `results/vagt.json`.
Vibetrad (claude.ai-artifact) regner dine beløb, ordresedler og dine egne aktier i browseren.

## Reglerne

| Zone | Indeks | Enkeltaktier | Andel af reserven |
|---|---|---|---|
| Korrektion | 10–20 % under toppen | 20–35 % | 20 % |
| Bjørnemarked | 20–30 % | 35–50 % | 30 % |
| Kollaps | over 30 % | over 50 % | 50 % |

En aktie kan også have faste kursniveauer i stedet for procent (fx SpaceX 131–140 / 105–118 / 75–95).

**Top og episode**
- En episode starter ved en ny top. Ny top betyder ny episode, og alle trancher er klar igen.
- Er toppen over 3 år gammel, flyttes den til det seneste års højeste lukkekurs. Ellers var 2008-krakket aldrig
  blevet en zone i OMXS30, som først nåede 2000-toppen igen i 2015.

**Tre trancher pr. zone (1/3 hver)**
1. *Liggende ordre* ved zonens øverste kurs. Ligger den mere end ca. 13 % under kursen, tager Nordnet ikke
   ordren; så bliver det en kursalarm.
2. *Bekræftelse*: første lukkekurs over de seneste 10 dages højeste lukkekurs efter bunden. Køber 2. tranche i
   alle zoner, der er nået.
3. *Fundament*: 20 handelsdage efter bunden uden ny bund, og kun efter bekræftelsen.

Zonerne er valgt ud fra historikken. Tre zonesæt blev testet på de store nedture (fald på 20 % eller mere):
- 10/20/30 % med vægtene 20/30/50
- med en ekstra krak-zone ved 45 %
- 15/25/40 %

Det enkleste sæt gav det største bidrag efter 3 år på S&P 500, Nasdaq 100 og OMXS30; krak-zonen vandt kun på OBX.

## Datakontrol
`zoner.guard` finder:
- databrud (mere end en halvering eller fordobling på én dag)
- huller på over 14 dage
- dubletter og ugyldige rækker
- forældede kurser

Ved et databrud skaleres de ældre kurser, og fundet meldes. EODHD's OMXS30 har et brud i april 1998, hvor
kurserne er divideret med 4,13; uden kontrollen ligner det et krak på 85 %.

## Kørsel
- `.github/workflows/vagt.yml` kører hverdage kl. 06:17, 16:53 og 21:23 UTC, og på knaptryk (Run workflow).
- Den kræver kun repo-secrets `EODHD_API_KEY` og `FRED_API_KEY`.
- Den skriver `results/vagt.json` og `results/vagt_history.json`. Afvises et push, hentes nyeste main, og
  resultatet lægges ind igen (`--merge`), op til 5 forsøg.
- Exit-kode 1, hvis hverken EODHD eller FRED gav data. Så bliver kørslen rød, og GitHub sender en mail.

Lokalt:

```bash
python runner/vagt.py --out /tmp/vagt                  # med EODHD_API_KEY og FRED_API_KEY i miljøet
python runner/vagt.py --offline mappe --out /tmp/vagt  # gemte kurser: mappe/<symbol>.json
python -m pytest -q                                    # alle tests inkl. JavaScript-motoren (kræver node)
```

## Samme facit i Python og JavaScript
`tests/fixtures/zoner_cases.json` bygges af `tests/make_zoner_fixtures.py` ud fra Python-motoren.
`tests/zoner_js_check.mjs` kører `web/zoner.js` på de samme tilfælde. Ændres reglerne, skal begge filer ændres,
og fixtures bygges igen.

## Forenklet 27/10/2026
- **Fjernet:**
  - Ugerapporten med agent-teams (Vibe-Trading 0.1.16)
  - Datatjek, kanariefugl, krypterede rapporter og VT_PORTFOLIO
- **Secrets, der ikke bruges mere:** `ANTHROPIC_API_KEY`, `VT_PORTFOLIO`, `VT_RESULTS_PASSPHRASE`. Ejeren kan slette
  dem under Settings → Secrets and variables → Actions.
- **Historikken** ligger stadig i git.
