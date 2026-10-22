"""Kanariefuglen: Anthropic-probe uden netværk, status, skrivning af results/canary.json og hele tørkørslen."""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
import kanarie as kn  # noqa: E402
import run_week as rw  # noqa: E402
from test_run_week import PASS, outputs, run_main  # noqa: E402

KEY = "sk-ant-test-0123456789abcdef0123456789"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http(code, body):
    return urllib.error.HTTPError(kn.ANTHROPIC_COUNT_URL, code, "x", {}, io.BytesIO(json.dumps(body).encode()))


def test_anthropic_probe_ok_and_request_shape():
    seen = []

    def op(req, timeout=0):
        seen.append(req)
        return _Resp(json.dumps({"input_tokens": 8}).encode())

    r = kn.anthropic_probe("claude-sonnet-5", KEY, opener=op)
    assert r == {"ok": True, "note": "nøglen virker, modellen claude-sonnet-5 findes (8 tokens talt – gratis kald)"}
    req = seen[0]
    assert req.get_method() == "POST" and req.get_header("X-api-key") == KEY
    assert req.get_header("Anthropic-version") == kn.ANTHROPIC_VERSION
    assert json.loads(req.data)["model"] == "claude-sonnet-5"


@pytest.mark.parametrize("code,body,ok,frag", [
    (401, {"error": {"message": "invalid x-api-key"}}, False, "nøglen afvist (HTTP 401)"),
    (404, {"error": {"message": "model: claude-x not found"}}, False, "modellen claude-x findes ikke"),
    (400, {"error": {"message": f"bad model {KEY}"}}, False, "***"),  # nøglen i et fejlsvar maskeres
    (429, {"error": {"message": "rate limit"}}, True, "HTTP 429"),
    (529, {"error": {"message": "overloaded"}}, None, "overbelastet"),
    (418, {"error": {"message": "teapot"}}, False, "HTTP 418: teapot"),
])
def test_anthropic_probe_http_errors(code, body, ok, frag):
    def op(req, timeout=0):
        raise _http(code, body)

    r = kn.anthropic_probe("claude-x", KEY, opener=op)
    assert r["ok"] is ok and frag in r["note"] and KEY not in r["note"]


def test_anthropic_probe_without_key_model_or_network():
    assert kn.anthropic_probe("m", "")["ok"] is False and "mangler" in kn.anthropic_probe("m", "")["note"]
    assert "VT_MODEL" in kn.anthropic_probe("", KEY)["note"]

    def op(req, timeout=0):
        raise OSError(f"dns down {KEY}")

    r = kn.anthropic_probe("m", KEY, opener=op)
    assert r["ok"] is None and "OSError" in r["note"] and KEY not in r["note"]


def test_status_and_summary():
    c = [kn.check("a", "A", True, "x"), kn.check("b", "B", None, "y"), kn.check("c", "C", False, "z" * 500)]
    assert kn.status_of(c[:1]) == "ok" and kn.status_of(c[:2]) == "warn" and kn.status_of(c) == "fail"
    s = kn.summary(c)
    assert (s["ok"], s["warn"], s["fail"], s["status"]) == (1, 1, 1, "fail") and len(c[2]["note"]) == 300
    assert kn.console_lines(c)[2].startswith("· C: FEJL – zzz")
    assert kn.STATUS_TO_RUN == {"ok": "ok", "warn": "partial", "fail": "failed"}


