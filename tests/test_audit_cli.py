"""`main.py audit` — the scheduled command's output contract.

`--json` is how a script consumes the audit (deploy/README.md documents it),
and it crashed with NameError on every invocation: cmd_audit printed with
`json.dumps` but never imported json. Nothing else in the suite ran the
command, so a one-line omission shipped and stayed. These tests run the
command function itself with the audit body stubbed, so they pin the CLI's
contract — parseable JSON, the rendered text otherwise, and the exit code —
without depending on what the audit finds on this machine.
"""
import argparse
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from helpers import capture_events  # noqa: F401  (sys.path via helpers import)

import main  # noqa: E402


def _args(**over):
    base = {"since": 3600.0, "json": False, "no_health": True, "fix": False}
    base.update(over)
    return argparse.Namespace(**base)


def _report(critical=0):
    findings = [{"severity": "critical", "area": "git", "what": "boom",
                 "detail": "", "action": "look"}] * critical
    return {"ts": 1_700_000_000.0, "since_s": 3600.0,
            "counts": {"critical": critical, "warning": 0, "info": 0},
            "findings": findings}


class AuditCli(unittest.TestCase):

    def _run(self, report, **over):
        out = io.StringIO()
        with mock.patch("audit.run", return_value=report), redirect_stdout(out):
            rc = main.cmd_audit(_args(**over))
        return rc, out.getvalue()

    def test_json_flag_emits_parseable_json(self):
        rc, out = self._run(_report(), json=True)
        self.assertEqual(rc, 0)
        parsed = json.loads(out)
        self.assertEqual(parsed["counts"], {"critical": 0, "warning": 0, "info": 0})
        self.assertEqual(parsed["findings"], [])

    def test_text_output_by_default(self):
        rc, out = self._run(_report())
        self.assertEqual(rc, 0)
        self.assertIn("ARC audit", out)
        with self.assertRaises(ValueError):
            json.loads(out)

    def test_exit_code_is_two_when_anything_is_critical(self):
        rc, out = self._run(_report(critical=1), json=True)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["counts"]["critical"], 1)

    def test_no_health_is_passed_through_as_with_health(self):
        with mock.patch("audit.run", return_value=_report()) as run, \
                redirect_stdout(io.StringIO()):
            main.cmd_audit(_args(no_health=True))
            main.cmd_audit(_args(no_health=False))
        self.assertEqual([c.kwargs["with_health"] for c in run.call_args_list],
                         [False, True])


if __name__ == "__main__":
    unittest.main()
