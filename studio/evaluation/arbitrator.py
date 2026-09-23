"""Module C3: stop the loop when the judges are arguing with each other.

A rotating panel of judges can disagree in two very different ways, and the
difference decides whether the loop is working or broken:

  * A CHANGE OF MIND — round 1 says the corridor is too dark, round 2 (after
    it was brightened) says the shadows now read flat. That is progress: the
    second note is about the state the first note produced.

  * OSCILLATION — round 1 says brighten the corridor, round 2 says darken the
    corridor, round 3 says brighten it again. Nothing is converging. The model
    is burning rounds and money swinging between two judges' tastes, and no
    number of further rounds will settle it because the disagreement is not
    about the build, it is about the target.

The second one is the failure this module exists to catch. Left alone it is
expensive and silent: every round looks productive in isolation, the score
hovers, and the run ends on the round budget with the same defect it started
with. Three alternations is the smallest window that can tell them apart —
A, B is a change of mind; A, B, A is an argument.

The arbitrator does not resolve the argument. It HALTS and names the conflict,
because a machine that picks a winner between two judges has just become a
third judge with no better information than either.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import config

# Opposing directive vocabulary, grouped by AXIS rather than by word pair.
#
# Pairs were the obvious encoding and they are wrong: "brighten/darken" and
# "lighten/darken" overlap, so the second pair reassigned "darken" to its own
# axis and the two halves of the same disagreement stopped matching. An axis
# is the thing being argued about; the words are just the two directions along
# it.
#
# "magnitude" is deliberately broad — increase/decrease, add/remove,
# bigger/smaller all live there — because the SUBJECT is what distinguishes
# "more grime" from "more light". Conflict requires the same axis AND the same
# subject, so a broad axis costs nothing and a missing one costs a detection.
_AXES = {
    "magnitude": (
        ("increase", "more", "add", "raise", "higher", "up", "stronger",
         "boost", "expand", "extend", "enlarge", "bigger", "larger", "longer",
         "widen", "wider", "emphasise", "emphasize", "heighten"),
        ("decrease", "less", "remove", "lower", "down", "weaker", "cut",
         "reduce", "shorten", "shrink", "smaller", "shorter", "narrow",
         "narrower", "downplay", "flatten"),
    ),
    "brightness": (
        ("brighten", "brighter", "lighten", "lighter"),
        ("darken", "darker", "dim", "dimmer"),
    ),
    "temperature": (("warmer", "warm"), ("cooler", "cool", "colder")),
    "saturation": (("saturate", "saturated", "vivid"),
                   ("desaturate", "desaturated", "muted")),
    "softness": (("soften", "softer", "blur"), ("sharpen", "sharper", "crisper")),
    "tightness": (("tighten", "tighter"), ("loosen", "looser")),
    "speed": (("faster", "quicker"), ("slower")),
    "density": (("denser", "busier", "cluttered"), ("sparser", "cleaner", "emptier")),
}
_POLARITY = {}
for _axis, (_pos, _neg) in _AXES.items():
    for _w in _pos:
        _POLARITY[_w] = (_axis, 1)
    for _w in _neg:
        _POLARITY.setdefault(_w, (_axis, -1))

_STOPWORDS = frozenset("""
a an the is are be was were to of in on at for with and or but it its this that
these those should must need needs please make made too very more less much
some any all not no down up by from as into over under
""".split())


@dataclass(frozen=True)
class Directive:
    """One instruction from a judge, reduced to (subject, axis, polarity)."""
    text: str
    subject: frozenset
    axis: str = ""
    polarity: int = 0

    @property
    def directional(self):
        return bool(self.axis)


def parse_directive(text):
    """Reduce a directive to something two rounds can be compared on."""
    words = re.findall(r"[a-z]+", (text or "").lower())
    axis, polarity, subject = "", 0, []
    for w in words:
        if not axis and w in _POLARITY:
            axis, polarity = _POLARITY[w]
            continue
        if w in _STOPWORDS or w in _POLARITY:
            continue
        subject.append(w)
    return Directive(text=(text or "").strip(), subject=frozenset(subject),
                     axis=axis, polarity=polarity)


# The things a judge actually gives directives ABOUT. A shared word from this
# set is strong evidence two directives concern the same thing; shared filler
# ("again", "slightly", "bit") is not.
#
# This list is why subject matching is not pure word overlap. Real directives
# are short and rephrase freely — "brighten the cell corridor lighting" and
# "brighten the corridor again, too murky" share exactly one word in three,
# which no sane ratio threshold accepts, and they are obviously the same
# argument. Anchoring on domain nouns catches that without matching every
# pair of sentences that happen to share "the".
SCENE_NOUNS = frozenset("""
corridor cell cells vent vents shaft shafts tower towers yard wall walls fence
door doors floor floors ceiling ceilings window windows roof stair stairs
light lighting lights shadow shadows lamp lamps searchlight spotlight
material materials texture textures palette colour color contrast silhouette
geometry mesh meshes topology normals uv seam seams
guard guards inmate inmates prisoner prisoners character characters
block perimeter gate gates bunk bunks desk desks bars grime rust dirt
fog haze dust alarm siren clock timer hud ui meter stamina suspicion noise
rig rigging skeleton armature animation animations clip clips pose weight
camera framing composition sky skybox horizon
""".split())


def same_subject(a, b, threshold=0.5):
    """Do two directives talk about the same thing?

    Two tests, in order of strength:

      1. They share a SCENE NOUN — the thing being argued about is named in
         both. This is the one that fires in practice.
      2. Failing that, generic word overlap above `threshold`, which catches
         subjects this module has no vocabulary for (a bespoke mechanic, a
         proper noun) at the cost of being easier to fool.
    """
    if not a.subject or not b.subject:
        return False
    shared = a.subject & b.subject
    if not shared:
        return False
    if shared & SCENE_NOUNS:
        return True
    return len(shared) / float(min(len(a.subject), len(b.subject))) >= threshold


def conflicts(a, b):
    """True when `a` and `b` demand opposite things about the same subject."""
    if not (a.directional and b.directional):
        return False
    if a.axis != b.axis:
        return False
    if a.polarity == 0 or b.polarity == 0 or a.polarity == b.polarity:
        return False
    return same_subject(a, b)


@dataclass
class Arbitration:
    halt: bool = False
    reason: str = ""
    conflicts: list = field(default_factory=list)
    rounds_examined: int = 0

    def to_dict(self):
        return {"halt": self.halt, "reason": self.reason,
                "conflicts": list(self.conflicts),
                "rounds_examined": self.rounds_examined}


def assess(rounds, *, window=None):
    """Decide whether the judge loop is oscillating.

    `rounds` is an ordered list of (round_number, [directive strings]) — oldest
    first. Returns an Arbitration; `halt` means stop the loop and escalate to
    a human, with `reason` written to be read by one.
    """
    window = config.STUDIO_OSCILLATION_ROUNDS if window is None else window
    rounds = [(n, [parse_directive(d) for d in ds]) for n, ds in rounds]
    found = []
    # Walk consecutive round pairs; a pair conflicts when any directive in the
    # later round contradicts any directive in the earlier one.
    streak = []
    for (n0, ds0), (n1, ds1) in zip(rounds, rounds[1:]):
        pair = None
        for d0 in ds0:
            for d1 in ds1:
                if conflicts(d0, d1):
                    pair = {"from_round": n0, "to_round": n1,
                            "axis": d0.axis,
                            "earlier": d0.text, "later": d1.text}
                    break
            if pair:
                break
        if pair:
            streak.append(pair)
            found.append(pair)
            # `window` rounds of argument means window-1 conflicting
            # transitions between them.
            if len(streak) >= max(1, window - 1):
                return Arbitration(
                    halt=True,
                    reason=(
                        f"judges have contradicted each other on '{pair['axis']}' "
                        f"across {len(streak) + 1} consecutive rounds "
                        f"(rounds {streak[0]['from_round']}-{pair['to_round']}). "
                        "The disagreement is about the target, not the build, "
                        "so further rounds will not converge. Resolve the "
                        "target with a human, then reset the baseline."),
                    conflicts=found,
                    rounds_examined=len(rounds))
        else:
            streak = []
    return Arbitration(halt=False, reason="", conflicts=found,
                       rounds_examined=len(rounds))
