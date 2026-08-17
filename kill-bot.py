#!/usr/bin/env python3
"""Stop the Discord bot, however it was started.

There are up to four layers to take down, and killing only the inner one is why "I killed it
and it came back" happens:

    launchd agent (com.$USER.<dir>)   restarts run-bot.sh if it exits non-zero
      run-bot.sh (the guard loop)     restarts the bot within ~15s if it looks dead or hung
        uv run python src/main.py     wrapper process
          .venv/bin/python3 …main.py  the actual bot

By default this stops all of them for THIS bot directory. It matches the current layout
(`src/main.py`) and the older one (`main.py` in the repo root), and both `python` and
`python3` venv binaries. The `uv run` wrapper isn't matched directly - it exits by itself once
its child is gone.

Stdlib only, so it still works if the venv is broken:

    ./kill-bot.py                 stop the bot and its daemon (default)
    ./kill-bot.py --keep-daemon   kill only the bot; the guard restarts it (i.e. a restart)
    ./kill-bot.py --all           also hunt bot processes from other checkouts (asks first)
    ./kill-bot.py --dry-run       show what would be killed, kill nothing
    ./kill-bot.py /path/to/bot    operate on another bot directory
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

GREEN, YELLOW, RED, NC = "\033[0;32m", "\033[1;33m", "\033[0;31m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = YELLOW = RED = NC = ""

TERM_GRACE = 5.0    # seconds to wait for a clean SIGTERM exit before SIGKILL
POLL = 0.25


# === Process discovery ===

def list_processes() -> list[tuple[int, str]]:
    """Return [(pid, command_line), …] for every process we can see."""
    for flags in ("-axo", "-eo"):  # BSD/macOS first, then the Linux form
        try:
            out = subprocess.run(["ps", flags, "pid=,command="],
                                 capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode != 0:
            continue
        procs = []
        for line in out.stdout.splitlines():
            pid_str, _, command = line.strip().partition(" ")
            if pid_str.isdigit() and command:
                procs.append((int(pid_str), command.strip()))
        if procs:
            return procs
    print(f"{RED}Could not list processes (ps failed).{NC}")
    return []


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


# A bot process is `<bot_dir>/.venv/bin/python3 [src/]main.py`. Anchoring on the project root
# rather than src/ matters: the interpreter path and the script path are separate arguments,
# so `src` never sits directly before `main.py`.
_MAIN_PY = re.compile(r"main\.py(\s|$)")
_ANY_BOT = re.compile(r"\.venv/bin/python3?\s+(src/)?main\.py(\s|$)")


def find_bot_processes(bot_dir: Path) -> list[tuple[int, str]]:
    ours = str(bot_dir)
    return [(pid, cmd) for pid, cmd in list_processes()
            if ours in cmd and _MAIN_PY.search(cmd) and pid not in (os.getpid(), os.getppid())]


def find_guard_processes(bot_dir: Path) -> list[tuple[int, str]]:
    runner = str(bot_dir / "run-bot.sh")
    return [(pid, cmd) for pid, cmd in list_processes()
            if runner in cmd and pid not in (os.getpid(), os.getppid())]


def find_foreign_bots(bot_dir: Path) -> list[tuple[int, str]]:
    """Bot processes that look like this bot but live in a different directory."""
    ours = str(bot_dir)
    return [(pid, cmd) for pid, cmd in list_processes()
            if _ANY_BOT.search(cmd) and ours not in cmd and pid != os.getpid()]


# === Killing ===

def terminate(procs: list[tuple[int, str]], label: str, dry_run: bool) -> bool:
    """SIGTERM the given processes, then SIGKILL whatever ignores it. True if we killed any."""
    if not procs:
        print("  none running.")
        return False

    for pid, cmd in procs:
        print(f"  {YELLOW}{label}{NC} (pid {pid}): {cmd[:90]}")
    if dry_run:
        return False

    pids = [pid for pid, _ in procs]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # already gone between listing and killing
        except PermissionError:
            print(f"  {RED}no permission to kill pid {pid}{NC}")

    # run-bot.sh traps SIGTERM to stop its child cleanly, so give it a moment.
    deadline = time.monotonic() + TERM_GRACE
    while time.monotonic() < deadline:
        pids = [pid for pid in pids if is_alive(pid)]
        if not pids:
            return True
        time.sleep(POLL)

    print(f"  {RED}still alive after SIGTERM, sending SIGKILL:{NC} {pids}")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(1)
    return True


def stop_launchd_agent(plist_name: str, plist_path: Path, dry_run: bool) -> bool:
    """Unload the launchd agent so it stops resurrecting run-bot.sh. macOS only."""
    if not plist_path.exists():
        print(f"  no launchd agent installed ({plist_name}).")
        return False
    try:
        listing = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        print(f"  {RED}could not query launchctl.{NC}")
        return False
    if plist_name not in listing.stdout:
        print(f"  agent {plist_name} is installed but not loaded.")
        return False

    print(f"  {YELLOW}launchd agent{NC}: {plist_name}")
    if dry_run:
        return False

    # `bootout` is the modern form; fall back to the deprecated `unload` on older macOS.
    booted = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{plist_name}"],
                            capture_output=True, text=True)
    if booted.returncode != 0:
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, text=True)
    time.sleep(1)
    return True


# === Main ===

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stop the Discord bot, however it was started.",
        epilog="Without --keep-daemon this also unloads the launchd agent, "
               "otherwise the guard loop just restarts the bot.",
    )
    parser.add_argument("bot_dir", nargs="?", default=str(PROJECT_ROOT),
                        help="bot project directory (default: where this script lives)")
    parser.add_argument("--keep-daemon", "--restart", action="store_true",
                        help="kill only the bot process and let the daemon restart it")
    parser.add_argument("--all", "-a", action="store_true",
                        help="also kill bot processes from other checkouts (asks first)")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="show what would be killed, without killing anything")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="don't prompt before killing processes from other directories")
    args = parser.parse_args()

    bot_dir = Path(args.bot_dir).expanduser().resolve()
    if not bot_dir.is_dir():
        print(f"{RED}No such directory: {bot_dir}{NC}")
        return 1

    plist_name = f"com.{os.environ.get('USER', 'user')}.{bot_dir.name}"
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{plist_name}.plist"

    print(f"{GREEN}=== Killing bot ==={NC}")
    print(f"Bot directory: {bot_dir}")
    if args.dry_run:
        print(f"{YELLOW}(dry run - nothing will actually be killed){NC}")
    print()

    killed = False

    print("Daemon:")
    if args.keep_daemon:
        print("  left running (--keep-daemon) - it will restart the bot within ~15s.")
    else:
        killed |= stop_launchd_agent(plist_name, plist_path, args.dry_run)
    print()

    if not args.keep_daemon:
        print("Guard loop:")
        killed |= terminate(find_guard_processes(bot_dir), "run-bot.sh", args.dry_run)
        print()

    print("Bot process:")
    procs = find_bot_processes(bot_dir)
    # Also honour the pid file the guard writes, in case the scan somehow misses it.
    pid_file = bot_dir / "bot.pid"
    if pid_file.exists():
        try:
            file_pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            file_pid = None
        if file_pid and is_alive(file_pid) and file_pid not in [p for p, _ in procs]:
            procs.append((file_pid, f"(from bot.pid) pid {file_pid}"))
    killed |= terminate(procs, "bot", args.dry_run)
    print()

    if args.all:
        print("Other checkouts:")
        foreign = find_foreign_bots(bot_dir)
        if not foreign:
            print("  none found.")
        elif args.dry_run:
            terminate(foreign, "other bot", True)
        else:
            for pid, cmd in foreign:
                print(f"  pid {pid}: {cmd[:90]}")
            # These live outside this repo, so never kill them without a clear yes.
            if args.yes or input("  Kill these? [y/N] ").strip().lower().startswith("y"):
                killed |= terminate(foreign, "other bot", False)
            else:
                print("  skipped.")
        print()

    if args.dry_run:
        print(f"{GREEN}Dry run complete - nothing was killed.{NC}")
        return 0

    if not args.keep_daemon:
        # Stale state from a killed process: bot.pid points at nothing, and an old heartbeat
        # would make the guard call a freshly started bot "hung" on its first health tick.
        for leftover in (bot_dir / "bot.pid", bot_dir / "bot.heartbeat"):
            leftover.unlink(missing_ok=True)

    remaining = find_bot_processes(bot_dir)
    if remaining:
        print(f"{RED}Warning: bot processes still running:{NC} {[p for p, _ in remaining]}")
        return 1

    print(f"{GREEN}Bot stopped.{NC}" if killed else f"{GREEN}Nothing was running.{NC}")

    if not args.keep_daemon and plist_path.exists():
        print("\nTo start it again:")
        print(f"  launchctl load {plist_path}")
        print("  uv run python src/main.py         # or just run it in the foreground")
    return 0


if __name__ == "__main__":
    sys.exit(main())
