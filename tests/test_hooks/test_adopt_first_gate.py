"""The adopt-first gate: does the trigger actually fire, and does it stay quiet.

The gate exists because the POLICY already existed three times over and was
inert every time (see the hook's module docstring). So the thing under test is
not "is adopt-first stated" — it is "does the question get ASKED at the two
moments where it is cheap to answer, and does it then shut up."

Two properties carry the design:

* the plan gate BLOCKS, because a plan proposing source files is the last moment
  the answer can still change what gets built;
* the new-file gate is ADVISORY and fires ONCE PER BRANCH, because a nudge that
  fires per-file is one you learn to tune out — and a tuned-out gate is
  indistinguishable from no gate at all, which is exactly the failure being fixed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "adopt_first_gate.py"


def _run(
    mode: str,
    payload: dict,
    home: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the hook exactly as Claude Code does: argv mode, JSON on stdin."""
    body = {"hook_event_name": "PreToolUse", "session_id": "test", **payload}
    env = {**os.environ, "HOME": str(home), **(extra_env or {})}
    return subprocess.run(
        [sys.executable, str(_HOOK), mode],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A throwaway HOME so the real ~/.genesis state is never touched."""
    h = tmp_path / "home"
    (h / ".claude" / "plans").mkdir(parents=True)
    return h


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo — the new-file gate keys its state on worktree + branch."""
    r = tmp_path / "repo"
    (r / "src" / "genesis" / "autonomy").mkdir(parents=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(args, cwd=r, check=True, capture_output=True)
    return r


def _plan(home: Path, text: str) -> Path:
    p = home / ".claude" / "plans" / "plan.md"
    p.write_text(text, encoding="utf-8")
    return p


def _plan_payload(path: Path) -> dict:
    """The REAL ExitPlanMode shape, not a convenient one.

    MEASURED across 1,862 live ExitPlanMode payloads on this install:
    `planFilePath` is present in 1862/1862, and `plan` carries the full markdown
    and serializes FIRST. An earlier version of this harness passed only
    ``{"plan": <path>}`` — a shape CC never sends — so the production resolution
    path went unexercised and five real defects sat green under a 15/15 suite.
    Passing both fields is what makes these tests evidence."""
    return {
        "tool_name": "ExitPlanMode",
        "tool_input": {
            "plan": path.read_text(encoding="utf-8"),
            "planFilePath": str(path),
        },
    }


def test_missing_hook_input_fails_open_at_import_time(tmp_path: Path):
    """This guidance hook skips cleanly when its shared parser cannot load."""
    hook_dir = tmp_path / "broken-hooks"
    hook_dir.mkdir()
    hook = hook_dir / "adopt_first_gate.py"
    shutil.copy2(_HOOK, hook)
    (hook_dir / "hook_input.py").write_text(
        'raise RuntimeError("poisoned helper")\n', encoding="utf-8"
    )
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        [sys.executable, str(hook), "--plan"],
        input="{}",
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**os.environ, "HOME": str(home)},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "GUARD DEGRADED (adopt_first_gate)" in result.stderr


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the launcher is a POSIX Bash entrypoint",
)
@pytest.mark.parametrize("mode", ["--plan", "--new-file"])
def test_settings_launches_each_mode(mode: str, home: Path, repo: Path):
    root = _HOOK.parents[2]
    # CI installs Python globally; give the real launcher its install layout
    # without depending on a developer checkout's .venv or main worktree.
    (repo / ".claude/hooks").mkdir(parents=True)
    shutil.copy2(root / ".claude/hooks/genesis-hook", repo / ".claude/hooks/genesis-hook")
    (repo / "scripts/hooks").mkdir(parents=True)
    for name in ("adopt_first_gate.py", "hook_input.py"):
        shutil.copy2(root / "scripts/hooks" / name, repo / "scripts/hooks" / name)
    (repo / ".venv/bin").mkdir(parents=True)
    shutil.copy2(sys.executable, repo / ".venv/bin/python")
    settings = json.loads((root / ".claude/settings.json").read_text())
    commands = [
        hook["command"]
        for entry in settings["hooks"]["PreToolUse"]
        for hook in entry.get("hooks", [])
        if "adopt_first_gate.py" in hook.get("command", "") and hook["command"].endswith(mode)
    ]
    assert len(commands) == 1
    plan = _plan(home, "Create src/genesis/new_capability.py")
    payload = {"cwd": str(repo), **_plan_payload(plan)}
    if mode == "--new-file":
        payload = {"cwd": str(repo), "tool_input": {"file_path": str(repo / "src/genesis/new.py")}}
    result = subprocess.run(
        ["bash", "-c", commands[0]],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home), "CLAUDE_PROJECT_DIR": str(repo)},
    )
    if mode == "--plan":
        assert result.returncode == 2, result.stderr
        assert "BLOCKED (adopt-first)" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "New module:" in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


