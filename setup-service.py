#!/usr/bin/env python3
"""
Setup claude-proxy as an auto-start service on Linux, macOS, or Windows.

Usage:
    python setup-service.py install [--port PORT]
    python setup-service.py uninstall
    python setup-service.py status
"""

import argparse
import os
import platform
import subprocess
import sys
import textwrap
from pathlib import Path

SERVICE_NAME = "claude-proxy"
LAUNCHD_LABEL = "com.claude-proxy"
SCHTASKS_NAME = "ClaudeProxy"
DEFAULT_PORT = 8082
REPO_URL = "https://github.com/oliverox/claude-proxy.git"


def detect_python() -> str:
    return sys.executable


def detect_proxy_script() -> Path:
    script = Path(__file__).resolve().parent / "claude-proxy.py"
    if not script.exists():
        print(f"Error: {script} not found.", file=sys.stderr)
        sys.exit(1)
    return script


def run_cmd(args: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a command, printing stderr on failure."""
    result = subprocess.run(args, capture_output=capture, text=True)
    if check and result.returncode != 0:
        stderr = result.stderr if capture else ""
        print(f"Command failed: {' '.join(args)}", file=sys.stderr)
        if stderr:
            print(stderr.strip(), file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
# Linux: systemd user service
# ---------------------------------------------------------------------------

def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"


def _systemd_install(python_path: str, script_path: Path, port: int):
    unit_dir = _systemd_unit_path().parent
    unit_dir.mkdir(parents=True, exist_ok=True)

    unit = textwrap.dedent(f"""\
        [Unit]
        Description=Claude CLI Proxy Server
        After=network.target

        [Service]
        Type=simple
        ExecStart="{python_path}" "{script_path}" {port}
        Restart=on-failure
        RestartSec=5
        Environment="PATH={os.environ.get("PATH", "")}"
        Environment="HOME={Path.home()}"

        [Install]
        WantedBy=default.target
    """)

    _systemd_unit_path().write_text(unit)
    print(f"Wrote {_systemd_unit_path()}")

    run_cmd(["systemctl", "--user", "daemon-reload"])
    run_cmd(["systemctl", "--user", "enable", f"{SERVICE_NAME}.service"])
    run_cmd(["systemctl", "--user", "start", f"{SERVICE_NAME}.service"])
    print(f"Service {SERVICE_NAME} installed and started on port {port}.")

    # Check linger status
    result = run_cmd(["loginctl", "show-user", os.environ.get("USER", ""), "--property=Linger"],
                     check=False, capture=True)
    if result.returncode == 0 and "Linger=no" in (result.stdout or ""):
        print()
        print("Warning: User linger is not enabled. The service will only run while you")
        print("are logged in. To keep it running after logout, run:")
        print(f"  sudo loginctl enable-linger {os.environ.get('USER', '$USER')}")


def _systemd_restart():
    unit = _systemd_unit_path()
    if not unit.exists():
        print(f"Service {SERVICE_NAME} is not installed.")
        return
    run_cmd(["systemctl", "--user", "restart", f"{SERVICE_NAME}.service"])
    print(f"Service {SERVICE_NAME} restarted.")


def _systemd_uninstall():
    unit = _systemd_unit_path()
    if not unit.exists():
        print(f"Service {SERVICE_NAME} is not installed.")
        return

    run_cmd(["systemctl", "--user", "stop", f"{SERVICE_NAME}.service"], check=False)
    run_cmd(["systemctl", "--user", "disable", f"{SERVICE_NAME}.service"], check=False)
    unit.unlink()
    run_cmd(["systemctl", "--user", "daemon-reload"])
    print(f"Service {SERVICE_NAME} uninstalled.")


def _systemd_status():
    unit = _systemd_unit_path()
    if not unit.exists():
        print(f"Service {SERVICE_NAME} is not installed.")
        return

    result = run_cmd(["systemctl", "--user", "status", f"{SERVICE_NAME}.service"],
                     check=False, capture=True)
    print(result.stdout or "No status output.")
    if result.stderr:
        print(result.stderr.strip())


# ---------------------------------------------------------------------------
# macOS: launchd LaunchAgent
# ---------------------------------------------------------------------------

def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _launchd_log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / SERVICE_NAME


def _launchd_install(python_path: str, script_path: Path, port: int):
    log_dir = _launchd_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    plist = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{LAUNCHD_LABEL}</string>
            <key>ProgramArguments</key>
            <array>
                <string>{python_path}</string>
                <string>{script_path}</string>
                <string>{port}</string>
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>StandardOutPath</key>
            <string>{log_dir}/stdout.log</string>
            <key>StandardErrorPath</key>
            <string>{log_dir}/stderr.log</string>
            <key>EnvironmentVariables</key>
            <dict>
                <key>PATH</key>
                <string>{os.environ.get("PATH", "")}</string>
            </dict>
        </dict>
        </plist>
    """)

    plist_path = _launchd_plist_path()

    # Unload existing if present
    if plist_path.exists():
        run_cmd(["launchctl", "unload", str(plist_path)], check=False)

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist)
    print(f"Wrote {plist_path}")

    run_cmd(["launchctl", "load", "-w", str(plist_path)])
    print(f"Service {SERVICE_NAME} installed and started on port {port}.")
    print(f"Logs: {log_dir}/")


