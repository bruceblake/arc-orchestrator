"""TranscriptActivity: the incremental JSONL -> readable-blocks reducer behind
/api/transcript?view=activity.

Reasonix transcripts are ~95% sub-word reasoning deltas; the reducer must fold
them into one 💭 paragraph per message, survive chunk splits anywhere, skip
streaming stubs and bookkeeping records, and answer "what is it doing right
now" through pending()."""
import json
import unittest

from helpers import ENTRY  # noqa: F401  (sys.path wiring)

import drivers
from drivers import TranscriptActivity, _fold


def rx(obj):
    return json.dumps(obj)


def reasoning(mid, attempt, text):
    return rx({"kind": "reasoning", "messageId": mid, "attemptId": attempt, "text": text})


def text_delta(mid, attempt, text):
    return rx({"kind": "text", "messageId": mid, "attemptId": attempt, "text": text})


class ChunkBoundaries(unittest.TestCase):
    """A record split across feed() calls must reduce exactly like a whole feed."""

    STREAM = "\n".join([
        reasoning("m1", "a1", "The spec says the knob was removed "),
        reasoning("m1", "a1", "deliberately, so let me verify "),
        reasoning("m1", "a1", "against git history first."),
        text_delta("m1", "a1", "I'll read the change that removed it. "),
        text_delta("m1", "a1", "Then decide."),
        rx({"kind": "tool_dispatch", "tool": {"id": "c1", "name": "bash",
            "args": json.dumps({"command": "git log --oneline -3"})}}),
        rx({"kind": "tool_result", "tool": {"runState": "completed", "id": "c1",
            "name": "bash", "output": "abc123 remove the knob"}}),
    ]) + "\n"

    def test_chunked_and_whole_feeds_produce_the_same_blocks(self):
        whole = TranscriptActivity()
        whole.feed(self.STREAM)
        whole.finish()
        chunked = TranscriptActivity()
        for i in range(0, len(self.STREAM), 7):
            chunked.feed(self.STREAM[i:i + 7])
        chunked.finish()
        self.assertEqual(list(whole.blocks), list(chunked.blocks))

    def test_reasoning_deltas_fold_to_one_block_per_message(self):
        a = TranscriptActivity()
        a.feed(self.STREAM)
        a.finish()
        thinking = [b for b in a.blocks if b.startswith("💭 ")]
        self.assertEqual(len(thinking), 1)
        self.assertEqual(thinking[0],
                         "💭 The spec says the knob was removed deliberately, "
                         "so let me verify against git history first.")


class ReasonixBlocks(unittest.TestCase):
    def feed_lines(self, lines):
        a = TranscriptActivity()
        a.feed("".join(line + "\n" for line in lines))
        a.finish()
        return list(a.blocks)

    def test_text_deltas_fold_to_one_block_and_a_new_message_starts_another(self):
        blocks = self.feed_lines([
            text_delta("m1", "a1", "First answer. "),
            text_delta("m1", "a1", "Still the first."),
            text_delta("m2", "a1", "Second answer."),
        ])
        self.assertEqual(blocks, ["✎ First answer. Still the first.",
                                  "✎ Second answer."])

    def test_partial_tool_dispatch_is_skipped_and_the_final_emits_the_arg(self):
        blocks = self.feed_lines([
            rx({"kind": "tool_dispatch", "messageId": "m1",
                "tool": {"id": "c1", "name": "bash", "partial": True, "attemptId": "a1"}}),
            rx({"kind": "tool_dispatch", "messageId": "m1",
                "tool": {"id": "c1", "name": "bash", "runState": "pending",
                         "args": json.dumps({"command": "cd /tmp && ls -la"})}}),
        ])
        self.assertEqual(blocks, ["🔧 bash cd /tmp && ls -la"])

    def test_tool_result_renders_output_and_a_failed_state_is_marked(self):
        blocks = self.feed_lines([
            rx({"kind": "tool_result", "tool": {"runState": "completed", "id": "c1",
                "name": "read", "output": "line one\nline two"}}),
            rx({"kind": "tool_result", "tool": {"runState": "failed", "id": "c2",
                "name": "bash", "output": "permission denied"}}),
        ])
        self.assertEqual(blocks, ["   ↳ line one line two",
                                  "   ↳ ✗ permission denied"])

    def test_message_usage_notice_user_message_and_result_blocks(self):
        blocks = self.feed_lines([
            rx({"kind": "user_message", "text": "Implement the thing."}),
            rx({"kind": "message", "messageId": "m9", "attemptId": "a1", "text": "Done here."}),
            rx({"kind": "usage", "usage": {"promptTokens": 23804, "completionTokens": 150,
                                           "totalTokens": 23954, "cacheHitTokens": 32}}),
            rx({"kind": "notice", "text": "Converging.", "detail": "soft budget after 11 rounds"}),
            rx({"type": "result", "subtype": "success", "is_error": False, "result": "Shipped it."}),
            rx({"type": "result", "subtype": "error", "is_error": True, "result": "Gave up."}),
        ])
        self.assertEqual(blocks, [
            "▸ Implement the thing.",
            "✉ Done here.",
            "— usage 24.0k tok (+150 out, 32 cached)",
            "— Converging. (soft budget after 11 rounds)",
            "✓ result: Shipped it.",
            "✗ result: Gave up.",
        ])

    def test_message_repeating_its_text_deltas_is_shown_once(self):
        """A `message` record is the final of its own text deltas — rendering
        both would double every assistant message."""
        blocks = self.feed_lines([
            text_delta("m1", "a1", "The full answer, streaming. "),
            text_delta("m1", "a1", "More of it."),
            rx({"kind": "message", "messageId": "m1", "attemptId": "a1",
                "text": "The full answer, streaming.\nMore of it."}),
            rx({"kind": "message", "messageId": "m2", "attemptId": "a1",
                "text": "A message with no streamed deltas."}),
        ])
        self.assertEqual(blocks, ["✎ The full answer, streaming. More of it.",
                                  "✉ A message with no streamed deltas."])

    def test_long_reasoning_folds_with_a_count(self):
        blocks = self.feed_lines([reasoning("m1", "a1", "x" * 1500)])
        (block,) = blocks
        self.assertTrue(block.startswith("💭 " + "x" * 700))
        self.assertIn("[+300 chars folded]", block)
        self.assertTrue(block.endswith("x" * 500))

    def test_bookkeeping_kinds_are_silent(self):
        blocks = self.feed_lines([
            rx({"kind": "turn_started"}),
            rx({"kind": "turn_phase", "phase": "main"}),
            rx({"kind": "stream_attempt", "n": 1}),
            rx({"kind": "tool_started", "tool": {"id": "c1"}}),
            rx({"kind": "tool_progress", "tool": {"id": "c1"}}),
            rx({"kind": "read_status"}),
        ])
        self.assertEqual(blocks, [])


