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

`playwright` is pinned in `requirements.txt` (`playwright==1.63.0`), so a
normal venv setup gets the Python API. The pin is exact on purpose: each
Playwright release bundles its own Chromium build, and a different build
renders glyph edges differently, so **upgrading Playwright means re-blessing
every golden** (`tools/visual/run.sh --update`) in the same change, after
looking at the panels.

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

## The capture: `tools/visual/`

| File | Job |
|---|---|
| `fixture.py` | A fixed world: two taskfiles, task rows in every status the UI colours differently, harness runs and an event log, every timestamp pinned before `FROZEN_NOW` (2026-09-20 12:00 UTC). Never touches `orchestrator.db`. |
| `serve.py` | Serves ONE checkout's dashboard over that fixture on a free port: every config path under the tree is rewritten into the fixture dir, `HOME` points into it, gh tokens are dropped, `/proc` "live runs" are reported empty, and `time.time()` is pinned to `FROZEN_NOW`. It builds `dashboard.Handler` directly — `main.py serve` would also arm the daily audit, the captain drain and the studio autopilot. |
| `capture.py` | Every view — `index`, `projects` (the Projects tab), `usage`, `phone` × desktop 1440x900 / phone 390x844 × light / dark — screenshotted headless (full page, clipped at 2400 px). The browser clock is pinned to the same instant, animations and the caret are frozen, locale/time zone fixed. Records every JavaScript error the pages throw. Exit 3 = this machine cannot capture. |
| `compare.py` | Golden diff with a tolerance (PASS / DIFF / MISSING / NO-BASELINE). ffmpeg decodes the PNGs; no imaging library needed. `--panels` draws captioned golden\|now\|diff images of every DIFF. |
| `run.sh` | capture → compare against `tests/visual/golden/`. `--update` re-blesses. |

**Determinism is measured, not assumed:** repeated captures of the same tree
differ by **0 pixels**. The first version let the server clock tick, and the
header's "updated HH:MM:SS" moved by a second between two runs — that is why
the server clock is stopped, not merely started at `FROZEN_NOW`.

**The tolerance is tight on purpose** (threshold 8 levels per channel,
tolerance 0.002% of pixels ≈ 26 px of a desktop page). Measured: dropping ONE
letter from the "Overview" tab changes 0.076% of the desktop page, so the
first draft's 0.2% tolerance passed a visible typo.

## The regression gate (check.sh)

`check.sh`'s "visual regression" step runs `tools/visual/run.sh` (~8 s). A
diff that changes how a page looks fails it until the goldens are re-blessed
**in the same diff** — `tools/visual/run.sh --update` — after LOOKING at the
golden|now|diff panels it writes to `logs/visual/check-diff/compare/`. A
machine without Playwright/Chromium prints `SKIP visual regression` and
passes. A page that throws a JavaScript error while rendering the fixture
fails the step.

Goldens depend on this box's Chromium build and fonts; upgrading Playwright
(see the pin above) or the fonts is a legitimate re-bless.

**Dark mode is covered on `phone.html` only.** It is the one page with
`prefers-color-scheme` styles; `index.html` and `usage.html` have a single
dark theme, so their `-dark` goldens are pixel-identical to their `-light`
ones. Those views stay in the matrix so a page that gains light/dark styles
is covered the moment it does, but today they prove nothing about dark mode.

### Where the gate runs: the fleet machine only, not CI

The pages ask for `ui-monospace, monospace`, and fontconfig resolves that per
host: **Adwaita Mono** on the fleet box (`fc-match monospace`), **DejaVu Sans
Mono** on GitHub's Ubuntu runner. The goldens are the fleet box's render, and
a different font is not a small change — measured by re-rendering with
`monospace` mapped to another font: the Overview page moved 3.6% of its
pixels and the Usage page 100%, against a 0.002% tolerance. Installing
Chromium on CI would therefore fail every view on every pull request.

So `tools/visual/run.sh` **skips when `GITHUB_ACTIONS` is set** and says so
(`SKIP visual regression: CI renders with different fonts ...`), and
`.github/workflows/check.yml` does not install a browser. The visual gate is
enforced where the fleet runs `check.sh`: in every task worktree on the
fleet machine, and by the operator.

## In the pipeline (AGENTS.md Rule 7e): `ui_evidence.py`

For a task whose worktree is this repo and whose diff touches `static/` or
`dashboard.py`, the gate — after `verify_cmd` passes — runs
`ui_evidence.capture`:

- **after**: every view rendered from the task worktree;
- **before**: the same views rendered from the task's **merge base**,
  `git archive`d into a temp dir (no worktree, no ref moved), cached per sha
  under `logs/evidence/_ui_baseline/`;
- **compare**: per view, the share of changed pixels and the box around them;
  a changed view gets a captioned before|after|diff panel **cropped to the
  region that changed** (a 1440 px row shrunk into a 640 px panel is
  unreadable). A page that grew is padded and compared row for row, not
  reported as 100% changed;
- a contact sheet of every view, and any JavaScript error the merge base did
  not throw.

The manifest has evidence.py's shape (`kind: "ui"`), so the same
`evidence.publish` pushes it to the orphan `arc-evidence` branch and
`post_pr_evidence` comments it onto the PR — before|after|diff inline, every
full-page shot in a collapsed block. `ui_evidence.presenter(manifest)` picks
the presenter (review images, prompt block, PR markdown) for a dashboard or a
game capture. Captures live under `logs/evidence/<project>/<task>/x<attempt>/`,
never in the worktree.

**Do the images render on the PR?** This repo is public (`gh repo view --json
visibility` → `PUBLIC`), so `blob/arc-evidence/...?raw=true` redirects to
`raw.githubusercontent.com` and renders for anyone. For a private repo the
same link renders only for signed-in viewers with access.

### Reviewers: who can actually see

- `drivers.sees_images(driver)` is the capability. `reasonix` (DeepSeek) and
  the subscription harnesses (claude, codex, cursor, agy, gemini) consume
  `driver.images`; `opencode` (GLM-5.3) does not — ARC rejects image input
  for GLM (`400 unsupported multimodal content: image_url`, 2026-09-18).
- A seeing reviewer's prompt says to LOOK at each image and that a visible
  regression is BLOCKING. A blind reviewer's prompt says plainly **YOU CANNOT
  SEE IMAGES**: it judges the numbers (which views changed, how much) and the
  JavaScript errors; the screenshots are on the PR for the human, and the
  golden test is the deterministic gate.
- **The view_image hazard.** reasonix has a `view_image` tool that feeds real
  pixels to DeepSeek, but asked about a PNG without being told to use it, it
  decoded the bytes with a python one-liner and never saw the image. So
  `ReasonixDriver.argv` appends: call `view_image` on EACH file, do NOT read
  the PNG bytes with bash or python.
- Both review prompts get a **VISUAL** clause for any diff touching the UI
  (`code_tasks._visual_review_prose`): a UI change needs visual evidence and,
  when the look changes on purpose, re-blessed goldens; no evidence and no
  golden update for a visible change is a blocking issue. Non-UI prompts are
  byte-identical to before.

Not done: routing a UI diff to a seeing reviewer when the paired reviewer is
blind (the `modality-aware-review-routing` task — it relaxes Rule 2 and needs
an operator decision). A DeepSeek-implemented UI change is still reviewed by
GLM, which is told it is blind; the human reads the PR comment.
