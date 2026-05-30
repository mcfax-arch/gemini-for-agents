#!/usr/bin/env python3
"""Launch gemini-for-agents as a background daemon (cross-platform).

Usage:
    python launch.py              # start (or restart) the daemon
    python launch.py --stop       # stop the daemon
    python launch.py --status     # check if running

Works on Windows, macOS, Linux — no external dependencies.
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "server.log"
PID_FILE = LOG_DIR / "server.pid"
HOST = "127.0.0.1"
PORT = 8081


def is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def is_pid_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                stderr=subprocess.DEVNULL,
            ).decode("mbcs", errors="replace")
            return str(pid) in out and "INFO:" not in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False


def stop_daemon() -> bool:
    pid = read_pid()
    if pid and is_pid_alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            if not is_pid_alive(pid):
                break
            time.sleep(0.3)
        if PID_FILE.exists():
            PID_FILE.unlink()
        print(f"🛑 Stopped (pid={pid})")
        return True
    print("ℹ️  Not running")
    return False


def status() -> int:
    pid = read_pid()
    if pid and is_pid_alive(pid):
        print(f"✅ Running (pid={pid}) — http://{HOST}:{PORT}")
        return 0
    print("❌ Not running")
    return 1


def start_daemon() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if is_port_open(HOST, PORT):
        print(f"✅ Already running on http://{HOST}:{PORT}")
        return 0

    python = sys.executable or "python3"
    script = ROOT / "gemini_web2api.py"

    if not script.exists():
        print(f"❌ {script} not found")
        return 2

    log_fh = open(LOG_FILE, "a", encoding="utf-8", errors="replace")

    if os.name == "nt":
        # Windows: DETACHED_PROCESS — no console window
        proc = subprocess.Popen(
            [python, str(script)],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )
    else:
        # Unix: double-fork daemon
        pid = os.fork()
        if pid > 0:
            # Parent — wait briefly, then exit
            time.sleep(1)
            if is_port_open(HOST, PORT):
                print(f"🚀 Started (pid={pid})")
                return 0
            else:
                print("❌ Daemon failed to start — check logs")
                return 1
        os.setsid()
        pid2 = os.fork()
        if pid2 > 0:
            sys.exit(0)
        # Daemon process
        proc = subprocess.Popen(
            [python, str(script)],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
        pid = proc.pid

    PID_FILE.write_text(str(proc.pid), encoding="utf-8")

    # Wait and verify
    for _ in range(10):
        if is_port_open(HOST, PORT):
            print(f"🚀 Started (pid={proc.pid}) — http://{HOST}:{PORT}")
            print(f"   Logs: {LOG_FILE}")
            return 0
        time.sleep(0.5)

    print("❌ Server started but not responding — check logs")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Launch gemini-for-agents daemon")
    ap.add_argument("--stop", action="store_true", help="Stop the daemon")
    ap.add_argument("--status", action="store_true", help="Check daemon status")
    ap.add_argument("--restart", action="store_true", help="Restart the daemon")
    args = ap.parse_args()

    if args.stop:
        return 0 if stop_daemon() else 1
    if args.status:
        return status()
    if args.restart:
        stop_daemon()
        time.sleep(0.5)
        return start_daemon()

    return start_daemon()


if __name__ == "__main__":
    sys.exit(main())