# ── the plan gate ────────────────────────────────────────────────────────────


def test_the_acceptance_bar_replay_the_real_defect(home: Path, repo: Path):
    """THE case this gate is made of, reconstructed.

    2026-09-09: a plan proposing `src/genesis/autonomy/desktop_gate.py` was
    approved with no adopt/build verdict and no search for alternatives, and the
    session then spent a day and ~5,300 reviewed lines building a capability
    that already existed free and open-source. If the gate does not catch this
    exact shape it does not ship, whatever else it passes."""
    p = _plan(
        home,
        "# PR-2 — the approval gate\n\n"
        "Build the gate in `src/genesis/autonomy/desktop_gate.py`, with the\n"
        "classifier in `src/genesis/autonomy/classification.py`.\n",
    )
    r = _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home)
    assert r.returncode == 2, r.stdout
    assert "adopt-first" in r.stderr
    assert "desktop_gate.py" in r.stderr, "name the files, so the block is checkable"
    assert "/evaluate" in r.stderr, "the remedy must point at the EXISTING skill"


def test_a_verdict_clears_the_gate(home: Path, repo: Path):
    """A documented build decision with search and time estimates is accepted."""
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/desktop_gate.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        "BUILD — cognitive core, no external substitute. Searched: desktop\n"
        "automation, approval gate. Found: none applicable.\n"
        "Time-to-capability: adopt 2 hours vs build 1 week.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


@pytest.mark.parametrize(
    "verdict",
    [
        "TODO: choose ADOPT or BUILD later.",
        "<ADOPT|ADAPT|BUILD> — <why>",
        "ADOPT/ADAPT/BUILD — choose one.",
        "ADOPT|ADAPT|BUILD — choose one.",
        "ADOPT vs ADAPT vs BUILD — choose one.",
        "ADOPT —",
        "ADAPT —",
        "ADOPT — use the existing adapter or BUILD a custom parser.",
        "ADOPT — do not adopt this library.",
        "ADOPT — never adopt this library.",
        "Do not BUILD this yet.",
        "Candidates: adopt A or build B. Decision: TBD.",
        "WATCH — keep looking.",
        "IGNORE — not relevant.",
        "BUILD — custom code is more sophisticated.",
        "BUILD — custom. Searched: none. Found: none applicable. "
        "Time-to-capability: adopt 2 hours vs build 1 week.",
        "BUILD — custom. Searched: package index. Found: none applicable. "
        "Time-to-capability: adopt quickly vs build slowly.",
        "BUILD — custom. Not searched: package index. Found: none applicable. "
        "Time-to-capability: adopt 2 hours vs build 1 week.",
        "BUILD — custom. Searched: will search package index. Found: none applicable. "
        "Time-to-capability: adopt 2 hours vs build 1 week.",
        "BUILD — custom. Searched: [search terms]. [more terms]. "
        "Found: [candidates]. [versions]. "
        "Time-to-capability: adopt 2 hours vs build 1 week.",
        "BUILD — custom. Searched: []. Found: !!!. "
        "Time-to-capability: adopt 2 hours vs build 1 week.",
    ],
)
def test_unresolved_or_unsupported_verdicts_still_block(
    home: Path, repo: Path, verdict: str
):
    """A vocabulary token is not a recorded disposition or build evidence."""
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        f"{verdict}\n",
    )
    result = _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home)
    assert result.returncode == 2, verdict


