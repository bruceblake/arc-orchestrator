"""Verdict salvage: the reasonix terminal payload and malformed review JSON.

Measured 2026-09-15 21:27-23:47: six COMPLETED reviews in one evening were
recorded as crashes ("review ended without a parseable verdict"), burning the
MAX_REVIEW_CRASHES budget — one discarded verdict was an outright pass/0-issues
approval. Every one of them had its verdict in the transcript's terminal
`{"type": "result", ..., "result": "<json string>"}` record; two had a payload
whose JSON does not load at all.

Two defects, one fixture per shape (sanitised copies of the real transcripts —
no logs/harness/ file is read here):

1. `parse_transcript` returned `payload[-3000:]`. A review verdict JSON sits at
   the very START of `.result`, so payloads of 3303/3477/4308 chars lost their
   `"pass"` key off the head (sub-3000-char payloads survived, which is why
   some verdicts did come through). Replaying the 2026-09-15 reviewer
   transcripts through the fixed code rescues ELEVEN reviews — five of them
   outright pass/0-issue approvals.
2. `_parse_verdict` had no reading for a payload whose braces never balance.
"""
import json
import tempfile
import os
import unittest

import helpers  # noqa: F401  (sys.path)
from helpers import capture_events, ENTRY, STRONGEST  # noqa: F401

import code_tasks
import drivers


def _result_payload(text):
    """A reasonix terminal record, as the harness prints it."""
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "duration_ms": 1234, "num_turns": 7, "result": text,
                       "session_id": "20260915-233000.000000000-DeepSeek-V4.1",
                       "usage": {"totalTokens": 1000}})


def _transcript(payload, work_lines=6):
    """A transcript ending in the terminal result record, with real filler."""
    lines = []
    for i in range(work_lines):
        lines.append(json.dumps({"kind": "tool_dispatch",
                                 "tool": {"name": "read_file",
                                          "args": json.dumps({"path": f"f{i}.py"})}}))
        lines.append(json.dumps({"kind": "tool_result",
                                 "tool": {"output": "ok", "runState": "completed"}}))
    lines.append(_result_payload(payload))
    return "\n".join(lines) + "\n"


def _review_verdict(n_issues):
    """A scope-locked verdict that renders wider than the 3000-char window."""
    issues = [{"label": "introduced-by-this-diff",
               "text": ("src/module.py:%d — the new branch skips the capacity "
                        "lease, so two runs can stack past the account cap; "
                        "take the lease before spawning." % (100 + i))}
              for i in range(n_issues)]
    obj = {"pass": False, "issues": issues}
    # Prose AFTER the JSON, as the real payloads had: the verdict is at the
    # head, the analysis that follows is what the 3000-char tail kept.
    prose = "\n\n" + ("Reviewed the full diff against the spec. " * 100)
    return json.dumps(obj) + prose


class ResultPayloadReachesTheVerdict(unittest.TestCase):
    """(a) The verdict lives in the terminal record and must be parseable."""

    def test_long_rejection_gives_pass_false_and_its_issues(self):
        payload = _review_verdict(2)                     # ~3.4k chars
        self.assertGreater(len(payload), 3000,
                           "fixture must exceed the old 3000-char window")
        sid, text = drivers.parse_transcript(_transcript(payload))
        verdict = code_tasks._parse_verdict(text)
        self.assertFalse(verdict["pass"])
        self.assertEqual(2, len(verdict["issues"]))
        self.assertNotIn("truncated", verdict)           # not a crash
        self.assertNotIn("salvaged", verdict)

    def test_long_approval_gives_pass_true_and_no_issues(self):
        # The audit-backup-generator-fix-x2 shape: a real 0-issue approval
        # thrown away as a crash.
        payload = json.dumps({"pass": True, "issues": []}) + \
            "\n\n" + ("Checked the diff and the tests. " * 150)
        self.assertGreater(len(payload), 3000)
        _, text = drivers.parse_transcript(_transcript(payload))
        verdict = code_tasks._parse_verdict(text)
        self.assertTrue(verdict["pass"])
        self.assertEqual([], verdict["issues"])

    def test_short_payload_is_unchanged(self):
        # A payload inside the window returns byte-identical to before: the
        # head+tail change must not double text that already worked.
        payload = '{"pass": true, "issues": []}'
        _, text = drivers.parse_transcript(_transcript(payload))
        self.assertEqual(payload, text)

    def test_verdict_object_straddling_the_window_is_not_cut_in_half(self):
        """The real shape: the verdict object ENDS past char 3000.

        Slicing a fixed-width head would hand _parse_verdict a half object and
        salvage would report a rejection that was never read. The head must be
        cut at the object's own closing brace.
        """
        payload = _review_verdict(2)
        payload += "\n" + json.dumps({"decoy": {"pass": False}})  # later span
        _, text = drivers.parse_transcript(_transcript(payload))
        verdict = code_tasks._parse_verdict(text)
        self.assertNotIn("salvaged", verdict)
        self.assertNotIn("truncated", verdict)
        # the LATER decoy span is the last carrying span, as ever
        self.assertFalse(verdict["pass"])

    def test_error_result_record_is_still_surfaced(self):
        # An auth/capacity failure arrives as the same record with is_error.
        body = ('authentication failed for provider "arc-deepseek-v4-1-flash-'
                'thinking-max" (HTTP 403): ARC_API_KEY is invalid or expired')
        raw = json.dumps({"type": "result", "subtype": "error_during_execution",
                          "is_error": True, "result": body,
                          "session_id": "s-1"})
        sid, text = drivers.parse_transcript(raw + "\n")
        self.assertEqual(body, text)
        self.assertEqual("s-1", sid)


