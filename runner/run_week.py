#!/usr/bin/env python3
"""Vibetrad ugekørsel – kører det rigtige HKUDS/Vibe-Trading og krypterer resultatet.

Kører i GitHub Actions hver lørdag kl. 08:00 svensk tid (se
.github/workflows/vibetrad-weekly.yml). Flowet:

1. Afgør om der skal køres (lørdag, efter 08:00 Stockholm, ugen ikke allerede kørt).
2. Læser porteføljen fra secret VT_PORTFOLIO (JSON, genereres af Vibetrad-siden).
3. Lægger en plan efter spillebogen:
   - hver uge: portfolio_review_board
   - første lørdag i måneden: risk_committee
   - regnskab inden for 14 dage: earnings_research_desk for den nærmeste
4. Kører swarms med Vibe-Tradings egen SwarmRuntime (samme motor som web-UI og CLI).
5. Pakker rapporter og agent-opsummeringer, gzip'er og krypterer med AES-256-GCM
   (nøgle afledt af VT_RESULTS_PASSPHRASE via PBKDF2-SHA256), og skriver
   results/latest.enc.json + results/weeks/<uge>.enc.json + results/index.json.

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
import os
import secrets
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

STO = ZoneInfo("Europe/Stockholm")
SCHEMA = 1
PBKDF2_ITER = 600_000
MIN_PASSPHRASE = 16
MAX_ENVELOPE_BYTES = 240_000  # artifact-db dokumenter må højst være 256 KiB

PRESET_LABELS = {
    "portfolio_review_board": "Porteføljegennemgang",
    "risk_committee": "Risikoudvalg",
    "earnings_research_desk": "Regnskabsdesk",
    "investment_committee": "Investeringskomité",
    "etf_allocation_desk": "ETF-allokering",
    "sentiment_intelligence_team": "Sentiment",
}

DEFAULT_GOAL = (
    "Gør porteføljen mere robust: maks. 10 % pr. enkeltaktie, mindre korrelation "
    "mellem positionerne og ingen unødige handler. Afslut med konkrete målvægte "
    "og hvad nye indskud bør gå til."
)
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


def week_window(ref: dt.datetime) -> dict:
    """Den senest afsluttede amerikanske handelsuge (mandag–fredag) set fra ref."""
    d = ref.date()
    # fredag i samme uge hvis ref er lørdag/søndag, ellers forrige fredag
    back = (d.weekday() - 4) % 7
    if back == 0 and ref.hour < 23:  # fredag før lukning tæller ikke som afsluttet
        back = 7
    friday = d - dt.timedelta(days=back)
    monday = friday - dt.timedelta(days=4)
    iso = monday.isocalendar()
    return {
        "id": f"{iso.year}-W{iso.week:02d}",
        "from": monday.isoformat(),
        "to": friday.isoformat(),
        "label": f"Uge {iso.week} · {dmy(monday)}–{dmy(friday)}",
    }


def should_run(ref: dt.datetime, latest_meta: dict | None, event: str, force: bool) -> tuple[bool, str]:
    week = week_window(ref)
    if force or event == "workflow_dispatch":
        return True, "manuel kørsel"
    if ref.weekday() != 5:
        return False, "ikke lørdag i Stockholm"
    if ref.hour < 8:
        return False, f"kun {ref:%H:%M} i Stockholm – venter til 08:00"
    if latest_meta and latest_meta.get("week_id") == week["id"] and latest_meta.get("status") in ("ok", "partial"):
        return False, f"{week['id']} er allerede kørt"
    return True, "planlagt lørdagskørsel"


# ---------------------------------------------------------------------------
# Portefølje
# ---------------------------------------------------------------------------
class PortfolioError(ValueError):
    pass


def load_portfolio(raw: str | None) -> dict:
    if not raw or not raw.strip():
        raise PortfolioError("VT_PORTFOLIO mangler – opret secret'en med JSON fra Vibetrad-siden")
    try:
        p = json.loads(raw)
    except json.JSONDecodeError as exc:  # beskeden må ikke citere indholdet
        raise PortfolioError(f"VT_PORTFOLIO er ikke gyldig JSON (linje {exc.lineno}, kolonne {exc.colno})") from None
    if not isinstance(p, dict) or not isinstance(p.get("holdings"), list) or not p["holdings"]:
        raise PortfolioError("VT_PORTFOLIO skal have en ikke-tom liste 'holdings'")
    clean = []
    for i, h in enumerate(p["holdings"]):
        if not isinstance(h, dict) or not str(h.get("ticker", "")).strip():
            raise PortfolioError(f"holdings[{i}] mangler 'ticker'")
        qty = h.get("qty")
        if qty is not None and not isinstance(qty, (int, float)):
            raise PortfolioError(f"holdings[{i}].qty skal være et tal")
        clean.append({
            "ticker": str(h["ticker"]).strip().upper(),
            "name": str(h.get("name") or h["ticker"]).strip(),
            "qty": qty,
            "cost_sek": float(h["cost_sek"]) if isinstance(h.get("cost_sek"), (int, float)) else None,
            "max_weight": float(h.get("max_weight") or 10),
            "kind": str(h.get("kind") or "stock"),
        })
    p["holdings"] = clean
    p.setdefault("account", "ISK Nordnet")
    p.setdefault("currency", "SEK")
    p["cash_sek"] = float(p.get("cash_sek") or 0)
    p.setdefault("watchlist", [])
    p.setdefault("next_reports", {})
    return p


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
    for h in p["holdings"]:
        bits = [f"{h['name']} ({h['ticker']})"]
        if h["qty"] is not None:
            bits.append(f"{h['qty']:g} stk")
        if h["cost_sek"]:
            bits.append(f"anskaffelse {h['cost_sek']:,.0f} SEK".replace(",", " "))
        bits.append(f"maks. vægt {h['max_weight']:g} %")
        lines.append("- " + ", ".join(bits))
    if p.get("cash_sek"):
        note = f" ({p['cash_note']})" if p.get("cash_note") else ""
        lines.append(f"- Kontant: {p['cash_sek']:,.0f} SEK{note}".replace(",", " "))
    if p.get("watchlist"):
        wl = "; ".join(f"{w.get('ticker')}: {w.get('note', '')}".strip(": ") for w in p["watchlist"] if isinstance(w, dict))
        lines.append(f"Overvågning/åbne ordrer: {wl}")
    if p.get("framework"):
        lines.append(f"Investeringsramme: {p['framework']}")
    return "\n".join(lines)


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


def upcoming_reports(p: dict, ref: dt.datetime, horizon_days: int = 14,
                     lookup: Callable[[str], dt.date | None] | None = yf_next_report) -> list[dict]:
    today = ref.date()
    found = []
    for h in p["holdings"]:
        if h["kind"] in ("cash", "fund", "etf"):
            continue
        raw = (p.get("next_reports") or {}).get(h["ticker"])
        d, src = None, None
        if raw:
            try:
                d, src = dt.date.fromisoformat(str(raw)[:10]), "portefølje"
            except ValueError:
                d = None
        if (d is None or d < today) and lookup:
            d2 = lookup(h["ticker"])
            if d2:
                d, src = d2, "yahoo"
        if d and today <= d <= today + dt.timedelta(days=horizon_days):
            found.append({"ticker": h["ticker"], "name": h["name"], "date": d.isoformat(), "source": src})
    return sorted(found, key=lambda x: x["date"])


def build_plan(p: dict, ref: dt.datetime, week: dict, *, max_swarms: int = 3, force_risk: bool = False,
               reports: list[dict] | None = None) -> list[dict]:
    goal = (p.get("goal") or DEFAULT_GOAL).strip()
    ptxt = portfolio_text(p)
    plan = [{
        "preset": "portfolio_review_board",
        "reason": "Fast ugentlig gennemgang (spillebog: hver uge)",
        "vars": {
            "portfolio": ptxt,
            "review_period": f"Seneste amerikanske handelsuge {dmy(dt.date.fromisoformat(week['from']))}–{dmy(dt.date.fromisoformat(week['to']))}",
            "goal": goal + LANG_SUFFIX,
        },
    }]
    if ref.day <= 7 or force_risk:
        biggest = max(p["holdings"], key=lambda h: h["cost_sek"] or 0)
        plan.append({
            "preset": "risk_committee",
            "reason": "Første lørdag i måneden (spillebog: månedligt risikoudvalg)",
            "vars": {"goal": (
                f"Vurdér drawdown, halerisiko og markedsregime for denne portefølje:\n{ptxt}\n"
                f"Stresstest: −30 % i {biggest['name']} ({biggest['ticker']}), −20 % i amerikansk tech samlet, "
                "og +10 % SEK mod USD. Hvad er det samlede tab i SEK, og hvilke ændringer sænker risikoen mest?"
                + LANG_SUFFIX)},
        })
    for r in (reports or [])[:1]:
        plan.append({
            "preset": "earnings_research_desk",
            "reason": f"Regnskab {dmy(dt.date.fromisoformat(r['date']))} inden for 14 dage (spillebog: før hvert regnskab)",
            "vars": {"target": f"{r['ticker']} (regnskab {dmy(dt.date.fromisoformat(r['date']))}; skriv på dansk, beløb også i SEK)"},
            "context": r,
        })
    return plan[:max(1, max_swarms)]


# ---------------------------------------------------------------------------
# Swarm-kørsel
# ---------------------------------------------------------------------------
TERMINAL = {"completed", "failed", "cancelled"}


def _status(x: Any) -> str:
    return getattr(x, "value", x) if x is not None else ""


def run_swarm(runtime: Any, store: Any, preset: str, user_vars: dict, *, timeout_s: int,
              poll_s: float = 2.0, clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> Any:
    # Agenterne får ikke shell-adgang (bash-værktøjet udelades); de bruger
    # Vibe-Tradings egne data- og analyseværktøjer.
    run = runtime.start_run(preset, user_vars, include_shell_tools=False)
    deadline = clock() + timeout_s
    current = run
    while True:
        current = store.load_run(run.id) or current
        if _status(current.status) in TERMINAL:
            return current
        if clock() > deadline:
            with contextlib.suppress(Exception):
                runtime.cancel_run(run.id)
            sleep(min(5.0, poll_s * 2))
            current = store.load_run(run.id) or current
            current.timed_out = True  # type: ignore[attr-defined]
            return current
        sleep(poll_s)


def _cap(text: str | None, n: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= n else text[:n] + f"\n\n[… afkortet, {len(text) - n} tegn udeladt]"


def summarize_run(run: Any, preset: str, reason: str, started: dt.datetime, duration_s: float,
                  *, price_in: float, price_out: float, report_cap: int = 60_000, task_cap: int = 16_000) -> dict:
    roles = {a.id: getattr(a, "role", a.id) for a in (getattr(run, "agents", None) or [])}
    tin = int(getattr(run, "total_input_tokens", 0) or 0)
    tout = int(getattr(run, "total_output_tokens", 0) or 0)
    tasks = []
    for t in getattr(run, "tasks", None) or []:
        tasks.append({
            "id": t.id, "agent_id": t.agent_id, "role": roles.get(t.agent_id, t.agent_id),
            "status": _status(t.status), "summary": _cap(t.summary, task_cap), "error": _cap(t.error, 2000),
            "iterations": int(getattr(t, "worker_iterations", 0) or 0),
            "started_at": getattr(t, "started_at", None), "completed_at": getattr(t, "completed_at", None),
        })
    status = _status(run.status)
    if getattr(run, "timed_out", False):
        status = "timeout"
    return {
        "preset": preset, "label": PRESET_LABELS.get(preset, preset), "reason": reason,
        "run_id": run.id, "status": status,
        "started_at": started.isoformat(timespec="seconds"),
        "completed_at": getattr(run, "completed_at", None),
        "duration_s": round(duration_s), "provider": getattr(run, "provider", None), "model": getattr(run, "model", None),
        "tokens_in": tin, "tokens_out": tout,
        "est_cost_usd": round(tin / 1e6 * price_in + tout / 1e6 * price_out, 2),
        "final_report": _cap(getattr(run, "final_report", None), report_cap),
        "tasks": tasks,
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


def aad_bytes(public: dict) -> bytes:
    """Metadata bindes til krypteringen. Skal give samme bytes som JSON.stringify i browseren."""
    return json.dumps(public, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


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
    (results / "weeks" / f"{pub['week_id']}.enc.json").write_text(body, "utf-8")
    (results / "latest.enc.json").write_text(body, "utf-8")
    idx_f = results / "index.json"
    try:
        idx = json.loads(idx_f.read_text("utf-8"))
    except Exception:
        idx = {"schema": SCHEMA, "weeks": []}
    idx["weeks"] = [w for w in idx.get("weeks", []) if w.get("week_id") != pub["week_id"]]
    idx["weeks"].insert(0, pub)
    idx["weeks"] = sorted(idx["weeks"], key=lambda w: w["generated_at"], reverse=True)[:104]
    idx["latest"] = pub["week_id"]
    idx_f.write_text(json.dumps(idx, ensure_ascii=False, indent=1) + "\n", "utf-8")


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

    def __init__(self, fail: Iterable[str] = ()):
        self.fail = set(fail)
        self.runs: dict[str, Any] = {}

    def start_run(self, preset: str, user_vars: dict, include_shell_tools: bool = False) -> Any:
        rid = f"dry-{preset}-{len(self.runs) + 1}"
        agents = [SimpleNamespace(id=f"{preset}_a{i}", role=f"Analytiker {i}") for i in (1, 2)]
        ok = preset not in self.fail
        tasks = [SimpleNamespace(id=f"task-{i}", agent_id=a.id, status="completed" if ok else "failed",
                                 summary=f"Tørkørsel: {a.role} for {preset}." if ok else None,
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


def make_runtime(dry: bool) -> tuple[Any, Any, str]:
    if dry:
        fake = FakeRuntime(fail=filter(None, os.environ.get("VT_DRY_FAIL", "").split(",")))
        return fake, fake, "dry-run"
    from importlib.metadata import version

    from src.config import load_swarm_agent_config
    from src.config.paths import get_swarm_runs_dir
    from src.swarm.runtime import SwarmRuntime
    from src.swarm.store import SwarmStore

    store = SwarmStore(base_dir=get_swarm_runs_dir())
    runtime = SwarmRuntime(store=store, agent_config=load_swarm_agent_config())
    return runtime, store, version("vibe-trading-ai")


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
    ap.add_argument("--now", default=os.environ.get("VT_NOW"))
    ap.add_argument("--gate-only", action="store_true", help="afgør kun om der skal køres (ingen secrets)")
    args = ap.parse_args(argv)

    con = Console()
    results = Path(args.results)
    results.mkdir(parents=True, exist_ok=True)
    ref = now_sto(args.now)
    week = week_window(ref)

    ok, why = should_run(ref, read_latest_meta(results), args.event, args.force)
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

    max_swarms = int(os.environ.get("VT_MAX_SWARMS", "3"))
    token_budget = int(os.environ.get("VT_TOKEN_BUDGET", "3000000"))
    timeout_s = int(float(os.environ.get("VT_SWARM_TIMEOUT_MIN", "45")) * 60)
    price_in = float(os.environ.get("VT_PRICE_IN_PER_MTOK", "3"))
    price_out = float(os.environ.get("VT_PRICE_OUT_PER_MTOK", "15"))
    iterations = int(os.environ.get("VT_PBKDF2_ITER", str(PBKDF2_ITER)))

    log_path = Path(tempfile.gettempdir()) / "vibetrad-vt.log"
    log_path.write_bytes(b"")
    errors: list[dict] = []
    runs: list[dict] = []
    t0 = time.monotonic()

    with quiet_fds(log_path):  # yfinance skriver advarsler, der kan nævne symboler
        reports = upcoming_reports(portfolio, ref, lookup=None if args.dry_run else yf_next_report)
    plan = build_plan(portfolio, ref, week, max_swarms=max_swarms, force_risk=args.force_risk, reports=reports)
    con.say("Plan: " + ", ".join(PRESET_LABELS.get(s["preset"], s["preset"]) for s in plan))

    vt_version = "?"
    with quiet_fds(log_path):
        try:
            runtime, store, vt_version = make_runtime(args.dry_run)
        except Exception as exc:
            errors.append({"stage": "init", "type": type(exc).__name__, "message": str(exc)[:2000],
                           "trace": traceback.format_exc()[-4000:]})
            runtime = store = None

    if runtime is None:
        con.say(f"FEJL ved opstart af Vibe-Trading ({errors[-1]['type']}). Detaljer ligger krypteret i rapporten.")

    used_tokens = 0
    for step in plan if runtime else []:
        label = PRESET_LABELS.get(step["preset"], step["preset"])
        if used_tokens >= token_budget:
            runs.append({"preset": step["preset"], "label": label, "reason": step["reason"], "status": "skipped",
                         "skip_reason": f"Tokenbudget nået ({used_tokens:,} af {token_budget:,})".replace(",", " "),
                         "tasks": [], "tokens_in": 0, "tokens_out": 0, "est_cost_usd": 0})
            con.say(f"· {label}: sprunget over (tokenbudget)")
            continue
        started, m0 = dt.datetime.now(STO), time.monotonic()
        con.say(f"· {label}: starter")
        with quiet_fds(log_path):
            try:
                validate_vars(step["preset"], step["vars"], args.dry_run)
                run = run_swarm(runtime, store, step["preset"], step["vars"], timeout_s=timeout_s,
                                poll_s=0.01 if args.dry_run else 5.0)
                summary = summarize_run(run, step["preset"], step["reason"], started, time.monotonic() - m0,
                                        price_in=price_in, price_out=price_out)
                if step.get("context"):
                    summary["context"] = step["context"]
                runs.append(summary)
            except Exception as exc:
                errors.append({"stage": step["preset"], "type": type(exc).__name__, "message": str(exc)[:2000],
                               "trace": traceback.format_exc()[-4000:]})
                runs.append({"preset": step["preset"], "label": label, "reason": step["reason"], "status": "error",
                             "error": f"{type(exc).__name__}: {str(exc)[:500]}", "tasks": [],
                             "tokens_in": 0, "tokens_out": 0, "est_cost_usd": 0,
                             "duration_s": round(time.monotonic() - m0)})
        r = runs[-1]
        used_tokens += r.get("tokens_in", 0) + r.get("tokens_out", 0)
        con.say(f"· {label}: {r['status']} · {r.get('duration_s', 0)} s · {r.get('tokens_in', 0) + r.get('tokens_out', 0):,} tokens".replace(",", " "))

    done = [r for r in runs if r["status"] == "completed"]
    status = "ok" if runs and len(done) == len(runs) else "partial" if done else "failed"
    tin = sum(r.get("tokens_in", 0) for r in runs)
    tout = sum(r.get("tokens_out", 0) for r in runs)
    generated = dt.datetime.now(STO).isoformat(timespec="seconds")
    log_tail = log_path.read_text("utf-8", "replace")[-6000:] if log_path.exists() else ""

    payload = {
        "schema": SCHEMA, "generated_at": generated, "week": week, "status": status,
        "vibe_trading_version": vt_version,
        "model": os.environ.get("LANGCHAIN_MODEL_NAME"), "provider": os.environ.get("LANGCHAIN_PROVIDER"),
        "dry_run": args.dry_run, "trigger": args.event,
        "portfolio": {"account": portfolio["account"], "as_of": portfolio.get("as_of"),
                      "holdings": portfolio["holdings"], "cash_sek": portfolio["cash_sek"],
                      "weights_cost": cost_weights(portfolio)},
        "upcoming_reports": reports, "plan": [{"preset": s["preset"], "reason": s["reason"]} for s in plan],
        "runs": runs,
        "totals": {"tokens_in": tin, "tokens_out": tout, "duration_s": round(time.monotonic() - t0),
                   "est_cost_usd": round(sum(r.get("est_cost_usd", 0) for r in runs), 2),
                   "price_basis": f"{price_in:g}/{price_out:g} USD pr. mio. tokens (ind/ud) – estimat"},
        "errors": errors, "log_tail": log_tail,
        "github": {"repo": os.environ.get("GITHUB_REPOSITORY"), "run_id": os.environ.get("GITHUB_RUN_ID"),
                   "server": os.environ.get("GITHUB_SERVER_URL", "https://github.com")},
    }
    public = {"week_id": week["id"], "week_label": week["label"], "generated_at": generated, "status": status,
              "runs": len(runs), "completed": len(done), "dry_run": args.dry_run,
              "presets": [r["preset"] for r in done]}
    envelope = fit_payload(payload, passphrase, public, iterations)
    assert decrypt(envelope, passphrase)["week"]["id"] == week["id"], "kryptering kunne ikke verificeres"
    write_results(results, envelope)
    kb = len(json.dumps(envelope)) / 1024
    con.say(f"Resultat: {status} · {len(done)}/{len(runs)} swarms · {tin + tout:,} tokens · {kb:.0f} KB krypteret".replace(",", " "))
    set_output(ran="true", status=status, week=week["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
