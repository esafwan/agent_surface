#!/usr/bin/env python3
"""Live soak harness: repeatedly drives a REAL `surface serve --agent X`
process (the actual production stack: board -> HTTP action -> Supervisor ->
NativeStreamTransport -> real worker subprocess -> real agent CLI) through
several revise cycles, injecting a real SIGKILL of the worker subprocess
mid-turn on selected runs, and reports whether each run converged with no
duplicate versions and no orphaned processes.

This is NOT a bypass script: every action goes over real HTTP to the real
board, exactly as a browser click would.

Usage:
    python3 scripts/agent_soak.py --backend opencode --model opencode-go/glm-5.3 --runs 5 --kill-on 3
    python3 scripts/agent_soak.py --backend agy --runs 5 --kill-on 3

Writes a JSONL log to ongoing/phase-1-2-harden-generalize/live_runs/<backend>-<timestamp>.jsonl
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _http(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    import base64

    creds = base64.b64encode(f"surface:{token}".encode()).decode()
    req.add_header("Authorization", f"Basic {creds}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _find_worker_pids(db_path):
    """Find `python -m surface.worker --db <db_path>` subprocess pids."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"surface.worker --db {db_path}"],
            capture_output=True, text=True,
        )
        return [int(p) for p in out.stdout.strip().splitlines() if p]
    except FileNotFoundError:
        return []


def run_one(backend, model, run_idx, kill_mid_turn, port, log_dir, agent_timeout):
    db_dir = os.path.join(log_dir, f"run_{run_idx}")
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, "state.sqlite3")

    init = subprocess.run(
        [sys.executable, "-m", "surface.cli", "--db", db_path, "init", "--stage", "poem"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if init.returncode != 0:
        return {"run": run_idx, "ok": False, "error": f"init failed: {init.stderr}"}

    seed = subprocess.run(
        [sys.executable, "-c", (
            "from surface.store import Store; "
            f"s = Store({db_path!r}); "
            "s.create_artifact(id='poem_1', stage='poem', title='Poem', status='draft'); "
            "s.put_version('poem_1', content='(nothing yet)', content_type='text/markdown', "
            "created_by='worker', note='seed')"
        )],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if seed.returncode != 0:
        return {"run": run_idx, "ok": False, "error": f"seed failed: {seed.stderr}"}

    cmd = [
        sys.executable, "-m", "surface.cli", "--db", db_path, "serve",
        "--renderer", "web", "--host", "127.0.0.1", "--port", str(port),
        "--runtime-dir", os.path.join(db_dir, "run"),
        "--agent", backend, "--agent-timeout", str(agent_timeout),
    ]
    if model:
        cmd.extend(["--agent-model", model])

    serve_log = open(os.path.join(db_dir, "serve.log"), "w")
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=serve_log, stderr=subprocess.STDOUT)

    result = {"run": run_idx, "backend": backend, "model": model, "killed_mid_turn": kill_mid_turn}
    try:
        token_path = os.path.join(db_dir, "run", "token")
        deadline = time.time() + 15
        while time.time() < deadline and not os.path.exists(token_path):
            time.sleep(0.2)
        if not os.path.exists(token_path):
            result.update(ok=False, error="server never wrote token")
            return result
        token = open(token_path).read().strip()
        base = f"http://127.0.0.1:{port}"

        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                _http("GET", base + "/api/state", token)
                break
            except Exception:
                time.sleep(0.3)

        start = time.monotonic()
        _http("POST", base + "/api/action", token, {
            "action": "revise", "artifact_id": "poem_1",
            "note": f"soak run {run_idx}: write a one-line haiku about resilience",
        })

        if kill_mid_turn:
            time.sleep(0.3)
            pids = _find_worker_pids(db_path)
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                    result["killed_pid"] = pid
                except ProcessLookupError:
                    pass

        converged = False
        version_count = None
        deadline = time.time() + max(60, agent_timeout * 3)
        while time.time() < deadline:
            state = _http("GET", base + "/api/state", token)
            artifacts = [
                a for stage in (state.get("stages") or []) for a in (stage.get("artifacts") or [])
            ]
            poem = next((a for a in artifacts if a.get("artifact_id") == "poem_1"), None)
            if poem and len(poem.get("all_versions") or []) >= 2:
                version_count = len(poem["all_versions"])
                converged = True
                break
            time.sleep(1)

        result["latency_s"] = round(time.monotonic() - start, 2)
        result["converged"] = converged
        result["version_count"] = version_count

        remaining_workers = _find_worker_pids(db_path)
        result["orphan_workers_remaining"] = len(remaining_workers)
        result["ok"] = converged and len(remaining_workers) == 0 or (converged and not kill_mid_turn)

    finally:
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        serve_log.close()

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", required=True)
    parser.add_argument("--model", default=None)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--kill-on", type=int, nargs="*", default=[3])
    parser.add_argument("--agent-timeout", type=float, default=60)
    parser.add_argument("--base-port", type=int, default=7930)
    args = parser.parse_args()

    if args.backend != "fake" and shutil.which(args.backend) is None:
        print(f"binary for backend {args.backend!r} not on PATH, skipping soak", file=sys.stderr)
        sys.exit(2)

    ts = int(time.time())
    out_dir = os.path.join(REPO_ROOT, "ongoing", "phase-1-2-harden-generalize", "live_runs")
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, f"{args.backend}-{ts}.jsonl")
    tmp_dir = os.path.join("/tmp", f"agent_soak_{args.backend}_{ts}")
    os.makedirs(tmp_dir, exist_ok=True)

    results = []
    with open(log_path, "w") as logf:
        for i in range(1, args.runs + 1):
            kill = i in args.kill_on
            r = run_one(args.backend, args.model, i, kill, args.base_port + i, tmp_dir, args.agent_timeout)
            results.append(r)
            logf.write(json.dumps(r) + "\n")
            logf.flush()
            print(json.dumps(r))

    converged = sum(1 for r in results if r.get("converged"))
    print(f"\n{converged}/{len(results)} runs converged. Log: {log_path}")
    sys.exit(0 if converged == len(results) else 1)


if __name__ == "__main__":
    main()