def test_adapt_is_a_concrete_disposition(home: Path, repo: Path):
    """An explicit adapt choice does not need build-only evidence fields."""
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        "ADAPT — wrap the existing client behind the repository interface.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


def test_versus_is_valid_in_a_build_time_comparison(home: Path, repo: Path):
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        "BUILD — custom. Searched: package index. Found: none applicable. "
        "Time-to-capability: adopt 2 hours versus build 1 week.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


def test_build_accepts_evidence_label_variants_and_one_concrete_duration(
    home: Path, repo: Path
):
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        "BUILD — custom implementation.\n"
        "Search evidence: package index and issue tracker.\n"
        "Discovery: no applicable candidate.\n"
        "Time to capability: 3 days.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


def test_build_accepts_inline_evidence_aliases(home: Path, repo: Path):
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        "BUILD — custom. Search evidence: package index. Discovery: none. "
        "Time to capability: 4 h.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


def test_a_verdict_does_not_hide_a_followup_todo(home: Path, repo: Path):
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\nADOPT — use the upstream library.\n"
        "TODO: run /evaluate before implementation.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 2


@pytest.mark.parametrize(
    "status_line",
    [
        "Decision status: pending external evaluation.",
        "- Decision status: pending external evaluation.",
        "* Status: pending external evaluation.",
        "1. Decision status: pending external evaluation.",
        "**Decision status:** pending external evaluation.",
        "**Decision status**: pending external evaluation.",
    ],
)
def test_a_pending_status_after_a_verdict_does_not_clear_the_gate(
    home: Path, repo: Path, status_line: str
):
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\nADOPT — use the upstream library.\n"
        f"{status_line}\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 2


def test_pending_in_a_concrete_rationale_is_not_an_unresolved_status(
    home: Path, repo: Path
):
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        "ADOPT — use the library to track pending jobs.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


def test_an_or_in_the_rationale_is_not_an_alternative_verdict(home: Path, repo: Path):
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        "ADOPT — use the library for JSON or YAML parsing.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


def test_capability_build_action_is_not_an_alternative_verdict(home: Path, repo: Path):
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        "ADOPT — use the library, which can parse JSON or build an index.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


def test_an_adopt_rationale_can_explain_why_not_to_build(home: Path, repo: Path):
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        "ADOPT — use the upstream parser; do not build a custom one.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


def test_a_later_negation_of_the_selected_disposition_still_blocks(
    home: Path, repo: Path
):
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        "ADOPT — do not build a custom parser; do not adopt the existing library.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 2


def test_an_uppercase_alternative_connector_still_blocks(home: Path, repo: Path):
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        "ADOPT — use the existing adapter OR BUILD a custom parser.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 2


def test_later_in_a_concrete_rationale_is_not_unresolved(home: Path, repo: Path):
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        "ADOPT — use the scheduler to retry later.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


def test_build_evidence_cannot_claim_findings_are_not_yet_evaluated(
    home: Path, repo: Path
):
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        "BUILD — custom. Searched: package index. "
        "Found: not yet evaluated. "
        "Time-to-capability: adopt 2 hours vs build 1 week.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 2


def test_a_second_unresolved_verdict_section_does_not_clear_the_gate(
    home: Path, repo: Path
):
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\nADOPT — use the upstream library.\n\n"
        "## Adopt / Adapt / Build\nTODO: decide after evaluation.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 2


@pytest.mark.parametrize(
    "heading",
    [
        "## Adopt / Adapt / Build",
        "## Adopt/Adapt/Build",
        "### ADOPT vs BUILD",
        "### ADOPT vs ADAPT vs BUILD",
        "# adopt - build",
    ],
)
def test_the_heading_spellings_a_writer_will_actually_use(
    home: Path, repo: Path, heading: str
):
    """A gate that only accepts one spelling teaches people to fight the gate."""
    p = _plan(home, f"# Plan\nAdd `src/genesis/x.py`.\n\n{heading}\nADOPT — use the library.\n")
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0, heading


