#!/usr/bin/env python3
"""Kimi Code session watchdog — resume interrupted CLI sessions (no web/tunnel).

A session is interrupted when it was cut off in the middle of a turn:
  - the most recent wire event is `turn.started` / `agent.turn.started`
    with NO subsequent `turn.ended` / `agent.turn.ended` / `prompt.completed`.
Completed turns are never resumed. `lastTurnReason` in state.json is also
checked as a cheap fast path (only completed is ignored).
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

KIMI = os.environ.get("KIMI") or shutil.which("kimi") or str(Path.home() / ".local/bin/kimi")
KIMI_HOME = Path(os.environ.get("KIMI_CODE_HOME", os.path.expanduser("~/.kimi-code")))
SESSIONS = Path(os.environ.get("KIMI_SESSIONS_DIR", str(KIMI_HOME / "sessions")))
LOG_DIR = Path(os.environ.get("KIMI_WATCHDOG_STATE", str(Path.home() / ".local/state/kimi-code-watchdog")))
ENV = {**os.environ, "PATH": os.pathsep.join(
    [str(Path(KIMI).parent), "/usr/local/bin", "/usr/bin", "/bin", os.environ.get("PATH", "")])}


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _read_state(session_dir):
    try:
        return json.loads((session_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _open_turn(session_dir):
    """True if any wire log has a turn.started with no matching turn.ended."""
    for wire in sorted(session_dir.glob("agents/*/wire.jsonl")):
        if not wire.is_file():
            continue
        try:
            with open(wire, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    kind = ev.get("type")
                    if kind in ("turn.started", "agent.turn.started"):
                        return True
                    if kind in ("turn.ended", "agent.turn.ended", "prompt.completed"):
                        return False
        except OSError:
            continue
    return False


def interrupted(session_dir):
    state = _read_state(session_dir)
    if state is None or state.get("archived"):
        return False
    if state.get("lastTurnReason") == "completed":
        return False
    return _open_turn(session_dir)


def resume(sid, cwd, dry):
    cmd = [KIMI, "--session", sid, "--prompt",
           "Continue exactly where you left off, using the recorded session state."]
    if not os.path.isdir(cwd):
        log(f"skip {sid}: missing cwd {cwd!r}")
        return
    if dry:
        log(f"DRY-RUN resume {sid} (cwd={cwd})")
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "resume.log", "a", encoding="utf-8") as out:
        subprocess.Popen(cmd, cwd=cwd, env=ENV, start_new_session=True,
                         stdout=out, stderr=subprocess.STDOUT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    for state_path in sorted(SESSIONS.glob("wd_*/session_*/state.json")):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        sid = state_path.parent.name
        if state.get("archived"):
            continue
        if interrupted(state_path.parent):
            resume(sid, state.get("cwd") or str(KIMI_HOME), args.dry_run)


if __name__ == "__main__":
    main()