def _launchd_restart():
    plist_path = _launchd_plist_path()
    if not plist_path.exists():
        print(f"Service {SERVICE_NAME} is not installed.")
        return
    run_cmd(["launchctl", "unload", str(plist_path)], check=False)
    run_cmd(["launchctl", "load", "-w", str(plist_path)])
    print(f"Service {SERVICE_NAME} restarted.")


def _launchd_uninstall():
    plist_path = _launchd_plist_path()
    if not plist_path.exists():
        print(f"Service {SERVICE_NAME} is not installed.")
        return

    run_cmd(["launchctl", "unload", str(plist_path)], check=False)
    plist_path.unlink()
    print(f"Service {SERVICE_NAME} uninstalled.")


def _launchd_status():
    plist_path = _launchd_plist_path()
    if not plist_path.exists():
        print(f"Service {SERVICE_NAME} is not installed.")
        return

    result = run_cmd(["launchctl", "list"], check=False, capture=True)
    for line in (result.stdout or "").splitlines():
        if LAUNCHD_LABEL in line:
            print(line)
            return
    print(f"Service {SERVICE_NAME} is installed but not currently running.")


# ---------------------------------------------------------------------------
# Windows: Scheduled Task
# ---------------------------------------------------------------------------

def _windows_log_path() -> Path:
    return Path.home() / ".claude-proxy.log"


def _windows_install(python_path: str, script_path: Path, port: int):
    log_path = _windows_log_path()

    # schtasks /TR needs a single command string
    tr = f'cmd /c ""{python_path}" "{script_path}" {port} >> "{log_path}" 2>&1"'

    run_cmd([
        "schtasks", "/Create",
        "/TN", SCHTASKS_NAME,
        "/TR", tr,
        "/SC", "ONLOGON",
        "/RL", "LIMITED",
        "/F",
    ])

    # Start immediately
    run_cmd(["schtasks", "/Run", "/TN", SCHTASKS_NAME], check=False)
    print(f"Task {SCHTASKS_NAME} installed and started on port {port}.")
    print(f"Log: {log_path}")


def _windows_restart():
    run_cmd(["schtasks", "/End", "/TN", SCHTASKS_NAME], check=False)
    run_cmd(["schtasks", "/Run", "/TN", SCHTASKS_NAME], check=False)
    print(f"Task {SCHTASKS_NAME} restarted.")


def _windows_uninstall():
    run_cmd(["schtasks", "/End", "/TN", SCHTASKS_NAME], check=False)
    result = run_cmd(["schtasks", "/Delete", "/TN", SCHTASKS_NAME, "/F"], check=False, capture=True)
    if result.returncode == 0:
        print(f"Task {SCHTASKS_NAME} uninstalled.")
    else:
        print(f"Task {SCHTASKS_NAME} is not installed or already removed.")


