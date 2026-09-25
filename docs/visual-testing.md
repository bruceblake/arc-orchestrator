# Visual testing: headless screenshots and golden-image regression

Playwright drives a **real headless Chromium** so the fleet can *see* the
dashboard and catch visual regressions the DOM unit tests cannot. It is a
test-only dependency: nothing in the orchestrator imports it at runtime.

Two jobs:

1. **Screenshot capture** — render `static/index.html`, `static/usage.html`
   and `static/phone.html` at a fixed viewport and keep the PNGs.
2. **Golden-image regression** — diff a fresh capture against a committed
   golden so a CSS/layout change that breaks a page fails a gate instead of
   shipping. `check.sh`'s Node checks (`tests/undefined_calls.mjs`,
   `tests/a11y_check.mjs`) prove a page's JavaScript *runs*; only a rendered
   image proves it *looks right*.

## Install

`playwright` is listed in `requirements.txt`, so a normal venv setup gets the
Python API:

```bash
./.venv/bin/python -m pip install -r requirements.txt
```

That installs the **library only** — the browser is a separate download:

```bash
./.venv/bin/python -m playwright install chromium
```

Both steps are required. A box that ran only `pip install` has an importable
`playwright` with no browser, and `BrowserType.launch` fails with
*"Executable doesn't exist at .../chrome-headless-shell"*.

## Host prerequisites

**This box needed OS libraries that were not installed, and they were NOT
installable through the system package manager.** Chromium's binary links
against NSS, and `ldd` on the downloaded browser reported:

```
libnspr4.so     => not found
libnss3.so      => not found
libnssutil3.so  => not found
```

so the browser died at startup with
`error while loading shared libraries: libnspr4.so: cannot open shared object
file: No such file or directory`.

This host is **Arch Linux**, which Playwright does not officially support —
`playwright install chromium` says so and downloads the `ubuntu24.04-x64`
fallback build. `playwright install-deps` calls the system package manager and
**requires root**; this box has no passwordless sudo (`sudo -n true` reports
*"a password is required"*), so it could not be used, and the libs could not
be installed system-wide.

The working fix is to unpack the two Arch packages into a private prefix and
point the loader at it. **These are the exact commands that were run** (the
private prefix is `~/.local/opt/nsslibs`; `bsdtar -C <prefix>` writes the
package's own `usr/lib/` under it, which is what the loader is pointed at):

```bash
# Tarballs are kept in pkgs/, extracted into the prefix root, so the libs land
# in ~/.local/opt/nsslibs/usr/lib/ — the directory smoke.py checks.
mkdir -p ~/.local/opt/nsslibs/pkgs
cd ~/.local/opt/nsslibs/pkgs
curl -O https://geo.mirror.pkgbuild.com/core/os/x86_64/nspr-4.40-1-x86_64.pkg.tar.zst
curl -O https://geo.mirror.pkgbuild.com/core/os/x86_64/nss-3.129-1-x86_64.pkg.tar.zst
for f in *.pkg.tar.zst; do bsdtar -xf "$f" -C ~/.local/opt/nsslibs; done
# result: ~/.local/opt/nsslibs/usr/lib/{libnspr4,libnss3,libnssutil3,...}.so
```

The result of that layout, reproduced from scratch:

```
~/.local/opt/nsslibs/
├── pkgs/          nspr-4.40-1-x86_64.pkg.tar.zst, nss-3.129-1-x86_64.pkg.tar.zst
└── usr/lib/       libnspr4.so, libnss3.so, libnssutil3.so, ... (+ pkgconfig/)
```

`libnssckbi.so` lands there as a symlink to `pkcs11/p11-kit-trust.so`, which
these two packages do not ship (it comes from `p11-kit`), so that one symlink is
dangling. That is harmless for headless Chromium — verified: `ldd` reports no
missing libraries and the browser launches and screenshots normally with the
dangling link in place.

There is no `usrdir` step: extracting into the prefix root is what produces
`usr/lib/`, and that is the path both `smoke.py` and the snippet below use.

Then the loader needs that directory. `tools/visual/smoke.py` sets
`LD_LIBRARY_PATH` for it **in-process, before Chromium is spawned** — no
re-exec, no shell wrapper:

```bash
LD_LIBRARY_PATH=$HOME/.local/opt/nsslibs/usr/lib \
  ~/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell --version
# Google Chrome for Testing 153.0.8010.12
```

Verified: with that variable set, `ldd` on both the headless shell and the
full `chrome` binary reports **no** missing libraries.

On a host where NSS is installed system-wide (any supported distro with
`playwright install-deps` usable), **none of this is needed** — the
`LD_LIBRARY_PATH` injection in `smoke.py` is guarded by an `is_dir()` check
and is a no-op when the private prefix is absent.

## The check: `tools/visual/smoke.py`

```bash
./py tools/visual/smoke.py                 # screenshot to a temp file
./py tools/visual/smoke.py --out /tmp/x.png
```

Run with `./py`, not `.venv/bin/python`: a task runs inside a git **worktree**
and worktrees have no `.venv`. See `py` at the repo root.

It launches Chromium headless, sets the content `<h1>ok</h1>`, screenshots,
and requires a non-empty file whose first 8 bytes are the PNG magic. On
success it prints `browser launch OK` and exits 0; on any failure it prints the
error and exits non-zero.

**This script is the gate, and `playwright --version` is NOT a substitute.**
Measured on this box: `playwright --version` exited 0 while
`from playwright.sync_api import sync_playwright` raised `ImportError`, and it
also exits 0 on a machine where the browser cannot load `libnspr4.so` at all.
Neither is a working headless browser, which is the only thing worth gating on.

Both failure modes were confirmed to fail loudly rather than pass quietly:
with `PLAYWRIGHT_BROWSERS_PATH` pointing at an empty directory the script exits
1 (*Executable doesn't exist*), and with the NSS prefix hidden it exits 1
(*error while loading shared libraries: libnspr4.so*).

## Hard rule: screenshot PNGs are NEVER written inside a task worktree

A task's `publish` step runs **`git add -A`** in the worktree. Anything a
capture leaves behind — PNGs, videos, diff images, a stray temp file — is
therefore **committed into the PR** and becomes part of the reviewed diff.

So:

- Captures go to a path **outside** the worktree, or to a path that is
  genuinely git-ignored. `logs/` is already blanket-ignored, so a capture
  directory under it is safe; confirm with
  `git check-ignore -q logs/visual/example.png` rather than assuming.
- Never write captures to the worktree root, to `static/`, or to `tests/`.
- A capture tool must take an explicit output directory and refuse to write
  under the worktrees root (`~/worktrees`) unless forced.
- Goldens are the exception — those are **committed on purpose**, under
  `tests/`, because they are the baseline the diff is measured against.

If a screenshot does land in a commit, treat it as a bug in the capture tool,
not as something to delete by hand in the branch.

## Not yet wired

This prerequisite only makes the tool available, proven and documented.
Nothing in the pipeline calls Playwright yet — the capture/diff helper, the PR
screenshot surfacing, and the reviewer instructions are separate tasks.
