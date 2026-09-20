#!/usr/bin/env python3
"""Make "adopt before you build" fire, instead of being stated and ignored.

WHY THIS EXISTS, and why it is a trigger rather than another statement of the
principle. Three artifacts in this install already said "adopt first" and all
three were inert:

  1. CC memory ``adopt_first_speed_is_a_factor`` — conditioned on "when
     recommending adopt vs adapt vs build", a moment that never arrives when the
     work goes from *gap identified* straight to *building*.
  2. ``src/genesis/skills/evaluate/SKILL.md`` — the whole framework, including an
     explicit ban on prose claims like "we already have this" in favour of an
     Overlap Comparison table. Nothing connects a candidate tool to invoking it.
  3. ``CLAUDE.md`` "Flexibility > lock-in" — governs a dependency already taken
     on, not the decision to take one on.

MEASURED 2026-09-09: a session spent a day and ~5,300 reviewed lines building a
desktop-takeover gate through six external review rounds, while a free,
open-source, actively-developed implementation of the entire capability existed
and was never searched for. Three searches were logged, all for teardowns of the
named product; zero for alternatives.

So this file adds no policy. It adds the two moments where the existing policy
gets asked for, and it points at ``/evaluate`` rather than inventing a rival
vocabulary.

TWO MODES, deliberately different strengths:

``--plan`` (PreToolUse: ExitPlanMode) — BLOCKS. A plan proposing new source
files must carry an adopt/adapt/build verdict. This is the cheap moment: the
question costs one line before any effort is spent, and it is the only moment
where the answer can still change what gets built.

``--new-file`` (PreToolUse: Write|Edit) — ADVISORY, and fires ONCE PER BRANCH.
The net for work that skipped plan mode. Advisory because a block here would
land on legitimate cognitive-core work at the worst possible moment, and the
standing design axiom is that advisory is the default while a block needs a
specific measured reason. The per-branch sentinel is the whole anti-annoyance
design: cost is proportional to how often you START work, never to how much you
type, so it cannot become the kind of noise you learn to tune out.

Neither mode ever ASKS the user for approval — both demand WORK (a recorded
verdict), which a background session can satisfy alone. Background stays exactly
as capable as foreground.

Fails OPEN on any internal error: a crash here must not wedge planning or
editing. Nothing in this file is a safety boundary.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Self-locate so hook_input resolves whether run as a script or imported (tests).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from hook_input import field, read_payload, tool_input  # noqa: E402
except Exception:  # noqa: BLE001 — this convenience hook is deliberately fail-open.
    if __name__ != "__main__":
        raise
    try:
        sys.stderr.write(
            "GUARD DEGRADED (adopt_first_gate): shared hook_input is unavailable; "
            "skipping this non-safety hook.\n"
        )
        sys.stderr.flush()
    except BaseException:  # noqa: BLE001 — a broken diagnostic stream cannot block work.
        pass
    os._exit(0)

_HOME_DIR = Path(os.environ.get("HOME") or Path.home())
_STATE_DIR = _HOME_DIR / ".genesis" / "adopt_first"

#: A plan "proposes new source files" if it names a source path. Deliberately
#: NOT a prose heuristic ("create", "new file") — those fire on any plan that
#: discusses files at all, and a gate that fires on everything is one you learn
#: to ack past. A concrete path is the honest signal that code is coming.
_SOURCE_EXTENSIONS = frozenset(
    {"py", "js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts", "html", "htm", "css"}
)
_SOURCE_EXTENSION_PATTERN = "|".join(sorted(_SOURCE_EXTENSIONS))
_SOURCE_PATH = re.compile(
    rf"(?<![\w./\\:-])(?P<path>(?:(?:[A-Za-z]:)?[\\/](?:[^\\/`'\"<>\r\n]+[\\/])*|\.[\\/])?"
    rf"src[\\/]genesis[\\/](?:[\w.-]+[\\/])*[\w.-]+\.(?:{_SOURCE_EXTENSION_PATTERN}))"
    rf"(?=$|[\s`'\"),;:!?\]]|\.(?=$|[\s]))",
    re.IGNORECASE,
)

#: The verdict header, tolerant of the spellings a writer will actually use:
#: "## Adopt / Adapt / Build", "## Adopt/Adapt/Build", "### ADOPT vs ADAPT vs BUILD".
_VERDICT_HEADER = re.compile(
    r"^(?P<hash>#{1,6})[ \t]+adopt[ \t]*(?:"
    r"(?:/[ \t]*|\|[ \t]*|-[ \t]*|vs\.?[ \t]+|versus[ \t]+)"
    r"adapt[ \t]*(?:/[ \t]*|\|[ \t]*|-[ \t]*|vs\.?[ \t]+|versus[ \t]+)"
    r"build|"
    r"(?:/[ \t]*|\|[ \t]*|-[ \t]*|vs\.?[ \t]+|versus[ \t]+)build"
    r")[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

#: The evaluate skill's vocabulary, matched in the section BODY only.
#:
#: An earlier version scanned the whole document and leaned on case-sensitivity
#: to stop the heading satisfying its own requirement. That reasoning was wrong
#: in both directions and both were reproduced: "### ADOPT vs BUILD" followed by
#: "(tbd)" PASSED (the all-caps heading is itself a matching token), while a
#: genuine lower-case prose verdict under "## Adopt / Adapt / Build" was BLOCKED.
#: Slicing the body first is what makes the question well-posed, so this can now
#: be case-insensitive and mean what it says.
_VERDICT_LINE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?"
    r"(?:(?:verdict|decision|disposition)[ \t]*(?::|=|-)[ \t]*)?"
    r"(?P<decision>ADOPT|ADAPT|BUILD)\b(?P<rest>.*)$",
    re.IGNORECASE,
)
_PROSE_VERDICT_LINE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?we[ \t]+"
    r"(?:should|will|recommend|are[ \t]+going[ \t]+to)[ \t]+"
    r"(?P<decision>ADOPT|ADAPT|BUILD)\b(?P<rest>.*)$",
    re.IGNORECASE,
)
_UNDECIDED = re.compile(
    r"(?:\b(?:TODO|TBD|TBC|undecided)\b|"
    r"\bnot[ \t]+(?:yet[ \t]+)?decided\b|<[^>\n]+>)",
    re.IGNORECASE,
)
# A second all-caps disposition is an alternative; lowercase verbs in a rationale
# ("or build an index") describe capability, not a competing decision.
_NEGATED_DECISION = re.compile(
    r"\b(?:do[ \t]+not|does[ \t]+not|did[ \t]+not|don't|doesn't|didn't|"
    r"not|never|cannot|can't|will[ \t]+not|won't|should[ \t]+not|shouldn't)"
    r"[ \t]+(?P<decision>ADOPT|ADAPT|BUILD)\b",
    re.IGNORECASE,
)
_AMBIGUOUS_VERDICT = re.compile(
    r"(?i:^\s*(?:[/|]\s*|vs\.?\s+|versus\s+))"
    r"(?:ADOPT|ADAPT|BUILD)\b|"
    r"\b(?:ADOPT|ADAPT|BUILD)\s*[/|]\s*(?:ADOPT|ADAPT|BUILD)\b|"
    r"(?i:\b(?:or|versus|vs\.?)\b)[^\n]*\b(?:ADOPT|ADAPT|BUILD)\b",
)
_FENCE_OPEN = re.compile(r"^(?P<indent> {0,3})(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
_FENCE_CLOSE = re.compile(r"^[ ]{0,3}(?P<marker>`{3,}|~{3,})[ \t]*$")
_DIVIDER = r"(?:[═=─━—–-][ \t]*){3,}"
_SUPERSEDED_DIVIDER = re.compile(
    rf"^[ ]{{0,3}}(?:#{{1,6}}[ \t]*)?(?:{_DIVIDER}[ \t]*)?"
    r"(?:SUPERSEDED(?:[ \t]+BELOW)?|ARCHIVED(?:[ \t]+BELOW)?)[ \t]*"
    rf"(?:{_DIVIDER})?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_UNRESOLVED_FOLLOWUP = re.compile(
    r"\b(?:TODO|TBD|TBC)\b",
    re.IGNORECASE,
)
_UNRESOLVED_STATUS = re.compile(
    r"^[ \t]*(?:(?:[-*+]|[0-9]+[.)])[ \t]+)?[*_`]*"
    r"(?:decision[ \t]+status|status|decision|verdict)[*_`]*[ \t]*"
    r"(?::|=|-)[*_`]*[ \t]*(?:pending|undecided|not[ \t]+(?:yet[ \t]+)?decided)\b",
    re.IGNORECASE | re.MULTILINE,
)
_PLACEHOLDER_EVIDENCE_PART = (
    r"(?:TODO|TBD|TBC|N/?A|[-—]|<[^>\r\n]+>|\[[^\]\r\n]+\])"
)
_PLACEHOLDER_EVIDENCE = re.compile(
    rf"{_PLACEHOLDER_EVIDENCE_PART}"
    rf"(?:[ \t,;:.—-]+{_PLACEHOLDER_EVIDENCE_PART})*\W*",
    re.IGNORECASE,
)
_EVIDENCE_LABEL = (
    r"(?:searched|search[ \t_-]+evidence|found|findings?|discovery|discoveries|"
    r"results?|time[ \t_-]+to[ \t_-]+capability)"
)
_UNRESOLVED_EVIDENCE = re.compile(
    r"(?:"
    r"\b(?:not|never)[ \t]+(?:yet[ \t]+)?(?:been[ \t]+)?"
    r"(?:evaluated|assessed|reviewed|checked|verified|determined)\b"
    r"|\b(?:pending|awaiting)(?:[ \t]+\w+){0,3}[ \t]+"
    r"(?:evaluation|assessment|review|verification|determination)\b"
    r"|\b(?:to[ \t]+be|yet[ \t]+to[ \t]+be)[ \t]+"
    r"(?:evaluated|assessed|reviewed|checked|verified|determined)\b"
    r"|\b(?:will|plan(?:s)?[ \t]+to|intend(?:s)?[ \t]+to|need(?:s)?[ \t]+to)[ \t]+"
    r"(?:evaluate|assess|review|check|verify|determine)\b"
    r")",
    re.IGNORECASE,
)

_GATED_PREFIX = "src/genesis/"


# ── state: one record per (worktree, branch) ─────────────────────────────────
def _run(args: list[str], cwd: str | None = None) -> str:
    """Run a bounded Git query without honoring ambient repository overrides."""
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        env.pop(key, None)
    try:
        out = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _worktree_key(cwd: str) -> str:
    """sha256-truncated worktree root, mirroring ``review_state._worktree_key``.

    Per-location on purpose. A shared fallback constant is what let concurrent
    sessions clobber each other's state in the review markers (#1244); the same
    trap applies here, so the same fix is used rather than a new one.
    """
    root = _run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if not root:
        probe = Path(cwd).resolve()
        for parent in [probe, *probe.parents]:
            if (parent / ".git").exists():
                root = str(parent)
                break
        else:
            root = str(probe)
    return hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:12]


def _branch(cwd: str) -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd) or "unknown"


def _sentinel(cwd: str) -> Path:
    """One empty file per (worktree, branch). Existence IS the state.

    An empty sentinel rather than a JSON dict, for two reasons. It matches what
    three sibling hooks already do for once-per-X nudges
    (``agent_tool_guidance``, ``stealth_skill_nudge``, ``subsystem_traps_hook``)
    — adding a fifth mechanism to a change whose whole subject is "stop
    reinventing what exists" would have been the funniest possible defect. And
    it is ATOMIC: the previous JSON version did a read-modify-write, and several
    sessions run concurrently on this box, so two starting work at once could
    lose one another's record.

    The branch is hashed rather than slugged because branch names contain "/".
    """
    branch = hashlib.sha256(_branch(cwd).encode()).hexdigest()[:8]
    return _STATE_DIR / f"{_worktree_key(cwd)}-{branch}"


def _already_nudged(cwd: str) -> bool:
    try:
        return _sentinel(cwd).exists()
    except OSError:
        return False


def _mark_nudged(cwd: str) -> bool:
    """Claim the branch sentinel, returning ``True`` only for its creator."""
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        # O_EXCL: two concurrent sessions race harmlessly, one wins, neither errors.
        fd = os.open(_sentinel(cwd), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with contextlib.suppress(OSError):
            os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return False


# ── plan resolution ──────────────────────────────────────────────────────────
def _plan_path(payload: dict) -> str:
    """Resolve the plan without guessing from another session's mtime.

    Claude Code normally supplies ``planFilePath``. Older payloads may carry
    the full plan text instead; that is safe only when it exactly matches one
    plan file. An absent or ambiguous association fails open.
    """
    inputs = tool_input(payload)
    # The AUTHORITATIVE field first. MEASURED across 1,862 real ExitPlanMode
    # payloads on this install: `planFilePath` was present in 1862/1862. The
    # inline-content fallback below is deliberately exact-match only; guessing
    # from a JSON blob or global mtime can select another session's document.
    for key in ("planFilePath", "plan_file_path"):
        direct = inputs.get(key) or payload.get(key)
        if isinstance(direct, str) and direct.endswith(".md"):
            return direct
    inline = inputs.get("plan") or payload.get("plan")
    if not isinstance(inline, str) or not inline.strip():
        return ""
    inline_path = Path(inline)
    if inline_path.suffix.lower() == ".md" and inline_path.is_file():
        return str(inline_path)

    plans = _HOME_DIR / ".claude" / "plans"
    matches: list[Path] = []
    try:
        for candidate in plans.glob("*.md"):
            try:
                if candidate.read_text(encoding="utf-8") == inline:
                    matches.append(candidate)
            except OSError:
                continue
    except OSError:
        return ""
    if len(matches) == 1:
        return str(matches[0])
    return ""


def _repo_root(payload: dict) -> Path | None:
    """Where to resolve the plan's source paths from, or None if unknowable.

    The plan writes repo-relative paths (`src/genesis/x.py`), so they resolve
    against the repo root, not the process cwd — which for a hook is whatever
    directory the tool call happened to be made from.

    RETURNS None RATHER THAN GUESSING, and the caller then refuses to block.
    An earlier version fell back to ``Path(cwd)``, which looked harmless and was
    the worst defect in this file: under a git timeout, a detached/broken repo,
    or simply a cwd outside the tree, EVERY source path resolves under a
    directory with no `src/`, every one reads as "does not exist yet", and the
    gate blocks every plan it sees. MEASURED: the plan "fix a bug in
    src/genesis/memory/retrieval.py" exited 2 from a non-repo cwd and 0 from the
    repo. That is both a silent reversion to the 65.9% fire rate this design
    exists to avoid, and a direct contradiction of the fail-open invariant this
    module claims in its own docstring.

    The `src/genesis` probe is the load-bearing half: a root that resolves but
    has no source tree cannot answer "does this file exist yet", so it is
    unknowable too.
    """
    cwd = payload.get("cwd") or os.getcwd()
    root = _run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if not root:
        return None
    try:
        candidate = Path(root).resolve()
        source_root = (candidate / "src" / "genesis").resolve()
        source_root.relative_to(candidate)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate if source_root.is_dir() else None


_REMEDY = (
    "Add a section to the plan before presenting it:\n"
    "\n"
    "  ## Adopt / Adapt / Build\n"
    "  <ADOPT|ADAPT|BUILD> — <why>. Searched: <terms>. Found: <candidates, or none>.\n"
    "  Time-to-capability: adopt <hours> vs build <hours>.\n"
    "\n"
    "Use the `evaluate` skill's vocabulary (ADOPT | WATCH | IGNORE | ADAPT) — run\n"
    "`/evaluate` on any candidate rather than inventing a fresh comparison. Its\n"
    'Overlap Comparison table exists specifically to replace the sentence "we\n'
    'already have this", which is the phrasing this gate is here to catch.\n'
    "\n"
    "A compact record is enough when building really is right:\n"
    "  BUILD — cognitive core, no external substitute.\n"
    "  Search evidence: <actual terms or sources>.\n"
    "  Discovery: <actual candidates/results, or none applicable>.\n"
    "  Time to capability: <concrete duration>.\n"
    "\n"
    'But RUN THE SEARCH first. "Nothing adoptable exists" is a claim that needs a\n'
    "logged search behind it, and the measured failure this gate is made of had\n"
    "three searches logged for the named product and ZERO for alternatives."
)


def _without_fenced_blocks(content: str) -> str:
    """Blank fenced Markdown blocks, including valid unclosed blocks."""
    lines = content.splitlines()
    visible: list[str] = []
    active: tuple[str, int] | None = None
    for line in lines:
        if active is not None:
            closing = _FENCE_CLOSE.match(line)
            if closing:
                marker = closing.group("marker")
                if marker[0] == active[0] and len(marker) >= active[1]:
                    active = None
            visible.append("")
            continue
        opening = _FENCE_OPEN.match(line)
        if opening:
            marker = opening.group("marker")
            active = (marker[0], len(marker))
            visible.append("")
        else:
            visible.append(line)
    return "\n".join(visible)


def _live_plan_content(content: str) -> str:
    """Return live plan text, excluding fenced examples and old archaeology."""
    visible = _without_fenced_blocks(content)
    divider = _SUPERSEDED_DIVIDER.search(visible)
    return visible[: divider.start()] if divider else visible


def _section_body(content: str, header: re.Match[str]) -> str:
    """Extract one verdict section body up to the next same-level heading."""
    depth = len(header.group("hash"))
    line_end = content.find("\n", header.end())
    rest = content[line_end + 1 :] if line_end != -1 else ""
    next_heading = re.search(rf"^[ ]{{0,3}}#{{1,{depth}}}[ \t]+", rest, re.MULTILINE)
    return rest[: next_heading.start()] if next_heading else rest


def _clean_verdict_line(line: str) -> str:
    """Remove harmless Markdown emphasis before parsing a decision line."""
    return re.sub(r"[`*_]", "", line)


def _decision_from_body(body: str) -> str | None:
    """Parse exactly one concrete ADOPT/ADAPT/BUILD disposition."""
    decisions: list[tuple[str, str]] = []
    for raw_line in body.splitlines():
        line = _clean_verdict_line(raw_line)
        match = _VERDICT_LINE.match(line) or _PROSE_VERDICT_LINE.match(line)
        if not match:
            continue
        decision = match.group("decision").upper()
        rest = match.group("rest").strip()
        evidence_field = re.search(
            rf"(?<!\w)[ \t]+{_EVIDENCE_LABEL}\s*:",
            rest,
            re.IGNORECASE,
        )
        if evidence_field:
            rest = rest[: evidence_field.start()].strip()
        negated = any(
            match.group("decision").upper() == decision
            for match in _NEGATED_DECISION.finditer(rest)
        )
        if (
            _UNDECIDED.search(rest)
            or _AMBIGUOUS_VERDICT.search(rest)
            or negated
        ):
            return None
        if decision != "BUILD" and not any(character.isalnum() for character in rest):
            return None
        decisions.append((decision, rest))
    if len(decisions) != 1:
        return None
    return decisions[0][0]


def _has_build_evidence(body: str) -> bool:
    """Require non-placeholder search, findings, and time-to-capability fields."""
    field_start = (
        r"(?:^|(?<=[.!?;,])[ \t]+)[ \t]*(?:[-*+][ \t]+)?[*_`]*"
    )
    field_end = rf"(?=[ \t]+[*_`]*{_EVIDENCE_LABEL}[*_`]*\s*:|[\r\n]|$)"
    labels = {
        "searched": re.compile(
            rf"{field_start}(?:searched|search[ \t_-]+evidence)[*_`]*\s*:\s*(.*?){field_end}",
            re.IGNORECASE | re.MULTILINE,
        ),
        "found": re.compile(
            rf"{field_start}(?:found|findings?|discovery|discoveries|results?)[*_`]*\s*:\s*(.*?){field_end}",
            re.IGNORECASE | re.MULTILINE,
        ),
        "time": re.compile(
            rf"{field_start}time[ \t_-]+to[ \t_-]+capability[*_`]*\s*:\s*([^\r\n]*)",
            re.IGNORECASE | re.MULTILINE,
        ),
    }
    values: dict[str, str] = {}
    for name, pattern in labels.items():
        match = pattern.search(body)
        if not match:
            return False
        value = match.group(1).strip(" `*_\t")
        if (
            not value
            or not any(character.isalnum() for character in value)
            or _PLACEHOLDER_EVIDENCE.fullmatch(value)
            or _UNRESOLVED_EVIDENCE.search(value)
        ):
            return False
        values[name] = value

    if re.fullmatch(
        r"(?:none|n/?a|not searched|no searches?|no search terms?|[-—])\W*",
        values["searched"],
        re.I,
    ) or re.match(
        r"^(?:will|plan(?:s)? to|intend(?:s)? to|need to) search\b|"
        r"^(?:not|have not|haven't) (?:yet )?searched\b",
        values["searched"],
        re.I,
    ):
        return False
    return bool(
        re.search(
            r"\b\d+(?:\.\d+)?[ \t]*(?:m|mins?|minutes?|h|hrs?|hours?|"
            r"d|days?|w|wks?|weeks?|mo|months?)\b",
            values["time"],
            re.IGNORECASE,
        )
    )


def _has_verdict(content: str) -> bool:
    """Return whether the live verdict section contains a valid disposition."""
    headers = list(_VERDICT_HEADER.finditer(content))
    if len(headers) != 1:
        return False
    body = _section_body(content, headers[0])
    if _UNRESOLVED_FOLLOWUP.search(body) or _UNRESOLVED_STATUS.search(body):
        return False
    decision = _decision_from_body(body)
    return bool(decision and (decision != "BUILD" or _has_build_evidence(body)))


def _repo_relative_source_path(path: str, root: Path) -> str | None:
    """Normalize a source path and reject absolute paths outside this repo."""
    try:
        root = root.resolve()
        candidate = Path(path.replace("\\", os.sep))
        if not candidate.is_absolute():
            candidate = root / candidate
        normalized = candidate.resolve().relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None
    return normalized if normalized.startswith(_GATED_PREFIX) else None


def _extract_source_paths(content: str, root: Path) -> list[str]:
    """Extract executable source paths from already-filtered live plan text."""
    paths = {
        relative
        for match in _SOURCE_PATH.finditer(content)
        if (relative := _repo_relative_source_path(match.group("path"), root)) is not None
    }
    return sorted(paths)


def _planned_rename_destinations(content: str, root: Path) -> set[str]:
    """Return destinations of explicit moves whose source already exists."""
    destinations: set[str] = set()
    for line in content.splitlines():
        matches = [
            (match, relative)
            for match in _SOURCE_PATH.finditer(line)
            if (relative := _repo_relative_source_path(match.group("path"), root)) is not None
        ]
        if len(matches) < 2:
            continue
        first, old = matches[0]
        second, new = matches[1]
        connector = line[first.end() : second.start()]
        separator = connector.strip("`*_ \t")
        explicit_pair = separator.casefold() in {"to", "->", "→"}
        command_prefix = line[: first.start()].strip("`*_ \t")
        shell_move = bool(
            re.search(r"(?:^|[ \t])(?:git[ \t]+mv|mv)$", command_prefix, re.IGNORECASE)
        ) and not separator
        if not explicit_pair and not shell_move:
            continue
        if (root / old).exists():
            destinations.add(new)
    return destinations


def _check_plan(payload: dict) -> int:
    """Block a new-source plan unless its live section records a disposition."""
    plan_path = _plan_path(payload)
    if not plan_path:
        return 0  # nothing to read — never block on our own inability to find it
    try:
        content = Path(plan_path).read_text(encoding="utf-8")
    except OSError:
        return 0

    live = _live_plan_content(content)
    root = _repo_root(payload)
    if root is None:
        return 0  # cannot tell new from existing — never block on our own blindness
    named = _extract_source_paths(live, root)
    if not named:
        return 0  # proposes no source files — not this gate's business

    # Only files that do NOT YET EXIST. Adopt-vs-build is a question about NEW
    # capability; "fix a bug in src/genesis/y.py" is not that question.
    #
    # MEASURED against 205 real plans: the path-only trigger fired on 135
    # (65.9%) — two of every three, mostly ordinary fixes to existing modules.
    # A gate firing that often gets acked reflexively, which is the same as no
    # gate at all.
    #
    # The honest rate for THIS filter is ~20%, measured POINT-IN-TIME via
    # `git log --diff-filter=A` — i.e. was the file new *when the plan was
    # written*. Two independent passes with slightly different "new at the time"
    # rules got 41/205 and 42/205, and that one-plan spread is the real
    # precision of the number. Replaying against today's tree instead gives
    # 28/205 (13.7%), which is biased LOW because every proposal that actually
    # got built now scores as "nothing new" — and 13.7% is exactly the figure a
    # reader would naively reproduce and wrongly trust, which is why both are
    # recorded here.
    renamed = _planned_rename_destinations(live, root)
    sources = [s for s in named if s not in renamed and not (root / s).exists()]
    if not sources:
        return 0

    if _has_verdict(live):
        return 0

    shown = ", ".join(sources[:4]) + (" …" if len(sources) > 4 else "")
    print(
        "BLOCKED (adopt-first): this plan proposes source files "
        f"({len(sources)}: {shown}) and carries no adopt/adapt/build verdict.\n"
        "\n"
        "This is the cheap moment to ask. The question costs one line here and a\n"
        "day of rework later — MEASURED 2026-09-09: ~5,300 reviewed lines and six\n"
        "external review rounds building a capability that already existed, free\n"
        "and open-source, and was never searched for.\n"
        "\n" + _REMEDY,
        file=sys.stderr,
    )
    return 2


def _check_new_file(payload: dict) -> int:
    """Emit one advisory for a genuinely new module in this repository."""
    raw = field(payload, "file_path")
    if not raw:
        return 0
    inputs = tool_input(payload)
    try:
        path = Path(raw)
        root = _repo_root(payload)
        if root is None:
            return 0
        root = root.resolve()
        path = (root / path if not path.is_absolute() else path).resolve()
        source_root = (root / _GATED_PREFIX).resolve()
        source_root.relative_to(root)
        path.relative_to(source_root)
        path.relative_to(root)
        if path.suffix[1:].lower() not in _SOURCE_EXTENSIONS:
            return 0
        old_raw = inputs.get("old_path") or inputs.get("source_path")
        if isinstance(old_raw, str) and old_raw:
            old_path = Path(old_raw)
            old_path = (root / old_path if not old_path.is_absolute() else old_path).resolve()
            old_path.relative_to(root)
            old_path.relative_to(source_root)
            if old_path.exists():
                return 0
    except (OSError, RuntimeError, ValueError):
        return 0

    posix = path.as_posix()
    if path.exists():
        return 0  # an edit to existing code, not a new module

    cwd = payload.get("cwd") or os.getcwd()
    if _already_nudged(cwd):
        return 0  # once per branch — this is the whole anti-annoyance design

    if not _mark_nudged(cwd):
        return 0
    nudge = (
        f"New module: {posix}\n"
        "Before building it: is there something to adopt? Default order is "
        "ADOPT > ADAPT > build, and the effort belongs in the GLUE around what "
        "already exists. Run `/evaluate` on any candidate; compare user-visible "
        'CAPABILITY, not architectural depth — "ours is more sophisticated" is a '
        "reason to upgrade, never a reason to build.\n"
        "If you already recorded a verdict, ignore this: it fires once per branch, "
        "not once per file."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": nudge,
                }
            }
        )
    )
    return 0


def main() -> int:
    """Dispatch the selected hook mode and fail open on internal errors."""
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = read_payload()
        if not payload:
            return 0
        if mode == "--plan":
            return _check_plan(payload)
        if mode == "--new-file":
            return _check_new_file(payload)
    except Exception:  # noqa: BLE001 — fail open; this is not a safety boundary
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
