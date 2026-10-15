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

Alt andet sendes uændret videre til ``vibe-trading`` (fx ``run -p "…"``).
Nøglen læses fra ``EODHD_API_KEY`` (Codespaces-secret) eller filen ``~/.config/vibetrad/eodhd_key``
(chmod 600) og vises aldrig.
"""
from __future__ import annotations

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


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "status"
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
    if cmd == "status":
        from src.market_data import detect_source

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
