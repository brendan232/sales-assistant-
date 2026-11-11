"""Install the background schedule.

On macOS this is a launchd agent that runs `run-jobs` every 15 minutes while
you are logged in (and catches up after the Mac wakes). On other systems it
prints the cron line to add.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import ROOT

LABEL = "com.sales-assistant.jobs"
INTERVAL_SECONDS = 15 * 60
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = ROOT / "var" / "jobs.log"
# macOS blocks background programs from these folders unless you grant access by hand.
PROTECTED = ("Downloads", "Desktop", "Documents")


def plist_bytes(root: Path = ROOT, python: str = sys.executable) -> bytes:
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [python, "-m", "sales_assistant", "run-jobs"],
            "WorkingDirectory": str(root),
            "StartInterval": INTERVAL_SECONDS,
            "RunAtLoad": True,
            "StandardOutPath": str(root / "var" / "jobs.log"),
            "StandardErrorPath": str(root / "var" / "jobs.log"),
            "ProcessType": "Background",
        }
    )


def protected_folder(root: Path = ROOT) -> str | None:
    try:
        first = root.resolve().relative_to(Path.home().resolve()).parts[0]
    except (ValueError, IndexError):
        return None
    return first if first in PROTECTED else None


def cron_line(root: Path = ROOT, python: str = sys.executable) -> str:
    return f"*/15 * * * * cd {root} && {python} -m sales_assistant run-jobs >> {root / 'var' / 'jobs.log'} 2>&1"


def install() -> int:
    (ROOT / "var").mkdir(exist_ok=True)
    if sys.platform != "darwin":
        print("Automatic install is only supported on macOS. Add this line to your crontab (crontab -e):\n")
        print(cron_line())
        return 0
    folder = protected_folder()
    if folder:
        print(
            f"This project is inside your {folder} folder. macOS does not let background programs read files there,\n"
            "so the schedule would fail silently. Move the project folder to your home folder first, for example:\n\n"
            f"  mv \"{ROOT}\" ~/sales-assistant\n\n"
            "then recreate the environment there (the install step in the README) and run this command again."
        )
        return 1
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(PLIST)], capture_output=True)  # replace any old copy
    PLIST.write_bytes(plist_bytes())
    result = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Could not start the schedule: {result.stderr.strip() or result.stdout.strip()}")
        return 1
    print(f"Scheduled. It checks every 15 minutes while you are logged in.\nActivity log: {LOG}")
    return 0


def remove() -> int:
    if sys.platform != "darwin":
        print("Remove the sales_assistant line from your crontab (crontab -e).")
        return 0
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True)
    PLIST.unlink(missing_ok=True)
    print("Schedule removed.")
    return 0


def status() -> int:
    if sys.platform == "darwin":
        loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True).returncode == 0
        print("Schedule is running." if loaded else "Schedule is not installed.")
    else:
        print("On this system the schedule is a crontab entry; check with: crontab -l")
    if LOG.exists():
        print(f"\nLast activity ({LOG}):")
        print("\n".join(LOG.read_text().splitlines()[-12:]))
    return 0