def test_a_plan_that_only_touches_EXISTING_modules_is_silent(home: Path, repo: Path):
    """Adopt-vs-build is a question about NEW capability. "Fix a bug in
    src/genesis/y.py" is not that question, and firing on it is what would teach
    tune-out.

    MEASURED over 205 real plans in ~/.claude/plans/: triggering on any source
    path fired 135 times (65.9%) — two of every three plans. Restricting to
    paths that do not yet exist drops it to ~20% (41/205 point-in-time; 28/205
    if replayed against today's tree, which is biased low because everything
    that got built now looks pre-existing). The acceptance-bar replay above
    still catches the defect this gate was built for — both were re-run
    together, because a filter that improves its rate by blinding the gate has
    made things worse."""
    existing = repo / "src" / "genesis" / "autonomy" / "already_here.py"
    existing.write_text("x = 1\n", encoding="utf-8")
    p = _plan(home, "# Plan\nFix the bug in `src/genesis/autonomy/already_here.py`.\n")
    payload = {**_plan_payload(p), "cwd": str(repo)}
    assert _run("--plan", payload, home).returncode == 0

    # CONTROL: a NEW module in the same plan still fires, or the refinement has
    # simply blinded the gate rather than sharpened it.
    p2 = _plan(
        home,
        "# Plan\nFix `src/genesis/autonomy/already_here.py` and add\n"
        "`src/genesis/autonomy/brand_new_thing.py`.\n",
    )
    r = _run("--plan", {**_plan_payload(p2), "cwd": str(repo)}, home)
    assert r.returncode == 2
    assert "brand_new_thing.py" in r.stderr
    assert "already_here.py" not in r.stderr, "name only the NEW files"


def test_a_plan_with_no_source_files_is_not_this_gates_business(home: Path):
    """Docs, config and research plans pass untouched. The trigger is a concrete
    source path, deliberately NOT prose like "create" or "new file" — a gate
    that fires on every plan is one that gets acked past reflexively."""
    p = _plan(home, "# Plan\nRewrite the README and update `config/x.yaml`.\n")
    assert _run("--plan", _plan_payload(p), home).returncode == 0


def test_an_empty_verdict_section_does_not_satisfy_the_gate(home: Path, repo: Path):
    """The header alone is not a verdict.

    This works ONLY because the token match is case-SENSITIVE: the heading
    itself reads "Adopt / Adapt / Build", so adding re.IGNORECASE would let the
    heading satisfy its own requirement and every section could be left blank.
    That is the whole reason this test exists."""
    p = _plan(home, "# Plan\nAdd `src/genesis/x.py`.\n\n## Adopt / Adapt / Build\n\n(tbd)\n")
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 2


def test_an_unreadable_plan_never_blocks(home: Path):
    """Fail OPEN. Wedging plan mode over our own inability to find a file would
    be a far worse defect than the one this gate prevents."""
    payload = {"tool_name": "ExitPlanMode", "tool_input": {"plan": "/nonexistent/x.md"}}
    assert _run("--plan", payload, home).returncode == 0


# ── the review's findings, each locked ───────────────────────────────────────


def test_an_unknowable_repo_root_never_blocks(home: Path, tmp_path: Path):
    """THE BLOCKER. An earlier `_repo_root` fell back to `Path(cwd)` when git
    failed or the cwd was outside a repo. Every `src/genesis/*.py` then resolved
    under a directory with no `src/`, read as "does not exist yet", and the gate
    blocked EVERY plan — silently reverting to the 65.9% fire rate the design
    exists to avoid, while the module docstring still promised fail-open.

    Reproduced at the time: the plan "fix a bug in src/genesis/memory/retrieval.py"
    exited 2 from a non-repo cwd and 0 from the repo."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    p = _plan(home, "# Plan\nFix a bug in `src/genesis/memory/retrieval.py`.\n")
    r = _run("--plan", {**_plan_payload(p), "cwd": str(outside)}, home)
    assert r.returncode == 0, "unknowable root must fail OPEN, never block"


def test_a_repo_without_a_source_tree_is_also_unknowable(home: Path, tmp_path: Path):
    """A root that resolves but has no `src/genesis` cannot answer "does this
    file exist yet" either, so it gets the same fail-open treatment."""
    bare = tmp_path / "empty-repo"
    bare.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=bare, check=True, capture_output=True)
    p = _plan(home, "# Plan\nAdd `src/genesis/autonomy/thing.py`.\n")
    assert _run("--plan", {**_plan_payload(p), "cwd": str(bare)}, home).returncode == 0


