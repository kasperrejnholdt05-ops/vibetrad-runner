# Vibetrad vagthund

Vagthund og købstiming for de amerikanske og skandinaviske aktiemarkeder. Værktøjet siger, **hvornår** der skal
købes under en korrektion eller et kollaps, i trancher og efter faste regler. Det giver ingen
investeringsrådgivning.

- **Makrovagt** (`runner/makrovagt.py`): 18 signaler fra FRED og EODHD (VIX, kreditspænd, finansiel stress,
  rentekurve, Sahm-reglen, nye ledige, olie, dollar, krone, guld, aktiefald og dagschok) plus Buffett-indikatoren.
  Hvert signal er grøn, gul eller rød. Langsomme baggrundstal (se nedenfor) tæller højst som gule.
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
  og geopolitisk chok. "Selvtilfredshed" markerer røde temaer i et roligt marked. Sort svane-varsel = et chok
  (en usædvanlig hurtig bevægelse); røde signaler i mindst tre temaer giver rødt niveau, men ikke sort svane (valgt ud
  fra analysen 28/10/2026, se nedenfor). Buffett-indikatoren, statens rentebyrde og chippriserne er *baggrund*: signalet
  vises med sin egen farve, men tæller højst som gult – i makrovagtens niveau, i sort svane, i temaets farve og i
  "selvtilfredshed" – fordi de kan ligge rødt i årevis.
  Rentebyrden er NIPA-renter divideret med statens samlede indtægter (`FGRECPT`); grænserne 18/24 % svarer til 15/20 %
  på CBO's nettobasis.
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

**Planens start (27/10/2026)**
- Live-status regner fra `PLAN_START` i `runner/zoner.py` (og `web/zoner.js`). Trancher, reglerne ville have udløst
  før, blev aldrig købt, så de er klar igen. Uden datoen troede motoren, at OMXC25's korrektion og bjørn var købt
  under Novo-faldet 2024–25, og viste kun en kollaps-alarm.
- Backtest og afspilning bruger ikke datoen: de viser, hvad reglerne ville have gjort i hele historikken.

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
- Exit-kode 1, hvis ingen af de fem markeder gav kurser (fx udløbet EODHD-nøgle). Så bliver kørslen rød, og GitHub
  sender en mail. Resultatet gemmes alligevel, så Vibetrad viser "0/5 markeder" i stedet for gamle tal.

Lokalt:

```bash
python runner/vagt.py --out /tmp/vagt                  # med EODHD_API_KEY og FRED_API_KEY i miljøet
python runner/vagt.py --offline mappe --out /tmp/vagt  # gemte kurser: mappe/<symbol>.json
python runner/vagt.py --plan-start 2026-10-27          # planens startdato (standard: PLAN_START)
python -m pytest -q                                    # alle tests inkl. JavaScript-motoren (kræver node)
```

## Bevis for reglerne (Vibetrad analyse)
`.github/workflows/analyse.yml` kører `runner/analyse.py` på knaptryk (Actions → Vibetrad analyse → Run workflow),
kun med læseadgang. Resultatet står i loggen.
- **Sort svane**: makrovagten genberegnes uge for uge siden 2007 på data, som de så ud den dag. For hver regel
  (`tre_temaer`, `frisk`, `kun_chok`) måles:
  - hvor stor en del af ugerne varslet er tændt, og hvor længe ad gangen
  - hvor ofte S&P 500 faldt mindst 10 % inden for 3 måneder efter et varsel
  - om varslet kom tidligt i de store fald (fra 3 måneder før toppen, til faldet når 10 %)
- **Top-alder**: zonerne backtestes med en top på højst 3 år, 2 år og 1 år. Målt pr. variant:
  - "tidlige køb": trancher, mens indekset er under 5 % fra sit seneste års top
  - afkast efter 1 og 3 år
  - hvor mange dybe fald der blev købt

Reglen for sort svane vælges med `SWAN_RULE` i `runner/makrovagt.py`, top-alderen med `TOP_DAYS` i `runner/zoner.py`.

### Resultat 28/10/2026 (kørsel 40658332270)
Sort svane, S&P 500, 2007–2026 (1.034 uger, 7 fald på mindst 15 %):

| Regel | Tændt | Sidste 2 år | Længste stræk | S&P −10 % inden 3 mdr | Tidligt i de store fald |
|---|---|---|---|---|---|
| tre_temaer (før) | 30,8 % af ugerne | 33,7 % | 86 uger | 11 af 87 varsler (13 %) | 6 af 7 |
| frisk | 27,5 % | 32,7 % | 22 uger | 17 af 102 (17 %) | 6 af 7 |
| **kun_chok (valgt)** | **18,5 %** | 29,8 % | **9 uger** | **19 af 102 (19 %)** | 6 af 7 |

Alle tre regler var tændt før bunden i 7 af 7 fald. Det fald, ingen af dem varslede tidligt, var corona-krakket
i februar 2020 (det kom på få dage). De tre røde temaer gav ingen tidligere varsler, kun flere og længere alarmer.

Epistemisk status: et varsel, der er tændt 18,5 % af ugerne, rammer et vindue på 4–6 måneder ret ofte ved
tilfældighed. Tallene viser, at "kun chok" er den bedste af de tre regler, ikke at sort svane kan forudsige et fald.
Brug det som stresssignal og følg trancherne.

Top-alder, 3 års median efter køb (% inkl. alle trancher) og antal trancher:

| Indeks | 3 år (i dag) | 2 år | 1 år |
|---|---|---|---|
| S&P 500 | 35,8 % · 137 | 30,2 % · 160 | 34,8 % · 722 |
| Nasdaq 100 | 60,0 % · 167 | 46,5 % · 177 | 60,0 % · 546 |
| OMXS30 | 27,2 % · 124 | 26,5 % · 151 | 38,4 % · 668 |
| OMXC25 | 37,0 % · 24 | 37,0 % · 29 | 10,9 % · 89 |
| OBX | 39,0 % · 69 | 49,3 % · 88 | 80,1 % · 339 |

En 1-årig top giver 3–5 gange så mange trancher, end reserven kan bære, og er dårligst for OMXC25. 2 år er ikke
bedre end 3 år på fire af fem indeks. **3 år bliver.** Prisen er, at OMXC25's første korrektionsordre i oktober 2026
ligger 4,5 % under kursen, fordi toppen er fra 13/09/2024.

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

## Rettet 28/10/2026
- **Planens start**: live-status ignorerede, at planen først gælder fra 27/10/2026 (se ovenfor). OMXC25 viste derfor
  ingen ordre ved korrektion eller bjørn.
- **Baggrundstal**: statens rentebyrde farvede temaet Statsgæld rødt og kom med i "selvtilfredshed", selv om
  vagten bevidst ikke tæller den som rød.
- **Exit-kode**: uden EODHD blev kørslen grøn uden zoner. Nu er den rød, og resultatet gemmes stadig.
- **Grafen**: sidste dag stod to gange i `chart` for Nasdaq 100, OMXC25 og OBX.
- **Tests på GitHub** kører nu også, når `web/zoner.js` ændres.