def test_write_keeps_history_and_never_touches_weekly_results(tmp_path):
    results = tmp_path / "results"
    pub = {"run_id": "2026-W43_20261020-0531_1", "week_id": "2026-W43", "generated_at": "2026-10-20T05:31:00+02:00",
           "status": "ok", "dry_run": True,
           "canary": {"status": "ok", "ok": 8, "warn": 0, "fail": 0, "duration_s": 95, "github_run_id": "1",
                      "checks": [kn.check("fred", "FRED", True, "ja")]}}
    env = rw.encrypt({"week": {"id": "2026-W43"}}, "p" * 20, pub, iterations=1000)
    assert kn.is_canary_envelope(env) and not kn.is_canary_envelope({"public": {"run_id": "x"}})
    out = kn.write(results, env)
    assert out == results / "canary" / "latest.enc.json" and json.loads(out.read_text())["public"]["run_id"] == pub["run_id"]
    doc = json.loads((results / "canary.json").read_text())
    assert doc["status"] == "ok" and doc["checks"][0]["id"] == "fred" and doc["stale_after_hours"] == kn.STALE_HOURS
    assert [h["run_id"] for h in doc["history"]] == [pub["run_id"]]
    assert not (results / "latest.enc.json").exists() and not (results / "index.json").exists() and not (results / "ledger.json").exists()
    # dagen efter: historikken vokser, samme run_id erstattes, og kun de seneste HISTORY beholdes
    for i in range(1, kn.HISTORY + 3):
        pub2 = dict(pub, run_id=f"r{i}", generated_at=f"2026-11-{min(i, 28):02d}T05:31:00+01:00",
                    canary=dict(pub["canary"], status="fail" if i == 3 else "ok"))
        kn.write(results, rw.encrypt({"week": {"id": "2026-W43"}}, "p" * 20, pub2, iterations=1000))
    kn.write(results, rw.encrypt({"week": {"id": "2026-W43"}}, "p" * 20, pub2, iterations=1000))  # samme kørsel igen
    doc = json.loads((results / "canary.json").read_text())
    assert len(doc["history"]) == kn.HISTORY and doc["history"][-1]["run_id"] == pub2["run_id"]
    assert len({h["run_id"] for h in doc["history"]}) == kn.HISTORY
    assert kn.day_label("2026-10-20T05:31:00+02:00") == "20/10/2026"


def test_should_run_canary_ignores_weekday_schedule_and_previous_runs(monkeypatch):
    monkeypatch.setenv("VT_SCHEDULE", "off")
    ref = rw.now_sto("2026-10-21T05:30")  # onsdag
    meta = {"week_id": "2026-W43", "status": "ok", "week_to": "2026-10-23", "dry_run": False}
    ok, why = rw.should_run(ref, meta, "schedule", False, canary=True)
    assert ok and why.startswith("kanariefugl")
    assert rw.should_run(ref, meta, "schedule", False)[0] is False


def test_extra_nightly_canary_fires_skip_when_tonight_is_already_green(monkeypatch):
    """22/10/2026 kom nattens kanariefugl aldrig: der fyres nu flere gange, men kun den første kører."""
    ref = rw.now_sto("2026-10-22T05:30")
    green = {"generated_at": "2026-10-22T04:19:00+02:00", "status": "ok"}
    ok, why = rw.should_run(ref, None, "schedule", False, canary=True, canary_meta=green)
    assert not ok and "04:19" in why
    assert rw.should_run(ref, None, "schedule", False, canary=True, canary_meta={**green, "status": "warn"})[0] is False
    # rød i nat → prøv igen; gårsdagens → kør; UTC-tid lige før midnat svensk tid tæller som i går
    assert rw.should_run(ref, None, "schedule", False, canary=True, canary_meta={**green, "status": "fail"})[0] is True
    assert rw.should_run(ref, None, "schedule", False, canary=True,
                         canary_meta={"generated_at": "2026-10-21T21:59:00+00:00", "status": "ok"})[0] is True
    assert rw.should_run(ref, None, "schedule", False, canary=True,
                         canary_meta={"generated_at": "2026-10-21T22:01:00+00:00", "status": "ok"})[0] is False
    # knaptryk kører altid; ødelagt eller manglende statusfil → kør
    assert rw.should_run(ref, None, "workflow_dispatch", False, canary=True, canary_meta=green)[0] is True
    assert rw.should_run(ref, None, "schedule", True, canary=True, canary_meta=green)[0] is True
    assert rw.should_run(ref, None, "schedule", False, canary=True, canary_meta={"generated_at": "x"})[0] is True
    assert rw.should_run(ref, None, "schedule", False, canary=True, canary_meta=None)[0] is True