def test_an_all_caps_heading_does_not_satisfy_its_own_section(home: Path, repo: Path):
    """`### ADOPT vs BUILD` + `(tbd)` used to PASS: the heading is itself a
    matching token, and the check scanned the whole document. The section BODY
    is what has to carry the answer."""
    p = _plan(home, "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n### ADOPT vs BUILD\n\n(tbd)\n")
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 2


def test_a_lowercase_prose_verdict_is_accepted(home: Path, repo: Path):
    """The other direction of the same defect: a genuine verdict written in
    ordinary prose was BLOCKED, which teaches people to fight the gate."""
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        "We should adopt the upstream library — searched pypi and github, found "
        "two candidates, hours to wire vs a week to write.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


def test_a_path_inside_a_code_fence_is_quoted_not_proposed(home: Path, repo: Path):
    """Showing an example is not proposing to build it."""
    p = _plan(
        home,
        "# Plan\nUpdate the docs.\n\n```python\n"
        "# e.g. src/genesis/autonomy/example_only.py\n```\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


@pytest.mark.parametrize(
    "fence",
    [
        "~~~python\n# src/genesis/autonomy/tilde.py\n~~~",
        "   ```python\n   # src/genesis/autonomy/indented.py\n   ```",
        "~~~python\n# src/genesis/autonomy/unclosed.py",
    ],
)
def test_all_supported_fence_forms_are_quoted(home: Path, repo: Path, fence: str):
    p = _plan(home, f"# Plan\nUpdate the docs.\n\n{fence}\n")
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


@pytest.mark.parametrize("extension", ["js", "ts", "html", "css"])
def test_non_python_executable_sources_are_gated(home: Path, repo: Path, extension: str):
    """Executable dashboard source additions must receive the plan gate."""
    p = _plan(home, f"# Plan\nAdd `src/genesis/dashboard/new_panel.{extension}`.\n")
    result = _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home)
    assert result.returncode == 2, extension


def test_a_bare_source_path_with_sentence_punctuation_is_gated(home: Path, repo: Path):
    p = _plan(home, "# Plan\nAdd src/genesis/autonomy/bare_path.py.\n")
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 2


def test_an_absolute_path_outside_the_repository_is_ignored(
    home: Path, repo: Path, tmp_path: Path
):
    foreign = tmp_path / "foreign checkout" / "src" / "genesis" / "outside.py"
    p = _plan(home, f"# Plan\nAdd `{foreign.as_posix()}`.\n")
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


def test_a_relative_source_path_cannot_escape_the_repository(home: Path, repo: Path):
    p = _plan(home, "# Plan\nAdd `src/genesis/../../../../outside.py`.\n")
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


def test_a_longer_data_suffix_is_not_mistaken_for_a_source_file(home: Path, repo: Path):
    p = _plan(home, "# Plan\nAdd `src/genesis/autonomy/example.py.json`.\n")
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


def test_absolute_source_path_is_recognized(home: Path, repo: Path):
    p = _plan(home, f"# Plan\nAdd `{repo.as_posix()}/src/genesis/autonomy/absolute.py`.\n")
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 2


def test_the_authoritative_plan_field_wins_over_prose(home: Path, repo: Path):
    """MEASURED: `planFilePath` is present in 1862/1862 real payloads, but `plan`
    (the full markdown) serializes FIRST — so a blob-wide regex could return a
    path quoted in the plan's own prose and gate on a different document."""
    decoy = home / ".claude" / "plans" / "some-other-spec.md"
    decoy.write_text("# Other\nAdd `src/genesis/autonomy/decoy.py`.\n", encoding="utf-8")
    real = _plan(
        home,
        f"# Plan\nSee the parent spec at {decoy} for context.\n"
        "This plan only edits documentation.\n",
    )
    r = _run("--plan", {**_plan_payload(real), "cwd": str(repo)}, home)
    assert r.returncode == 0, "must read planFilePath, not the first path in the prose"


