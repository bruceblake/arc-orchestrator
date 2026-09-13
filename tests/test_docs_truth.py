"""Docs must agree with the code they describe.

The README, AGENTS.md and every file in docs/ are the contract a human or an
agent reads before touching this repo. When those docs name a subcommand, an
``ARC_*`` env var, a ``config.NAME`` attribute, a relative link or a path to a
file that does not exist (or an ``ARC_*`` var the code defines but no doc
mentions), the doc is lying. Each test here pins one class of lie so a doc edit
that drifts from the code fails loudly instead of quietly misleading the next
reader.
"""

import ast
import glob
import os
import re
import unittest

from helpers import capture_events  # noqa: F401  (sys.path via helpers import)

import config  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = [os.path.join(ROOT, "README.md"), os.path.join(ROOT, "AGENTS.md")]
DOCS += sorted(glob.glob(os.path.join(ROOT, "docs", "*.md")))


def _doc_text():
    text = ""
    for path in DOCS:
        with open(path, encoding="utf-8") as fh:
            text += fh.read() + "\n"
    return text


def _fenced_blocks():
    """Yield the body of every fenced code block across the doc files."""
    for path in DOCS:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for block in re.findall(r"```[^\n]*\n(.*?)```", text, re.S):
            yield block


def _main_subcommands():
    """Extract the argparse subcommand tree from main.py via AST.

    We parse main.py rather than hardcode the list so the test can never
    silently go stale when a command is added or renamed.
    """
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    fn = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"),
        None,
    )
    if fn is None:
        raise AssertionError("main.py defines no function named main()")

    objects = {}
    commands = set()

    def classify(call):
        if not isinstance(call, ast.Call):
            return None
        f = call.func
        if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)):
            return None
        attr = f.attr
        base = f.value.id
        if attr == "add_subparsers":
            return ("subparsers", base)
        if attr == "add_parser":
            args = call.args
            if args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                return ("parser", base, args[0].value)
        return None

    def record(statement):
        value = getattr(statement, "value", None)
        kind = classify(value)
        if kind is None:
            return
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            if not (len(targets) == 1 and isinstance(targets[0], ast.Name)):
                return
            name = targets[0].id
            if kind[0] == "subparsers":
                objects[name] = objects.get(kind[1], [])
            elif kind[0] == "parser":
                path = objects.get(kind[1], []) + [kind[2]]
                objects[name] = path
                commands.add(tuple(path))
        elif isinstance(statement, ast.Expr):
            if kind[0] == "parser":
                commands.add(tuple(objects.get(kind[1], []) + [kind[2]]))

    for statement in fn.body:
        record(statement)

    return commands


COMMANDS = _main_subcommands()
DOC_TEXT = _doc_text()


class TestDocsTruthMainCommands(unittest.TestCase):
    """Every ``main.py <cmd>`` in a fenced code block must be a real subcommand.

    Docs teach operators the CLI. A command that does not exist in main.py's
    argparse tree makes the documented invocation fail at the shell, so an
    operator following the docs would think the orchestrator is broken.
    """

    def test_fenced_main_commands_exist(self):
        violations = []
        for block in _fenced_blocks():
            for line in block.splitlines():
                tokens = line.split()
                for i, tok in enumerate(tokens):
                    if tok != "main.py":
                        continue
                    if i + 1 >= len(tokens):
                        continue
                    first = tokens[i + 1]
                    if (first,) not in COMMANDS:
                        violations.append((first, None, line.strip()))
                        continue
                    if first in ("code", "bench") and i + 2 < len(tokens):
                        second = tokens[i + 2]
                        if second and second[0].isalpha() and (first, second) not in COMMANDS:
                            violations.append((first, second, line.strip()))
        self.assertEqual(
            violations, [],
            "fenced docs call a subcommand main.py does not define: %s"
            % [(v[0], v[1]) for v in violations],
        )


