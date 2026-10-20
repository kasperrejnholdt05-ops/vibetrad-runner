#!/usr/bin/env python3
"""Start Vibe-Trading i codespacet med EODHD koblet på.

Samme kobling som ugekørslen på GitHub: EODHD som datakilde, nordiske og europæiske
symboler (NOVO-B.CO, INVE-B.ST, SAP.DE …) routes til EODHD i stedet for de kinesiske
A-aktie-kilder, EODHD som reserve for amerikanske aktier, og agent-værktøjet
``get_eodhd_data``. Den installerede pakke ændres ikke.

    python ~/vibetrad-runner/tools/vt_med_eodhd.py serve --port 8899   # web-UI
    python ~/vibetrad-runner/tools/vt_med_eodhd.py mcp                 # MCP til Claude Code
    python ~/vibetrad-runner/tools/vt_med_eodhd.py tjek --portfolio-file ~/portefolje.json
    python ~/vibetrad-runner/tools/vt_med_eodhd.py status              # hvad er koblet på?
    python ~/vibetrad-runner/tools/vt_med_eodhd.py nøgle               # virker EODHD-nøglen?
    python ~/vibetrad-runner/tools/vt_med_eodhd.py serve --port 8899 --portfolio-file ~/vt_portfolio.json
    python ~/vibetrad-runner/tools/vt_med_eodhd.py nulstil             # fjern Vibetrads presets igen

Siden 20/10/2026 får web-UI, terminal og MCP også ugekørslens Vibetrad-regler, så en swarm startet i
browseren arbejder som på GitHub: presets uden shell (kvant-værktøjer i stedet), ISK/Nordnet/UCITS-rammer,
team-regler (ingen Kina/A-aktie-sektioner, ingen optioner), EODHD- og FRED-guides og Vibe-Trading-rettelserne
(værktøjskontekst, temperature, prompt, flere forsøg ved stream-fejl, leverancekontrakt). Med
``--portfolio-file`` (samme JSON som VT_PORTFOLIO) får alle agenter også porteføljeteksten med mandat og regler.
``VT_VANILLA=1`` starter Vibe-Trading uden Vibetrads regler (kun EODHD).

Alt andet sendes uændret videre til ``vibe-trading`` (fx ``run -p "…"``).
Nøglen læses fra ``EODHD_API_KEY`` (Codespaces-secret) eller filen ``~/.config/vibetrad/eodhd_key``
(chmod 600) og vises aldrig.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "runner"
sys.path.insert(0, str(RUNNER))

import eodhd  # noqa: E402


def _status(info: dict) -> str:
    if not info.get("key"):
        return ("EODHD: nøgle mangler. Opret Codespaces-secret EODHD_API_KEY (github.com → Settings → Codespaces) "
                "og genstart codespacet, eller gem nøglen i ~/.config/vibetrad/eodhd_key (chmod 600). "
                "Nordiske symboler hentes indtil da via yfinance.")
    bits = ["datakilde" if info.get("loader") else None, "nordisk routing" if info.get("routing") else None,
            "US-reserve" if info.get("us_fallback") else None, "get_eodhd_data" if info.get("tool") else None]
    return "EODHD: koblet på (" + ", ".join(b for b in bits if b) + ")"


USER_PRESETS = Path.home() / ".vibe-trading" / "swarm" / "presets"
MARK = "# vibetrad: genereret af tools/vt_med_eodhd.py – slet med 'nulstil'\n"


def _pop_opt(args: list[str], name: str) -> str | None:
    """Fjern ``--navn værdi`` eller ``--navn=værdi`` fra args (Vibe-Trading kender ikke flaget)."""
    for i, a in enumerate(list(args)):
        if a == name and i + 1 < len(args):
            val = args[i + 1]
            del args[i:i + 2]
            return val
        if a.startswith(name + "="):
            del args[i]
            return a.split("=", 1)[1]
    return None


def apply_vibetrad(info: dict, portfolio_file: str | None = None, dst: Path | None = None) -> dict:
    """Ugekørslens presets og rettelser i dette Vibe-Trading. Fejler aldrig: status til stderr."""
    out: dict = {"presets": [], "patches": {}, "portfolio": False, "error": None}
    if os.environ.get("VT_VANILLA", "").strip().lower() in ("1", "true", "on", "ja"):
        out["error"] = "slået fra med VT_VANILLA"
        return out
    try:
        import run_week as rw

        brief = ""
        if portfolio_file:
            p = rw.load_portfolio(Path(portfolio_file).expanduser().read_text("utf-8"))
            brief = "## Investorens portefølje og mandat (Vibetrad)\n" + rw.portfolio_text(p)
            out["portfolio"] = True
        target = dst or USER_PRESETS
        out["presets"] = rw.prepare_shellfree_presets(target, brief=brief,
                                                      eodhd_ready=bool(info.get("tool") and info.get("key")))
        for name in out["presets"]:  # mærk filerne, så 'nulstil' kun sletter vores egne
            f = target / f"{name}.yaml"
            f.write_text(MARK + f.read_text("utf-8"), "utf-8")
        out["patches"] = rw.patch_vibe_trading()
    except Exception as exc:  # noqa: BLE001 - Vibe-Trading skal stadig kunne starte
        out["error"] = f"{type(exc).__name__}: {eodhd.redact(exc)}"[:300]
    return out


def _rules_status(r: dict) -> str:
    if r.get("error") and not r.get("presets"):
        return f"Vibetrad-regler: ikke anvendt ({r['error']})"
    ok = sum(1 for v in r.get("patches", {}).values() if str(v).startswith("ok"))
    return (f"Vibetrad-regler: {len(r['presets'])} presets tilpasset, {ok}/{len(r.get('patches', {}))} rettelser"
            + (", portefølje og mandat med" if r.get("portfolio") else ", uden portefølje (brug --portfolio-file)"))


def reset_presets(dst: Path | None = None) -> int:
    target = dst or USER_PRESETS
    n = 0
    for f in sorted(target.glob("*.yaml")) if target.exists() else []:
        if f.read_text("utf-8").startswith(MARK):
            f.unlink()
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "status"
    if cmd in ("nulstil", "reset"):
        print(f"Fjernede {reset_presets()} Vibetrad-presets. Vibe-Trading bruger nu sine egne igen.")
        return 0
    portfolio_file = _pop_opt(args, "--portfolio-file")
    if cmd in ("nøgle", "noegle", "key"):
        st = eodhd.key_status()
        if st["ok"]:
            print(f"EODHD-nøglen virker. Abonnement: {st['subscription'] or 'ukendt'}. "
                  f"Kald i dag: {st['requests_today']} af {st['daily_limit']}.")
            return 0
        print(f"EODHD-nøglen virker ikke: {st['error']}")
        return 1
    if cmd == "tjek":
        import datatjek

        return datatjek._cli(args[1:])
    info = eodhd.install_into_vibe_trading()
    # stderr: MCP over stdio må ikke få andet end protokollen på stdout
    print(_status(info), file=sys.stderr, flush=True)
    if cmd != "status":
        print(_rules_status(apply_vibetrad(info, portfolio_file)), file=sys.stderr, flush=True)
    if cmd == "status":
        from src.market_data import detect_source

        n_own = sum(1 for f in USER_PRESETS.glob("*.yaml") if f.read_text("utf-8").startswith(MARK)) if USER_PRESETS.exists() else 0
        print(f"Vibetrad-presets installeret: {n_own}", file=sys.stderr)

        for sym in ("NOVO-B.CO", "INVE-B.ST", "SAP.DE", "NVDA.US"):
            print(f"  {sym:<10} → {detect_source(sym)}", file=sys.stderr)
        return 0 if info.get("routing") else 1
    if cmd == "mcp":
        import mcp_server

        tool_cls = eodhd._SHARED.get("tool_cls")
        if tool_cls is not None and info.get("key"):
            mcp_server._register_mirrored_tool(tool_cls)  # get_eodhd_data også i Claude Code
        sys.argv = ["vibe-trading-mcp", *args[1:]]
        return mcp_server.main() or 0
    from cli import main as vt_main

    return vt_main(args)


if __name__ == "__main__":
    sys.exit(main())
