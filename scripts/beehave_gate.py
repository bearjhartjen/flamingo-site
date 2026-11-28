#!/usr/bin/env python3
"""Gate for beehest on this repository.

Runs the same two checks as the Verify workflow, then writes a JUnit XML
report so the engine records a real test result. A gate that exits 0 with no
report fails, so the report is written even when a check fails.

Nothing here writes into the repository: the build's --check mode verifies a
copy in a temp folder, and Python's bytecode cache is redirected to /tmp.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "gate.xml"

COMPILE_TARGETS = [str(p) for p in sorted((ROOT / "scripts").glob("*.py"))] + [
    str(ROOT / "edit.py")
]

CHECKS = [
    (
        "compile",
        [sys.executable, "-m", "py_compile", *COMPILE_TARGETS],
        "python3 -m py_compile scripts/*.py edit.py",
    ),
    (
        "site",
        [sys.executable, str(ROOT / "scripts" / "build.py"), "--check"],
        "python3 scripts/build.py --check",
    ),
]


def run(command: list[str], cwd: Path, env: dict[str, str]) -> tuple[bool, str, int, float]:
    started = time.monotonic()
    try:
        done = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        output, code = done.stdout, done.returncode
    except OSError as error:
        output, code = f"cannot start {command[0]}: {error}", 127

    return code == 0, output, code, time.monotonic() - started


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="beehest-gate-") as cache:
        env = dict(os.environ, PYTHONPYCACHEPREFIX=cache)
        results = [(name, *run(command, ROOT, env)) for name, command, _ in CHECKS]

    failed = [result for result in results if not result[1]]

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<testsuite name="beehest-gate" tests="%d" failures="%d" errors="0" time="%.3f">'
        % (len(results), len(failed), sum(seconds for *_, seconds in results)),
    ]
    for name, passed, output, code, seconds in results:
        lines.append('  <testcase classname="gate" name="%s" time="%.3f">' % (escape(name), seconds))
        if not passed:
            command = next(text for check, _, text in CHECKS if check == name)
            lines.append(
                '    <failure message="%s exited %d">%s</failure>'
                % (escape(command), code, escape(output[-4000:]))
            )
        lines.append("  </testcase>")
    lines.append("</testsuite>")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    for name, passed, output, code, seconds in results:
        print("%s %s (%.2fs)" % ("PASS" if passed else "FAIL", name, seconds))
        if not passed:
            print(output.rstrip())

    print("report: %s" % REPORT)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
