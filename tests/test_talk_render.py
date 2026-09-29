"""Browser render of the open chat and captain dialogs and the phone Plan view.

The golden shots leave those dialogs closed, so they cannot show this layout.
This test opens them and checks the geometry. It also writes
tests/visual/talk/*.png for a reviewer. Those files are not golden views:
tools/visual/compare.py does not read that directory.
"""
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (sys.path)

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "tests" / "visual" / "talk"


def _capture():
    spec = importlib.util.spec_from_file_location(
        "talk_capture", ROOT / "tools" / "visual" / "capture.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TalkDialogRender(unittest.TestCase):
    def test_open_dialogs_and_plan_view(self):
        capture = _capture()
        capture._browser_env()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright is not importable")
        fx = Path(tempfile.mkdtemp(prefix="arc-talk-render-"))
        self.addCleanup(shutil.rmtree, fx, ignore_errors=True)
        try:
            proc, port = capture.start_server(ROOT, fx)
        except Exception as exc:                                # noqa: BLE001
            self.skipTest(f"fixture dashboard did not start: {exc}")
        self.addCleanup(capture.stop_server, proc)
        base = f"http://127.0.0.1:{port}"
        SHOTS.mkdir(parents=True, exist_ok=True)
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch(headless=True)
                except Exception as exc:                        # noqa: BLE001
                    self.skipTest(f"chromium will not launch: {exc}")
                self._desktop(browser, base)
                self._phone(browser, base)
                self._phone_short(browser, base)
                browser.close()
        except unittest.SkipTest:
            raise
        except Exception as exc:                                # noqa: BLE001
            self.fail(f"talk render failed: {exc}")

    def _desktop(self, browser, base):
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base + "/", wait_until="domcontentloaded")
        page.click('button.tab[data-tab="projects"]')
        page.click("#btn-chat")
        page.wait_for_selector("#chatmodal.open")
        # chatOpen adds .open before it finishes loading, then paints the
        # empty state. Wait for that paint, then freeze the renderer.
        page.wait_for_function(
            "() => (document.querySelector('#c-log')||{}).innerText"
            " && document.querySelector('#c-log').innerText.includes('Send a message')")
        self._assert_empty_centered(page, "#c-log")
        inside = page.evaluate("""() => {
          if (CHAT_POLL) { clearInterval(CHAT_POLL); CHAT_POLL = null; }
          chatRender = () => {};
          document.querySelector('#c-log').innerHTML =
            chatTurnHTML({role:'user', text:'Add a settings menu with volume sliders for the game.'})
            + chatTurnHTML({role:'assistant', text:'I will add a settings menu and a verify gate of ./check.sh. The implementer is the medium tier.', model:'Sub Seat'});
          const b = document.querySelector('#c-log .chat-turn.assistant .chat-bubble');
          const log = document.querySelector('#c-log').getBoundingClientRect();
          const user = document.querySelector('#c-log .chat-turn.user').getBoundingClientRect();
          const asst = document.querySelector('#c-log .chat-turn.assistant').getBoundingClientRect();
          return {
            model: !!(b && b.querySelector('.chat-model-name')),
            userFromRight: log.right - user.right,
            asstFromLeft: asst.left - log.left,
            userLeft: user.left, asstLeft: asst.left,
          };
        }""")
        box = page.locator("#chatmodal .chat-dialog").bounding_box()
        self.assertIsNotNone(box)
        self.assertGreater(box["height"], 700)
        self.assertLess(box["y"] + box["height"], 901)
        self.assertEqual(page.locator("#c-text").evaluate("el => el.tagName"),
                         "TEXTAREA")
        self.assertEqual(page.locator("#c-send").inner_text(), "Send")
        self.assertGreater(page.locator("#c-model option").count(), 0)
        self._assert_select_label_visible(page, "#c-sessions", "plan-")
        self.assertTrue(inside["model"], "model name must be inside the chat bubble")
        # Previous CSS used one 12px radius on every corner. The person bubble
        # now has a flat bottom-right corner; that value is 12px without it.
        self.assertEqual(
            page.locator("#c-log .chat-turn.user .chat-bubble").evaluate(
                "el => getComputedStyle(el).borderBottomRightRadius"),
            "4px")
        self.assertLess(inside["userFromRight"], 30, inside)
        self.assertLess(inside["asstFromLeft"], 30, inside)
        self.assertGreater(inside["userLeft"], inside["asstLeft"], inside)
        page.screenshot(path=str(SHOTS / "chat-dialog.png"))
        page.click("#c-close")

        page.click("#btn-captain")
        page.wait_for_selector("#capmodal.open")
        page.wait_for_function(
            "() => (document.querySelector('#k-log')||{}).innerText"
            " && document.querySelector('#k-log').innerText.includes('Send a message')")
        self._assert_empty_centered(page, "#k-log")
        inside = page.evaluate("""() => {
          if (CAP_POLL) { clearInterval(CAP_POLL); CAP_POLL = null; }
          capRender = () => {};
          document.querySelector('#k-log').innerHTML =
            capTurnHTML({role:'user', text:'What is stuck in the fleet right now?'})
            + capTurnHTML({role:'assistant', text:'Two tasks are failed and one chain is waiting. I would resume the failed task once a slot is free.', model:'Sub Seat'});
          const b = document.querySelector('#k-log .chat-turn.assistant .chat-bubble');
          const log = document.querySelector('#k-log').getBoundingClientRect();
          const user = document.querySelector('#k-log .chat-turn.user').getBoundingClientRect();
          const asst = document.querySelector('#k-log .chat-turn.assistant').getBoundingClientRect();
          return {
            model: !!(b && b.querySelector('.chat-model-name')),
            userFromRight: log.right - user.right,
            asstFromLeft: asst.left - log.left,
            userLeft: user.left, asstLeft: asst.left,
          };
        }""")
        open_fleet = page.locator("#capmodal details").evaluate("el => el.open")
        self.assertFalse(open_fleet)
        self.assertTrue(inside["model"], "model name must be inside the captain bubble")
        self.assertEqual(
            page.locator("#k-log .chat-turn.user .chat-bubble").evaluate(
                "el => getComputedStyle(el).borderBottomRightRadius"),
            "4px")
        self.assertLess(inside["userFromRight"], 30, inside)
        self.assertLess(inside["asstFromLeft"], 30, inside)
        self.assertGreater(inside["userLeft"], inside["asstLeft"], inside)
        self.assertEqual(page.locator("#k-send").inner_text(), "Send")
        self._assert_select_label_visible(page, "#k-sessions", "captain-")
        cbox = page.locator("#capmodal .chat-dialog").bounding_box()
        self.assertLess(cbox["y"] + cbox["height"], 901)
        page.screenshot(path=str(SHOTS / "captain-dialog.png"))
        page.close()

    def _assert_empty_centered(self, page, log_sel):
        # The old rule pinned "Send a message to start." to the top of the
        # log (no margin, text-align start). margin:auto + text-align:center
        # puts that one sentence in the middle. Side alignment of bubbles
        # already held before this CSS, so it does not catch the change.
        pos = page.locator(log_sel).evaluate("""log => {
          const empty = log.querySelector('.chat-empty');
          const lr = log.getBoundingClientRect();
          const er = empty.getBoundingClientRect();
          const cs = getComputedStyle(empty);
          return {
            text: (empty.textContent || '').trim(),
            textAlign: cs.textAlign,
            delta: Math.abs((er.top + er.height / 2) - (lr.top + lr.height / 2)),
            logH: lr.height,
          };
        }""")
        self.assertEqual(pos["text"], "Send a message to start.")
        self.assertEqual(pos["textAlign"], "center", pos)
        self.assertGreater(pos["logH"], 200, pos)
        self.assertLess(pos["delta"], pos["logH"] * 0.2, pos)

    def _assert_select_label_visible(self, page, selector, needle):
        # Option text existing is not enough: a flex <select> can shrink to
        # the dropdown arrow while the option string is still in the DOM.
        fit = page.locator(selector).evaluate("""el => {
          const opt = el.selectedOptions && el.selectedOptions[0];
          const text = (opt && opt.text) || "";
          const cs = getComputedStyle(el);
          const probe = document.createElement("span");
          probe.style.cssText = "position:absolute;left:-9999px;top:0;white-space:nowrap;font:" + cs.font;
          probe.textContent = text;
          document.body.appendChild(probe);
          const need = probe.getBoundingClientRect().width;
          probe.remove();
          return {text: text, need: need, width: el.getBoundingClientRect().width};
        }""")
        self.assertIn(needle, fit["text"], fit)
        # ~22px is the closed-arrow chrome beside the label.
        self.assertGreaterEqual(
            fit["width"], fit["need"] + 22,
            f"{selector} session name is clipped {fit}")

    def _phone(self, browser, base):
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(base + "/phone.html", wait_until="domcontentloaded")
        page.click("#nav-plan")
        page.wait_for_timeout(300)
        page.evaluate("""() => {
          planTurns = Array.from({length: 40}, (_, i) => ({
            role: i % 2 ? 'user' : 'assistant',
            text: 'line ' + i + ' of a long plan transcript',
            model: i % 2 ? '' : 'Sub Seat',
            ts: 1700000000 + i,
          }));
          renderPlanTranscript();
        }""")
        view = page.locator("#view-plan").evaluate(
            "el => el.getBoundingClientRect().bottom")
        self.assertLessEqual(view, 844)
        scrolled = page.locator("#plan-transcript").evaluate(
            "el => el.scrollHeight > el.clientHeight + 20")
        self.assertTrue(scrolled, "a long plan transcript must scroll")
        send_bottom = page.locator("#plan-send").evaluate(
            "el => el.getBoundingClientRect().bottom")
        self.assertLess(send_bottom, 800)
        self.assertEqual(
            page.locator("#plan-input").evaluate("el => el.tagName"), "TEXTAREA")
        self.assertGreater(page.locator("#plan-model option").count(), 0)
        page.screenshot(path=str(SHOTS / "phone-plan.png"))
        page.close()

    def _phone_short(self, browser, base):
        # A keyboard-sized viewport. The transcript is long and a taskfile
        # is present; Run must stay tappable, not clipped under the composer.
        page = browser.new_page(viewport={"width": 390, "height": 480})
        page.goto(base + "/phone.html", wait_until="domcontentloaded")
        page.click("#nav-plan")
        page.wait_for_timeout(200)
        page.evaluate("""() => {
          planTurns = Array.from({length: 24}, (_, i) => ({
            role: i % 2 ? 'user' : 'assistant',
            text: 'line ' + i + ' of a long plan transcript',
            model: i % 2 ? '' : 'Sub Seat',
            ts: 1700000000 + i,
          }));
          planTurns.push({
            role: 'assistant', text: 'plan ready', model: 'Sub Seat',
            taskfile: 'demo.json', ts: 1700000099,
          });
          renderPlanTranscript();
          renderPlanTaskcard();
        }""")
        run = page.locator('#plan-taskcard [data-run-file="demo.json"]')
        self.assertEqual(run.count(), 1)
        run.scroll_into_view_if_needed()
        visible = page.evaluate("""() => {
          const el = document.querySelector('#plan-taskcard [data-run-file]');
          const r = el.getBoundingClientRect();
          const hit = document.elementFromPoint(
            r.x + r.width / 2, r.y + r.height / 2);
          const view = document.querySelector('#view-plan').getBoundingClientRect();
          return {
            top: r.top, bottom: r.bottom, height: r.height,
            viewTop: view.top, viewBottom: view.bottom,
            hit: !!(hit && (hit === el || el.contains(hit))),
            vh: window.innerHeight,
          };
        }""")
        self.assertGreater(visible["height"], 30, visible)
        self.assertGreaterEqual(visible["top"], visible["viewTop"] - 1, visible)
        self.assertLessEqual(visible["bottom"], visible["viewBottom"] + 1, visible)
        self.assertLessEqual(visible["bottom"], visible["vh"] + 1, visible)
        self.assertTrue(visible["hit"], visible)
        send = page.locator("#plan-send").bounding_box()
        self.assertIsNotNone(send)
        self.assertLess(send["y"] + send["height"], 481)
        page.screenshot(path=str(SHOTS / "phone-plan-short.png"))
        page.close()
