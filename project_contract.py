"""The target repo's own agent contract, kept separate from fleet governance.

``arc-orchestrator/AGENTS.md`` tells agents how this fleet runs. A product
repo's ``AGENTS.md`` (and ``CLAUDE.md``, and ``.cursor/rules``) tells agents
how THAT project is structured. Harnesses run with their working directory
set to the product worktree, so those files are on disk — this module names
them in the prompts so they are not skipped, and tells the operator when a
repo has none. It never copies the orchestrator's governance into a product.
"""
from pathlib import Path

ROOT_NAMES = ("AGENTS.md", "CLAUDE.md")
RULE_SUFFIXES = (".md", ".mdc")
MAX_RULES = 12
EXCERPT_CHARS = 1600


def _contained_file(base, path):
    """A regular file that stays inside ``base`` after resolving symlinks."""
    try:
        if not path.is_file():
            return False
        path.resolve().relative_to(base)
    except (OSError, ValueError):
        return False
    return True


def discover(repo):
    """Root contract files and Cursor rule names for ``repo``.

    Missing directories and unreadable trees yield empty lists. A symlink
    that points outside the repo is ignored.
    """
    root = Path(repo)
    files, rules = [], []
    if not root.is_dir():
        return {"root_files": files, "cursor_rules": rules}
    try:
        base = root.resolve()
    except OSError:
        return {"root_files": files, "cursor_rules": rules}
    for name in ROOT_NAMES:
        if _contained_file(base, root / name):
            files.append(name)
    rules_dir = root / ".cursor" / "rules"
    if rules_dir.is_dir():
        found = []
        try:
            entries = sorted(rules_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            entries = []
        for path in entries:
            if path.suffix.lower() not in RULE_SUFFIXES:
                continue
            if _contained_file(base, path):
                found.append(path.name)
        rules = found[:MAX_RULES]
    return {"root_files": files, "cursor_rules": rules}


def _named(info):
    parts = list(info["root_files"])
    if info["cursor_rules"]:
        parts.append(".cursor/rules")
    return ", ".join(parts)


def status_line(repo):
    """One operator-facing line. Never raises."""
    try:
        info = discover(repo)
    except OSError:
        return "project contract: unreadable"
    named = _named(info)
    if not named:
        return ("project contract: none "
                "(no AGENTS.md or CLAUDE.md at the repo root)")
    extra = ""
    if info["cursor_rules"]:
        extra = f" ({len(info['cursor_rules'])} rule file(s))"
    return f"project contract: {named}{extra}"


def _excerpt(repo, name):
    path = Path(repo) / name
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) <= EXCERPT_CHARS:
        return text
    return (text[:EXCERPT_CHARS].rstrip()
            + "\n…(excerpt truncated — read the file for the rest)")


def planner_block(repo):
    """Prose for a planner that is about to split a goal in ``repo``."""
    info = discover(repo)
    named = _named(info)
    if not named:
        return (
            "PROJECT CONTRACT:\n"
            "This target repo has no AGENTS.md and no CLAUDE.md at its root, "
            "and no .cursor/rules. Those files are how THIS project is "
            "structured. The orchestrator repository's AGENTS.md governs the "
            "fleet only — never copy it into the product.\n"
            "If this goal starts or reshapes the project, the first task must "
            "add an AGENTS.md stating the layout, the test command, and what "
            "agents must not change, and later tasks must depend on it. If "
            "this goal is a small change to an existing codebase, do not add "
            "a contract task; name the real files and the test command in "
            "each task prompt instead.\n\n"
        )
    parts = [
        "PROJECT CONTRACT (the target repo's own instructions, not the "
        "orchestrator's):\n",
        f"Read {named} before you split the goal. Every task prompt must "
        "carry the rules an implementer needs from them (layout, how to "
        "test, what not to touch). The implementer sees only its own prompt "
        "and the repo, never this planning conversation. Do not copy fleet "
        "governance into the product.\n",
    ]
    for name in info["root_files"]:
        excerpt = _excerpt(repo, name)
        if excerpt:
            parts.append(f"\n--- {name} (excerpt) ---\n{excerpt}\n")
    if info["cursor_rules"]:
        listed = ", ".join(info["cursor_rules"])
        parts.append(
            f"\nCursor rules in .cursor/rules: {listed}. Name the ones a "
            "task must obey; do not paste them all into every prompt.\n")
    parts.append("\n")
    return "".join(parts)


def role_block(repo, role):
    """Prose for an implementer or a reviewer working inside ``repo``."""
    info = discover(repo)
    named = _named(info)
    if role == "implementer":
        if not named:
            return (
                "Project contract: this repository has no AGENTS.md or "
                "CLAUDE.md. Follow only this task. Do not apply another "
                "repository's AGENTS.md."
            )
        return (
            f"Project contract: read {named} in this repository before "
            "editing. That is how this project is structured. Obey it. "
            "The orchestrator's AGENTS.md does not apply in this worktree."
        )
    if not named:
        return (
            "Project contract: this repository has no AGENTS.md or "
            "CLAUDE.md. Do not reject the diff for breaking a contract "
            "file that is not in the tree."
        )
    return (
        f"Project contract: this repository's structure is {named}. "
        "A diff that breaks a rule stated there is a failure of this "
        "change when the task did not explicitly override that rule. "
        "Quote the rule and the file."
    )


def captain_block(repo):
    """Short note for the captain. The planner prompt carries the excerpt."""
    line = status_line(repo)
    if line.startswith("project contract: none"):
        guidance = (
            "Tell the operator this repo has no project contract. When they "
            "are starting or reshaping it, the first plan should add "
            "AGENTS.md (layout, how to test, what not to change) before "
            "feature work. Do not put product rules in the orchestrator "
            "checkout."
        )
    else:
        guidance = (
            "Product structure lives in those files inside the target repo. "
            "When you plan, the planner reads them and must bake their rules "
            "into each task. Do not treat the orchestrator checkout's "
            "AGENTS.md as the product's."
        )
    return f"{line}\n{guidance}\n"
