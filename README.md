# vibetrad-runner

Kører det rigtige [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) hver **lørdag kl. 08:00 svensk tid** på min Nordnet-ISK og lægger en krypteret ugerapport klar til Vibetrad-siden på claude.ai inden mandag morgen.

```
lør 08:00  GitHub Actions ── vibe-trading-ai (SwarmRuntime) ── Anthropic API
               │  portfolio_review_board hver uge
               │  risk_committee første lørdag i måneden
               │  earnings_research_desk når et regnskab er under 14 dage væk
               ▼
           results/latest.enc.json   (AES-256-GCM, kun ciffertekst i repoet)
               ▼
lør 10:40  planlagt Claude-opgave henter filen ind i Vibetrad (søndag 10:40 igen, hvis noget fejlede)
               ▼
           Vibetrad → Ugerapport: dekrypteres i browseren med min adgangsfrase
```

## Hvorfor repoet er offentligt, og hvorfor det er i orden

Claude kan kun hente filer fra offentlige GitHub-repoer. Derfor:

- Repoet indeholder **kun kode og krypterede resultater**. Porteføljen ligger i secret `VT_PORTFOLIO`, rapporterne er krypteret med AES-256-GCM (nøgle via PBKDF2-SHA256, 600 000 runder) fra secret `VT_RESULTS_PASSPHRASE`.
- **Loggen er offentlig** i et offentligt repo. Al output fra Vibe-Trading sendes til en midlertidig fil, og loggen viser kun status-linjer (`Porteføljegennemgang: completed · 812 s · 448 000 tokens`). Testene tjekker, at ticker, navne og beløb ikke slipper ud.
- Ukrypteret er kun: ugenummer, tidspunkt, status, antal gennemførte teams og navnene på de presets, der blev kørt.

## Opsætning (én gang)

1. Opret repoet `vibetrad-runner` (Public, uden README) og upload indholdet af denne mappe.
2. **Settings → Secrets and variables → Actions → New repository secret**, tre gange:

   | Secret | Indhold |
   |---|---|
   | `ANTHROPIC_API_KEY` | Din Anthropic API-nøgle (samme slags som i codespacet). |
   | `VT_RESULTS_PASSPHRASE` | Mindst 16 tegn. Brug "Generér stærk adgangsfrase" på Vibetrad → Ugerapport → Opsætning. Gem den i din adgangskodemanager. |
   | `VT_PORTFOLIO` | JSON fra "Kopiér VT_PORTFOLIO" på samme side. Formatet ses i `runner/portfolio.example.json`. |

3. **Actions → Vibetrad ugekørsel → Run workflow** med *Tørkørsel* slået til. Det koster ingenting og tester hele kæden: kryptering, commit og visning i Vibetrad.
4. Kør den igen uden tørkørsel, hvis du vil have en rigtig rapport med det samme. Ellers kommer den første lørdag kl. 08:00.

## Valgfrie variabler (Settings → Variables → Actions)

| Variabel | Standard | Betydning |
|---|---|---|
| `VT_MODEL` | `claude-sonnet-5` | Model til alle agenter. |
| `VT_MAX_SWARMS` | `3` | Højst så mange agent-teams pr. lørdag. |
| `VT_TOKEN_BUDGET` | `3000000` | Ingen nye teams startes, når forbruget har nået dette. |
| `VT_SWARM_TIMEOUT_MIN` | `45` | Et team, der kører længere, stoppes og markeres "Tidsgrænse". |

Forbruget trækkes fra Anthropic API-kreditten, ikke fra Max-abonnementet. Se det på platform.claude.com → Usage. Rapporten viser et estimat ud fra 3/15 USD pr. mio. tokens (ind/ud). Ret `VT_PRICE_IN_PER_MTOK` og `VT_PRICE_OUT_PER_MTOK` i workflowet, hvis din models pris er en anden.

## Tidsplan og sommertid

GitHub-cron er UTC. Workflowet fyrer 06:00 og 07:00 UTC om lørdagen (= 08:00 svensk sommer- og vintertid) plus 09:30 UTC som sikkerhedsnet. `run_week.py` kører kun, når klokken i Stockholm er 08:00 eller senere, og kun hvis ugen ikke allerede har en gennemført eller delvis rapport. En fejlet kørsel prøves igen kl. 09:30 UTC.

## Når du handler

Opdatér `VT_PORTFOLIO` (Vibetrad → Ugerapport → Opsætning → Kopiér VT_PORTFOLIO → indsæt på GitHub). Ellers analyserer lørdagens kørsel den gamle beholdning.

## Udvikling

```bash
pip install "cryptography>=42" pytest
python -m pytest -q                                   # 22 tests, inkl. dekryptering med WebCrypto i Node
VT_PORTFOLIO="$(cat runner/portfolio.example.json)" VT_RESULTS_PASSPHRASE=0123456789abcdef \
  python runner/run_week.py --dry-run --event workflow_dispatch --results /tmp/vt-results
```

Agenterne får ikke shell-adgang (Vibe-Tradings `bash`-værktøj er slået fra). De bruger Vibe-Tradings egne data- og analyseværktøjer. Vibe-Trading er fastlåst til 0.1.16 i `requirements.txt`. Opgradér bevidst, og kør en tørkørsel bagefter.

Dette er research, ikke investeringsrådgivning. Intet her kan lægge ordrer.
