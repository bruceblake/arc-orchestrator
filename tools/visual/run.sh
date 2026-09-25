#!/usr/bin/env bash
# Visual regression for the dashboard: capture every view, compare to goldens.
#
#     tools/visual/run.sh            # check: exit 1 on any DIFF / MISSING view
#     tools/visual/run.sh --update   # re-bless: copy this tree's shots over the goldens
#
# Screenshots land in logs/visual/check/ (gitignored); goldens are committed
# in tests/visual/golden/. On a DIFF, captioned golden|now|diff panels are
# written to logs/visual/check-diff/ — LOOK at them before re-blessing: an
# intended change gets `--update` in the same diff, an unintended one is a
# regression to fix. A machine that cannot run headless Chromium SKIPS with a
# message and exit 0: that is an infrastructure gap, not a failing change.
#
# The goldens are the FLEET MACHINE's render: the pages ask for `monospace`,
# which fontconfig resolves per host (Adwaita Mono on the fleet box, DejaVu
# Sans Mono on an Ubuntu CI runner), and a different font moves far more
# than the 0.002% tolerance. So CI skips: this gate is enforced on the fleet
# machine only.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1
if [ -n "${GITHUB_ACTIONS:-}" ]; then
    echo "SKIP visual regression: CI renders with different fonts than the goldens;"
    echo "     the visual gate is enforced on the fleet machine only (docs/visual-testing.md)"
    exit 0
fi
PY="${PY:-./py}"
out=logs/visual/check
# logs/ is gitignored, so a fresh checkout has no logs/visual/ yet: the
# capture log below is written before capture.py makes its own --out dir.
mkdir -p logs/visual
rm -rf "$out" logs/visual/check-diff
"$PY" tools/visual/capture.py --out "$out" --strict >"$out.log" 2>&1
rc=$?
if [ $rc -eq 3 ]; then
    # The Unavailable message can be a multi-line Playwright banner; its first
    # line names the cause, its last is box-drawing.
    reason="$(grep -m1 '^SKIP: ' "$out.log" || head -1 "$out.log")"
    echo "SKIP visual regression: ${reason#SKIP: }"
    echo "     (install with: ./py -m pip install -r requirements.txt && ./py -m playwright install chromium)"
    exit 0
fi
if [ $rc -eq 4 ]; then
    echo "FAIL: a dashboard page throws a JavaScript error while rendering the fixture:"
    grep '^PAGE ERROR' "$out.log"
    exit 1
fi
if [ $rc -ne 0 ]; then
    echo "FAIL: the dashboard could not be captured:"
    tail -20 "$out.log"
    exit 1
fi
if [ "${1:-}" = "--update" ]; then
    mkdir -p tests/visual/golden
    rm -f tests/visual/golden/*.png
    cp "$out"/*.png tests/visual/golden/
    echo "goldens updated from this tree: $(ls tests/visual/golden/*.png | wc -l) view(s) in tests/visual/golden/"
    exit 0
fi
if ! "$PY" tools/visual/compare.py tests/visual/golden "$out" --panels logs/visual/check-diff; then
    echo "FAIL: the dashboard no longer renders like tests/visual/golden/."
    echo "      Look at the golden|now|diff panels above. If the change is intended,"
    echo "      re-bless in the same diff:  tools/visual/run.sh --update"
    exit 1
fi
echo "visual regression: every view matches its golden"
