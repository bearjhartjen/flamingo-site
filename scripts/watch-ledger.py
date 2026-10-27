#!/usr/bin/env python3
"""
Watch the Desktop ledger and keep the repository in step.

The ledger lives at ~/Desktop/The Flank.md and is the source of truth. It sits
outside the git repository, so editing it on its own leaves the repository
looking clean -- and a git GUI (GitKraken) will not offer to commit anything
when there is nothing staged. This watcher closes that gap: it notices the
change, copies the ledger into the repository, rebuilds the site, and stages
both. By the time the owner opens GitKraken, the change is waiting to be
committed like any other edit.

It never commits and never pushes. It only stages.

Usage:
    python3 scripts/watch-ledger.py            # run in the foreground
    systemctl --user start flamingo-ledger-watcher
"""

import hashlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = Path.home() / "Desktop" / "The Flank.md"
LEDGER = ROOT / "data" / "The Flank.md"
ENCRYPT = ROOT / "scripts" / "encrypt_flank.py"
BUILD = ROOT / "scripts" / "build.py"
SITE = ROOT / "site"

POLL_SECONDS = 2.0


def log(message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def digest(path: Path) -> str | None:
    """sha256 of a file, or None if it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def sync_and_build() -> bool:
    """Encrypt the ledger in, rebuild the site, stage both. True on success."""
    if not SOURCE.exists():
        log(f"waiting for {SOURCE}")
        return False

    # The Desktop file is the only plaintext copy of the ledger. The sync
    # replaces each private note with its encrypted token, so the repo copy
    # never holds private note text. A failed encryption is a hard stop --
    # never a plaintext fallback.
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    code, out = run([sys.executable, str(ENCRYPT)])
    if code != 0:
        log("ledger encryption FAILED (repo copy left untouched):")
        for line in out.splitlines():
            log(f"  {line}")
        return False
    log("ledger synced with private notes encrypted")

    code, out = run([sys.executable, str(BUILD)])
    if code != 0:
        log("site build FAILED:")
        for line in out.splitlines():
            log(f"  {line}")
        return False
    log("site rebuilt")

    # Stage the ledger and the generated site. `git add` on a path with a
    # space is fine here because each path is passed as its own argument.
    code, out = run(["git", "add", "--", str(LEDGER.relative_to(ROOT)), str(SITE.relative_to(ROOT))])
    if code != 0:
        log(f"could not stage changes: {out}")
        return False
    log("changes staged - the ledger edit is ready to commit")
    return True


def main() -> int:
    if not SOURCE.exists():
        log(f"{SOURCE} does not exist yet; will keep watching for it")

    log(f"watching {SOURCE}")
    last = digest(SOURCE)
    if last is not None:
        log("starting from the current contents")

    try:
        while True:
            time.sleep(POLL_SECONDS)
            current = digest(SOURCE)
            if current is None or current == last:
                continue
            log("ledger changed on the Desktop")
            sync_and_build()
            last = current
    except KeyboardInterrupt:
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