class MalformedVerdictSalvage(unittest.TestCase):
    """(b)+(c) A payload whose JSON never loads, salvaged fail-safe."""

    @staticmethod
    def _malformed(trailing, pass_value):
        """Unbalanced quotes around char ~840 — tonight's real shape.

        Everything up to `trailing` parses fine; the unescaped quote inside
        the issue text makes `json.loads` raise "Expecting ',' delimiter"
        and leaves no balanced span carrying "pass".
        """
        head = ('{"pass": %s, "issues": [{"label": "introduced-by-this-diff", '
                '"text": "watch out, the reviewer wrote an quote here "that '
                'never closes, and the rest of the object is prose"}],'
                % pass_value)
        assert len(head) > 130                      # past the JSON's own braces
        return head + " " + ("reasoning about the diff. " * 40) + trailing

    def test_salvaged_false_is_a_rejection_without_fabricated_issues(self):
        text = self._malformed(' ... so I am not approving. "pass": false', "false")
        with self.assertRaises(ValueError):
            json.loads(text)
        verdict = code_tasks._parse_verdict(text)
        self.assertFalse(verdict["pass"])
        self.assertTrue(verdict["salvaged"])
        self.assertNotIn("truncated", verdict)       # NOT a crash
        self.assertEqual(1, len(verdict["issues"]))  # one generic issue
        self.assertIn("malformed", verdict["issues"][0])

    def test_salvaged_true_is_not_trusted(self):
        # A malformed verdict could have carried blocking issues: the crash
        # fallback is kept, never an approval.
        text = self._malformed(' ... looks good to me. "pass": true', "true")
        verdict = code_tasks._parse_verdict(text)
        self.assertFalse(verdict["pass"])
        self.assertTrue(verdict["truncated"])
        self.assertNotIn("salvaged", verdict)

    def test_no_pass_key_at_all_still_crashes(self):
        text = '{"issues": [{"text": "an unbalanced "quote here, and prose'
        verdict = code_tasks._parse_verdict(text)
        self.assertTrue(verdict["truncated"])
        self.assertNotIn("salvaged", verdict)

    def test_last_pass_key_wins(self):
        text = self._malformed(' first "pass": false ... but finally "pass": true',
                               "false")
        self.assertTrue(code_tasks._parse_verdict(text)["truncated"])


class DuplicateVerdictSpans(unittest.TestCase):
    """(d) head+tail may repeat a verdict; that must not change the answer."""

    def test_same_verdict_twice_returns_one_verdict(self):
        payload = json.dumps({"pass": False,
                              "issues": [{"label": "introduced-by-this-diff",
                                          "text": "a.py:1 — blocks"}]})
        text = payload + "\n" + ("filler. " * 100) + "\n" + payload
        first, second = code_tasks._parse_verdict(payload), \
            code_tasks._parse_verdict(text)
        self.assertEqual(first, second)
        self.assertEqual(1, len(second["issues"]))

    def test_truncated_head_plus_tail_still_parses(self):
        """The real drivers.py output for a long payload: head + tail."""
        payload = _review_verdict(3)
        text = payload[:3000] + "\n" + payload[-3000:]
        verdict = code_tasks._parse_verdict(text)
        self.assertFalse(verdict["pass"])
        self.assertEqual(3, len(verdict["issues"]))


class TranscriptFileAssembly(unittest.TestCase):
    """The same path over a real file on disk (never logs/harness/)."""

    def test_transcript_on_disk_surfaces_the_verdict(self):
        payload = _review_verdict(2)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "task-x1-reviewer-1.jsonl")
            with open(path, "w") as fh:
                fh.write(_transcript(payload))
            with open(path) as fh:
                _, text = drivers.parse_transcript(fh.read())
        verdict = code_tasks._parse_verdict(text)
        self.assertFalse(verdict["pass"])
        self.assertEqual(2, len(verdict["issues"]))

    def test_streaming_view_is_untouched(self):
        # parse_transcript is end-of-run only; the dashboard's live view reads
        # the file as it grows and must not be affected by this change.
        payload = _review_verdict(2)
        view = drivers.TranscriptActivity()
        view.feed(_transcript(payload))
        view.finish()
        self.assertTrue(any("result" in b for b in view.blocks),
                        "the live view still surfaces the terminal record")


class ReviewedEventCarriesTheMarker(unittest.TestCase):
    """The salvaged marker reaches the task.reviewed event."""

    def test_emit_accepts_the_salvaged_flag(self):
        import events
        with capture_events() as ev:
            events.emit("task.reviewed", task="t1", passed=False,
                        reviewer="glm", n_issues=1, salvaged=True)
        rec = ev.first("task.reviewed")
        self.assertTrue(rec["salvaged"])


if __name__ == "__main__":
    unittest.main()