def test_canary_gate_reads_results_canary_json(tmp_path, monkeypatch, capsys):
    import json as _j
    res = tmp_path / "results"
    res.mkdir()
    (res / "canary.json").write_text(_j.dumps({"generated_at": "2026-10-22T04:19:00+02:00", "status": "ok"}))
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("VT_CANARY", "true")
    assert rw.main(["--results", str(res), "--event", "schedule", "--gate-only", "--now", "2026-10-22T06:41"]) == 0
    assert "ran=false" in out.read_text()
    out.write_text("")
    assert rw.main(["--results", str(res), "--event", "schedule", "--gate-only", "--now", "2026-10-23T04:17"]) == 0
    assert "ran=true" in out.read_text()


def test_canary_run_end_to_end(tmp_path):
    import http.server
    import threading

    posts = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            posts.append((self.path, self.headers.get("x-api-key"), json.loads(self.rfile.read(n) or b"{}")))
            raw = json.dumps({"input_tokens": 8}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        p = run_main(tmp_path, "--event", "schedule", "--now", "2026-10-21T05:30", VT_CANARY="true", VT_SCHEDULE="off",
                     VT_ANTHROPIC_COUNT_URL=f"http://127.0.0.1:{srv.server_address[1]}/v1/messages/count_tokens",
                     ANTHROPIC_API_KEY=KEY, LANGCHAIN_MODEL_NAME="claude-sonnet-5", GITHUB_RUN_ID="4242")
    finally:
        srv.shutdown()
    assert p.returncode == 0, p.stderr
    log = p.stdout + p.stderr
    assert "KØRER: kanariefugl" in log and "· Anthropic-nøgle og model: ok – nøglen virker, modellen claude-sonnet-5 findes" in log
    assert "Kanariefugl: " in log and KEY not in log
    for secret in ("ZZTOP", "Hemmelig", "21480"):  # porteføljedata må aldrig nå loggen eller results/canary.json
        assert secret not in log
    assert posts and posts[0][1] == KEY and posts[0][2]["model"] == "claude-sonnet-5"
    res = tmp_path / "results"
    doc = json.loads((res / "canary.json").read_text())
    ids = [c["id"] for c in doc["checks"]]
    assert ids[:3] == ["secrets", "anthropic", "vibe_trading"] and "datatjek" in ids and "fred" in ids and "runtime" in ids
    by = {c["id"]: c for c in doc["checks"]}
    assert by["anthropic"]["ok"] is True and by["runtime"]["ok"] is True and by["fred"]["ok"] is None  # ingen FRED-nøgle: advarsel
    assert doc["github_run_id"] == "4242" and doc["history"][-1]["run_id"] == doc["run_id"] and doc["duration_s"] >= 0
    raw = (res / "canary.json").read_text()
    for secret in ("ZZTOP", "Hemmelig", "21480", KEY):
        assert secret not in raw
    # uden EODHD-nøgle fejler kæden, og kørslen bliver rød (GitHub sender fejlmail)
    assert by["eodhd"]["ok"] is False and doc["status"] == "fail"
    o = outputs(tmp_path)
    assert o["status"] == "failed" and o["day"] == rw.now_sto().strftime("%d/%m/%Y")  # commit-besked: dagen i Stockholm
    assert o["result_file"] == str(res / "canary" / "latest.enc.json")
    # ugerapporterne er urørte, og kuverten kan dekrypteres
    assert not (res / "latest.enc.json").exists() and not (res / "index.json").exists()
    data = rw.decrypt(json.loads((res / "canary" / "latest.enc.json").read_text()), PASS)
    assert data["dry_run"] is True and data["week"]["id"] == "2026-W43"
    # --merge-results på en kanarie-kuvert skriver canary.json igen (push-konflikt) – uden ugefiler
    import subprocess
    f = tmp_path / "kuvert.json"
    f.write_text((res / "canary" / "latest.enc.json").read_text())
    (res / "canary.json").unlink()
    q = subprocess.run([sys.executable, str(ROOT / "runner" / "run_week.py"), "--results", str(res), "--merge-results", str(f)],
                       capture_output=True, text=True)
    assert q.returncode == 0 and "lagt ind" in q.stdout
    assert json.loads((res / "canary.json").read_text())["run_id"] == doc["run_id"] and not (res / "index.json").exists()


def _datacheck(status="PASS", counts=(9, 0, 0), eodhd_ok=True, earnings=("ZZTOP.ST", "QQQ.US")):
    return {"status": status, "counts": dict(zip(("ok", "warn", "fail"), counts)),
            "sources": {"eodhd": {"available": True, "calls": 12 if eodhd_ok else 0,
                                  "account": {"ok": True} if eodhd_ok else {"ok": False, "error": "HTTP 401"}},
                        "vibe_trading": {"available": True}},
            "earnings": [{"ticker": t, "verified": "2026-11-04"} for t in earnings]}


PF = {"holdings": [{"ticker": "ZZTOP.ST", "kind": "stock"}, {"ticker": "QQQ.US", "kind": "stock"},
                   {"ticker": "ACWI.US", "kind": "etf"}, {"ticker": "CASH", "kind": "cash"}]}


def test_canary_checks_all_green_and_each_failure_mode(monkeypatch):
    import fred as fr
    import makro as mk
    monkeypatch.setattr(kn, "anthropic_probe", lambda model, key: {"ok": True, "note": "nøglen virker"})
    monkeypatch.setattr(kn, "vibe_trading_probe", lambda: {"ok": True, "note": "vibe-trading-ai 0.1.16 installeret og importeret"})
    fred = {"key": True, "ok": True, "calls": 9, "series": [{"id": "DGS10", "name": "US 10-årig statsrente", "unit": "%",
                                                               "value": 4.1, "date": "2026-10-17"}]}
    makro = {"ok": True, "countries": []}
    checks = rw.canary_checks(PF, _datacheck(), fred, makro, True)
    assert kn.status_of(checks) == "ok" and [c["id"] for c in checks] == [
        "secrets", "anthropic", "vibe_trading", "eodhd", "datatjek", "regnskab", "fred", "makro", "runtime"]
    assert all("ZZTOP" not in c["note"] for c in checks)
    assert checks[6]["note"] == fr.status_line(fred) and checks[7]["note"] == mk.status_line(makro)
    # advarsler: datatjek ADVARSEL, en aktie uden regnskabsdato
    by = {c["id"]: c for c in rw.canary_checks(PF, _datacheck("ADVARSEL", (7, 2, 0), earnings=("ZZTOP.ST",)), fred, makro, True)}
    assert by["datatjek"]["ok"] is None and by["regnskab"]["ok"] is None and by["regnskab"]["note"] == "mangler for 1 aktie(r)"
    assert kn.status_of(list(by.values())) == "warn"
    # fejl: EODHD-nøgle afvist, datatjek FEJL, FRED-nøgle afvist, makro uden data, runner-opstart fejlet, datatjek ikke kørt
    by = {c["id"]: c for c in rw.canary_checks(PF, _datacheck("FEJL", (1, 2, 6), eodhd_ok=False), {"key": True, "ok": False, "error": "forkert nøgle"},
                                                 {"ok": False, "errors": ["kalender nede"]}, False)}
    assert by["eodhd"]["ok"] is False and "HTTP 401" in by["eodhd"]["note"] and by["datatjek"]["ok"] is False
    assert by["fred"]["ok"] is False and by["makro"]["ok"] is False and by["runtime"]["ok"] is False
    by = {c["id"]: c for c in rw.canary_checks(PF, None, None, None, True)}
    assert by["datatjek"]["ok"] is False and "regnskab" not in by and by["fred"]["ok"] is None and "makro" not in by