class TestDocsTruthArcEnvVars(unittest.TestCase):
    """Doc ``ARC_*`` vars must exist in config.py and every config one must be documented.

    The env table in the docs is how an operator tunes a running deployment.
    Referencing a var config.py never reads is a no-op that silently does
    nothing; defining an override that no doc mentions leaves it undiscoverable.
    """

    @classmethod
    def setUpClass(cls):
        # config.py is not the only thing that reads ARC_* — the shell
        # entrypoints do too (ARC_QUEUE_PARALLEL is read by run-queue.sh and
        # appears nowhere in config.py). Scanning config.py alone reported a
        # real, working env var as a documentation lie.
        src = ""
        for rel in ["config.py"] + sorted(
                os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "*.sh"))):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                src += fh.read() + "\n"
        defined = set(re.findall(r"ARC_[A-Z0-9_]+", src))
        # Derived from the registry, not listed here: a literal list kept
        # minting a gpt-oss driver-limit knob after it was removed from
        # FAMILIES, so the suite demanded docs for a knob nothing reads.
        for prefix in ("ARC_LIMIT_", "ARC_DRIVER_LIMIT_"):
            for family in config.FAMILIES:
                defined.add(prefix + family.upper().replace("-", "_"))
        # config.harness_limit builds its override name the same way
        # driver_limit does — os.getenv(f"ARC_HARNESS_LIMIT_{harness.upper()}")
        # — so the literal never appears in config.py and the scan cannot see
        # it. Expanded from the configured harness set (was a hardcoded
        # ("OPENCODE", "KIMI") tuple, which demanded docs for a harness that
        # had left and none for dsh when it arrived, 2026-09-12).
        for harness in config._HARNESS_CAP:
            defined.add("ARC_HARNESS_LIMIT_" + harness.upper())
        cls.defined = defined
        cls.mentioned = set(re.findall(r"ARC_[A-Z0-9_]+", DOC_TEXT))

    def test_doc_arc_vars_are_defined(self):
        bogus = sorted(self.mentioned - self.defined)
        self.assertEqual(
            bogus, [],
            "docs reference ARC_* vars config.py never reads: %s" % bogus,
        )

    def test_defined_arc_vars_are_documented(self):
        undocumented = sorted(self.defined - self.mentioned)
        self.assertEqual(
            undocumented, [],
            "ARC_* vars config.py reads are never mentioned in any doc: %s"
            % undocumented,
        )