class OpencodeBlocks(unittest.TestCase):
    def feed_lines(self, lines):
        a = TranscriptActivity()
        a.feed("".join(line + "\n" for line in lines))
        a.finish()
        return list(a.blocks)

    def test_text_tool_use_with_output_and_step_finish(self):
        blocks = self.feed_lines([
            rx({"type": "step_start", "part": {"type": "step-start"}}),
            rx({"type": "text", "part": {"text": '{"pass": true}'}}),
            rx({"type": "tool_use", "part": {"tool": "read", "state": {
                "status": "completed", "input": {"filePath": "/repo/a.py"},
                "output": "def f():\n    return 1"}}}),
            rx({"type": "step_finish", "part": {"tokens": {"total": 23029}}}),
        ])
        self.assertEqual(blocks, [
            "✎ {\"pass\": true}",
            "🔧 read /repo/a.py\n   ↳ def f(): return 1",
            "— step (23.0k tok)",
        ])

    def test_tool_use_without_output_is_a_single_line(self):
        blocks = self.feed_lines([
            rx({"type": "tool_use", "part": {"tool": "bash", "state": {
                "status": "running", "input": {"command": "make test"}}}}),
        ])
        self.assertEqual(blocks, ["🔧 bash make test"])


class PendingAndFlush(unittest.TestCase):
    def test_pending_reports_the_open_group_stops_after_a_flush(self):
        a = TranscriptActivity()
        a.feed(reasoning("m1", "a1", "still thinking about it") + "\n")
        pending = a.pending()
        self.assertTrue(pending.startswith("💭 … thinking (23 chars so far): "))
        self.assertTrue(pending.endswith("still thinking about it"))
        a.feed(text_delta("m1", "a1", "writing now") + "\n")
        self.assertIn("✎ writing (11 chars so far)", a.pending())
        a.feed(rx({"kind": "notice", "text": "hi"}) + "\n")
        self.assertIsNone(a.pending())
        a.finish()
        self.assertIsNone(a.pending())

    def test_finish_flushes_a_trailing_partial_line(self):
        a = TranscriptActivity()
        a.feed(reasoning("m1", "a1", "unterminated"))   # no newline yet
        self.assertEqual(list(a.blocks), [])
        a.finish()
        self.assertEqual(list(a.blocks), ["💭 unterminated"])
        self.assertIsNone(a.pending())


class FoldHelper(unittest.TestCase):
    def test_short_text_is_left_intact(self):
        self.assertEqual(_fold("plain text"), "plain text")
        self.assertEqual(_fold("x" * 1200), "x" * 1200)

    def test_long_text_keeps_head_and_tail_and_counts_the_middle(self):
        out = _fold("h" * 700 + "m" * 800 + "t" * 500)
        self.assertEqual(out, "h" * 700 + "\n ⋯ [+800 chars folded] ⋯\n" + "t" * 500)


class JunkTolerance(unittest.TestCase):
    def test_blank_broken_and_unrecognized_lines_are_skipped_silently(self):
        a = TranscriptActivity()
        a.feed("\n   \nnot json at all\n{broken\n[1, 2]\n"
               + json.dumps({"foo": "bar"}) + "\n"
               + reasoning("m1", "a1", "real") + "\n")
        a.finish()
        self.assertEqual(list(a.blocks), ["💭 real"])
        # The blank lines pass silently; everything else counted exactly once.
        self.assertEqual(a.unknown, 4)


if __name__ == "__main__":
    unittest.main()