def _windows_status():
    result = run_cmd(["schtasks", "/Query", "/TN", SCHTASKS_NAME, "/V", "/FO", "LIST"],
                     check=False, capture=True)
    if result.returncode == 0:
        print(result.stdout)
    else:
        print(f"Task {SCHTASKS_NAME} is not installed.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

PLATFORMS = {
    "linux": {
        "install": _systemd_install,
        "uninstall": _systemd_uninstall,
        "restart": _systemd_restart,
        "status": _systemd_status,
    },
    "darwin": {
        "install": _launchd_install,
        "uninstall": _launchd_uninstall,
        "restart": _launchd_restart,
        "status": _launchd_status,
    },
    "windows": {
        "install": _windows_install,
        "uninstall": _windows_uninstall,
        "restart": _windows_restart,
        "status": _windows_status,
    },
}


def _update(handlers):
    """Pull latest changes from git and restart the service if running."""
    repo_dir = Path(__file__).resolve().parent

    # Check if this is a git repo
    if not (repo_dir / ".git").exists():
        print(f"Error: {repo_dir} is not a git repository.", file=sys.stderr)
        print(f"Clone it first: git clone {REPO_URL}", file=sys.stderr)
        sys.exit(1)

    print(f"Updating claude-proxy in {repo_dir}...")
    result = run_cmd(["git", "-C", str(repo_dir), "pull", "--ff-only"], capture=True)
    if result.returncode != 0:
        print("Update failed. You may have local changes — try: git pull --rebase", file=sys.stderr)
        sys.exit(1)

    output = (result.stdout or "").strip()
    print(output)

    if "Already up to date" in output:
        return

    # Restart the service if installed
    print()
    print("Restarting service...")
    handlers["restart"]()


def _help():
    """Print detailed help for all commands."""
    print("setup-service.py — Manage claude-proxy as an auto-start service")
    print()
    print("Usage: python setup-service.py <command> [options]")
    print()
    print("Commands:")
    print("  install     Install and start claude-proxy as a system service")
    print("  uninstall   Stop and remove the service")
    print("  restart     Restart the running service")
    print("  update      Pull latest changes from git and restart")
    print("  status      Show current service status")
    print("  help        Show this help message")
    print()
    print("Options:")
    print(f"  --port PORT  Port for claude-proxy (default: {DEFAULT_PORT}, install only)")
    print()
    print("Platform support:")
    print("  Linux    systemd user service (~/.config/systemd/user/claude-proxy.service)")
    print("  macOS    launchd LaunchAgent  (~/Library/LaunchAgents/com.claude-proxy.plist)")
    print("  Windows  Scheduled Task       (Task Scheduler: ClaudeProxy)")
    print()
    print("Examples:")
    print("  python setup-service.py install             # Install on default port 8082")
    print("  python setup-service.py install --port 9000 # Install on custom port")
    print("  python setup-service.py update              # Pull latest and restart")
    print("  python setup-service.py status              # Check if running")


def main():
    # Handle help before argparse so "help" doesn't need to be in choices
    if len(sys.argv) > 1 and sys.argv[1] == "help":
        _help()
        return

    parser = argparse.ArgumentParser(
        description="Manage claude-proxy as an auto-start service.",
    )
    parser.add_argument("action", choices=["install", "uninstall", "restart", "update", "status"],
                        help="Action to perform")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port for claude-proxy (default: {DEFAULT_PORT}, install only)")
    args = parser.parse_args()

    system = platform.system().lower()
    if system == "windows" or system.startswith("win"):
        system = "windows"

    if system not in PLATFORMS:
        print(f"Unsupported platform: {platform.system()}", file=sys.stderr)
        sys.exit(1)

    handlers = PLATFORMS[system]

    if args.action == "install":
        python_path = detect_python()
        script_path = detect_proxy_script()
        print(f"Platform:  {platform.system()}")
        print(f"Python:    {python_path}")
        print(f"Script:    {script_path}")
        print(f"Port:      {args.port}")
        print()
        handlers["install"](python_path, script_path, args.port)
    elif args.action == "uninstall":
        handlers["uninstall"]()
    elif args.action == "restart":
        handlers["restart"]()
    elif args.action == "update":
        _update(handlers)
    elif args.action == "status":
        handlers["status"]()


if __name__ == "__main__":
    main()
