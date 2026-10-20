"""Kanariefugl: en daglig, gratis tørkørsel, der tester hele kæden, før lørdagens rigtige kørsel.

Kører som den samme workflow (VT_CANARY=true + VT_DRY_RUN=true) hver nat og tjekker det,
en rigtig kørsel er afhængig af, uden at bruge API-kredit:

- secrets findes og kan læses (VT_RESULTS_PASSPHRASE, VT_PORTFOLIO),
- Anthropic-nøglen virker, og modellen findes (count_tokens-endepunktet er gratis),
- Vibe-Trading kan installeres og importeres (requirements.txt holder stadig),
- EODHD-nøglen virker, og datatjekket kan gennemføres,
- regnskabsdatoer findes for alle aktier,
- FRED og EODHD's europæiske makrokalender svarer,
- kuverten kan krypteres, verificeres og gemmes (push-trinnet tester GitHub-tokenet).

Resultatet skrives til results/canary.json (offentligt, uden porteføljedata) og den fulde
kuvert til results/canary/latest.enc.json. Den daglige kørsel rører ikke index.json, ledger.json
eller latest.enc.json, så ugerapporterne i Vibetrad forbliver rene. Fejler et tjek, får kørslen
status "failed", og GitHub sender den sædvanlige fejlmail.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

ANTHROPIC_COUNT_URL = "https://api.anthropic.com/v1/messages/count_tokens"  # VT_ANTHROPIC_COUNT_URL overstyrer (tests)
ANTHROPIC_VERSION = "2023-06-01"
HISTORY = 30  # dage i results/canary.json
STALE_HOURS = 30  # ingen kanariefugl i 30 timer = advarsel på siden


def _redact(text: Any, key: str) -> str:
    s = str(text)
    return s.replace(key, "***") if key and len(key) >= 6 else s


def anthropic_probe(model: str, key: str, opener: Callable | None = None, timeout: float = 20) -> dict:
    """Tester nøglen og modellen med det gratis count_tokens-kald. ok: True, False eller None (kunne ikke afgøres)."""
    if not key:
        return {"ok": False, "note": "secret ANTHROPIC_API_KEY mangler"}
    if not model:
        return {"ok": False, "note": "LANGCHAIN_MODEL_NAME (repo-variablen VT_MODEL) er tom"}
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}]}).encode()
    url = os.environ.get("VT_ANTHROPIC_COUNT_URL") or ANTHROPIC_COUNT_URL
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION,
                                          "content-type": "application/json"})
    op = opener or urllib.request.urlopen
    try:
        with op(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        return {"ok": True, "note": f"nøglen virker, modellen {model} findes "
                                    f"({data.get('input_tokens', '?')} tokens talt – gratis kald)"}
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read().decode("utf-8") or "{}").get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            msg = ""
        msg = _redact(msg, key)[:160]
        if exc.code in (401, 403):
            return {"ok": False, "note": f"nøglen afvist (HTTP {exc.code}) – tjek secret ANTHROPIC_API_KEY"}
        if exc.code in (400, 404) and "model" in msg.lower():
            return {"ok": False, "note": f"modellen {model} findes ikke (HTTP {exc.code}: {msg})"}
        if exc.code == 429:
            return {"ok": True, "note": "nøglen virker (HTTP 429: rate limit lige nu)"}
        if exc.code in (500, 502, 503, 529):
            return {"ok": None, "note": f"Anthropic svarede HTTP {exc.code} (overbelastet) – nøglen kunne ikke testes"}
        return {"ok": False, "note": f"HTTP {exc.code}: {msg or 'uventet svar'}"}
    except Exception as exc:  # noqa: BLE001 - netværk
        return {"ok": None, "note": f"intet svar fra Anthropic ({type(exc).__name__}: {_redact(exc, key)[:120]})"}


def vibe_trading_probe() -> dict:
    """Er Vibe-Trading installeret, og kan motoren importeres?"""
    try:
        from importlib.metadata import version
        ver = version("vibe-trading-ai")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "note": f"vibe-trading-ai er ikke installeret ({type(exc).__name__})"}
    try:
        import src.swarm.runtime  # noqa: F401
        import src.swarm.presets  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "note": f"vibe-trading-ai {ver} er installeret, men kan ikke importeres ({type(exc).__name__}: {str(exc)[:160]})"}
    return {"ok": True, "note": f"vibe-trading-ai {ver} installeret og importeret"}


def check(cid: str, label: str, ok: bool | None, note: str) -> dict:
    return {"id": cid, "label": label, "ok": ok, "note": str(note)[:300]}


def status_of(checks: list[dict]) -> str:
    if any(c["ok"] is False for c in checks):
        return "fail"
    if any(c["ok"] is None for c in checks):
        return "warn"
    return "ok"


STATUS_TO_RUN = {"ok": "ok", "warn": "partial", "fail": "failed"}


def summary(checks: list[dict]) -> dict:
    return {"status": status_of(checks), "ok": sum(1 for c in checks if c["ok"] is True),
            "warn": sum(1 for c in checks if c["ok"] is None), "fail": sum(1 for c in checks if c["ok"] is False),
            "checks": checks}


def console_lines(checks: list[dict]) -> list[str]:
    mark = {True: "ok", None: "advarsel", False: "FEJL"}
    return [f"· {c['label']}: {mark[c['ok']]} – {c['note']}" for c in checks]


def write(results: Path, envelope: dict) -> Path:
    """Skriver kuverten til results/canary/latest.enc.json og status + historik til results/canary.json."""
    pub = envelope["public"]
    can = pub.get("canary") or {}
    d = results / "canary"
    d.mkdir(parents=True, exist_ok=True)
    (d / "latest.enc.json").write_text(json.dumps(envelope, ensure_ascii=False, indent=1) + "\n", "utf-8")
    f = results / "canary.json"
    try:
        old = json.loads(f.read_text("utf-8"))
        hist = [h for h in old.get("history", []) if h.get("run_id") != pub.get("run_id")]
    except Exception:  # noqa: BLE001
        hist = []
    entry = {"run_id": pub.get("run_id"), "generated_at": pub.get("generated_at"), "status": can.get("status"),
             "ok": can.get("ok"), "warn": can.get("warn"), "fail": can.get("fail"), "duration_s": can.get("duration_s")}
    hist = sorted(hist + [entry], key=lambda h: str(h.get("generated_at") or ""))[-HISTORY:]
    doc = {"schema": 1, "generated_at": pub.get("generated_at"), "run_id": pub.get("run_id"), "week_id": pub.get("week_id"),
           "status": can.get("status"), "ok": can.get("ok"), "warn": can.get("warn"), "fail": can.get("fail"),
           "duration_s": can.get("duration_s"), "checks": can.get("checks", []), "github_run_id": can.get("github_run_id"),
           "stale_after_hours": STALE_HOURS, "history": hist}
    f.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", "utf-8")
    return d / "latest.enc.json"


def is_canary_envelope(envelope: dict) -> bool:
    return bool((envelope.get("public") or {}).get("canary"))


def day_label(generated_at: str) -> str:
    try:
        return dt.datetime.fromisoformat(generated_at).strftime("%d/%m/%Y")
    except Exception:  # noqa: BLE001
        return generated_at[:10]


def env_flag(name: str = "VT_CANARY") -> bool:
    return os.environ.get(name, "").strip().lower() == "true"
