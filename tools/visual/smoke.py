#!/usr/bin/env python3
"""Prove that headless chromium ACTUALLY LAUNCHES, not merely that it imports.

`playwright --version` is not a gate. On this fleet's box it exited 0 while
`from playwright.sync_api import sync_playwright` raised ImportError, and it
also exits 0 on a machine where the browser binary cannot load its shared
libraries at all (measured: chromium exits immediately with
"error while loading shared libraries: libnspr4.so"). Neither is a working
headless browser, so this script opens a real page, writes a real PNG and
checks the bytes are there.

    ./py tools/visual/smoke.py            # screenshot to a temp file
    ./py tools/visual/smoke.py --out /tmp/x.png

Exit 0 and print "browser launch OK" only when a non-empty PNG exists on disk;
otherwise print the error and exit non-zero.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# Chromium's binary needs NSS libraries that are not installed system-wide on
# this fleet's box (Arch, no passwordless sudo). `playwright install-deps`
# cannot be used, so a prior setup step unpacks the nspr/nss packages under
# ~/.local/opt/nsslibs/usr/lib — see docs/visual-testing.md § Host
# prerequisites. The dynamic loader reads LD_LIBRARY_PATH from the ENVIRONMENT
# the child process is forked with, so setting it here (before chromium is
# spawned) is enough; no re-exec is needed. This is a no-op wherever the
# libraries are already present.
_NSS_LIB_DIR = Path.home() / ".local" / "opt" / "nsslibs" / "usr" / "lib"
if _NSS_LIB_DIR.is_dir():
    _existing = os.environ.get("LD_LIBRARY_PATH", "")
    if str(_NSS_LIB_DIR) not in _existing.split(os.pathsep):
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(
            p for p in (str(_NSS_LIB_DIR), _existing) if p
        )

from playwright.sync_api import sync_playwright  # noqa: E402  (needs the env above)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        default=None,
        help="where to write the screenshot PNG (default: a temp file)",
    )
    args = ap.parse_args()

    tmp_created = None
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
    else:
        fd, name = tempfile.mkstemp(suffix=".png", prefix="visual-smoke-")
        os.close(fd)
        out, tmp_created = Path(name), Path(name)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content("<h1>ok</h1>")
                page.screenshot(path=str(out))
            finally:
                browser.close()

        if not out.is_file():
            print(f"FAIL: screenshot was not written to {out}", file=sys.stderr)
            return 1
        size = out.stat().st_size
        if size <= 0:
            print(f"FAIL: screenshot {out} is empty (0 bytes)", file=sys.stderr)
            return 1
        # A PNG always starts with this magic; an HTML error page saved as .png
        # would otherwise pass the size check.
        with out.open("rb") as fh:
            if fh.read(8) != b"\x89PNG\r\n\x1a\n":
                print(f"FAIL: {out} is not a PNG", file=sys.stderr)
                return 1
    except Exception as exc:  # noqa: BLE001 — every failure must exit non-zero
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if tmp_created is not None and tmp_created.exists():
            try:
                tmp_created.unlink()
            except OSError:
                pass

    print(f"browser launch OK ({size} bytes -> {out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