def test_missing_plan_path_does_not_use_another_sessions_newest_plan(home: Path, repo: Path):
    """Without a path, only an exact unique inline-plan match is inspectable."""
    live_docs = "# Plan\nUpdate documentation only.\n"
    newest = home / ".claude" / "plans" / "newest.md"
    newest.write_text("# Other session\nAdd `src/genesis/autonomy/foreign.py`.\n", encoding="utf-8")
    payload = {
        "tool_name": "ExitPlanMode",
        "tool_input": {"plan": live_docs},
        "cwd": str(repo),
    }
    assert _run("--plan", payload, home).returncode == 0

    matched = home / ".claude" / "plans" / "matched.md"
    matched.write_text(
        "# Plan\nAdd `src/genesis/autonomy/inline.py`.\n", encoding="utf-8"
    )
    payload["tool_input"]["plan"] = matched.read_text(encoding="utf-8")
    assert _run("--plan", payload, home).returncode == 2

    duplicate = home / ".claude" / "plans" / "duplicate.md"
    duplicate.write_text(matched.read_text(encoding="utf-8"), encoding="utf-8")
    assert _run("--plan", payload, home).returncode == 0


def test_superseded_and_archived_content_is_not_live(home: Path, repo: Path):
    """Archived proposals cannot block and archived verdicts cannot clear a live one."""
    old_proposal = "Add `src/genesis/autonomy/old.py`.\n"
    p = _plan(home, "# Plan\nUpdate docs.\n\n## ═══ SUPERSEDED BELOW ═══\n" + old_proposal)
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0

    p.write_text(
        "# Plan\nAdd `src/genesis/autonomy/live.py`.\n\n"
        "## ARCHIVED\nADOPT — use the library.\n",
        encoding="utf-8",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 2

    p.write_text(
        "# Plan\nUpdate docs.\n\n──── SUPERSEDED ────\n"
        "Add `src/genesis/autonomy/old_box.py`.\n",
        encoding="utf-8",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0

    p.write_text(
        "# Plan\nAdd `src/genesis/autonomy/live.py`.\n\n"
        "ARCHIVED\nADOPT — use the library.\n",
        encoding="utf-8",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 2


@pytest.mark.parametrize(
    "move_line",
    [
        "Rename `src/genesis/autonomy/old.py` to `src/genesis/autonomy/new.py`.",
        "`src/genesis/autonomy/old.py` -> `src/genesis/autonomy/new.py`.",
        "git mv `src/genesis/autonomy/old.py` `src/genesis/autonomy/new.py`.",
    ],
)
def test_existing_source_rename_does_not_trigger_but_real_addition_does(
    home: Path, repo: Path, move_line: str
):
    """Existing files moved to a new path do not count as new capabilities."""
    old = repo / "src" / "genesis" / "autonomy" / "old.py"
    old.write_text("x = 1\n", encoding="utf-8")
    p = _plan(
        home,
        f"# Plan\n{move_line}\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0

    p.write_text(
        f"# Plan\n{move_line} Then add "
        "`src/genesis/autonomy/real_new.py`.\n",
        encoding="utf-8",
    )
    result = _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home)
    assert result.returncode == 2
    assert "real_new.py" in result.stderr
    assert "src/genesis/autonomy/new.py" not in result.stderr


def test_moving_an_old_file_and_adding_a_new_one_still_blocks(
    home: Path, repo: Path
):
    old = repo / "src" / "genesis" / "autonomy" / "old.py"
    old.write_text("x = 1\n", encoding="utf-8")
    p = _plan(
        home,
        "# Plan\nMove `src/genesis/autonomy/old.py` and add "
        "`src/genesis/autonomy/new.py`.\n",
    )
    result = _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home)
    assert result.returncode == 2
    assert "new.py" in result.stderr


def test_git_location_overrides_cannot_change_payload_repo(home: Path, repo: Path, tmp_path: Path):
    """Inherited Git location variables must not redirect source existence checks."""
    foreign = tmp_path / "foreign"
    (foreign / "src" / "genesis" / "autonomy").mkdir(parents=True)
    for args in (
        ["git", "init", "-q", "-b", "foreign"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(args, cwd=foreign, check=True, capture_output=True)
    (foreign / "src" / "genesis" / "autonomy" / "new.py").write_text("x = 1\n", encoding="utf-8")
    p = _plan(home, "# Plan\nAdd `src/genesis/autonomy/new.py`.\n")
    for key, value in (
        ("GIT_DIR", str(foreign / ".git")),
        ("GIT_WORK_TREE", str(foreign)),
        ("GIT_COMMON_DIR", str(foreign / ".git")),
    ):
        result = _run(
            "--plan",
            {**_plan_payload(p), "cwd": str(repo)},
            home,
            {key: value},
        )
        assert result.returncode == 2, key


# ── the new-file gate, and the anti-annoyance property ───────────────────────


def test_a_new_module_nudges_once_then_stays_silent(home: Path, repo: Path):
    """The property that keeps this from becoming noise.

    Cost is proportional to how often you START work, never to how much you
    type — so the second, third and hundredth new file on the same branch are
    silent. A per-file nudge would be tuned out within a day, and a tuned-out
    gate is worth exactly nothing."""
    first = repo / "src" / "genesis" / "autonomy" / "brand_new.py"
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(first)}, "cwd": str(repo)}

    r1 = _run("--new-file", payload, home)
    assert r1.returncode == 0, "advisory only — it must never block an edit"
    assert "ADOPT" in r1.stdout
    assert "hookSpecificOutput" in r1.stdout, "PreToolUse advisories reach the model only here"

    second = repo / "src" / "genesis" / "autonomy" / "another_new.py"
    r2 = _run(
        "--new-file",
        {"tool_name": "Write", "tool_input": {"file_path": str(second)}, "cwd": str(repo)},
        home,
    )
    assert r2.returncode == 0
    assert r2.stdout.strip() == "", "second new file on the same branch must be silent"


def test_concurrent_new_file_calls_have_one_sentinel_winner(home: Path, repo: Path):
    """Only the O_EXCL winner emits a branch advisory during a race."""
    target = repo / "src" / "genesis" / "autonomy" / "race.py"
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(target)}, "cwd": str(repo)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _run("--new-file", payload, home), range(8)))
    assert sum(bool(result.stdout.strip()) for result in results) == 1


