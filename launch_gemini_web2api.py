import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8081
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "gemini-web2api.log"
PID_FILE = LOG_DIR / "gemini-web2api.pid"


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8", errors="replace") as fh:
        fh.write(f"\n[{stamp}] launcher: {message}\n")


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if port_open(HOST, PORT):
        log(f"already running on http://{HOST}:{PORT}; not starting duplicate")
        return 0

    python_exe = sys.executable or "python"
    script = ROOT / "gemini_web2api.py"
    if not script.exists():
        log(f"ERROR: {script} not found")
        return 2

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")

    with LOG_FILE.open("a", encoding="utf-8", errors="replace") as log_fh:
        log_fh.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] starting gemini-web2api via {python_exe}\n")
        proc = subprocess.Popen(
            [python_exe, str(script)],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            env=env,
        )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    log(f"started pid={proc.pid}, log={LOG_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