class TestDocsTruthDefaults(unittest.TestCase):
    """The README's tuning table must state the default config.py actually uses.

    The table is where an operator learns what happens when they set nothing.
    It said ``ARC_BASE_BRANCH`` defaulted to ``development`` for a full day
    after the code moved to ``main`` — the name test above passed, because
    the variable existed, and the wrong default sat there being read. This
    compares every row against the literal ``os.getenv(NAME, "default")`` in
    config.py. Defaults that are computed (a path under ROOT, a derived
    number) have no literal to compare and are skipped; a row for a
    templated name (``ARC_LIMIT_<FAMILY>``) likewise.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "config.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        literal = {}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "getenv" and len(node.args) == 2
                    and all(isinstance(a, ast.Constant) for a in node.args)):
                continue
            name, default = node.args[0].value, node.args[1].value
            if isinstance(name, str) and name.startswith("ARC_") and isinstance(default, str):
                literal[name] = default
        cls.literal = literal
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        cls.rows = re.findall(r"^\| `(ARC_[A-Z0-9_<>]+)` \| ([^|]*) \|", readme, re.M)

    @staticmethod
    def _same(doc, code):
        doc = doc.strip().strip("`")
        if doc == code:
            return True
        try:
            return float(doc) == float(code)
        except ValueError:
            pass
        # "(unset)" is how the table says the code default is the empty string
        return doc.lower() in ("(unset)", "unset", "(none)") and code == ""

    def test_readme_table_has_rows(self):
        self.assertGreater(len(self.rows), 20, "the README tuning table was not found")

    def test_documented_defaults_match_config(self):
        wrong = []
        for name, doc_default in self.rows:
            if "<" in name or name not in self.literal:
                continue
            if not self._same(doc_default, self.literal[name]):
                wrong.append(f"{name}: README says {doc_default.strip()!r}, "
                             f"config.py says {self.literal[name]!r}")
        self.assertEqual(wrong, [], "README tuning table defaults disagree with "
                                    "config.py:\n  " + "\n  ".join(wrong))


class TestDocsTruthConfigNames(unittest.TestCase):
    """Every ``config.NAME`` in prose must exist as a real attribute.

    AGENTS.md and the docs cite ``config.<attr>`` as the source of truth for a
    tunable or a model. A reference to an attribute config.py does not define
    points a reader at code that is not there.
    """

    def test_config_names_exist(self):
        refs = set(re.findall(r"config\.([A-Za-z_][A-Za-z0-9_]*)", DOC_TEXT))
        refs.discard("py")
        refs.discard("toml")
        missing = sorted(name for name in refs if not hasattr(config, name))
        self.assertEqual(
            missing, [],
            "docs reference config attributes that do not exist: %s" % missing,
        )


class TestDocsTruthMarkdownLinks(unittest.TestCase):
    """Every relative markdown link must resolve to a real file.

    A doc that links a missing page sends a reader to a 404; the fleet then
    cannot follow the pointer it was give for the contract it must uphold.
    """

    def test_relative_links_resolve(self):
        broken = []
        for path in DOCS:
            base = os.path.dirname(path)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for match in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", text):
                target = match.group(1).strip()
                if target.startswith(("http://", "https://", "mailto:", "tel:", "#")):
                    continue
                target = target.split("#")[0]
                if not target:
                    continue
                resolved = os.path.normpath(os.path.join(base, target))
                if not os.path.exists(resolved):
                    broken.append((path, match.group(1)))
        self.assertEqual(
            broken, [], "docs link files that do not exist: %s" % broken,
        )


class TestDocsTruthReferencedPaths(unittest.TestCase):
    """Every ``tests/``, ``static/`` or ``docs/`` path named in a doc must exist.

    The file map in README/AGENTS.md is how someone finds the module that owns a
    behavior. Naming a path that is not in the tree sends them hunting for a
    file that does not exist.
    """

    def test_referenced_paths_exist(self):
        pattern = re.compile(
            r"\b((?:tests|static|docs)/[A-Za-z0-9_.\-/]+\.(?:py|html|json|md|css|js|jsonl))"
        )
        missing = []
        for path in DOCS:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for match in pattern.finditer(text):
                rel = match.group(1)
                if not os.path.exists(os.path.join(ROOT, rel)):
                    missing.append((path, rel))
        self.assertEqual(
            missing, [],
            "docs name paths that do not exist: %s" % missing,
        )


class TestDocsTruthTaskfileExamples(unittest.TestCase):
    """A taskfile example in the docs must be one a run would actually accept.

    The docs are what an agent copies from. README.md listed `gpt-oss-120b` as
    a valid `model` value for eleven days after the model left the roster, and
    the whole docs-truth suite passed the entire time: it checked env vars,
    config attributes and file paths, and never once looked at the model names
    the examples tell the reader to write. A taskfile built from those examples
    is rejected by load_taskfile before a single model is called.

    Scope is deliberately narrow — fenced blocks only. Prose that discusses a
    retired model ("gpt-oss-120b was retired on 2026-09-11") is history and
    must stay readable; an EXAMPLE is an instruction.
    """

    @staticmethod
    def _pairs(key):
        for block in _fenced_blocks():
            for m in re.finditer(r'"%s"\s*:\s*"([^"]+)"' % key, block):
                yield m.group(1)

    def test_example_models_are_live_implementers(self):
        live = set(config.IMPLEMENTER_MODELS)
        # A schema line naming the alternatives ("gpt-oss|DeepSeek|GLM") is a
        # placeholder, not a value; the loader would reject it either way, and
        # the per-alternative check below covers its parts.
        bad = sorted({m for m in self._pairs("model")
                      if "|" not in m and m not in live and not m.startswith("<")})
        self.assertEqual(
            bad, [],
            "docs show taskfile examples whose `model` is not a live "
            "implementer %s: %s" % (sorted(live), bad))

    def test_example_model_alternatives_are_live(self):
        live = set(config.IMPLEMENTER_MODELS)
        bad = []
        for value in self._pairs("model"):
            if "|" not in value:
                continue
            for alt in value.split("|"):
                alt = alt.strip()
                if alt and not alt.startswith("<") and alt not in live:
                    bad.append(alt)
        self.assertEqual(
            sorted(set(bad)), [],
            "a docs schema line offers models that are not live: %s" % sorted(set(bad)))

    def test_example_reviewers_are_live_review_families(self):
        live = set(config.REVIEW_FAMILIES)
        bad = []
        for value in self._pairs("reviewer"):
            for alt in str(value).split("|"):
                alt = alt.strip()
                if alt and not alt.startswith("<") and alt not in live:
                    bad.append(alt)
        self.assertEqual(
            sorted(set(bad)), [],
            "docs show taskfile examples whose `reviewer` is not a live review "
            "family %s: %s" % (sorted(live), sorted(set(bad))))


if __name__ == "__main__":
    unittest.main()