def test_a_new_branch_gets_its_own_nudge(home: Path, repo: Path):
    """State is keyed per (worktree, branch): starting new work asks again."""
    f = repo / "src" / "genesis" / "autonomy" / "thing.py"
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(f)}, "cwd": str(repo)}
    assert "ADOPT" in _run("--new-file", payload, home).stdout

    subprocess.run(["git", "checkout", "-q", "-b", "feat/other"], cwd=repo, check=True)
    assert "ADOPT" in _run("--new-file", payload, home).stdout, "new branch, new question"


def test_editing_existing_code_is_silent(home: Path, repo: Path):
    """The trigger is CREATION. Ordinary work on code that already exists is
    none of this gate's business."""
    existing = repo / "src" / "genesis" / "autonomy" / "existing.py"
    existing.write_text("x = 1\n", encoding="utf-8")
    r = _run(
        "--new-file",
        {"tool_name": "Edit", "tool_input": {"file_path": str(existing)}, "cwd": str(repo)},
        home,
    )
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_files_outside_the_source_tree_are_silent(home: Path, repo: Path):
    """Tests, docs and scratch files are not new capabilities."""
    for rel in ("tests/test_x.py", "docs/x.md", "scratch.py"):
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        r = _run(
            "--new-file",
            {"tool_name": "Write", "tool_input": {"file_path": str(target)}, "cwd": str(repo)},
            home,
        )
        assert r.stdout.strip() == "", rel


def test_non_source_file_inside_source_tree_does_not_consume_nudge(home: Path, repo: Path):
    """Docs and assets under src/genesis are not new executable modules."""
    doc = repo / "src" / "genesis" / "skills" / "new-skill.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        "--new-file",
        {"tool_name": "Write", "tool_input": {"file_path": str(doc)}, "cwd": str(repo)},
        home,
    )
    assert result.stdout.strip() == ""

    source = repo / "src" / "genesis" / "new_module.py"
    result = _run(
        "--new-file",
        {"tool_name": "Write", "tool_input": {"file_path": str(source)}, "cwd": str(repo)},
        home,
    )
    assert "ADOPT" in result.stdout


