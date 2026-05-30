#!/usr/bin/env python3
"""Install gemini-for-agents as a system service (cross-platform).

Usage:
    python install.py                       # install + start
    python install.py --uninstall           # remove service
    python install.py --status              # check service status

Detects platform and creates:
  - Windows: Task Scheduler (hidden, at logon)
  - macOS:   launchd user agent
  - Linux:   systemd user service
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = "https://github.com/mcfax-arch/gemini-for-agents.git"
SERVICE_NAME = "gemini-for-agents"
INSTALL_DIR = Path.home() / ".gemini-for-agents"


# ── helpers ─────────────────────────────────────────────────────────────────


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, **kwargs)


def check_python() -> str:
    for candidate in ("python3", "python"):
        path = shutil.which(candidate)
        if path:
            try:
                ver = subprocess.run(
                    [path, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                    capture_output=True, text=True, timeout=10,
                )
                if ver.returncode == 0 and ver.stdout.strip() >= "3.8":
                    return path
            except Exception:
                continue
    print("❌ Python 3.8+ required. Install from https://python.org")
    sys.exit(1)


def clone_or_pull() -> None:
    if INSTALL_DIR.exists():
        print("⏫ Updating existing installation...")
        run(["git", "-C", str(INSTALL_DIR), "pull", "--ff-only"])
    else:
        print(f"📦 Cloning into {INSTALL_DIR}...")
        run(["git", "clone", "--depth=1", REPO, str(INSTALL_DIR)])


def start_server() -> bool:
    print("🚀 Starting server...")
    result = run([sys.executable, str(INSTALL_DIR / "launch.py")])
    return result.returncode == 0


def wait_for_server(timeout: int = 10) -> bool:
    import socket
    for _ in range(timeout):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", 8081)) == 0:
                return True
        time.sleep(1)
    return False


# ── Platform installers ─────────────────────────────────────────────────────


def install_windows():
    """Create a Task Scheduler entry that runs launch.py hidden at logon."""

    python = sys.executable.replace("\\", "\\\\")
    launch_script = str(INSTALL_DIR / "launch.py").replace("\\", "\\\\")
    task_name = SERVICE_NAME
    cmd = (
        f'New-ScheduledTaskAction -Execute "{python}" '
        f'-Argument "{launch_script}" -WorkingDirectory "{INSTALL_DIR}"'
    )
    trigger = "New-ScheduledTaskTrigger -AtLogOn"
    settings = (
        "New-ScheduledTaskSettingsSet "
        "-MultipleInstances IgnoreNew "
        "-ExecutionTimeLimit ([TimeSpan]::MaxValue) "
        "-AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries "
        "-Hidden"
    )
    ps = (
        f"$action = {cmd}; "
        f"$trigger = {trigger}; "
        f"$settings = {settings}; "
        f'Register-ScheduledTask -TaskName "{task_name}" '
        f"-Action $action -Trigger $trigger -Settings $settings "
        f'-Description "Gemini for Agents — Google Gemini OpenAI proxy" -Force'
    )

    code = subprocess.run(["powershell", "-NoProfile", "-Command", ps]).returncode

    if code == 0:
        print(f"✅ Task Scheduler: '{task_name}' created (starts at logon, hidden)")
    else:
        print(f"⚠️  Task Scheduler registration returned code {code}")

    return code


def install_macos():
    """Create a launchd user agent plist."""
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist = plist_dir / f"com.mcfax.{SERVICE_NAME}.plist"

    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mcfax.{SERVICE_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{INSTALL_DIR / "launch.py"}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{INSTALL_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{INSTALL_DIR / "logs" / "service.log"}</string>
    <key>StandardErrorPath</key>
    <string>{INSTALL_DIR / "logs" / "service.log"}</string>
</dict>
</plist>""")

    subprocess.run(["launchctl", "load", str(plist)])
    print(f"✅ launchd agent installed: {plist}")
    return 0


def install_linux():
    """Create a systemd user service."""
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service = service_dir / f"{SERVICE_NAME}.service"

    service.write_text(f"""[Unit]
Description=Gemini for Agents — Google Gemini OpenAI-compatible proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={sys.executable} {INSTALL_DIR / "launch.py"}
WorkingDirectory={INSTALL_DIR}
Restart=on-failure
RestartSec=5
StandardOutput=append:{INSTALL_DIR / "logs" / "service.log"}
StandardError=inherit

[Install]
WantedBy=default.target
""")

    subprocess.run(["systemctl", "--user", "daemon-reload"])
    subprocess.run(["systemctl", "--user", "enable", SERVICE_NAME])
    subprocess.run(["systemctl", "--user", "start", SERVICE_NAME])
    print(f"✅ systemd service installed: {service}")
    return 0


# ── Uninstallers ────────────────────────────────────────────────────────────


def uninstall_windows():
    task_name = SERVICE_NAME
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f'Unregister-ScheduledTask -TaskName "{task_name}" -Confirm:$false'],
    )
    print(f"🗑️  Task '{task_name}' removed")


def uninstall_macos():
    plist = Path.home() / "Library" / "LaunchAgents" / f"com.mcfax.{SERVICE_NAME}.plist"
    if plist.exists():
        subprocess.run(["launchctl", "unload", str(plist)])
        plist.unlink()
        print(f"🗑️  launchd plist removed")


def uninstall_linux():
    subprocess.run(["systemctl", "--user", "stop", SERVICE_NAME])
    subprocess.run(["systemctl", "--user", "disable", SERVICE_NAME])
    service = Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"
    if service.exists():
        service.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    print(f"🗑️  systemd service removed")


# ── main ────────────────────────────────────────────────────────────────────


def get_platform():
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def main() -> int:
    ap = argparse.ArgumentParser(description="Install gemini-for-agents system service")
    ap.add_argument("--uninstall", action="store_true", help="Remove the service")
    ap.add_argument("--status", action="store_true", help="Check service status")
    args = ap.parse_args()

    platform = get_platform()
    print(f"🖥️  Detected platform: {platform}")

    if args.uninstall:
        {"windows": uninstall_windows, "macos": uninstall_macos, "linux": uninstall_linux}[platform]()
        print("✅ Done — you may delete the install dir manually:")
        print(f"   rm -rf {INSTALL_DIR}")
        return 0

    if args.status:
        return subprocess.run([sys.executable, str(INSTALL_DIR / "launch.py"), "--status"]).returncode

    check_python()
    clone_or_pull()

    print(f"🔧 Installing {platform} service...")
    {"windows": install_windows, "macos": install_macos, "linux": install_linux}[platform]()

    start_server()

    if wait_for_server():
        print(f"""
✅ gemini-for-agents is running!
   http://127.0.0.1:8081/v1/models
   Install dir: {INSTALL_DIR}
   Logs:        {INSTALL_DIR / "logs" / "server.log"}

   The server will start automatically on next login/boot.
""")
    else:
        print("⚠️  Server may still be starting. Check logs:")
        print(f"   {INSTALL_DIR / 'logs' / 'server.log'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