def test_an_unrelated_repo_path_does_not_consume_the_nudge(home: Path, repo: Path, tmp_path: Path):
    """Only a new file in this repository's source tree may claim the sentinel."""
    other = tmp_path / "other-repo" / "src" / "genesis"
    other.mkdir(parents=True)
    unrelated = other / "foreign.py"
    r = _run(
        "--new-file",
        {"tool_name": "Write", "tool_input": {"file_path": str(unrelated)}, "cwd": str(repo)},
        home,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == ""

    real = repo / "src" / "genesis" / "real.py"
    r = _run(
        "--new-file",
        {"tool_name": "Write", "tool_input": {"file_path": str(real)}, "cwd": str(repo)},
        home,
    )
    assert "ADOPT" in r.stdout, "an out-of-tree path must not consume the branch nudge"


def test_a_source_tree_symlink_outside_the_repo_does_not_nudge(
    home: Path, repo: Path, tmp_path: Path
):
    outside = tmp_path / "outside-source"
    outside.mkdir()
    source_root = repo / "src" / "genesis"
    shutil.rmtree(source_root)
    try:
        source_root.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    target = outside / "new_module.py"
    result = _run(
        "--new-file",
        {"tool_name": "Write", "tool_input": {"file_path": str(target)}, "cwd": str(repo)},
        home,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_a_nested_symlink_cannot_hide_a_new_source_as_a_rename(
    home: Path, repo: Path, tmp_path: Path
):
    outside = tmp_path / "outside-source"
    outside.mkdir()
    old = outside / "old.py"
    old.write_text("x = 1\n", encoding="utf-8")
    link = repo / "src" / "genesis" / "autonomy" / "external"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    p = _plan(
        home,
        "# Plan\nRename `src/genesis/autonomy/external/old.py` to "
        "`src/genesis/autonomy/new.py`.\n",
    )
    result = _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home)
    assert result.returncode == 2
    assert "new.py" in result.stderr


def test_a_move_payload_does_not_consume_the_nudge(home: Path, repo: Path):
    """A Write payload describing an existing source move leaves the sentinel unused."""
    old = repo / "src" / "genesis" / "autonomy" / "old.py"
    old.write_text("x = 1\n", encoding="utf-8")
    moved = repo / "src" / "genesis" / "autonomy" / "moved.py"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(moved), "old_path": str(old)},
        "cwd": str(repo),
    }
    assert _run("--new-file", payload, home).stdout.strip() == ""

    real = repo / "src" / "genesis" / "autonomy" / "real.py"
    payload["tool_input"] = {"file_path": str(real)}
    assert "ADOPT" in _run("--new-file", payload, home).stdout


def test_new_file_gate_fails_open_without_a_repo_root(home: Path, tmp_path: Path):
    """A path cannot claim state when its payload repository is unknowable."""
    cwd = tmp_path / "not-a-repo"
    target = cwd / "src" / "genesis" / "new.py"
    target.parent.mkdir(parents=True)
    r = _run(
        "--new-file",
        {"tool_name": "Write", "tool_input": {"file_path": str(target)}, "cwd": str(cwd)},
        home,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == ""


# ── wiring: a hook nobody registered is a hook that does nothing ─────────────


def test_registered_in_settings_json():
    """Both modes must be wired, or the whole exercise is decorative — which is
    precisely the failure mode this gate was built to fix."""
    settings = json.loads(
        (Path(__file__).resolve().parents[2] / ".claude" / "settings.json").read_text()
    )
    pre = settings["hooks"]["PreToolUse"]
    commands = [h.get("command", "") for block in pre for h in block.get("hooks", [])]
    assert any("adopt_first_gate.py --plan" in c for c in commands), "plan gate not wired"
    assert any("adopt_first_gate.py --new-file" in c for c in commands), "new-file gate not wired"

    plan_matchers = [
        block.get("matcher", "")
        for block in pre
        if any("adopt_first_gate.py --plan" in h.get("command", "") for h in block.get("hooks", []))
    ]
    assert any("ExitPlanMode" in m for m in plan_matchers), (
        f"the plan gate must match ExitPlanMode, got {plan_matchers}"
    )
