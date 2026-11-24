#!/usr/bin/env python3
"""
Research Pipeline Orchestrator

Orchestrates a multi-stage research pipeline using the Cursor Cloud Agents API.
Each stage runs as a separate cloud agent with configurable model selection.

Usage:
    python orchestrator.py --dry-run              # Offline check, no network
    python orchestrator.py --check-online         # API key, models, GitHub; launches nothing
    python orchestrator.py --execute              # Start a new project
    python orchestrator.py --resume manifest.json # Continue an existing project
    python orchestrator.py --resume manifest.json --apply-decision decision.json
    python orchestrator.py --adopt STAGE --adopt-branch B --adopt-commit SHA \
                           --adopt-input-commit SHA   # Register existing work; launches nothing

A stage is worked on in rounds, up to a fixed number per stage. Every round
that ends at a human gate is first checked out in the local Git checkout,
then announced by a signed notice file (review_bridge.py). Nothing further
happens until the host has acknowledged the notice and the owner's signed
decision for that exact commit has been applied. Accepting a stage launches
nothing; the next stage starts only on a separate decision.

Starting a new project (--execute) always asks for the location of the
model setup / equilibrium definition document (.qmd, .tex or .md). Nothing
is launched until a valid document has been given.

Official API docs: https://cursor.com/docs/cloud-agent/api/endpoints
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import yaml
except ImportError:
    print("Error: PyYAML required. Install with: pip install pyyaml")
    sys.exit(1)

import trial_log
import verification
from api_client import (
    APIError,
    AgentRun,
    CursorAPIClient,
    CursorAPIError,
    GitHubAPIError,
    GitHubRepoReader,
    RunStatus,
    parse_github_repo_url,
)
from review_bridge import (
    ACTION_ACCEPT,
    ACTION_HALT,
    ACTION_ITERATE,
    ACTION_RAISE_LIMIT,
    ACTION_REDO,
    ACTION_START_STAGE,
    BridgeError,
    ReviewBridge,
    canonical_json,
)
from trial_log import TrialJournal, TrialLogError


@dataclass
class StageConfig:
    """Configuration for a single pipeline stage."""
    name: str
    display_name: str
    description: str
    prompt: str
    model_id: Optional[str] = None
    model_params: list[dict] = field(default_factory=list)
    depends_on: Optional[str] = None
    output_manifest: dict = field(default_factory=dict)
    guardrails: list[str] = field(default_factory=list)
    receives_model_setup_document: bool = False
    # {max_rounds, autonomous_fix}: how many rounds the stage may take, and
    # whether a failed round may be followed by another without the owner.
    iteration: dict = field(default_factory=dict)
    # {kind, evidence_file, ...}: the evidence file the stage must push,
    # checked by verification.py. None: no evidence is required.
    verification: Optional[dict] = None
    # {enabled, secondary_model_id, secondary_model_params, pass_policy,
    # on_disagreement, review_dir}: review of each candidate by a second model.
    cross_model: dict = field(default_factory=dict)
    
    @property
    def max_rounds(self) -> int:
        return self.iteration["max_rounds"]
    
    @property
    def autonomous_fix(self) -> bool:
        return self.iteration.get("autonomous_fix", False)
    
    @property
    def cross_model_enabled(self) -> bool:
        return self.cross_model.get("enabled", False)


GUARDRAILS_HEADING = "REPRODUCIBILITY GUARDRAILS (follow all of these):"

MODEL_SETUP_FORMATS = {
    ".qmd": "Quarto Markdown (.qmd)",
    ".tex": "LaTeX (.tex)",
    ".md": "Markdown (.md)",
}
# The accepted extensions as written in the question and in error messages:
# ".qmd, .tex or .md".
*_FIRST_EXTENSIONS, _LAST_EXTENSION = MODEL_SETUP_FORMATS
MODEL_SETUP_EXTENSIONS = f"{', '.join(_FIRST_EXTENSIONS)} or {_LAST_EXTENSION}"
MODEL_SETUP_QUESTION = (
    "Location of the model setup / equilibrium definition document "
    f"for this project ({MODEL_SETUP_EXTENSIONS}): "
)
MODEL_SETUP_HEADING = (
    "MODEL SETUP AND EQUILIBRIUM DEFINITION DOCUMENT "
    "(supplied by the project owner for this project):"
)
MODEL_SETUP_BEGIN_LINE = "----- BEGIN DOCUMENT -----"
MODEL_SETUP_END_LINE = "----- END DOCUMENT -----"

WORKING_DOCUMENT_HEADING = (
    "WORKING DOCUMENT (one cumulative file; the project owner reviews it "
    "after every stage):"
)
REVISION_HEADING = "REVISION REQUEST FROM THE PROJECT OWNER:"
FIX_HEADING = "CORRECTION REQUEST (from the orchestrator's checks, not from the project owner):"
RECONCILE_HEADING = "RECONCILIATION REQUEST (after an independent review by another model):"
FEEDBACK_BEGIN_LINE = "----- BEGIN FEEDBACK -----"
FEEDBACK_END_LINE = "----- END FEEDBACK -----"
# The limits of a round that was not asked for by the owner.
AUTONOMOUS_RULES = (
    "Limits of this correction, all checked or reviewed afterwards:\n"
    "- Correct real errors, and show the evidence for each correction.\n"
    "- Do not invent an assumption, a value or a functional form.\n"
    "- Do not change the model or anything the project owner has accepted.\n"
    "- Do not weaken, remove or skip a check, and do not edit a check's output.\n"
    "- An item that depends on a choice of the project owner stays open. Do not "
    "close it; say what decision is needed.\n"
    "- If you see no way to meet the requirements within these limits, say so in "
    "the evidence file (\"no_plausible_alternative\": true) instead of forcing a result."
)
ROUND_HEADING = "ROUND:"
EVIDENCE_HEADING = "VERIFICATION EVIDENCE (checked at the commit you push; a failed check ends the round):"
OUTPUT_PATHS_HEADING = "OUTPUT PATHS:"
TRIAL_LOG_HEADING = "TRIAL LOG:"
LOG_ENTRIES_BEGIN_LINE = "----- BEGIN ENTRIES TO APPEND -----"
LOG_ENTRIES_END_LINE = "----- END ENTRIES TO APPEND -----"
OWN_ENTRY_BEGIN_LINE = "----- BEGIN YOUR ENTRY -----"
OWN_ENTRY_END_LINE = "----- END YOUR ENTRY -----"
TASK_BEGIN_LINE = "----- BEGIN THE TASK -----"
TASK_END_LINE = "----- END THE TASK -----"

# Directives that pull the text of another file into the document. Only the
# selected file's own text is sent to the cloud agent, so a document that
# uses one of these would reach the agent incomplete.
TEX_COMMENT_PATTERN = re.compile(r"(?<!\\)%.*$")
QUARTO_INCLUDE = r"\{\{<\s*include\s"
# Markdown has no include syntax of its own; tools that read .md files add
# one. A .md file is checked for two of them: the Quarto shortcode, and the
# MyST `{include}` / `{literalinclude}` directives at the start of a fence.
# The include forms of other Markdown tools are not detected.
MYST_INCLUDE = r"^\s*(?:`{3,}|~{3,}|:{3,})\s*\{(?:include|literalinclude)\}"
INCLUDE_DIRECTIVE_PATTERNS = {
    ".tex": re.compile(r"\\(?:input|include|subfile|import|subimport)(?![A-Za-z])"),
    ".qmd": re.compile(QUARTO_INCLUDE),
    ".md": re.compile(f"{QUARTO_INCLUDE}|{MYST_INCLUDE}"),
}


class ModelSetupDocumentError(Exception):
    """No usable model setup / equilibrium definition document is available."""


@dataclass
class ModelSetupDocument:
    """The document that states the model setup and equilibrium definition.

    `text` is the document's content. It is sent to the cloud agent but is
    not written to the project manifest; `to_record` gives what is.
    """
    path: str
    format: str
    size_bytes: int
    sha256: str
    text: str = field(repr=False)
    
    @property
    def file_name(self) -> str:
        return Path(self.path).name
    
    def to_record(self) -> dict:
        return {
            "path": self.path,
            "format": self.format,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


def find_include_directives(text: str, suffix: str) -> list[str]:
    """Return the lines of a document that pull in the text of another file."""
    pattern = INCLUDE_DIRECTIVE_PATTERNS[suffix]
    found = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        checked = TEX_COMMENT_PATTERN.sub("", line) if suffix == ".tex" else line
        if pattern.search(checked):
            found.append(f"line {line_number}: {line.strip()}")
    return found


def load_model_setup_document(location: str) -> ModelSetupDocument:
    """Read the model setup document at a location given by the user.

    The file is only read. It must exist, be a `.qmd`, `.tex` or `.md` file,
    hold UTF-8 text, be non-empty and be self-contained. Whether it states a
    correct or complete equilibrium definition cannot be checked here.

    Raises:
        ModelSetupDocumentError: if the location is blank or the file
            cannot be used.
    """
    cleaned = location.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
        cleaned = cleaned[1:-1].strip()
    if not cleaned:
        raise ModelSetupDocumentError(
            "No location was given for the model setup / equilibrium "
            f"definition document. Give the path of a {MODEL_SETUP_EXTENSIONS} file."
        )
    
    path = Path(cleaned).expanduser()
    if not path.exists():
        raise ModelSetupDocumentError(f"The model setup document does not exist: {path}")
    if not path.is_file():
        raise ModelSetupDocumentError(
            f"The model setup document location is not a file: {path}"
        )
    
    suffix = path.suffix.lower()
    if suffix not in MODEL_SETUP_FORMATS:
        raise ModelSetupDocumentError(
            f"The model setup document must be a {MODEL_SETUP_EXTENSIONS} file, "
            f"but this file has the extension '{path.suffix}': {path}"
        )
    
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ModelSetupDocumentError(
            f"The model setup document could not be read: {path} ({error})"
        ) from error
    
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ModelSetupDocumentError(
            f"The model setup document is not UTF-8 text: {path} ({error}). "
            f"Save it as UTF-8 and start again."
        ) from error
    
    if not text.strip():
        raise ModelSetupDocumentError(f"The model setup document is empty: {path}")
    
    includes = find_include_directives(text=text, suffix=suffix)
    if includes:
        raise ModelSetupDocumentError(
            f"The model setup document pulls in other files, and only the "
            f"text of this one file is sent to the cloud agent: {path}\n  "
            + "\n  ".join(includes)
            + "\nGive a single self-contained file with that text written in."
        )
    
    return ModelSetupDocument(
        path=str(path.resolve()),
        format=suffix.lstrip("."),
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        text=text,
    )


def ask_for_model_setup_document(ask: Callable[[str], str]) -> ModelSetupDocument:
    """Ask the user where the model setup document is, and read it.

    Raises:
        ModelSetupDocumentError: if no answer can be read or the answer
            does not name a usable document.
    """
    try:
        answer = ask(MODEL_SETUP_QUESTION)
    except EOFError:
        raise ModelSetupDocumentError(
            "The location of the model setup / equilibrium definition "
            "document was asked for, but input ended before an answer was "
            "given. Run the orchestrator in an interactive terminal and "
            "type the location when asked."
        ) from None
    return load_model_setup_document(location=answer)


def restore_model_setup_document(record: Optional[dict]) -> ModelSetupDocument:
    """Re-read the document a project recorded, and check it is unchanged.

    Raises:
        ModelSetupDocumentError: if the project recorded no document, or
            the recorded file is gone, unusable or has different content.
    """
    if not record:
        raise ModelSetupDocumentError(
            "This project's manifest records no model setup / equilibrium "
            "definition document, so the stages that need it cannot be "
            "resumed. Start a new project with --execute."
        )
    missing_keys = [key for key in ("path", "sha256") if not record.get(key)]
    if missing_keys:
        raise ModelSetupDocumentError(
            "This project's manifest has an incomplete model setup document "
            f"record (missing: {', '.join(missing_keys)}), so the stages "
            "that need it cannot be resumed. Start a new project with --execute."
        )
    
    try:
        document = load_model_setup_document(location=record["path"])
    except ModelSetupDocumentError as error:
        raise ModelSetupDocumentError(
            f"The model setup document recorded for this project can no "
            f"longer be used. {error}"
        ) from error
    
    if document.sha256 != record["sha256"]:
        raise ModelSetupDocumentError(
            f"The model setup document recorded for this project has changed "
            f"since the project was started: {document.path} "
            f"(recorded SHA-256 {record['sha256']}, now {document.sha256}). "
            f"Restore the recorded version to resume this project, or start "
            f"a new project with --execute to use the new version."
        )
    return document


def build_stage_prompt(
    stage: StageConfig,
    model_setup_document: Optional[ModelSetupDocument] = None,
    working_document_rules: Optional[str] = None,
    revision: Optional[dict] = None,
    extra_sections: Optional[list[str]] = None,
) -> str:
    """Return the prompt text sent to the cloud agent for a stage.

    The model setup document (for a stage that receives it), the working
    document rules, the stage's guardrails, the round's sections and, for
    a round that revises an earlier output, the feedback are appended to
    the stage prompt, so the agent receives them in the same launch
    request as the task. The document's local path is left out: the cloud
    agent cannot open it.

    Args:
        working_document_rules: Text of Orchestrator.working_document_rules().
        revision: The `revision` record of the stage's round, when the
            round revises an output of an earlier round. Its `source` says
            who asked: "owner" (a decision), "checks" (the orchestrator's
            checks failed) or "review" (another model's review disagreed).
        extra_sections: Further sections (round, evidence, output paths,
            trial log), placed after the guardrails.

    Raises:
        ModelSetupDocumentError: if the stage receives the model setup
            document and none is given.
    """
    sections = [stage.prompt.rstrip()]
    
    if stage.receives_model_setup_document:
        if model_setup_document is None:
            raise ModelSetupDocumentError(
                f"Stage '{stage.name}' needs the model setup / equilibrium "
                f"definition document and none has been selected"
            )
        sections.append(
            f"{MODEL_SETUP_HEADING}\n"
            f"File name: {model_setup_document.file_name}\n"
            f"Format: {MODEL_SETUP_FORMATS['.' + model_setup_document.format]}\n"
            f"SHA-256: {model_setup_document.sha256}\n"
            f"The full text of the document is between the two lines below.\n"
            f"{MODEL_SETUP_BEGIN_LINE}\n"
            f"{model_setup_document.text.rstrip()}\n"
            f"{MODEL_SETUP_END_LINE}"
        )
    
    if working_document_rules:
        sections.append(working_document_rules.rstrip())
    
    if stage.guardrails:
        numbered = "\n".join(
            f"{number}. {guardrail}"
            for number, guardrail in enumerate(stage.guardrails, start=1)
        )
        sections.append(f"{GUARDRAILS_HEADING}\n{numbered}")
    
    sections.extend(section.rstrip() for section in extra_sections or [])
    
    if revision:
        start = (
            f"The repository snapshot you start from already holds an output of "
            f"this stage (round {revision['replaces_round']})."
        )
        finish = (
            "Keep the same file paths and the same working document section, and "
            "keep every rule above."
        )
        if revision["source"] == "owner":
            intro = (
                f"{REVISION_HEADING}\n{start} The project owner reviewed it and did "
                f"not accept it (decision {revision['decision_id']}). Do the task "
                f"above again by revising that output as the feedback below asks. "
                f"Change nothing the feedback does not call for. {finish}"
            )
        elif revision["source"] == "checks":
            intro = (
                f"{FIX_HEADING}\n{start} The orchestrator's checks of it found the "
                f"problems below. Correct them by revising that output. {finish}\n"
                f"{AUTONOMOUS_RULES}"
            )
        else:
            intro = (
                f"{RECONCILE_HEADING}\n{start} A reviewer running another model "
                f"wrote its own assessment without seeing that output, then compared "
                f"the two; both files are in the snapshot. The differences it found "
                f"are below. Check each one against the source and the accepted "
                f"sections. Correct the output where the reviewer is right, and say "
                f"in your section, with the evidence, where it is not. Do not adopt "
                f"a claim only because the reviewer made it. {finish}\n"
                f"{AUTONOMOUS_RULES}"
            )
        sections.append(
            f"{intro}\n"
            f"{FEEDBACK_BEGIN_LINE}\n"
            f"{revision['feedback'].rstrip()}\n"
            f"{FEEDBACK_END_LINE}"
        )
    
    if len(sections) == 1:
        return stage.prompt
    return "\n\n".join(sections) + "\n"


STAGE_PENDING = "PENDING"
STAGE_BLOCKED = "BLOCKED"
STAGE_DRY_RUN = "DRY_RUN"
STAGE_LAUNCHING = "LAUNCHING"
STAGE_RUNNING = "RUNNING"
STAGE_RUN_FINISHED = "RUN_FINISHED"
STAGE_OUTPUT_CHECKED = "OUTPUT_CHECKED"
STAGE_ARTIFACTS_FAILED = "ARTIFACTS_FAILED"
# The round is closed and its output is on GitHub but not (yet) checked out
# in the local checkout, so no notice is written for it.
STAGE_SYNC_PENDING = "LOCAL_SYNC_PENDING"
# The signed notice is written; the host has not acknowledged posting it.
STAGE_NOTICE_PENDING = "NOTICE_PENDING"
STAGE_AWAITING_REVIEW = "AWAITING_REVIEW"
# Accepted by a signed decision of the owner for the exact output commit.
STAGE_COMPLETE = "COMPLETE"

# Only a launch in one of these states can lead to a launch request. No
# agent exists for it yet, or (LAUNCHING) it is not known whether one does.
LAUNCHABLE_STATUSES = (STAGE_PENDING, STAGE_BLOCKED, STAGE_DRY_RUN, STAGE_LAUNCHING)
# A round in one of these states is at its human gate, or past it.
GATE_STATUSES = (
    STAGE_SYNC_PENDING, STAGE_NOTICE_PENDING, STAGE_AWAITING_REVIEW, STAGE_COMPLETE,
)

# How a round ended. Only "passed" can be accepted.
OUTCOME_PASSED = "passed"
OUTCOME_CHECKS_FAILED = "checks_failed"
OUTCOME_BLOCKERS_OPEN = "blockers_open"
OUTCOME_DISAGREEMENT = "disagreement"
OUTCOME_REVIEW_INVALID = "review_invalid"
OUTCOME_REVIEW_FAILED = "review_failed"
OUTCOME_RUN_FAILED = "run_failed"
OUTCOME_LAUNCH_REJECTED = "launch_rejected"
# A launch did not take place or did not finish. No new launch follows
# from these without a decision of the owner.
INFRASTRUCTURE_OUTCOMES = (OUTCOME_RUN_FAILED, OUTCOME_LAUNCH_REJECTED, OUTCOME_REVIEW_FAILED)

# Why a round stopped at the human gate.
STOP_PASSED = "passed"
STOP_REVIEW_REQUIRED = "review_required"
STOP_CAP_EXHAUSTED = "cap_exhausted"
STOP_NO_ALTERNATIVE = "no_plausible_alternative"
STOP_OWNER_DECISION = "owner_decision_needed"
STOP_DISAGREEMENT = "disagreement"
STOP_INFRASTRUCTURE = "infrastructure_failure"
STOP_ADOPTED = "adopted"

# The launches of a round, in order. Without cross-model review a round
# has the first one only.
PHASE_PROPOSE = "propose"
PHASE_BLIND = "blind_review"
PHASE_COMPARE = "compare"
ROLE_PROPOSER = "proposer"
ROLE_REVIEWER = "reviewer"
ROLE_BY_PHASE = {
    PHASE_PROPOSE: ROLE_PROPOSER, PHASE_BLIND: ROLE_REVIEWER, PHASE_COMPARE: ROLE_REVIEWER,
}

KIND_INITIAL = "initial"
KIND_REVISE = "revise"        # the owner asked for a revision
KIND_REDO = "redo"            # the owner asked for a new start from the stage's input
KIND_FIX = "fix"              # autonomous: the checks failed
KIND_RECONCILE = "reconcile"  # autonomous: the reviewer disagreed
KIND_ADOPTED = "adopted"      # existing work registered with --adopt; no launch
ORIGIN_PIPELINE = "pipeline"
ORIGIN_ADOPTED = "adopted"

PASS_POLICIES = ("both_agree", "reviewer_accept")
DISAGREEMENT_POLICIES = ("human_review", "revise_next_round")

CHECKS_NOTE = (
    "These are checks of form and of cross-references between the files at the "
    "commit. Evidence files are statements of the model that wrote them. A passed "
    "check is not a proof that the mathematics, the code or a number is right."
)

MANIFEST_FORMAT_VERSION = 4
COMMIT_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40}")
WINDOWS_RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


class ProjectStateError(Exception):
    """The project manifest does not allow the requested step."""


class UnsafeArtifactPathError(Exception):
    """An artifact path from the API must not be written to disk."""


class OutputGateError(Exception):
    """A finished stage's output could not be identified."""


class WorkingDocumentError(Exception):
    """The working document does not hold a stage's section as required."""


class LocalSyncError(Exception):
    """The local checkout cannot show a stage's output; says what to do."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def unsafe_relative_path_reason(path: Any) -> Optional[str]:
    """Return why a path is not a safe relative path, or None if it is safe.

    Safe means: relative, separated by forward slashes, with no backslash,
    colon or control character, and with no segment that is empty, '.',
    '..', a Windows device name, or ends in a dot or a space.
    """
    if not isinstance(path, str) or not path:
        return "it is not a non-empty string"
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        return "it contains a control character"
    if "\\" in path:
        return "it contains a backslash"
    if ":" in path:
        return "it contains a colon"
    if path.startswith("/"):
        return "it is an absolute path"
    for segment in path.split("/"):
        if segment == "":
            return "it has an empty segment"
        if segment in (".", ".."):
            return f"it has the segment '{segment}'"
        if segment != segment.rstrip(". "):
            return f"the segment '{segment}' ends in a dot or a space"
        if segment.split(".")[0].upper() in WINDOWS_RESERVED_NAMES:
            return f"the segment '{segment}' is a Windows device name"
    return None


def safe_artifact_destination(root: Path, artifact_path: Any) -> Path:
    """Return where an artifact is saved under `root`.

    The artifact path comes from the API and is not trusted.

    Raises:
        UnsafeArtifactPathError: if the path is unsafe or would land
            outside `root`.
    """
    reason = unsafe_relative_path_reason(artifact_path)
    if reason:
        raise UnsafeArtifactPathError(reason)
    destination = root.joinpath(*artifact_path.split("/"))
    if not destination.resolve().is_relative_to(root.resolve()):
        raise UnsafeArtifactPathError("it resolves to a location outside the download folder")
    return destination


def stage_section_markers(number: int, name: str) -> tuple[str, str]:
    """Return the two lines that enclose a stage's section of the working document."""
    return (
        f"<!-- BEGIN STAGE {number}: {name} -->",
        f"<!-- END STAGE {number}: {name} -->",
    )


def _marker_positions(text: str, marker: str) -> list[tuple[int, int]]:
    """Return (start, end) of every line of `text` that holds only this marker."""
    pattern = re.compile(rf"^[ \t]*{re.escape(marker)}[ \t]*\r?$", re.MULTILINE)
    return [match.span() for match in pattern.finditer(text)]


def has_stage_section_marker(text: str, number: int, name: str) -> bool:
    """True if either marker line of a stage's section is in the text."""
    return any(
        _marker_positions(text=text, marker=marker)
        for marker in stage_section_markers(number=number, name=name)
    )


def extract_stage_section(text: str, number: int, name: str) -> str:
    """Return the text between a stage's two marker lines.

    Raises:
        WorkingDocumentError: unless each marker is on a line of its own
            exactly once, the begin marker first.
    """
    begin_marker, end_marker = stage_section_markers(number=number, name=name)
    begins = _marker_positions(text=text, marker=begin_marker)
    ends = _marker_positions(text=text, marker=end_marker)
    if len(begins) != 1 or len(ends) != 1:
        raise WorkingDocumentError(
            f"the section of stage {number} must be enclosed by the lines "
            f"'{begin_marker}' and '{end_marker}', each exactly once on a line "
            f"of its own (found {len(begins)} and {len(ends)})"
        )
    if ends[0][0] < begins[0][1]:
        raise WorkingDocumentError(
            f"the end marker of the section of stage {number} comes before its begin marker"
        )
    return text[begins[0][1]:ends[0][0]]


SSH_GITHUB_REMOTE_PATTERN = re.compile(
    r"^(?:ssh://)?git@github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.IGNORECASE,
)


def github_repository_of(url: str) -> Optional[tuple[str, str]]:
    """Return (owner, repository) in lower case for a github.com URL, https or ssh."""
    cleaned = url.strip()
    ssh_match = SSH_GITHUB_REMOTE_PATTERN.match(cleaned)
    if ssh_match:
        return ssh_match.group(1).lower(), ssh_match.group(2).lower()
    try:
        owner, repo = parse_github_repo_url(cleaned)
    except ValueError:
        return None
    return owner.lower(), repo.lower()


# A Git operation that is under way in a checkout leaves one of these in
# its Git folder.
GIT_OPERATION_MARKERS = {
    "MERGE_HEAD": "a merge",
    "CHERRY_PICK_HEAD": "a cherry-pick",
    "REVERT_HEAD": "a revert",
    "rebase-merge": "a rebase",
    "rebase-apply": "a rebase",
}


class LocalReviewCheckout:
    """The local Git checkout in which stage outputs are shown for review.

    It checks out a stage's branch at exactly the commit that passed the
    output check. The only Git commands that change anything are
    `fetch` (without `+`: a rewritten remote branch is refused),
    `checkout` of a branch (without `--force`), `checkout -b` and
    `merge --ff-only`. Nothing is forced, reset, stashed or merged, and
    nothing is done at all while tracked files have uncommitted changes.
    Untracked files are left where they are.
    """
    
    def __init__(
        self,
        path: Path,
        repo_url: str,
        remote: str,
        running_code_paths: Optional[list[str]] = None,
    ):
        """
        Args:
            path: Top folder of the local checkout.
            repo_url: The repository the pipeline works on. The checkout's
                remote must point at it.
            remote: Name of that remote in the checkout.
            running_code_paths: Paths in the checkout, from its top folder,
                of the orchestrator that is running. A commit in which
                these differ from the checked-out ones is not checked out.
        """
        self.path = Path(path)
        self.repo_url = repo_url
        self.remote = remote
        self.running_code_paths = list(running_code_paths or [])
    
    def _run(self, arguments: tuple[str, ...]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", "-C", str(self.path), *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except OSError as error:
            raise LocalSyncError(
                f"Git could not be run in {self.path}: {error}. Git must be "
                f"installed and the folder must exist."
            ) from error
    
    def _git(self, *arguments: str, problem: str) -> str:
        """Run a Git command and return its output; fail with `problem` if it fails."""
        completed = self._run(arguments=arguments)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise LocalSyncError(f"{problem} (git {' '.join(arguments)}: {detail})")
        return completed.stdout.rstrip()
    
    def _git_says_yes(self, *arguments: str, no_codes: tuple[int, ...] = (1,)) -> bool:
        """Run a Git command that answers yes (exit 0) or no (one of `no_codes`)."""
        completed = self._run(arguments=arguments)
        if completed.returncode == 0:
            return True
        if completed.returncode in no_codes:
            return False
        raise LocalSyncError(
            f"git {' '.join(arguments)} failed in {self.path}: "
            f"{(completed.stderr or completed.stdout).strip()}"
        )
    
    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self._git_says_yes("merge-base", "--is-ancestor", ancestor, descendant)
    
    def changed_tracked_files(self) -> list[str]:
        """Tracked files with staged or unstaged changes, as `git status` lists them."""
        output = self._git(
            "status", "--porcelain", "--untracked-files=no",
            problem=f"The state of the local checkout {self.path} could not be read",
        )
        return [line for line in output.splitlines() if line.strip()]
    
    def current_branch(self) -> Optional[str]:
        """The checked-out branch, or None when HEAD is detached."""
        completed = self._run(arguments=("symbolic-ref", "--quiet", "--short", "HEAD"))
        return completed.stdout.strip() if completed.returncode == 0 else None
    
    def state(self) -> dict:
        """Return the checkout's commit, branch and changed tracked files. Changes nothing."""
        return {
            "commit": self._git(
                "rev-parse", "HEAD",
                problem=f"{self.path} is not a Git checkout with a commit",
            ),
            "branch": self.current_branch(),
            "changed_files": self.changed_tracked_files(),
        }
    
    def check_ready(self) -> None:
        """Check that a stage output could be checked out here. Changes nothing.

        Raises:
            LocalSyncError: if the folder is not the top of a checkout of
                the repository, a Git operation is under way, or tracked
                files have uncommitted changes.
        """
        top = self._git(
            "rev-parse", "--show-toplevel",
            problem=f"{self.path} is not a Git checkout",
        )
        if Path(top).resolve() != self.path.resolve():
            raise LocalSyncError(
                f"{self.path} is inside the Git checkout {top}, not its top folder. "
                f"Set review.local_checkout to the top folder."
            )
        remote_url = self._git(
            "config", "--get", f"remote.{self.remote}.url",
            problem=f"The local checkout {self.path} has no remote named '{self.remote}'",
        )
        if github_repository_of(remote_url) != github_repository_of(self.repo_url):
            raise LocalSyncError(
                f"The remote '{self.remote}' of the local checkout {self.path} points at "
                f"{remote_url}, not at the pipeline's repository {self.repo_url}."
            )
        for marker, operation in GIT_OPERATION_MARKERS.items():
            marker_path = self._git(
                "rev-parse", "--git-path", marker,
                problem=f"The Git folder of {self.path} could not be read",
            )
            if (self.path / marker_path).exists():
                raise LocalSyncError(
                    f"{operation.capitalize()} is under way in the local checkout "
                    f"{self.path}. Finish or abort it, then resume."
                )
        changed = self.changed_tracked_files()
        if changed:
            raise LocalSyncError(
                f"The local checkout {self.path} has uncommitted changes to tracked "
                f"files, and a stage output is never checked out over them:\n    "
                + "\n    ".join(changed[:20])
                + ("\n    ..." if len(changed) > 20 else "")
                + "\n  Commit them (and push them, if they are review edits to hand "
                "on) or restore the files yourself, then resume."
            )
    
    def show_commit(self, branch: str, commit: str, review_files: list[str]) -> dict:
        """Check out `branch` at exactly `commit` and return the record of it.

        Raises:
            LocalSyncError: if that cannot be done without forcing, or the
                result cannot be verified. The message says what to do.
        """
        self.check_ready()
        previous_branch = self.current_branch()
        previous_commit = self._git(
            "rev-parse", "HEAD", problem=f"{self.path} has no commit checked out",
        )
        tracking_ref = f"refs/remotes/{self.remote}/{branch}"
        self._git(
            "fetch", "--no-tags", self.remote, f"refs/heads/{branch}:{tracking_ref}",
            problem=(
                f"Branch '{branch}' could not be fetched from '{self.remote}' into "
                f"{self.path}. A remote branch that was rewritten is not accepted"
            ),
        )
        if not self._git_says_yes("cat-file", "-e", f"{commit}^{{commit}}", no_codes=(1, 128)):
            raise LocalSyncError(
                f"Commit {commit} is not in the local checkout {self.path} after "
                f"fetching branch '{branch}'."
            )
        if not self._is_ancestor(ancestor=commit, descendant=tracking_ref):
            raise LocalSyncError(
                f"Commit {commit} is not on branch '{branch}' of '{self.remote}' as fetched."
            )
        if self.running_code_paths and not self._git_says_yes(
            "diff", "--quiet", "HEAD", commit, "--", *self.running_code_paths,
        ):
            raise LocalSyncError(
                f"Commit {commit} holds another version of the orchestrator that is "
                f"running ({', '.join(self.running_code_paths)}) than the local checkout "
                f"{self.path}. Checking it out would replace the running orchestrator, "
                f"so it is not done. Bring the local checkout to the version of the "
                f"orchestrator the project was started from, then resume."
            )
        
        local_ref = f"refs/heads/{branch}"
        if self._git_says_yes("rev-parse", "--verify", "--quiet", local_ref):
            local_tip = self._git(
                "rev-parse", local_ref, problem=f"Local branch '{branch}' could not be read",
            )
            if local_tip != commit and not self._is_ancestor(ancestor=local_tip, descendant=commit):
                if self._is_ancestor(ancestor=commit, descendant=local_tip):
                    raise LocalSyncError(
                        f"Local branch '{branch}' in {self.path} has commits of your own "
                        f"on top of the stage output {commit}; they are left as they "
                        f"are. To hand them on, push them (git push {self.remote} "
                        f"{branch}) and name the pushed commit when you approve or revise."
                    )
                raise LocalSyncError(
                    f"Local branch '{branch}' in {self.path} (at {local_tip}) and the "
                    f"stage output {commit} have diverged. Nothing was changed. Move "
                    f"your local commits to another branch (git branch -m {branch} "
                    f"<another name>), then resume."
                )
            if previous_branch != branch:
                self._git(
                    "checkout", "--quiet", branch,
                    problem=f"Branch '{branch}' could not be checked out in {self.path}",
                )
            if local_tip != commit:
                self._git(
                    "merge", "--ff-only", "--quiet", commit,
                    problem=f"Branch '{branch}' could not be fast-forwarded to {commit}",
                )
        else:
            self._git(
                "checkout", "--quiet", "-b", branch, commit,
                problem=(
                    f"Branch '{branch}' could not be created at {commit} and checked "
                    f"out in {self.path}"
                ),
            )
            self._git(
                "branch", "--quiet", f"--set-upstream-to={self.remote}/{branch}", branch,
                problem=f"Branch '{branch}' could not be set to follow '{self.remote}/{branch}'",
            )
        
        state = self.state()
        if state["commit"] != commit or state["changed_files"]:
            raise LocalSyncError(
                f"After the checkout, {self.path} is at {state['commit']} with "
                f"{len(state['changed_files'])} changed tracked file(s), not cleanly at {commit}."
            )
        missing = [path for path in review_files if not (self.path / path).is_file()]
        if missing:
            raise LocalSyncError(
                f"After the checkout of {commit}, these files to review are not in "
                f"{self.path}: {', '.join(missing)}"
            )
        return {
            "verified": True,
            "synced_at": utc_now(),
            "checkout": str(self.path),
            "remote": self.remote,
            "branch": branch,
            "commit": commit,
            "previous_branch": previous_branch,
            "previous_commit": previous_commit,
            "review_files": [str(self.path / path) for path in review_files],
        }


@dataclass
class StageResult:
    """The recorded state of one stage of a project: its current round.

    Earlier rounds of the stage are kept, whole, under the manifest's
    `previous_rounds`.
    """
    name: str
    status: str = STAGE_PENDING
    # 0: no round was started. Each round counts once against the stage's
    # limit, whatever started it and however it ended.
    round: int = 0
    kind: Optional[str] = None
    origin: str = ORIGIN_PIPELINE
    # What started the round: "execute", "adopt" or the ID of a decision.
    started_by: Optional[str] = None
    phase: str = PHASE_PROPOSE
    
    # The current launch of the round (one per phase).
    role: Optional[str] = None
    model: Optional[dict] = None
    agent_id: Optional[str] = None
    run_id: Optional[str] = None
    agent_url: Optional[str] = None
    run_status: Optional[str] = None
    # The exact commit sent as startingRef of the current launch.
    launch_commit: Optional[str] = None
    prompt_sha256: Optional[str] = None
    # IDs of the journal events whose entries the agent was given to append
    # to the trial log (None: the launch does not write the trial log).
    log_event_ids: Optional[list] = None
    pushed_branch: Optional[str] = None
    duration_ms: Optional[int] = None
    artifacts: Optional[dict] = None
    # Records of the launches of this round that are over, in order.
    launches: list = field(default_factory=list)
    
    # What the round started from, and the commit the stage started from in
    # its first round. The source document, the earlier sections and the
    # allowed paths are checked against the baseline in every round.
    input_source: Optional[str] = None
    input_commit: Optional[str] = None
    baseline_commit: Optional[str] = None
    # The proposer's (or the adopted) commit and the record of its checks.
    candidate_branch: Optional[str] = None
    candidate_commit: Optional[str] = None
    # Adoption only: the commit that holds the stage's content, when the
    # adopted commit is a later one.
    content_commit: Optional[str] = None
    gate: Optional[dict] = None
    # Cross-model review: the blind assessment and the comparison.
    cross_review: Optional[dict] = None
    outcome: Optional[str] = None
    stop_reason: Optional[str] = None
    # The branch and commit shown for review. Later stages start from this
    # commit once it is accepted, not from the branch.
    output_branch: Optional[str] = None
    output_commit: Optional[str] = None
    local_sync: Optional[dict] = None
    # {"event": the signed notice, "ack": the host's signed ack or None}.
    notice: Optional[dict] = None
    notice_seq: int = 0
    review: Optional[dict] = None
    # After acceptance: {"event": the signed notice of the acceptance}.
    receipt: Optional[dict] = None
    # Set on a round that revises an earlier round's output.
    revision: Optional[dict] = None
    error: Optional[str] = None
    closed_at: Optional[str] = None
    completed_at: Optional[str] = None


LAUNCH_FIELDS = (
    "phase", "role", "model", "agent_id", "run_id", "agent_url", "run_status",
    "launch_commit", "prompt_sha256", "log_event_ids", "pushed_branch",
    "duration_ms", "artifacts",
)


@dataclass
class PipelineManifest:
    """Manifest tracking pipeline execution state."""
    pipeline_name: str
    repo_url: str
    started_at: str
    project_id: str = ""
    # {config_sha256, code (file -> SHA-256), created_by, repins}.
    provenance: dict = field(default_factory=dict)
    stages: dict[str, StageResult] = field(default_factory=dict)
    completed_at: Optional[str] = None
    # The document selected when this project was started: path, format,
    # size_bytes, sha256 and selected_at. The document's text is not stored.
    model_setup_document: Optional[dict] = None
    # The configured starting ref, and the commit it pointed at when the
    # project was started. The first stage starts from that commit.
    source_ref: Optional[str] = None
    source_commit: Optional[str] = None
    # Records of the rounds that were followed by another round.
    previous_rounds: list[dict] = field(default_factory=list)
    # The owner's decisions that were applied, whole and signed, in order.
    decisions: list[dict] = field(default_factory=list)
    # Rounds added to a stage's limit by raise_limit decisions: stage -> number.
    round_limit_raises: dict = field(default_factory=dict)
    # Set while the project is halted: {at, reason, by}.
    halted: Optional[dict] = None
    
    def to_dict(self) -> dict:
        return {
            "format_version": MANIFEST_FORMAT_VERSION,
            "pipeline_name": self.pipeline_name,
            "repo_url": self.repo_url,
            "project_id": self.project_id,
            "provenance": self.provenance,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "model_setup_document": self.model_setup_document,
            "source_ref": self.source_ref,
            "source_commit": self.source_commit,
            "stages": {name: asdict(result) for name, result in self.stages.items()},
            "previous_rounds": self.previous_rounds,
            "decisions": self.decisions,
            "round_limit_raises": self.round_limit_raises,
            "halted": self.halted,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "PipelineManifest":
        """
        Raises:
            ProjectStateError: if the manifest has another format version
                or a stage record with unknown fields. A manifest of an
                earlier version is not converted: its records do not hold
                what this version checks before it goes on.
        """
        if data.get("format_version") != MANIFEST_FORMAT_VERSION:
            raise ProjectStateError(
                f"The manifest has format version {data.get('format_version')!r}, "
                f"but this orchestrator reads version {MANIFEST_FORMAT_VERSION}. "
                f"It cannot be resumed with this version of the orchestrator, and "
                f"it is not converted. Work that exists on a branch can be "
                f"registered in a new project with --adopt."
            )
        manifest = cls(
            pipeline_name=data["pipeline_name"],
            repo_url=data["repo_url"],
            started_at=data["started_at"],
            project_id=data.get("project_id", ""),
            provenance=dict(data.get("provenance") or {}),
            completed_at=data.get("completed_at"),
            model_setup_document=data.get("model_setup_document"),
            source_ref=data.get("source_ref"),
            source_commit=data.get("source_commit"),
            previous_rounds=list(data.get("previous_rounds", [])),
            decisions=list(data.get("decisions", [])),
            round_limit_raises=dict(data.get("round_limit_raises") or {}),
            halted=data.get("halted"),
        )
        known_fields = {stage_field.name for stage_field in fields(StageResult)}
        for name, stage_data in data.get("stages", {}).items():
            unknown = sorted(set(stage_data) - known_fields)
            if unknown:
                raise ProjectStateError(
                    f"The manifest record of stage '{name}' has unknown field(s): {unknown}"
                )
            manifest.stages[name] = StageResult(**stage_data)
        return manifest


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# The files of the orchestrator that is running. Their hashes are recorded
# with a project, and a commit that holds another version of them is not
# checked out over them.
ORCHESTRATOR_FILES = (
    "orchestrator.py", "api_client.py", "review_bridge.py", "trial_log.py", "verification.py",
)


def running_code_sha256() -> dict:
    """SHA-256 of each file of the orchestrator that is running."""
    folder = Path(__file__).resolve().parent
    return {name: file_sha256(path=folder / name) for name in ORCHESTRATOR_FILES}


def path_is_under(path: str, entries: list) -> bool:
    """True if a path is one of the entries or inside one of them (a folder)."""
    return any(path == entry or path.startswith(entry + "/") for entry in entries)


class Orchestrator:
    """
    Pipeline orchestrator that launches cloud agents for each stage.
    
    Key behaviors:
    - Asks for the model setup document whenever a new project is started
    - Validates models against GET /v1/models before launch
    - Works on one stage at a time, in rounds, up to a fixed number per
      stage; a round is counted before any work is done for it
    - Creates each agent at most once, under a recorded agent ID
    - Checks each round's output at the pushed commit: required files, the
      working document's sections, changed paths, the trial log and the
      stage's evidence file (form and cross-references, not correctness)
    - Ends every round at a human gate unless the stage allows an
      autonomous correction round and one is possible; never starts
      another stage on its own
    - At the gate: checks the output out in the local checkout, writes a
      signed notice, and waits for the host's ack and the owner's signed
      decision for that exact commit
    - Records every step in the manifest and in the trial journal
    """
    
    def __init__(
        self,
        config: dict,
        api_client: Optional[CursorAPIClient] = None,
        dry_run: bool = True,
        ask: Callable[[str], str] = input,
        github: Optional[GitHubRepoReader] = None,
        sleep: Callable[[float], None] = time.sleep,
        manifest_path: Optional[Path] = None,
        local_review: Optional[LocalReviewCheckout] = None,
        bridge_secret: Optional[str] = None,
    ):
        self.config = config
        self.dry_run = dry_run
        self.api = api_client or CursorAPIClient(
            base_url=config.get("api", {}).get("base_url"),
        )
        # Called with the question text; returns the user's answer.
        self.ask = ask
        self._github = github
        # Called with a number of seconds to wait between polls and retries.
        self.sleep = sleep
        # The project's manifest file. Every recorded step is written to it.
        self.manifest_path = manifest_path
        # The local checkout that shows each stage's output for review.
        self.local_review = local_review
        # The secret shared with the host of the main chat. None: it is
        # read from the environment variable named in review.delivery.
        self.bridge_secret = bridge_secret
        
        self.pipeline_config = config.get("pipeline", {})
        self.repo_config = self.pipeline_config.get("repository", {})
        self.api_config = config.get("api", {})
        self.stages = self._parse_stages(config.get("stages", []))
        self.execution_config = config.get("execution", {})
        self.artifacts_config = config.get("artifacts", {})
        # None when the config has no cumulative working document.
        self.working_document_config: Optional[dict] = config.get("working_document")
        self.review_config = config.get("review", {})
        # None when the config has no trial log in the repository.
        self.trial_log_config: Optional[dict] = config.get("trial_log")
        
        self.manifest: Optional[PipelineManifest] = None
        self.model_setup_document: Optional[ModelSetupDocument] = None
        # Branches reported by the run that was just waited for.
        self._reported_branches: Optional[list[dict]] = None
        # Content of files at commits, as read from GitHub. A commit never changes.
        self._file_cache: dict[tuple[str, str], Optional[bytes]] = {}
    
    @property
    def github(self) -> GitHubRepoReader:
        if self._github is None:
            self._github = GitHubRepoReader(repo_url=self.repo_config["url"])
        return self._github
    
    @property
    def retry_max_attempts(self) -> int:
        """How many times a request is tried in all, the first try included."""
        return self.api_config["retries"]["max_attempts"]
    
    @property
    def retry_backoff(self) -> list:
        """Seconds to wait before each further try."""
        return self.api_config["retries"]["backoff_seconds"]
    
    @property
    def config_sha256(self) -> str:
        """SHA-256 of the configuration as it was read, in canonical JSON."""
        return hashlib.sha256(canonical_json(self.config)).hexdigest()
    
    def _project_file(self, suffix: str) -> Path:
        if self.manifest_path is None:
            raise RuntimeError("The project has no manifest file. Set manifest_path.")
        return self.manifest_path.resolve().with_name(self.manifest_path.stem + suffix)
    
    @property
    def bridge(self) -> ReviewBridge:
        """The outbox and inbox of this project's notices, beside its manifest."""
        delivery = self.review_config["delivery"]
        secret = self.bridge_secret
        if secret is None:
            secret = os.environ.get(delivery["secret_env"])
        if self.manifest_path is None:
            raise RuntimeError("The project has no manifest file. Set manifest_path.")
        return ReviewBridge(
            directory=(
                self.manifest_path.resolve().parent / delivery["directory"]
                / self.manifest_path.stem
            ),
            secret=secret,
            secret_env=delivery["secret_env"],
        )
    
    @property
    def journal(self) -> TrialJournal:
        """The orchestrator's own record of this project's events, beside its manifest."""
        return TrialJournal(path=self._project_file(suffix=".trial-journal.jsonl"))
    
    @property
    def trial_log_path(self) -> Optional[str]:
        """Path of the trial log in the repository, or None if there is none."""
        return self.trial_log_config["path"] if self.trial_log_config else None
    
    def _parse_stages(self, stages_list: list) -> list[StageConfig]:
        """Parse stage configurations from config."""
        stages = []
        for stage_data in stages_list:
            stages.append(StageConfig(
                name=stage_data["name"],
                display_name=stage_data.get("display_name", stage_data["name"]),
                description=stage_data.get("description", ""),
                prompt=stage_data.get("prompt", ""),
                model_id=stage_data.get("model_id"),
                model_params=stage_data.get("model_params", []),
                depends_on=stage_data.get("depends_on"),
                output_manifest=stage_data.get("output_manifest", {}),
                guardrails=stage_data.get("guardrails", []),
                receives_model_setup_document=stage_data.get(
                    "receives_model_setup_document", False
                ),
                iteration=stage_data.get("iteration", {}),
                verification=stage_data.get("verification"),
                cross_model=stage_data.get("cross_model", {}),
            ))
        return stages
    
    def stage_number(self, stage: StageConfig) -> int:
        """The stage's place in the pipeline, counted from 1."""
        return self.stages.index(stage) + 1
    
    def stage_named(self, name: Any) -> StageConfig:
        """
        Raises:
            ProjectStateError: if the config defines no stage of this name.
        """
        for stage in self.stages:
            if stage.name == name:
                return stage
        raise ProjectStateError(
            f"The config defines no stage named {name!r}. The stages are: "
            f"{', '.join(stage.name for stage in self.stages)}."
        )
    
    def round_limit(self, stage: StageConfig) -> int:
        """The most rounds the stage may take: the configured limit plus the owner's raises."""
        raised = self.manifest.round_limit_raises.get(stage.name, 0) if self.manifest else 0
        return stage.max_rounds + raised
    
    def working_document_rules(self, stage: StageConfig) -> Optional[str]:
        """Return the working document rules appended to a stage's prompt.

        They name the same marker lines and the same rules that
        _working_document_problems checks at the commit the stage pushes.
        None when the config has no working document.
        """
        if not self.working_document_config:
            return None
        number = self.stage_number(stage=stage)
        begin_marker, end_marker = stage_section_markers(number=number, name=stage.name)
        earlier = [str(n) for n in range(1, number)]
        later = [str(n) for n in range(number + 1, len(self.stages) + 1)]
        rules = [
            f"Your section is enclosed by these two lines, each exactly once, each "
            f"on a line of its own, written exactly as here:\n"
            f"   {begin_marker}\n"
            f"   {end_marker}\n"
            f"   Write this stage's work between them. It must not be empty.",
        ]
        if earlier:
            rules.append(
                f"The section(s) of the earlier stage(s) ({', '.join(earlier)}) stay "
                f"byte-for-byte as they are in the commit you start from. The project "
                f"owner accepted them. If you find an error in one, do not edit it: "
                f"report it in your own section under the heading \"Issues found in "
                f"earlier stages\"."
            )
        if later:
            rules.append(
                f"The document has no section and no marker line of a later stage "
                f"({', '.join(later)}). Write nothing for a later stage, not even a "
                f"placeholder."
            )
        rules.append(
            f"The file {self.working_document_config['source_document']} (the original "
            f"model setup document) stays byte-for-byte unchanged."
        )
        numbered = "\n".join(f"{index}. {rule}" for index, rule in enumerate(rules, start=1))
        return (
            f"{WORKING_DOCUMENT_HEADING}\n"
            f"Path in the repository: {self.working_document_config['path']}\n"
            f"This is stage {number} of {len(self.stages)}. The rules below are checked "
            f"at the commit you push; a failed check stops the pipeline.\n"
            f"{numbered}"
        )
    
    def validate_config(self) -> tuple[bool, list[str]]:
        """
        Validate the configuration file.
        
        Returns:
            Tuple of (is_valid, list_of_errors)
        """
        errors = []
        
        if not self.repo_config.get("url"):
            errors.append("Missing pipeline.repository.url")
        
        if not self.stages:
            errors.append("No stages defined")
        
        stage_names = {s.name for s in self.stages}
        for stage in self.stages:
            if stage.depends_on and stage.depends_on not in stage_names:
                errors.append(f"Stage '{stage.name}' depends on unknown stage '{stage.depends_on}'")
            
            if not stage.prompt.strip():
                errors.append(f"Stage '{stage.name}' has empty prompt")
            
            guardrails_ok = isinstance(stage.guardrails, list) and all(
                isinstance(guardrail, str) and guardrail.strip()
                for guardrail in stage.guardrails
            )
            if not guardrails_ok:
                errors.append(
                    f"Stage '{stage.name}': guardrails must be a list of non-empty strings"
                )
            
            if not isinstance(stage.receives_model_setup_document, bool):
                errors.append(
                    f"Stage '{stage.name}': receives_model_setup_document "
                    f"must be true or false"
                )
            
            errors.extend(self._model_selection_errors(stage=stage))
            errors.extend(self._stage_setting_errors(stage=stage))
            errors.extend(self._iteration_setting_errors(stage=stage))
            errors.extend(self._verification_setting_errors(stage=stage))
            errors.extend(self._cross_model_setting_errors(stage=stage))
        
        # The document asked for at project start has to reach the stage
        # that starts the pipeline, or asking for it would have no effect.
        if self.stages and self.stages[0].receives_model_setup_document is not True:
            errors.append(
                f"Stage '{self.stages[0].name}' is the first stage and must set "
                f"receives_model_setup_document: true"
            )
        
        errors.extend(self._pipeline_setting_errors())
        
        return len(errors) == 0, errors
    
    def _stage_setting_errors(self, stage: StageConfig) -> list[str]:
        """Check a stage's review and output-check settings."""
        prefix = f"Stage '{stage.name}'"
        errors = []
        
        stage_data = self.config["stages"][self.stages.index(stage)]
        if "manual_review" in stage_data:
            errors.append(
                f"{prefix}: manual_review is not a setting. Every stage stops "
                f"for review and goes on only by a decision of the owner; remove the line"
            )
        
        if not isinstance(stage.output_manifest, dict):
            return errors + [f"{prefix}: output_manifest must be a mapping"]
        if "completion_record" in stage.output_manifest:
            errors.append(
                f"{prefix}: output_manifest.completion_record is not a setting. The "
                f"stage's evidence file (verification) takes its place; remove the line"
            )
        for key in ("required_files", "allowed_paths"):
            if key == "allowed_paths" and key not in stage.output_manifest:
                continue
            paths = stage.output_manifest.get(key, [])
            if not isinstance(paths, list):
                errors.append(f"{prefix}: output_manifest.{key} must be a list of paths")
                continue
            for path in paths:
                reason = unsafe_relative_path_reason(path)
                if reason:
                    errors.append(
                        f"{prefix}: output_manifest.{key} entry {path!r} is not a safe "
                        f"path relative to the repository root: {reason}"
                    )
        return errors
    
    def _iteration_setting_errors(self, stage: StageConfig) -> list[str]:
        """Check a stage's round limit and whether it may correct itself."""
        prefix = f"Stage '{stage.name}': iteration"
        iteration = stage.iteration
        if not isinstance(iteration, dict):
            return [f"{prefix} must be a mapping with max_rounds"]
        errors = []
        unknown = sorted(set(iteration) - {"max_rounds", "autonomous_fix"})
        if unknown:
            errors.append(f"{prefix} has unknown setting(s): {unknown}")
        max_rounds = iteration.get("max_rounds")
        if not (isinstance(max_rounds, int) and not isinstance(max_rounds, bool) and max_rounds >= 1):
            errors.append(
                f"{prefix}.max_rounds must be a whole number, 1 or more: the most "
                f"rounds the stage may take. There is no default"
            )
        if not isinstance(iteration.get("autonomous_fix", False), bool):
            errors.append(f"{prefix}.autonomous_fix must be true or false")
        return errors
    
    def _verification_setting_errors(self, stage: StageConfig) -> list[str]:
        """Check a stage's evidence settings."""
        prefix = f"Stage '{stage.name}': verification"
        settings = stage.verification
        if settings is None:
            return []
        if not isinstance(settings, dict) or settings.get("kind") not in verification.KINDS:
            return [f"{prefix}.kind must be one of {', '.join(verification.KINDS)}"]
        path_keys = {
            verification.KIND_TEST_MAP: ("implementation_file", "test_file"),
            verification.KIND_PARAMETER_BATCHES: ("next_batch_spec",),
        }.get(settings["kind"], ())
        number_keys = (
            ("max_configurations",)
            if settings["kind"] == verification.KIND_PARAMETER_BATCHES else ()
        )
        errors = []
        unknown = sorted(set(settings) - {"kind", "evidence_file", *path_keys, *number_keys})
        if unknown:
            errors.append(f"{prefix} has unknown setting(s) for kind {settings['kind']}: {unknown}")
        for key in ("evidence_file", *path_keys):
            reason = unsafe_relative_path_reason(settings.get(key))
            if reason:
                errors.append(
                    f"{prefix}.{key} must be a safe path relative to the repository root: {reason}"
                )
        for key in number_keys:
            value = settings.get(key)
            if not (isinstance(value, int) and not isinstance(value, bool) and value >= 1):
                errors.append(f"{prefix}.{key} must be a whole number, 1 or more")
        if settings["kind"] == verification.KIND_PARAMETER_BATCHES and stage.autonomous_fix is True:
            errors.append(
                f"Stage '{stage.name}': iteration.autonomous_fix cannot be true for "
                f"verification kind {verification.KIND_PARAMETER_BATCHES}: every batch "
                f"needs the owner's approval before it is run"
            )
        return errors
    
    def _cross_model_setting_errors(self, stage: StageConfig) -> list[str]:
        """Check a stage's cross-model review settings."""
        prefix = f"Stage '{stage.name}': cross_model"
        settings = stage.cross_model
        if not isinstance(settings, dict):
            return [f"{prefix} must be a mapping"]
        errors = []
        known = {
            "enabled", "secondary_model_id", "secondary_model_params",
            "pass_policy", "on_disagreement", "review_dir",
        }
        unknown = sorted(set(settings) - known)
        if unknown:
            errors.append(f"{prefix} has unknown setting(s): {unknown}")
        enabled = settings.get("enabled", False)
        if not isinstance(enabled, bool):
            return errors + [f"{prefix}.enabled must be true or false"]
        if not enabled:
            return errors
        
        secondary = settings.get("secondary_model_id")
        if stage.model_id is None or not (isinstance(secondary, str) and secondary.strip()):
            errors.append(
                f"{prefix} is enabled, so the stage's model_id and "
                f"{prefix}.secondary_model_id must both name a model. No model is "
                f"chosen for you"
            )
        elif secondary == stage.model_id:
            errors.append(
                f"{prefix}.secondary_model_id is the stage's own model "
                f"({stage.model_id}); the reviewer must be a different model"
            )
        errors.extend(self._model_errors(
            prefix=f"{prefix} secondary",
            model_id=secondary,
            params=settings.get("secondary_model_params", []),
        ))
        if settings.get("pass_policy") not in PASS_POLICIES:
            errors.append(f"{prefix}.pass_policy must be one of {', '.join(PASS_POLICIES)}")
        if settings.get("on_disagreement") not in DISAGREEMENT_POLICIES:
            errors.append(
                f"{prefix}.on_disagreement must be one of {', '.join(DISAGREEMENT_POLICIES)}"
            )
        elif settings["on_disagreement"] == "revise_next_round" and stage.autonomous_fix is not True:
            errors.append(
                f"{prefix}.on_disagreement is revise_next_round, which starts a round "
                f"without the owner; that needs iteration.autonomous_fix: true"
            )
        reason = unsafe_relative_path_reason(settings.get("review_dir"))
        if reason:
            errors.append(
                f"{prefix}.review_dir must be a safe folder relative to the repository "
                f"root: {reason}"
            )
        return errors
    
    def _pipeline_setting_errors(self) -> list[str]:
        """Check the repository, retry, artifact, review and trial log settings."""
        errors = []
        
        repo_url = self.repo_config.get("url")
        if repo_url:
            try:
                parse_github_repo_url(repo_url)
            except ValueError:
                errors.append(
                    f"pipeline.repository.url must be a github.com repository URL "
                    f"(got {repo_url!r}). Commits and required files are read "
                    f"through the GitHub REST API."
                )
        
        retries = self.api_config.get("retries")
        max_attempts = retries.get("max_attempts") if isinstance(retries, dict) else None
        backoff = retries.get("backoff_seconds") if isinstance(retries, dict) else None
        if not (isinstance(max_attempts, int) and not isinstance(max_attempts, bool)
                and max_attempts >= 1):
            errors.append("api.retries.max_attempts must be a whole number, 1 or more")
        if not (
            isinstance(backoff, list) and backoff
            and all(
                isinstance(delay, (int, float)) and not isinstance(delay, bool) and delay >= 0
                for delay in backoff
            )
        ):
            errors.append(
                "api.retries.backoff_seconds must be a non-empty list of waits "
                "in seconds, each 0 or more"
            )
        
        collect = self.artifacts_config.get("collect", False)
        if not isinstance(collect, bool):
            errors.append("artifacts.collect must be true or false")
        elif collect:
            output_dir = self.artifacts_config.get("local_output_dir")
            reason = unsafe_relative_path_reason(output_dir)
            if reason:
                errors.append(
                    f"artifacts.local_output_dir must be a safe relative folder "
                    f"when artifacts.collect is true: {reason}"
                )
        
        review = self.review_config if isinstance(self.review_config, dict) else {}
        for key in ("local_checkout", "remote"):
            value = review.get(key)
            if not (isinstance(value, str) and value.strip()):
                errors.append(
                    f"review.{key} must be set: every stage output is checked out "
                    f"in a local Git checkout before it is announced for review"
                )
        delivery = review.get("delivery")
        if not isinstance(delivery, dict):
            errors.append(
                "review.delivery must be set (directory and secret_env): an output is "
                "announced for review by a signed notice file, and only the host's "
                "signed ack counts as delivery"
            )
        else:
            reason = unsafe_relative_path_reason(delivery.get("directory"))
            if reason:
                errors.append(
                    f"review.delivery.directory must be a safe relative folder: {reason}"
                )
            secret_env = delivery.get("secret_env")
            if not (isinstance(secret_env, str) and re.fullmatch(r"[A-Z_][A-Z0-9_]*", secret_env)):
                errors.append(
                    "review.delivery.secret_env must be the name of an environment "
                    "variable (capital letters, digits and _). The secret itself is "
                    "never written in the config"
                )
            unknown = sorted(set(delivery) - {"directory", "secret_env"})
            if unknown:
                errors.append(
                    f"review.delivery has unknown setting(s): {', '.join(unknown)}. Only "
                    f"directory and secret_env are settings; the secret itself is "
                    f"never written in the config"
                )
        
        if self.working_document_config is not None:
            for key in ("path", "source_document"):
                value = (
                    self.working_document_config.get(key)
                    if isinstance(self.working_document_config, dict) else None
                )
                reason = unsafe_relative_path_reason(value)
                if reason:
                    errors.append(
                        f"working_document.{key} must be a safe path relative to "
                        f"the repository root: {reason}"
                    )
        
        if self.trial_log_config is not None:
            value = (
                self.trial_log_config.get("path")
                if isinstance(self.trial_log_config, dict) else None
            )
            reason = unsafe_relative_path_reason(value)
            if reason:
                errors.append(
                    f"trial_log.path must be a safe path relative to the repository "
                    f"root: {reason}"
                )
        return errors
    
    def _model_selection_errors(self, stage: StageConfig) -> list[str]:
        """Check that a stage's model settings can be written as the API's `model` field."""
        return self._model_errors(
            prefix=f"Stage '{stage.name}'", model_id=stage.model_id, params=stage.model_params,
        )
    
    def _model_errors(self, prefix: str, model_id: Any, params: Any) -> list[str]:
        """Check that a model ID and its parameters can be written as the API's `model` field.

        The API takes `model.id` (a string) and `model.params` (a list of
        `{id, value}` items). Whether the API lists these IDs and values is
        checked separately, by validate_models().
        """
        errors = []
        
        if model_id is not None and not (isinstance(model_id, str) and model_id.strip()):
            errors.append(f"{prefix}: model_id must be a non-empty string or null")
        
        if not isinstance(params, list):
            return errors + [f"{prefix}: model_params must be a list of {{id, value}} items"]
        
        if params and model_id is None:
            errors.append(
                f"{prefix}: model_params is set but model_id is null. The API "
                f"takes parameters only together with model.id, so they would "
                f"not be sent"
            )
        
        seen_ids = set()
        for position, param in enumerate(params, start=1):
            is_well_formed = (
                isinstance(param, dict)
                and set(param) == {"id", "value"}
                and all(
                    isinstance(param[key], str) and param[key].strip()
                    for key in ("id", "value")
                )
            )
            if not is_well_formed:
                errors.append(
                    f"{prefix}: model_params item {position} must have exactly "
                    f"'id' and 'value', both non-empty strings (quote values "
                    f"such as \"true\")"
                )
                continue
            if param["id"] in seen_ids:
                errors.append(
                    f"{prefix}: model_params names the parameter '{param['id']}' "
                    f"more than once"
                )
            seen_ids.add(param["id"])
        
        return errors
    
    def model_selections(self) -> list[tuple[str, Optional[str], list]]:
        """Every (label, model ID, parameters) the config can send in a launch request."""
        selections = []
        for stage in self.stages:
            selections.append((f"Stage '{stage.name}'", stage.model_id, stage.model_params))
            if stage.cross_model_enabled:
                selections.append((
                    f"Stage '{stage.name}' (cross_model secondary)",
                    stage.cross_model["secondary_model_id"],
                    stage.cross_model.get("secondary_model_params", []),
                ))
        return selections
    
    def select_model_setup_document(self) -> ModelSetupDocument:
        """Ask the user for the model setup document and read it.

        The question is asked every time this is called; an earlier answer
        is never reused.

        Raises:
            ModelSetupDocumentError: if no usable document is given.
        """
        print("\n=== Model Setup / Equilibrium Definition Document ===\n")
        print("The text of this document is sent to the cloud agent in the launch")
        print("request. The file is only read; it is not changed, moved or committed.\n")
        
        self.model_setup_document = None
        document = ask_for_model_setup_document(ask=self.ask)
        self.model_setup_document = document
        
        receiving = [s.name for s in self.stages if s.receives_model_setup_document]
        print(f"  Document: {document.path}")
        print(f"  Format: {MODEL_SETUP_FORMATS['.' + document.format]}")
        print(f"  Size: {document.size_bytes} bytes")
        print(f"  SHA-256: {document.sha256}")
        print(f"  Sent to stage(s): {', '.join(receiving)}")
        return document
    
    def start_new_project(
        self, created_by: str = "execute", source_ref: Optional[str] = None,
    ) -> PipelineManifest:
        """Start a new project: ask for its document, then open its manifest.

        Args:
            created_by: "execute" (the first stage's first round may be
                launched) or "adopt" (existing work is registered; nothing
                is launched for it).
            source_ref: The ref the project starts from. None means the
                configured starting ref.

        Raises:
            ModelSetupDocumentError: if no usable document is given. No
                manifest is opened in that case.
        """
        self.manifest = None
        document = self.select_model_setup_document()
        
        started_at = datetime.now(timezone.utc).isoformat()
        self.manifest = PipelineManifest(
            pipeline_name=self.pipeline_config.get("name", "unnamed"),
            repo_url=self.repo_config.get("url", ""),
            started_at=started_at,
            project_id=f"prj-{uuid.uuid4().hex[:12]}",
            provenance={
                "created_by": created_by,
                "created_at": started_at,
                "config_sha256": self.config_sha256,
                "code": running_code_sha256(),
                "repins": [],
            },
            model_setup_document={**document.to_record(), "selected_at": started_at},
            source_ref=source_ref or self.repo_config.get("starting_ref", "main"),
        )
        return self.manifest
    
    def resume_project(self, manifest: PipelineManifest, repin: bool = False) -> PipelineManifest:
        """Continue an existing project from its manifest, without asking.

        The manifest is first checked against what this orchestrator can
        verify: its signed records and the configuration it was made with.
        The document is the one recorded when the project was started. It
        is re-read only if a stage that receives it is not yet accepted,
        and it must then have the recorded content.

        Args:
            manifest: The project's manifest.
            repin: Accept that the configuration has changed since the
                project recorded it, and record the new one. Open notices
                are written again, bound to the new configuration.

        Raises:
            ProjectStateError: if the manifest belongs to another repository
                or pipeline layout, fails a check, or was made with another
                configuration and `repin` is not set.
            ModelSetupDocumentError: if a stage still needs the document
                and the recorded document is missing or has changed.
        """
        self.manifest = None
        self.model_setup_document = None
        
        configured_repo = self.repo_config.get("url", "")
        if manifest.repo_url != configured_repo:
            raise ProjectStateError(
                f"The manifest is for repository {manifest.repo_url!r}, but the "
                f"config names {configured_repo!r}."
            )
        known_stages = {stage.name for stage in self.stages}
        unknown_stages = sorted(set(manifest.stages) - known_stages)
        if unknown_stages:
            raise ProjectStateError(
                f"The manifest has stage(s) the config does not define: {unknown_stages}"
            )
        
        if not manifest.source_ref:
            raise ProjectStateError("The manifest records no source ref (source_ref).")
        
        problems = self._manifest_problems(manifest=manifest)
        if problems:
            raise ProjectStateError(
                "The manifest is not accepted as the record of a project:\n  - "
                + "\n  - ".join(problems)
                + "\n  A manifest is written only by this orchestrator. A status in it "
                "counts only together with the signed records that led to it."
            )
        
        pinned = manifest.provenance["config_sha256"]
        if pinned != self.config_sha256:
            if not repin:
                raise ProjectStateError(
                    f"The configuration has changed since this project recorded it "
                    f"(recorded SHA-256 {pinned}, now {self.config_sha256}). Notices "
                    f"and decisions are bound to the recorded configuration. Restore "
                    f"it, or resume once with --repin to record the new one; open "
                    f"notices are then written again and must be answered again."
                )
            self.manifest = manifest
            self._repin(manifest=manifest)
        
        document_still_needed = any(
            stage.receives_model_setup_document
            and not self._is_stage_finished(manifest=manifest, stage=stage)
            for stage in self.stages
        )
        if document_still_needed:
            self.model_setup_document = restore_model_setup_document(
                record=manifest.model_setup_document,
            )
            print("\n=== Model Setup / Equilibrium Definition Document ===\n")
            print("  Using the document recorded for this project (not asked again):")
            print(f"  Document: {self.model_setup_document.path}")
            print(f"  SHA-256: {self.model_setup_document.sha256} (matches the record)")
        
        self.manifest = manifest
        return self.manifest
    
    def _is_stage_finished(self, manifest: PipelineManifest, stage: StageConfig) -> bool:
        existing = manifest.stages.get(stage.name)
        return existing is not None and existing.status == STAGE_COMPLETE
    
    def _manifest_problems(self, manifest: PipelineManifest) -> list[str]:
        """Check a manifest read from a file against what can be verified.

        A stage counts as accepted only with the owner's signed decision
        for its output commit, and as announced only with the signed notice
        and the host's signed ack. A record that claims more than its
        signed records show is refused, so a manifest written by hand
        cannot stand in for a review.
        """
        problems = []
        if not manifest.project_id or not manifest.provenance.get("config_sha256"):
            return ["it has no project ID or no recorded configuration (provenance)"]
        
        bridge = self.bridge
        decisions = {}
        try:
            for decision in manifest.decisions:
                bridge.verify_stored(record=decision, expected_type="decision")
                bridge.check_decision_form(decision=decision)
                decisions[decision["decision_id"]] = decision
        except BridgeError as error:
            return [f"a recorded decision cannot be verified: {error}"]
        
        earlier_accepted = True
        for number, stage in enumerate(self.stages, start=1):
            result = manifest.stages.get(stage.name)
            if result is None or result.round == 0:
                if result is not None and result.status not in (STAGE_PENDING, STAGE_DRY_RUN):
                    problems.append(
                        f"stage '{stage.name}' has status {result.status} but no round"
                    )
                earlier_accepted = False
                continue
            
            prefix = f"stage '{stage.name}' (round {result.round}, {result.status})"
            limit = stage.max_rounds + manifest.round_limit_raises.get(stage.name, 0)
            if result.round > limit:
                problems.append(f"{prefix} is past the stage's limit of {limit} round(s)")
            if not earlier_accepted:
                problems.append(f"{prefix} has a round although the stage before it is not accepted")
            first_round = next(
                (
                    earlier for earlier in manifest.previous_rounds
                    if earlier["name"] == stage.name and earlier["round"] == 1
                ),
                asdict(result),
            )
            if number > 1 and first_round["origin"] != ORIGIN_ADOPTED:
                start = decisions.get(first_round["started_by"])
                if not (
                    start and start["action"] == ACTION_START_STAGE
                    and start["stage"] == self.stages[number - 2].name
                ):
                    problems.append(
                        f"{prefix} was started without a recorded decision of the "
                        f"owner to start it"
                    )
            if result.origin == ORIGIN_ADOPTED:
                if result.agent_id or result.run_id or result.artifacts or result.launches:
                    problems.append(f"{prefix} is adopted work but records an agent or a run")
            elif (
                result.status not in (STAGE_PENDING, STAGE_BLOCKED)
                and not result.agent_id and not result.launches
            ):
                problems.append(f"{prefix} records no agent")
            
            try:
                if result.status in (STAGE_AWAITING_REVIEW, STAGE_COMPLETE):
                    notice = (result.notice or {}).get("event")
                    ack = (result.notice or {}).get("ack")
                    bridge.verify_stored(record=notice, expected_type="notice")
                    bridge.verify_stored(record=ack, expected_type="ack")
                    if (
                        ack["event_id"] != notice["event_id"]
                        or ack["event_payload_sha256"] != notice["payload_sha256"]
                    ):
                        raise BridgeError("the recorded ack is not the ack of its notice")
                if result.status == STAGE_COMPLETE:
                    decision = decisions.get((result.review or {}).get("decision_id"))
                    if not (
                        decision
                        and decision["action"] == ACTION_ACCEPT
                        and decision["stage"] == stage.name
                        and decision["event_id"] == result.notice["event"]["event_id"]
                        and decision["commit"] == result.output_commit
                        and result.output_commit
                        and result.outcome == OUTCOME_PASSED
                    ):
                        raise BridgeError(
                            "no recorded decision of the owner accepts its output "
                            "commit, or its round did not pass its checks"
                        )
            except BridgeError as error:
                problems.append(f"{prefix}: {error}")
            earlier_accepted = result.status == STAGE_COMPLETE
        return problems
    
    def _repin(self, manifest: PipelineManifest) -> None:
        """Record a changed configuration for a project, and open its notices again.

        Raises:
            ProjectStateError: if a round is under way. The configuration
                of a round is fixed from its first launch to its gate.
        """
        settled = (STAGE_SYNC_PENDING, STAGE_NOTICE_PENDING, STAGE_AWAITING_REVIEW, STAGE_COMPLETE)
        under_way = [
            name for name, result in manifest.stages.items()
            if result.round > 0 and result.status not in settled
        ]
        if under_way:
            raise ProjectStateError(
                f"--repin is refused while a round is under way ({', '.join(under_way)}: "
                f"{manifest.stages[under_way[0]].status}). Resume with the recorded "
                f"configuration until the round is at its gate."
            )
        previous = manifest.provenance["config_sha256"]
        repins = manifest.provenance.setdefault("repins", [])
        self._record_event(
            stage=None, round_number=0, kind=f"repin-{len(repins) + 1}",
            facts={
                "What happened": "The configuration was changed and recorded with --repin",
                "Earlier config SHA-256": previous,
                "New config SHA-256": self.config_sha256,
                "Effect": "Open notices are written again; earlier decisions for them no longer apply",
            },
        )
        repins.append({"at": utc_now(), "from": previous, "to": self.config_sha256})
        manifest.provenance["config_sha256"] = self.config_sha256
        manifest.provenance["code"] = running_code_sha256()
        for result in manifest.stages.values():
            if result.status in (STAGE_NOTICE_PENDING, STAGE_AWAITING_REVIEW):
                self._reopen_notice(result=result)
            elif result.status == STAGE_COMPLETE:
                result.receipt = None
        self.checkpoint()
        print(f"\nThe new configuration is recorded (SHA-256 {self.config_sha256}).")
    
    def validate_models(self) -> tuple[bool, list[str]]:
        """
        Validate every configured model against GET /v1/models.
        
        Returns:
            Tuple of (all_valid, list_of_errors)
        """
        errors = []
        
        try:
            models = self._with_retries(
                action=self.api.list_models, description="Listing models",
            )
            available_ids = set()
            for model in models:
                available_ids.add(model.id)
                available_ids.update(model.aliases)
            
            print(f"  Available models: {len(models)}")
            
            for label, model_id, model_params in self.model_selections():
                if model_id is None:
                    print(f"  {label}: using configured default model")
                    continue
                
                is_valid, model = self.api.validate_model(model_id)
                if not is_valid:
                    errors.append(
                        f"{label}: model '{model_id}' is not "
                        f"listed by GET /v1/models. Listed model IDs and "
                        f"aliases: {sorted(available_ids)}"
                    )
                    continue
                
                alias_note = "" if model.id == model_id else f" (alias of '{model.id}')"
                print(f"  {label}: model '{model_id}' OK{alias_note}")
                
                if model_params:
                    params_valid, param_errors = self.api.validate_model_params(
                        model_id,
                        model_params,
                    )
                    if not params_valid:
                        errors.extend(f"{label}: {e}" for e in param_errors)
                        continue
                    
                    permitted = {
                        definition["id"]: [v["value"] for v in definition.get("values", [])]
                        for definition in model.parameters
                    }
                    for param in model_params:
                        print(
                            f"    {param['id']} = {param['value']} OK "
                            f"(values the API lists: {', '.join(permitted[param['id']])})"
                        )
        
        except CursorAPIError as e:
            errors.append(f"Failed to fetch models: {e}")
        
        return len(errors) == 0, errors
    
    def validate_api_key(self) -> tuple[bool, list[str]]:
        """Check that API key is configured."""
        if not os.environ.get("CURSOR_API_KEY") and not self.api.api_key:
            return False, ["CURSOR_API_KEY environment variable not set"]
        return True, []
    
    def run_config_validation(self) -> bool:
        """
        Check the configuration file. Makes no network call.
        
        Returns:
            True if the configuration is valid
        """
        print("\n=== Configuration ===\n")
        print("Checking configuration...")
        config_valid, config_errors = self.validate_config()
        if not config_valid:
            for e in config_errors:
                print(f"  ERROR: {e}")
        else:
            print("  Configuration OK")
        return config_valid
    
    def run_online_validation(self, check_models: bool = True) -> bool:
        """
        Run the checks that need the network. Launches nothing.
        
        Checks the API key and the configured models (GET /v1/models),
        unless `check_models` is False (a command that can launch nothing),
        and that the project's starting point can be read on GitHub.
        
        Returns:
            True if all checks pass
        """
        print("\n=== Online Validation (uses the network, launches nothing) ===\n")
        all_valid = True
        
        if check_models:
            print("Checking API key...")
            key_valid, key_errors = self.validate_api_key()
            if not key_valid:
                all_valid = False
                for e in key_errors:
                    print(f"  ERROR: {e}")
            else:
                print("  API key configured")
            
            if key_valid:
                print("\nValidating models against GET /v1/models...")
                models_valid, model_errors = self.validate_models()
                if not models_valid:
                    all_valid = False
                    for e in model_errors:
                        print(f"  ERROR: {e}")
                else:
                    print("  All models validated")
        else:
            print("The API key and the models are not checked: this command launches no agent.")
        
        print("\nChecking the starting point on GitHub...")
        source_valid, source_errors = self.validate_source()
        if not source_valid:
            all_valid = False
            for e in source_errors:
                print(f"  ERROR: {e}")
        
        return all_valid
    
    def validate_source(self) -> tuple[bool, list[str]]:
        """
        Check that the starting point can be read on GitHub.
        
        For an open project the commit found is recorded as the project's
        source commit, so the first stage starts from exactly that commit.
        The committed copy of the model setup document is checked at that
        commit every time, also for a project that recorded it earlier.
        
        Returns:
            Tuple of (is_readable, list_of_errors)
        """
        ref = (
            self.manifest.source_ref
            if self.manifest is not None
            else self.repo_config.get("starting_ref", "main")
        )
        recorded = self.manifest.source_commit if self.manifest is not None else None
        try:
            commit = recorded or self._resolve_ref(ref=ref)
            if recorded:
                print(f"  Source commit recorded for this project: {recorded}")
            else:
                print(f"  '{ref}' in {self.github.full_name} is at commit {commit}")
            errors = self._source_document_errors(ref=ref, commit=commit)
        except GitHubAPIError as error:
            return False, [
                f"Could not read '{ref}' in {self.github.full_name} on GitHub: {error}. "
                f"A private repository needs a token in the GITHUB_TOKEN "
                f"environment variable."
            ]
        if errors:
            return False, errors
        if self.manifest is not None:
            self.manifest.source_commit = commit
        return True, []
    
    def _source_document_errors(self, ref: str, commit: str) -> list[str]:
        """Check the committed copy of the model setup document at the starting commit.

        It must be there, and for an open project it must be the same file
        (same SHA-256) as the document selected for the project: the first
        stage is sent the selected document, the later stages read the
        committed copy.
        """
        if not self.working_document_config:
            return []
        path = self.working_document_config["source_document"]
        try:
            content = self._with_retries(
                action=lambda: self.github.get_file_bytes(path=path, commit=commit),
                description=f"Reading {path}",
            )
        except GitHubAPIError as error:
            return [f"Could not read {path} at '{ref}' (commit {commit}) on GitHub: {error}"]
        if content is None:
            return [
                f"{path} is not in the repository at '{ref}' (commit {commit}). The "
                f"original model setup document must be committed there before a "
                f"project is started."
            ]
        committed_sha256 = hashlib.sha256(content).hexdigest()
        print(f"  {path} is in the repository at that commit (SHA-256 {committed_sha256})")
        record = self.manifest.model_setup_document if self.manifest is not None else None
        if record and record["sha256"] != committed_sha256:
            return [
                f"The model setup document selected for this project ({record['path']}, "
                f"SHA-256 {record['sha256']}) is not the same file as {path} in the "
                f"repository at '{ref}' (SHA-256 {committed_sha256}). The first stage "
                f"is sent the selected document and the later stages read the "
                f"repository copy, so the two must be identical. Select the "
                f"repository copy, or commit the new version first."
            ]
        return []
    
    def _with_retries(self, action: Callable[[], Any], description: str) -> Any:
        """Run a read-only request, retrying transient failures a bounded number of times.

        Only for requests that change nothing (GET). A launch request is
        never sent through this; see _create_agent.

        Raises:
            APIError: the last error, when it is not transient or the
                attempts are used up.
        """
        for attempt in range(1, self.retry_max_attempts + 1):
            try:
                return action()
            except APIError as error:
                if not error.is_transient or attempt == self.retry_max_attempts:
                    raise
                delay = self._retry_delay(failed_attempts=attempt)
                print(
                    f"  {description} failed ({error}). "
                    f"Try {attempt + 1} of {self.retry_max_attempts} in {delay}s"
                )
                self.sleep(delay)
    
    def _retry_delay(self, failed_attempts: int) -> float:
        """Seconds to wait after a number of failed attempts (the last value repeats)."""
        return self.retry_backoff[min(failed_attempts, len(self.retry_backoff)) - 1]
    
    def _resolve_ref(self, ref: str) -> str:
        """Return the commit SHA of a branch, or check that a commit SHA exists."""
        if COMMIT_SHA_PATTERN.fullmatch(ref):
            return self._with_retries(
                action=lambda: self.github.get_commit(commit=ref),
                description=f"Reading commit {ref}",
            )
        return self._with_retries(
            action=lambda: self.github.get_branch_commit(branch=ref),
            description=f"Reading branch '{ref}'",
        )
    
    # ------------------------------------------------------------------
    # Reading the repository
    # ------------------------------------------------------------------
    
    def _file_bytes(self, path: Any, commit: str) -> Optional[bytes]:
        """Return a file's content at a commit, or None if no file is there.

        A path that is not a safe relative path names no file.

        Raises:
            APIError: if GitHub cannot be read.
        """
        if unsafe_relative_path_reason(path):
            return None
        key = (commit, path)
        if key not in self._file_cache:
            self._file_cache[key] = self._with_retries(
                action=lambda: self.github.get_file_bytes(path=path, commit=commit),
                description=f"Reading {path}",
            )
        return self._file_cache[key]
    
    def _compare(self, base: str, head: str) -> dict:
        """Return how `head` relates to `base` and the files changed between them."""
        return self._with_retries(
            action=lambda: self.github.compare(base=base, head=head),
            description=f"Comparing {base[:12]} with {head[:12]}",
        )
    
    def _branch_head(self, branch: str) -> str:
        return self._with_retries(
            action=lambda: self.github.get_branch_commit(branch=branch),
            description=f"Reading branch '{branch}'",
        )
    
    def _resolve_input(self, stage: StageConfig, result: StageResult) -> tuple[str, str]:
        """Return (description, commit SHA) of the snapshot a round starts from.

        A round that revises an earlier round's output starts from the
        commit recorded when it was reserved. Any other round starts from
        the stage's input: the accepted output commit of the stage before
        it, or, for the first stage, the project's source commit, which is
        read once and then kept.
        """
        if result.revision:
            return (
                f"output of round {result.revision['replaces_round']} "
                f"(branch '{result.revision['base_branch']}')",
                result.revision["base_commit"],
            )
        
        if stage.depends_on:
            prior = self.manifest.stages[stage.depends_on]
            return f"accepted output of stage '{stage.depends_on}'", prior.output_commit
        
        if not self.manifest.source_commit:
            self.manifest.source_commit = self._resolve_ref(ref=self.manifest.source_ref)
            self.checkpoint()
        return f"'{self.manifest.source_ref}'", self.manifest.source_commit
    
    # ------------------------------------------------------------------
    # Trial journal
    # ------------------------------------------------------------------
    
    def _record_event(
        self, stage: Optional[StageConfig], round_number: int, kind: str, facts: dict,
    ) -> Optional[dict]:
        """Write an event to the trial journal before the step after it is taken.

        The event's ID follows from the project, the stage, the round and
        the kind, so an event that is already recorded (the step is taken
        again after an interruption) is not recorded twice. A dry run
        records nothing.

        Raises:
            TrialLogError: if the journal cannot be written. Nothing
                further is done then.
        """
        if self.dry_run:
            return None
        number = self.stage_number(stage=stage) if stage is not None else 0
        identifier = trial_log.event_id(
            project_id=self.manifest.project_id, stage_number=number,
            round_number=round_number, kind=kind,
        )
        existing = self.journal.find(event_id=identifier)
        if existing is not None:
            return existing
        event = {
            "event_id": identifier,
            "project_id": self.manifest.project_id,
            "at": utc_now(),
            "stage": stage.name if stage is not None else "project",
            "stage_number": number,
            "round": round_number,
            "kind": kind,
            "facts": facts,
        }
        self.journal.record(event=event)
        return event
    
    def _project_events(self) -> list[dict]:
        return [
            event for event in self.journal.events()
            if event.get("project_id") == self.manifest.project_id
        ]
    
    def _code_sha256(self) -> str:
        """One SHA-256 over the hashes of the orchestrator's files that are running."""
        return hashlib.sha256(canonical_json(running_code_sha256())).hexdigest()
    
    # ------------------------------------------------------------------
    # Rounds
    # ------------------------------------------------------------------
    
    def _reserve_round(
        self,
        stage: StageConfig,
        kind: str,
        started_by: str,
        reason: str,
        revision: Optional[dict] = None,
        origin: str = ORIGIN_PIPELINE,
        fields_to_set: Optional[dict] = None,
    ) -> StageResult:
        """Count a new round of a stage, on disk, before any work is done for it.

        The round's number is one more than the stage's last round,
        whatever became of that round. The earlier round's record is kept.

        Raises:
            ProjectStateError: if the stage has used all its rounds.
        """
        previous = self.manifest.stages.get(stage.name)
        last_round = previous.round if previous is not None else 0
        limit = self.round_limit(stage=stage)
        if last_round + 1 > limit:
            raise ProjectStateError(
                f"Stage '{stage.name}' has used all {limit} round(s) it is allowed. "
                f"No further round is started. Only a raise_limit decision of the "
                f"project owner raises the limit."
            )
        result = StageResult(**{
            "name": stage.name,
            "round": last_round + 1,
            "kind": kind,
            "origin": origin,
            "started_by": started_by,
            "revision": revision,
            # Later rounds of a stage are checked against the same input commit.
            "baseline_commit": previous.baseline_commit if previous is not None else None,
            **(fields_to_set or {}),
        })
        self._record_event(
            stage=stage, round_number=result.round, kind="reserved",
            facts={
                "What happened": f"Round {result.round} of at most {limit} was counted ({kind})",
                "Started by": started_by,
                "Why this route": reason,
                "Starts from": (
                    f"commit {revision['base_commit']} (output of round "
                    f"{revision['replaces_round']})" if revision else
                    f"commit {result.candidate_commit} on branch '{result.candidate_branch}'"
                    if origin == ORIGIN_ADOPTED else "the stage's input"
                ),
                "Config SHA-256": self.manifest.provenance["config_sha256"],
                "Orchestrator SHA-256": self._code_sha256(),
            },
        )
        if previous is not None and previous.round > 0:
            self.manifest.previous_rounds.append(asdict(previous))
        self.manifest.stages[stage.name] = result
        self.checkpoint()
        print(
            f"\nStage '{stage.name}': round {result.round} of at most {limit} is "
            f"counted ({kind}; {reason})."
        )
        return result
    
    def _models_for_round(
        self, stage: StageConfig, round_number: int,
    ) -> tuple[dict, Optional[dict]]:
        """Return (the proposer's model, the reviewer's model or None) for a round.

        With cross-model review the two configured models change roles
        every round: the stage's model proposes in odd rounds and reviews
        in even ones.
        """
        primary = {"id": stage.model_id, "params": list(stage.model_params)}
        if not stage.cross_model_enabled:
            return primary, None
        secondary = {
            "id": stage.cross_model["secondary_model_id"],
            "params": list(stage.cross_model.get("secondary_model_params", [])),
        }
        return (primary, secondary) if round_number % 2 == 1 else (secondary, primary)
    
    def _review_paths(self, stage: StageConfig, round_number: int) -> dict:
        """Paths of the two files a cross-model reviewer writes in a round."""
        folder = f"{stage.cross_model['review_dir']}/round_{round_number:02d}"
        return {
            "blind": f"{folder}/blind_assessment.md",
            "comparison": f"{folder}/comparison.json",
        }
    
    def _stage_content_paths(self, stage: StageConfig) -> list[str]:
        """The paths a stage's own output lives at."""
        paths = []
        if self.working_document_config:
            paths.append(self.working_document_config["path"])
        paths.extend(stage.output_manifest.get("required_files", []))
        paths.extend(stage.output_manifest.get("allowed_paths", []))
        if stage.verification:
            paths.append(stage.verification["evidence_file"])
        return list(dict.fromkeys(paths))
    
    def _stage_output_paths(self, stage: StageConfig) -> list[str]:
        """Every path a round of a stage may add or change."""
        paths = self._stage_content_paths(stage=stage)
        if stage.cross_model_enabled:
            paths.append(stage.cross_model["review_dir"])
        if self.trial_log_path:
            paths.append(self.trial_log_path)
        return list(dict.fromkeys(paths))
    
    def _reviewed_files(self, stage: StageConfig) -> list[str]:
        """The files of a candidate that a reviewer's verdict is bound to."""
        paths = []
        if self.working_document_config:
            paths.append(self.working_document_config["path"])
        paths.extend(stage.output_manifest.get("required_files", []))
        if stage.verification:
            paths.append(stage.verification["evidence_file"])
        return list(dict.fromkeys(paths))
    
    def _evidence_phase(self, stage: StageConfig, result: StageResult) -> Optional[str]:
        """For a stage that runs parameter batches: what this round may do."""
        if not stage.verification or stage.verification["kind"] != verification.KIND_PARAMETER_BATCHES:
            return None
        if result.revision and result.revision.get("approve_batch"):
            return verification.PHASE_BATCH
        return verification.PHASE_DOMAIN_PROPOSAL
    
    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------
    
    def _evidence_section(self, stage: StageConfig, result: StageResult) -> Optional[str]:
        settings = stage.verification
        if not settings:
            return None
        lines = [
            EVIDENCE_HEADING,
            f"Write the file {settings['evidence_file']}.",
            verification.COMMON_INSTRUCTIONS,
            "Entries for this stage:",
            verification.EVIDENCE_INSTRUCTIONS[settings["kind"]],
        ]
        if settings["kind"] == verification.KIND_TEST_MAP:
            lines.append(
                f"The implementation is {settings['implementation_file']}; the tests "
                f"are in {settings['test_file']}."
            )
        phase = self._evidence_phase(stage=stage, result=result)
        if phase == verification.PHASE_DOMAIN_PROPOSAL:
            lines.append(
                f"THIS ROUND IS A DOMAIN PROPOSAL (\"phase\": \"domain_proposal\"). Run "
                f"no computation. Propose the parameter domain and the first batch in "
                f"{settings['next_batch_spec']}, with at most "
                f"{settings['max_configurations']} configurations. Nothing is computed "
                f"until the project owner has approved that file."
            )
        elif phase == verification.PHASE_BATCH:
            lines.append(
                f"THIS ROUND RUNS ONE APPROVED BATCH (\"phase\": \"batch\"). The project "
                f"owner approved the batch in {settings['next_batch_spec']} as it is in "
                f"the commit you start from. Run exactly that batch: no more "
                f"configurations than it allows (never more than "
                f"{settings['max_configurations']}), every one inside its domain. Copy "
                f"the batch file unchanged into this round's results folder and name "
                f"the copy as \"batch_spec_file\". List every run, failed runs included. "
                f"To propose a further batch, write a new {settings['next_batch_spec']}; "
                f"it is not run until the owner approves it."
            )
        decisions = [
            f"- {decision['decision_id']}: {decision['action']} on stage "
            f"'{decision['stage']}', round {decision['round']}: "
            f"\"{' '.join(decision['chat']['quote'].split())[:300]}\""
            for decision in (self.manifest.decisions if self.manifest else [])
        ]
        if decisions:
            lines.append(
                "Decisions of the project owner recorded for this project (where an "
                "item was decided by one of them, cite its ID as \"decision_ref\"):\n"
                + "\n".join(decisions)
            )
        return "\n".join(lines)
    
    def _trial_log_section(self, result: StageResult, report: bool = True) -> Optional[str]:
        """The trial log rules of a launch: the entries to append and the agent's own entry."""
        path = self.trial_log_path
        if not path:
            return None
        if self.dry_run:
            entries = "(the orchestrator's entries are read when the stage is launched)"
            report_id = "<event ID given at launch>"
        else:
            entries = "\n".join(self._expected_log_entries(result=result)).rstrip()
            report_id = self._report_id(result=result)
        model = result.model or {}
        return (
            f"{TRIAL_LOG_HEADING}\n"
            f"The file {path} is the append-only record of every attempt of this "
            f"project. Add to its END, in this order, and change nothing that is "
            f"already in it (if the file does not exist, create it with what is "
            f"listed here):\n"
            f"1. The entries between the two lines below, word for word. They are "
            f"the orchestrator's own record.\n"
            f"{LOG_ENTRIES_BEGIN_LINE}\n{entries}\n{LOG_ENTRIES_END_LINE}\n"
            f"2. One entry of your own, with exactly this heading and these lines, "
            f"each filled in:\n"
            f"{OWN_ENTRY_BEGIN_LINE}\n"
            + trial_log.report_template(
                report_id=report_id,
                role=result.role or ROLE_BY_PHASE[result.phase],
                model=model.get("id") or "default",
            )
            + f"{OWN_ENTRY_END_LINE}\n"
            f"Write short statements of what you did and what you found, from the "
            f"evidence. Do not write out your reasoning. Add no other entry. The "
            f"file is checked at the commit you push."
        )
    
    def _round_sections(self, stage: StageConfig, result: StageResult) -> list[str]:
        """The sections appended to a proposer's prompt for a round."""
        round_number = max(result.round, 1)
        sections = [
            f"{ROUND_HEADING}\n"
            f"This is round {round_number} of at most {self.round_limit(stage=stage)} "
            f"for stage {self.stage_number(stage=stage)}. A round is one attempt and "
            f"is counted whatever its result. Do the whole task in this run; do not "
            f"leave work for a later round."
        ]
        evidence = self._evidence_section(stage=stage, result=result)
        if evidence:
            sections.append(evidence)
        if "allowed_paths" in stage.output_manifest:
            sections.append(
                f"{OUTPUT_PATHS_HEADING}\n"
                f"You may add or change only these paths (a folder includes "
                f"everything in it). Any other changed file fails the output check.\n"
                + "\n".join(f"- {path}" for path in self._stage_output_paths(stage=stage))
            )
        log_section = self._trial_log_section(result=result)
        if log_section:
            sections.append(log_section)
        return sections
    
    def _task_text(self, stage: StageConfig, result: StageResult) -> str:
        """The task as the proposer was given it, without the round's own sections."""
        evidence = self._evidence_section(stage=stage, result=result)
        return build_stage_prompt(
            stage=stage,
            model_setup_document=self.model_setup_document,
            working_document_rules=self.working_document_rules(stage=stage),
            extra_sections=[evidence] if evidence else None,
        ).rstrip()
    
    def _phase_prompt(self, stage: StageConfig, result: StageResult) -> str:
        """Return the prompt of the round's current launch.

        Raises:
            ModelSetupDocumentError: if the stage receives the model setup
                document and none is selected.
        """
        if result.phase == PHASE_PROPOSE:
            return build_stage_prompt(
                stage=stage,
                model_setup_document=self.model_setup_document,
                working_document_rules=self.working_document_rules(stage=stage),
                revision=result.revision,
                extra_sections=self._round_sections(stage=stage, result=result),
            )
        
        number = self.stage_number(stage=stage)
        paths = self._review_paths(stage=stage, round_number=max(result.round, 1))
        task = (
            f"THE TASK (given to the other agent; do NOT carry it out and do not "
            f"write its output files):\n{TASK_BEGIN_LINE}\n"
            f"{self._task_text(stage=stage, result=result)}\n{TASK_END_LINE}"
        )
        if result.phase == PHASE_BLIND:
            return (
                f"You are an independent reviewer in the RMB_AI research project for "
                f"Yang Lu.\n\n"
                f"INDEPENDENT REVIEW, PART 1 OF 2 (blind assessment): stage {number}, "
                f"round {max(result.round, 1)}.\n"
                f"Another agent, running a different model, was given the task printed "
                f"below and has produced a candidate output. You are NOT shown the "
                f"candidate in this part, and you must not look for it: do not fetch, "
                f"list or read any other branch of the repository.\n\n"
                f"WHAT TO DO:\n"
                f"- From the inputs the task names, work out yourself what a correct "
                f"output must state, item by item, and which of the task's "
                f"requirements are hard to meet.\n"
                f"- Write that assessment to {paths['blind']}. Make it specific enough "
                f"to be compared item by item with a candidate.\n"
                f"- Say what you could not determine. Do not fill a gap with an "
                f"assumption; name it as open.\n\n"
                f"RULES:\n"
                f"- Add only the file {paths['blind']}. Change no other file: not the "
                f"working document, not the trial log, not any output file of the task.\n"
                f"- Commit and push before finishing.\n\n"
                f"{task}\n"
            )
        
        blind = result.cross_review["blind"]
        candidate_files = self._candidate_file_hashes(stage=stage, result=result)
        log_section = self._trial_log_section(result=result)
        return (
            f"You are an independent reviewer in the RMB_AI research project for "
            f"Yang Lu.\n\n"
            f"INDEPENDENT REVIEW, PART 2 OF 2 (comparison): stage {number}, round "
            f"{result.round}.\n"
            f"The commit you start from holds a blind assessment, {paths['blind']}, "
            f"written by your model without sight of the candidate (commit "
            f"{blind['commit']}). The candidate is commit {result.candidate_commit} "
            f"on branch '{result.candidate_branch}'.\n\n"
            f"STEPS:\n"
            f"1. Bring the candidate in without changing it:\n"
            f"     git fetch origin {result.candidate_branch}\n"
            f"     git merge --no-edit {result.candidate_commit}\n"
            f"2. Compare the candidate with the blind assessment item by item, and "
            f"check it against the requirements of the task printed below.\n"
            f"3. Write {paths['comparison']}, a JSON object with:\n"
            f"   - \"candidate_commit\": \"{result.candidate_commit}\"\n"
            f"   - \"candidate_files\": {json.dumps(candidate_files, sort_keys=True)}\n"
            f"     (copy it exactly: the SHA-256 of the files your verdict is about)\n"
            f"   - \"blind_commit\": \"{blind['commit']}\"\n"
            f"   - \"blind_assessment_sha256\": \"{blind['sha256']}\"\n"
            f"   - \"verdict\": \"accept\" or \"reject\": whether the candidate meets "
            f"the task's requirements\n"
            f"   - \"agreement\": \"agree\" or \"disagree\": whether the blind "
            f"assessment and the candidate reach the same results\n"
            f"   - \"differences\": a list of {{\"item\", \"severity\" (\"blocking\" or "
            f"\"minor\"), \"detail\"}}, empty if there are none\n"
            f"   - \"plausible_alternative\": false only if the candidate fails and you "
            f"see no other route that could meet the requirements; otherwise true\n"
            f"   - \"rationale\": a short statement, from the evidence, of why you "
            f"reached the verdict. Do not write out your reasoning.\n"
            f"   Accept only if no difference is blocking. Do not force agreement: "
            f"where the two differ and you cannot tell which is right, say so and "
            f"reject.\n\n"
            f"RULES:\n"
            f"- Change only {paths['comparison']}"
            + (f" and {self.trial_log_path}" if log_section else "")
            + f". Do not change any file of the candidate, and do not change "
            f"{paths['blind']}.\n"
            f"- Your verdict holds for exactly the candidate commit named above. Any "
            f"later change to the candidate is not covered by it.\n"
            f"- Commit and push before finishing.\n\n"
            + (f"{log_section}\n\n" if log_section else "")
            + f"{task}\n"
        )
    
    def _agent_name(self, stage: StageConfig, result: StageResult) -> str:
        detail = {
            PHASE_PROPOSE: f"round {result.round}",
            PHASE_BLIND: f"round {result.round}, blind review",
            PHASE_COMPARE: f"round {result.round}, comparison",
        }[result.phase]
        return f"Pipeline: {stage.display_name} ({detail})"
    
    def _report_id(self, result: StageResult) -> str:
        """ID of the trial log entry the agent of the current launch writes itself."""
        stage = self.stage_named(name=result.name)
        return trial_log.event_id(
            project_id=self.manifest.project_id,
            stage_number=self.stage_number(stage=stage),
            round_number=result.round,
            kind=f"report-{result.phase}",
        )
    
    def _expected_log_entries(self, result: StageResult) -> list[str]:
        """The orchestrator's entries the current launch was given to append, in order."""
        wanted = set(result.log_event_ids or [])
        return [
            trial_log.render_entry(event=event)
            for event in self._project_events() if event["event_id"] in wanted
        ]
    
    # ------------------------------------------------------------------
    # Running a stage
    # ------------------------------------------------------------------
    
    def _run_stage(self, stage: StageConfig) -> StageResult:
        """Move a stage's current round forward from its recorded state as far as it can go.

        The steps of a launch are: launch, wait for the run, check the
        output, collect artifacts. A round has one launch, or three with
        cross-model review. A closed round either leads to one more round
        of the same stage (only where the stage allows that) or to the
        human gate: local checkout, signed notice, the host's ack. Each
        step is recorded before the next one starts, so a later run
        continues from the recorded step and never launches a second agent
        for the same step. Nothing here leads past the gate or into
        another stage: only apply_decision() does.
        """
        result = self.manifest.stages[stage.name]
        print(f"\n--- Stage: {stage.display_name} ---")
        print(f"  Recorded status: {result.status} (round {result.round}, {result.phase})")
        
        self._advance_stage(stage=stage)
        
        result = self.manifest.stages[stage.name]
        if result.status == STAGE_AWAITING_REVIEW:
            self._print_review_notice(stage=stage, result=result)
        elif result.status == STAGE_COMPLETE:
            print("  Stage accepted")
        elif result.status != STAGE_DRY_RUN:
            print(f"  Stopped with status {result.status}: {result.error}")
        return result
    
    def _advance_stage(self, stage: StageConfig) -> None:
        """Take each step the stage's recorded status allows, until one stops it."""
        while True:
            # A closed round may have been followed by a new one.
            result = self.manifest.stages[stage.name]
            if result.status in LAUNCHABLE_STATUSES:
                if result.origin == ORIGIN_ADOPTED:
                    raise RuntimeError(
                        f"Stage '{stage.name}': an adopted round is never launched."
                    )
                moved = self._launch(stage=stage, result=result)
            elif result.status == STAGE_RUNNING:
                moved = self._wait_for_run(stage=stage, result=result)
            elif result.status == STAGE_RUN_FINISHED:
                moved = self._check_output(stage=stage, result=result)
            elif result.status in (STAGE_OUTPUT_CHECKED, STAGE_ARTIFACTS_FAILED):
                moved = (
                    self._collect_artifacts(stage=stage, result=result)
                    and self._finish_launch(stage=stage, result=result)
                )
            elif result.status == STAGE_SYNC_PENDING:
                moved = self._show_output_locally(stage=stage, result=result)
            elif result.status == STAGE_NOTICE_PENDING:
                moved = self._deliver_notice(stage=stage, result=result)
            else:
                return
            if not moved:
                return
    
    def _local_review(self) -> LocalReviewCheckout:
        """
        Raises:
            RuntimeError: if no local checkout was given.
        """
        if self.local_review is None:
            raise RuntimeError(
                "No local checkout is set for showing stage outputs for review. "
                "Set local_review."
            )
        return self.local_review
    
    def _check_gate_ready(self) -> None:
        """Check that an output could be shown locally and announced. Changes nothing but folders.

        Raises:
            LocalSyncError, BridgeError: if it could not. No paid run is
                started while its output could not reach the owner.
        """
        self._local_review().check_ready()
        self.bridge.check_ready()
    
    def _launch(self, stage: StageConfig, result: StageResult) -> bool:
        """Launch the agent of the round's current phase. Returns True once the round has moved on."""
        proposer, reviewer = self._models_for_round(
            stage=stage, round_number=max(result.round, 1),
        )
        model = proposer if result.phase == PHASE_PROPOSE else reviewer
        print(f"  Role: {ROLE_BY_PHASE[result.phase]} ({result.phase})")
        print(f"  Model: {model['id'] or '(default)'}")
        print(f"  Guardrails appended to prompt: {len(stage.guardrails)}")
        
        # A stage that receives the model setup document cannot be launched
        # (or reported as ready by a dry run) when none has been selected.
        if stage.receives_model_setup_document and self.model_setup_document is None:
            result.status = STAGE_BLOCKED
            result.error = (
                f"Stage '{stage.name}' needs the model setup / equilibrium "
                f"definition document and none has been selected"
            )
            self.checkpoint()
            return False
        if stage.receives_model_setup_document:
            print(
                f"  Model setup document in prompt: "
                f"{self.model_setup_document.file_name} "
                f"(SHA-256 {self.model_setup_document.sha256})"
            )
        if self.working_document_config:
            print(
                f"  Working document rules in prompt: section "
                f"{self.stage_number(stage=stage)} of {self.working_document_config['path']}"
            )
        
        if self.dry_run:
            self._print_dry_run(stage=stage, result=result, reviewer=reviewer)
            result.status = STAGE_DRY_RUN
            return False
        
        if self.manifest_path is None:
            raise RuntimeError(
                "No agent is launched for a project without a manifest file: "
                "the record of a launch must be on disk before the launch "
                "request is sent. Set manifest_path."
            )
        
        look_before_launching = result.status == STAGE_LAUNCHING
        if not look_before_launching:
            # A paid run is not started while its output could not be
            # shown for review and announced afterwards.
            try:
                self._check_gate_ready()
            except (LocalSyncError, BridgeError) as error:
                result.status = STAGE_BLOCKED
                result.error = (
                    f"No agent was launched: the stage's output could not be shown "
                    f"for review or announced. {error}"
                )
                self.checkpoint()
                return False
            try:
                self._prepare_launch(stage=stage, result=result, model=model)
            except GitHubAPIError as error:
                result.error = f"Could not read the starting commit on GitHub: {error}"
                self.checkpoint()
                return False
        
        prompt_text = self._phase_prompt(stage=stage, result=result)
        if not look_before_launching:
            result.prompt_sha256 = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
            result.status = STAGE_LAUNCHING
            result.error = None
            # The agent ID is on disk before the launch request is sent, so
            # an interrupted launch can be picked up under the same ID.
            self.checkpoint()
        if result.revision and result.phase == PHASE_PROPOSE:
            print(
                f"  Feedback in prompt ({result.revision['source']}): "
                f"{len(result.revision['feedback'])} character(s) "
                f"(SHA-256 {result.revision['feedback_sha256']})"
            )
        print(f"  Prompt: {len(prompt_text)} characters (SHA-256 {result.prompt_sha256})")
        print(f"  Starting from: commit {result.launch_commit}")
        print(f"  Agent ID: {result.agent_id}")
        run = self._create_agent(
            stage=stage,
            result=result,
            prompt_text=prompt_text,
            look_before_launching=look_before_launching,
        )
        if run is not None and not run.run_id:
            result.error = (
                f"The API accepted the launch of agent {result.agent_id} but gave "
                f"no run ID. A resume looks the agent up and sends no new launch."
            )
            run = None
        if run is None:
            if result.outcome is not None:
                return self._close_round(stage=stage, result=result)
            self.checkpoint()
            return False
        
        result.run_id = run.run_id
        result.agent_url = run.agent_url
        result.status = STAGE_RUNNING
        result.error = None
        self.checkpoint()
        print(f"  Run ID: {run.run_id}")
        if run.agent_url:
            print(f"  URL: {run.agent_url}")
        return True
    
    def _print_dry_run(
        self, stage: StageConfig, result: StageResult, reviewer: Optional[dict],
    ) -> None:
        """Print what a stage's first round would do. Builds its prompts; sends nothing."""
        draft = StageResult(name=stage.name, round=1, kind=KIND_INITIAL)
        prompt_text = self._phase_prompt(stage=stage, result=draft)
        print(f"  Prompt: {len(prompt_text)} characters")
        source = (
            f"the accepted output of stage '{stage.depends_on}'"
            if stage.depends_on
            else f"'{self.manifest.source_ref}'"
        )
        required = stage.output_manifest.get("required_files", [])
        limit = self.round_limit(stage=stage)
        print(
            f"  [DRY RUN] Rounds: at most {limit}; autonomous correction rounds: "
            f"{'on' if stage.autonomous_fix else 'off'}"
        )
        print(f"  [DRY RUN] Would launch a cloud agent from {source}")
        print(f"  [DRY RUN] Would then check {len(required)} required output file(s)")
        if self.working_document_config:
            print("  [DRY RUN] Would then check the working document's sections")
        if stage.verification:
            print(
                f"  [DRY RUN] Would then check the evidence file "
                f"{stage.verification['evidence_file']} ({stage.verification['kind']}; "
                f"form and cross-references, not correctness)"
            )
        if self.trial_log_path:
            print(f"  [DRY RUN] Would then check the trial log {self.trial_log_path}")
        if reviewer is not None:
            for phase in (PHASE_BLIND, PHASE_COMPARE):
                draft.phase = phase
                draft.candidate_branch, draft.candidate_commit = "<candidate branch>", "<candidate commit>"
                draft.cross_review = {"blind": {"commit": "<blind commit>", "sha256": "<SHA-256>"}}
                self._phase_prompt(stage=stage, result=draft)
            print(
                f"  [DRY RUN] Would then launch the reviewer ({reviewer['id']}) twice: "
                f"a blind assessment, then a comparison with the candidate "
                f"(pass policy: {stage.cross_model['pass_policy']})"
            )
        else:
            print("  [DRY RUN] Cross-model review: off")
        print("  [DRY RUN] Would then check the output out in the local checkout")
        print(
            "  [DRY RUN] Would then write a signed notice and wait for the host's "
            "ack and the owner's decision"
        )
    
    def _prepare_launch(self, stage: StageConfig, result: StageResult, model: dict) -> None:
        """Fix what the current launch starts from and is given, and record it.

        Raises:
            GitHubAPIError: if the starting point cannot be read. Nothing
                is recorded as launched then.
        """
        if result.phase == PHASE_PROPOSE:
            result.input_source, result.input_commit = self._resolve_input(
                stage=stage, result=result,
            )
            result.baseline_commit = result.baseline_commit or result.input_commit
            result.launch_commit = result.input_commit
        elif result.phase == PHASE_BLIND:
            result.launch_commit = result.input_commit
        else:
            result.launch_commit = result.cross_review["blind"]["commit"]
        
        writes_log = bool(self.trial_log_path) and result.phase != PHASE_BLIND
        log_before = None
        if writes_log:
            # The comparison merges the candidate, so its log continues the candidate's.
            log_commit = (
                result.candidate_commit if result.phase == PHASE_COMPARE else result.launch_commit
            )
            log_before = self._file_bytes(path=self.trial_log_path, commit=log_commit)
        
        result.role = ROLE_BY_PHASE[result.phase]
        result.model = model
        if result.agent_id is None:
            result.agent_id = f"bc-{uuid.uuid4()}"
            self.checkpoint()
        self._record_event(
            stage=stage, round_number=result.round, kind=f"launch-{result.phase}",
            facts={
                "What happened": f"The {result.role} was launched ({result.phase})",
                "Model": model["id"] or "configured default",
                "Model parameters": [f"{p['id']}={p['value']}" for p in model["params"]],
                "Agent ID": result.agent_id,
                "Starts from": f"commit {result.launch_commit}",
            },
        )
        if writes_log:
            present = (log_before or b"").decode("utf-8", errors="replace")
            result.log_event_ids = [
                event["event_id"]
                for event in trial_log.entries_missing_from(
                    log_text=present, events=self._project_events(),
                )
            ]
        else:
            result.log_event_ids = None
    
    def _find_agent(self, agent_id: str) -> Optional[AgentRun]:
        """Return the agent with this ID, or None if the API says there is none."""
        try:
            return self.api.get_agent(agent_id=agent_id)
        except CursorAPIError as error:
            if error.status_code == 404:
                return None
            raise
    
    def _create_agent(
        self,
        stage: StageConfig,
        result: StageResult,
        prompt_text: str,
        look_before_launching: bool,
    ) -> Optional[AgentRun]:
        """Create the current launch's agent at most once, under the agent ID recorded for it.

        A launch request starts a paid agent, so it is never simply sent
        again. When the outcome of a launch request is unknown (no response,
        429 or 5xx), the next try first asks the API whether the agent with
        the recorded ID exists. Only if it does not is the request sent
        again, with the same ID, which the API answers with
        409 agent_id_conflict instead of creating a second agent.

        Returns:
            The agent's run, or None. On None either the result holds an
            outcome (the API refused; no agent exists; the round is over)
            or it stays LAUNCHING (outcome unknown; a resume checks again).
        """
        last_error: Optional[APIError] = None
        for attempt in range(1, self.retry_max_attempts + 1):
            if attempt > 1:
                delay = self._retry_delay(failed_attempts=attempt - 1)
                print(
                    f"  Launch outcome unknown ({last_error}). "
                    f"Try {attempt} of {self.retry_max_attempts} in {delay}s"
                )
                self.sleep(delay)
            if look_before_launching:
                try:
                    existing = self._find_agent(agent_id=result.agent_id)
                except CursorAPIError as error:
                    last_error = error
                    if error.is_transient:
                        continue
                    result.error = (
                        f"Could not check whether agent {result.agent_id} exists: "
                        f"{error}. No launch request was sent on this try; a "
                        f"resume checks again."
                    )
                    return None
                if existing is not None:
                    print("  The agent already exists. No launch request is sent.")
                    return existing
            try:
                print("  Sending the launch request...")
                return self.api.create_agent(
                    prompt_text=prompt_text,
                    repo_url=self.repo_config["url"],
                    starting_ref=result.launch_commit,
                    model_id=result.model["id"],
                    model_params=result.model["params"] or None,
                    name=self._agent_name(stage=stage, result=result),
                    auto_create_pr=self.execution_config.get("auto_create_pr", False),
                    agent_id=result.agent_id,
                )
            except CursorAPIError as error:
                last_error = error
                # 409: the API docs give `agent_id_conflict` for an agent ID
                # that is already in use, so the agent may exist.
                if error.status_code == 409 or error.is_transient:
                    look_before_launching = True
                    continue
                self._launch_failed(
                    stage=stage, result=result,
                    description=(
                        f"The API refused the launch request; no agent was created: {error}"
                    ),
                )
                return None
        
        result.error = (
            f"The outcome of the launch is unknown after {self.retry_max_attempts} "
            f"tries: {last_error}. Agent ID {result.agent_id} is recorded; a resume "
            f"first checks whether that agent exists."
        )
        return None
    
    def _launch_failed(self, stage: StageConfig, result: StageResult, description: str) -> None:
        """Record that the current launch did not take place or did not finish.

        The round is over: no launch is repeated without a decision of
        the owner.
        """
        if result.phase == PHASE_PROPOSE:
            result.outcome = (
                OUTCOME_LAUNCH_REJECTED if result.run_id is None else OUTCOME_RUN_FAILED
            )
            result.gate = self._failure_gate(stage=stage, problem=description)
        else:
            result.outcome = OUTCOME_REVIEW_FAILED
            result.cross_review = {**(result.cross_review or {}), "failure": description}
        result.error = description
        self._record_event(
            stage=stage, round_number=result.round, kind=f"failed-{result.phase}",
            facts={
                "What happened": f"The {result.role}'s launch ({result.phase}) failed",
                "Agent ID": result.agent_id,
                "Detail": description,
                "Result": "No output of this launch was checked",
            },
        )
        self._archive_launch(result=result)
    
    def _wait_for_run(self, stage: StageConfig, result: StageResult) -> bool:
        """Poll the current run until it ends.

        Returns:
            True once the run has ended: the launch is then RUN_FINISHED,
            or the round is closed because the run failed. False while
            the run is still going or its status could not be read.
        """
        polling_config = self.api_config.get("polling", {})
        interval = polling_config.get("interval_seconds", 30)
        max_polls = polling_config.get("max_attempts", 120)
        
        print("  Polling for completion...")
        run = None
        for poll in range(1, max_polls + 1):
            try:
                run = self._with_retries(
                    action=lambda: self.api.get_run(
                        agent_id=result.agent_id, run_id=result.run_id,
                    ),
                    description="Reading the run status",
                )
            except CursorAPIError as error:
                result.error = (
                    f"Could not read the run status: {error}. The agent may still "
                    f"be running. A resume checks the same run again and launches "
                    f"no new agent."
                )
                self.checkpoint()
                return False
            print(f"  [{poll}/{max_polls}] Status: {run.status}")
            if RunStatus.is_terminal(run.status):
                break
            if poll < max_polls:
                self.sleep(interval)
        
        if run is None or not RunStatus.is_terminal(run.status):
            result.error = (
                f"The run was still going after {max_polls} polls. A resume "
                f"keeps waiting for the same run and launches no new agent."
            )
            self.checkpoint()
            return False
        
        result.run_status = run.status
        result.duration_ms = run.duration_ms
        if not RunStatus.is_success(run.status):
            self._launch_failed(
                stage=stage, result=result,
                description=f"The run ended with status {run.status}: {run.result_text}",
            )
            return self._close_round(stage=stage, result=result)
        
        result.status = STAGE_RUN_FINISHED
        result.error = None
        self._reported_branches = run.branches
        self.checkpoint()
        return True
    
    def _select_output_branch(self, reported_branches: list[dict]) -> str:
        """Pick the one branch the run pushed to this repository.

        Raises:
            OutputGateError: if the run reported no branch, or several.
        """
        branches = sorted({
            entry["branch"]
            for entry in reported_branches
            if entry.get("branch") and self.github.is_same_repo(repo_url=entry.get("repoUrl", ""))
        })
        if not branches:
            raise OutputGateError(
                f"The run finished but reported no pushed branch in {self.github.full_name}"
            )
        if len(branches) > 1:
            raise OutputGateError(
                f"The run reported more than one pushed branch ({', '.join(branches)}); "
                f"which one holds the stage output cannot be told"
            )
        return branches[0]
    
    def _pushed_branch_head(self, result: StageResult) -> tuple[str, str]:
        """Return (the branch the current run pushed, its head commit).

        Raises:
            OutputGateError: if the run reported no branch, or several.
            APIError: if the run or the branch cannot be read.
        """
        if result.pushed_branch is None:
            reported = self._reported_branches
            if reported is None:
                reported = self._with_retries(
                    action=lambda: self.api.get_run(
                        agent_id=result.agent_id, run_id=result.run_id,
                    ),
                    description="Reading the run",
                ).branches
            result.pushed_branch = self._select_output_branch(reported_branches=reported)
        return result.pushed_branch, self._branch_head(branch=result.pushed_branch)
    
    def _missing_required_files(self, stage: StageConfig, commit: str) -> list[str]:
        """Return the stage's required output files that are absent at a commit."""
        return [
            path
            for path in stage.output_manifest.get("required_files", [])
            if not self._with_retries(
                action=lambda: self.github.file_exists(path=path, commit=commit),
                description=f"Checking {path}",
            )
        ]
    
    def _read_text_file(self, path: str, commit: str) -> Optional[str]:
        """Return a file's text at a commit, or None if no file is there.

        Raises:
            WorkingDocumentError: if the file is not UTF-8 text.
            APIError: if GitHub cannot be read.
        """
        content = self._with_retries(
            action=lambda: self.github.get_file_bytes(path=path, commit=commit),
            description=f"Reading {path}",
        )
        if content is None:
            return None
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorkingDocumentError(f"{path} at {commit} is not UTF-8 text ({error})") from error
    
    def _working_document_problems(
        self, stage: StageConfig, input_commit: str, commit: str,
    ) -> list[str]:
        """Check the working document's rules at a commit a stage pushed.

        The rules are the ones working_document_rules() puts in the prompt:
        the stage's own section is there and not empty, the sections of
        earlier stages are byte-for-byte as at the stage's input commit
        (given as `input_commit`, also in a later round of the stage), no marker
        of a later stage is there, and the source document is unchanged.
        """
        path = self.working_document_config["path"]
        source_path = self.working_document_config["source_document"]
        number = self.stage_number(stage=stage)
        problems = []
        try:
            text = self._read_text_file(path=path, commit=commit)
            if text is None:
                return [f"the working document {path} is not at commit {commit}"]
            
            section = extract_stage_section(text=text, number=number, name=stage.name)
            if not section.strip():
                problems.append(f"the section of stage {number} in {path} is empty")
            
            for later_number, later in enumerate(self.stages[number:], start=number + 1):
                if has_stage_section_marker(text=text, number=later_number, name=later.name):
                    problems.append(
                        f"{path} holds a marker of stage {later_number} "
                        f"('{later.name}'), which has not been carried out"
                    )
            
            if number > 1:
                text_before = self._read_text_file(path=path, commit=input_commit)
                if text_before is None:
                    return problems + [
                        f"the working document {path} is not at the input commit {input_commit}"
                    ]
                for earlier_number, earlier in enumerate(self.stages[:number - 1], start=1):
                    before = extract_stage_section(
                        text=text_before, number=earlier_number, name=earlier.name,
                    )
                    after = extract_stage_section(
                        text=text, number=earlier_number, name=earlier.name,
                    )
                    if before != after:
                        problems.append(
                            f"the section of stage {earlier_number} ('{earlier.name}') in "
                            f"{path} was changed; it must stay as accepted"
                        )
        except WorkingDocumentError as error:
            problems.append(f"in {path}: {error}")
        
        source_before = self._with_retries(
            action=lambda: self.github.get_file_bytes(path=source_path, commit=input_commit),
            description=f"Reading {source_path}",
        )
        source_after = self._with_retries(
            action=lambda: self.github.get_file_bytes(path=source_path, commit=commit),
            description=f"Reading {source_path}",
        )
        if source_before is None:
            problems.append(f"the source document {source_path} is not at the input commit")
        elif source_after != source_before:
            problems.append(
                f"the source document {source_path} was "
                f"{'removed' if source_after is None else 'changed'}; it must stay unchanged"
            )
        return problems
    
    def _path_problems(self, stage: StageConfig, baseline_commit: str, commit: str) -> list[str]:
        """Check that only the stage's own paths differ between its input and a commit."""
        if "allowed_paths" not in stage.output_manifest:
            return []
        comparison = self._compare(base=baseline_commit, head=commit)
        if comparison["status"] != "ahead":
            return [
                f"commit {commit} does not build on the stage's input commit "
                f"{baseline_commit} (GitHub reports '{comparison['status']}')"
            ]
        if comparison["truncated"]:
            return [
                "too many files differ from the stage's input commit for GitHub to "
                "list them all, so the changed paths cannot be checked"
            ]
        permitted = self._stage_output_paths(stage=stage)
        outside = [path for path in comparison["files"] if not path_is_under(path, permitted)]
        if outside:
            return [
                "file(s) outside the stage's output paths were added or changed: "
                + ", ".join(outside[:20]) + (" ..." if len(outside) > 20 else "")
            ]
        return []
    
    def _trial_log_problems(
        self, result: StageResult, before_commit: str, after_commit: str,
    ) -> list[str]:
        """Check the trial log at a commit a launch pushed, or at an adopted commit."""
        path = self.trial_log_path
        if not path:
            return []
        before = self._file_bytes(path=path, commit=before_commit)
        after = self._file_bytes(path=path, commit=after_commit)
        if result.origin == ORIGIN_ADOPTED and before is None and after is None:
            # Adopted work was given no entry to append. The journal beside
            # the manifest records the adoption; the next launched round
            # writes the journal's entries into the repository's log.
            return []
        problems = trial_log.repository_log_problems(
            before=before,
            after=after,
            path=path,
            expected_entries=self._expected_log_entries(result=result),
            report_id=self._report_id(result=result) if result.origin == ORIGIN_PIPELINE else None,
        )
        baseline = result.baseline_commit
        if after is not None and baseline and baseline != before_commit:
            at_baseline = self._file_bytes(path=path, commit=baseline) or b""
            if not after.startswith(at_baseline):
                problems.append(
                    f"the trial log {path} no longer begins with what it held at the "
                    f"stage's input commit {baseline}"
                )
        return problems
    
    def _evidence_result(
        self, stage: StageConfig, result: StageResult, commit: str,
    ) -> Optional[verification.EvidenceResult]:
        """Check the stage's evidence file at a commit. None: the stage requires none."""
        settings = stage.verification
        if not settings:
            return None
        number = self.stage_number(stage=stage)
        section = ""
        earlier_sections = {}
        if self.working_document_config:
            content = self._file_bytes(path=self.working_document_config["path"], commit=commit)
            text = (content or b"").decode("utf-8", errors="replace")
            for position, earlier in enumerate(self.stages[:number], start=1):
                try:
                    extracted = extract_stage_section(text=text, number=position, name=earlier.name)
                except WorkingDocumentError:
                    extracted = ""
                if position == number:
                    section = extracted
                else:
                    earlier_sections[position] = extracted
        upstream = {}
        for earlier in self.stages[:number - 1]:
            if earlier.verification:
                evidence, _ = verification.load_evidence(
                    content=self._file_bytes(
                        path=earlier.verification["evidence_file"], commit=commit,
                    ),
                    path=earlier.verification["evidence_file"],
                )
                upstream[earlier.verification["kind"]] = evidence
        return verification.check_evidence(
            kind=settings["kind"],
            context=verification.EvidenceContext(
                stage_name=stage.name,
                round_number=result.round,
                settings=settings,
                read=lambda path: self._file_bytes(path=path, commit=commit),
                read_input=lambda path: self._file_bytes(path=path, commit=result.input_commit),
                section=section,
                earlier_sections=earlier_sections,
                upstream_evidence=upstream,
                decision_ids=frozenset(
                    decision["decision_id"] for decision in self.manifest.decisions
                ),
                phase=self._evidence_phase(stage=stage, result=result),
            ),
        )
    
    def _failure_gate(
        self, stage: StageConfig, problem: str, branch: Optional[str] = None,
    ) -> dict:
        """The check record of a round that produced no commit to check."""
        return {
            "checked_at": utc_now(),
            "branch": branch,
            "commit": None,
            "required_files": list(stage.output_manifest.get("required_files", [])),
            "missing_files": [],
            "problems": [problem],
            "blockers": [],
            "no_plausible_alternative": False,
            "evidence": None,
            "acceptable": False,
            "passed": False,
            "note": CHECKS_NOTE,
        }
    
    def _candidate_gate(
        self, stage: StageConfig, result: StageResult, branch: str, commit: str,
    ) -> dict:
        """Check a candidate commit and return the record of the checks.

        Checked at the commit: the required files, the working document's
        rules and the source document (against the stage's input commit),
        the changed paths, the trial log and the stage's evidence file.
        No problem means the output has the required form; it does not
        mean its content is right.

        Raises:
            APIError: if GitHub cannot be read.
        """
        baseline = result.baseline_commit
        problems = []
        missing = self._missing_required_files(stage=stage, commit=commit)
        if missing:
            problems.append(f"required output file(s) missing: {', '.join(missing)}")
        if self.working_document_config:
            problems.extend(self._working_document_problems(
                stage=stage, input_commit=baseline, commit=commit,
            ))
        problems.extend(self._path_problems(
            stage=stage, baseline_commit=baseline, commit=result.content_commit or commit,
        ))
        problems.extend(self._trial_log_problems(
            result=result, before_commit=result.launch_commit or result.input_commit,
            after_commit=commit,
        ))
        evidence = self._evidence_result(stage=stage, result=result, commit=commit)
        if evidence is not None:
            problems.extend(evidence.problems)
        proposal_only = (
            self._evidence_phase(stage=stage, result=result) == verification.PHASE_DOMAIN_PROPOSAL
        )
        return {
            "checked_at": utc_now(),
            "branch": branch,
            "commit": commit,
            "baseline_commit": baseline,
            "required_files": list(stage.output_manifest.get("required_files", [])),
            "missing_files": missing,
            "problems": problems,
            # Open items only the owner can close. Reported, never closed here.
            "blockers": evidence.blockers if evidence is not None else [],
            "no_plausible_alternative": (
                evidence.no_plausible_alternative if evidence is not None else False
            ),
            # As the stage wrote it: reported, not verified.
            "evidence": evidence.to_record() if evidence is not None else None,
            # A domain proposal holds no results, so it cannot end the stage.
            "acceptable": not proposal_only,
            "passed": not problems,
            "note": CHECKS_NOTE,
        }
    
    def _check_output(self, stage: StageConfig, result: StageResult) -> bool:
        """Check what the current launch pushed. Launches nothing.

        A failed check is a result of the round and is recorded. If GitHub
        cannot be read, nothing is recorded as checked: the launch stays
        RUN_FINISHED and a resume checks again.
        """
        check = {
            PHASE_PROPOSE: self._check_candidate,
            PHASE_BLIND: self._check_blind,
            PHASE_COMPARE: self._check_comparison,
        }[result.phase]
        try:
            check(stage=stage, result=result)
        except APIError as error:
            result.error = (
                f"The output could not be checked: {error}. A resume checks again "
                f"and launches nothing."
            )
            self.checkpoint()
            return False
        result.status = STAGE_OUTPUT_CHECKED
        result.error = None
        self.checkpoint()
        return True
    
    def _check_candidate(self, stage: StageConfig, result: StageResult) -> None:
        """Check the proposer's output and record the candidate commit."""
        try:
            branch, commit = self._pushed_branch_head(result=result)
        except OutputGateError as error:
            result.gate = self._failure_gate(stage=stage, problem=str(error))
        else:
            if commit == result.launch_commit:
                result.gate = self._failure_gate(
                    stage=stage, branch=branch,
                    problem=(
                        f"branch '{branch}' is still at the input commit: the stage "
                        f"pushed no new commit"
                    ),
                )
            else:
                result.candidate_branch, result.candidate_commit = branch, commit
                result.gate = self._candidate_gate(
                    stage=stage, result=result, branch=branch, commit=commit,
                )
        gate = result.gate
        self._record_event(
            stage=stage, round_number=result.round, kind="checked-propose",
            facts={
                "What happened": "The proposer's output was checked",
                "Run status": result.run_status,
                "Prompt SHA-256": result.prompt_sha256,
                "Candidate": (
                    f"commit {gate['commit']} on branch '{gate['branch']}'"
                    if gate["commit"] else "none"
                ),
                "Evidence file": (
                    stage.verification["evidence_file"] if stage.verification else "none required"
                ),
                "Result": "checks passed" if gate["passed"] else "checks FAILED",
                "Problems": gate["problems"],
                "Open items for the owner": gate["blockers"],
                "Note": "Checks of form and cross-references, not of correctness",
            },
        )
        print(
            f"  Candidate: {gate['commit'] or 'none'}"
            + (f" on branch '{gate['branch']}'" if gate["branch"] else "")
        )
        print(
            f"  Checks: {'passed' if gate['passed'] else 'FAILED'} "
            f"({len(gate['problems'])} problem(s), {len(gate['blockers'])} open "
            f"item(s) for the owner)"
        )
    
    def _check_blind(self, stage: StageConfig, result: StageResult) -> None:
        """Check the reviewer's blind assessment: one new file on top of the round's input."""
        path = self._review_paths(stage=stage, round_number=result.round)["blind"]
        record = {"path": path, "branch": None, "commit": None, "sha256": None, "problems": []}
        try:
            branch, commit = self._pushed_branch_head(result=result)
        except OutputGateError as error:
            record["problems"].append(str(error))
        else:
            record["branch"], record["commit"] = branch, commit
            relation = self._compare(base=result.input_commit, head=commit)
            content = self._file_bytes(path=path, commit=commit)
            if relation["status"] != "ahead":
                record["problems"].append(
                    f"commit {commit} does not build on the round's input commit "
                    f"{result.input_commit} (GitHub reports '{relation['status']}')"
                )
            elif relation["files"] != [path]:
                record["problems"].append(
                    f"the blind assessment must add only {path}; the files changed "
                    f"are: {', '.join(relation['files']) or 'none'}"
                )
            if content is None or not content.strip():
                record["problems"].append(f"{path} is missing or empty at commit {commit}")
            else:
                record["sha256"] = hashlib.sha256(content).hexdigest()
        record["checked_at"] = utc_now()
        result.cross_review = {
            "reviewer_model": result.model,
            "pass_policy": stage.cross_model["pass_policy"],
            "blind": record,
        }
        self._record_event(
            stage=stage, round_number=result.round, kind="checked-blind_review",
            facts={
                "What happened": "The reviewer's blind assessment was checked",
                "Run status": result.run_status,
                "Prompt SHA-256": result.prompt_sha256,
                "Blind assessment": (
                    f"{path} at commit {record['commit']}" if record["commit"] else "none"
                ),
                "Written without the candidate": (
                    "the launch was not given the candidate, and the commit adds only "
                    "this file to the round's input" if not record["problems"] else "not shown"
                ),
                "Problems": record["problems"],
            },
        )
    
    def _candidate_file_hashes(self, stage: StageConfig, result: StageResult) -> dict:
        """SHA-256 of each reviewed file as it is at the candidate commit."""
        if self.dry_run:
            return {path: "<SHA-256>" for path in self._reviewed_files(stage=stage)}
        hashes = {}
        for path in self._reviewed_files(stage=stage):
            content = self._file_bytes(path=path, commit=result.candidate_commit)
            if content is not None:
                hashes[path] = hashlib.sha256(content).hexdigest()
        return hashes
    
    def _check_comparison(self, stage: StageConfig, result: StageResult) -> None:
        """Check the reviewer's comparison: bound to the candidate, which it leaves unchanged."""
        paths = self._review_paths(stage=stage, round_number=result.round)
        blind = result.cross_review["blind"]
        candidate = result.candidate_commit
        record = {
            "path": paths["comparison"], "branch": None, "commit": None,
            "problems": [], "verdict": None, "contains_candidate_unchanged": False,
        }
        try:
            branch, commit = self._pushed_branch_head(result=result)
        except OutputGateError as error:
            record["problems"].append(str(error))
        else:
            record["branch"], record["commit"] = branch, commit
            problems = record["problems"]
            if self._compare(base=blind["commit"], head=commit)["status"] != "ahead":
                problems.append(
                    f"commit {commit} does not build on the blind assessment's commit "
                    f"{blind['commit']}"
                )
            from_candidate = self._compare(base=candidate, head=commit)
            if from_candidate["status"] != "ahead":
                problems.append(f"commit {commit} does not contain the candidate commit {candidate}")
            else:
                permitted = [paths["blind"], paths["comparison"]]
                if self.trial_log_path:
                    permitted.append(self.trial_log_path)
                changed = [path for path in from_candidate["files"] if path not in permitted]
                if changed or from_candidate["truncated"]:
                    problems.append(
                        f"the reviewer changed file(s) other than its own: "
                        f"{', '.join(changed[:20]) or 'too many to list'}"
                    )
                else:
                    record["contains_candidate_unchanged"] = True
            blind_now = self._file_bytes(path=paths["blind"], commit=commit)
            if blind_now is None or hashlib.sha256(blind_now).hexdigest() != blind["sha256"]:
                problems.append(f"{paths['blind']} was changed or removed after it was committed")
            comparison, load_problems = verification.load_evidence(
                content=self._file_bytes(path=paths["comparison"], commit=commit),
                path=paths["comparison"],
            )
            problems.extend(
                problem.replace("evidence file", "comparison file") for problem in load_problems
            )
            if comparison is not None:
                comparison_problems, record["verdict"] = verification.check_comparison(
                    comparison=comparison,
                    candidate_commit=candidate,
                    candidate_files=self._candidate_file_hashes(stage=stage, result=result),
                    blind_commit=blind["commit"],
                    blind_sha256=blind["sha256"],
                )
                problems.extend(f"{paths['comparison']}: {p}" for p in comparison_problems)
            problems.extend(self._trial_log_problems(
                result=result, before_commit=candidate, after_commit=commit,
            ))
        record["checked_at"] = utc_now()
        result.cross_review["comparison"] = record
        verdict = record["verdict"] or {}
        self._record_event(
            stage=stage, round_number=result.round, kind="checked-compare",
            facts={
                "What happened": "The reviewer's comparison with the candidate was checked",
                "Run status": result.run_status,
                "Prompt SHA-256": result.prompt_sha256,
                "Candidate reviewed": f"commit {candidate}",
                "Comparison": (
                    f"{paths['comparison']} at commit {record['commit']}"
                    if record["commit"] else "none"
                ),
                "Reviewer's verdict": (
                    ("accept" if verdict.get("accept") else "reject")
                    + ", " + ("agrees" if verdict.get("agree") else "does not agree")
                    + " with its blind assessment" if record["verdict"] else "not usable"
                ),
                "Blocking differences": verdict.get("blocking_differences", []),
                "Reviewer's rationale": verdict.get("rationale", ""),
                "Problems": record["problems"],
                "Note": "Two models that agree are not two independent proofs",
            },
        )
    
    def _collect_artifacts(self, stage: StageConfig, result: StageResult) -> bool:
        """Download the current launch's artifacts into the project's artifact folder.

        An artifact whose path is unsafe is not written; it is recorded
        under `rejected`. A failed listing or download stops the stage with
        ARTIFACTS_FAILED; a resume tries the download again and launches
        nothing.
        """
        if (
            not self.artifacts_config.get("collect", False)
            or result.agent_id is None or result.artifacts is not None
        ):
            return True
        
        # One folder per launch: files of one round or one role are never
        # mixed with those of another.
        root = self.artifacts_root / stage.name / f"round_{result.round}" / result.phase
        downloaded = []
        rejected = []
        try:
            items = self._with_retries(
                action=lambda: self.api.list_artifacts(agent_id=result.agent_id),
                description="Listing artifacts",
            )
            for item in items:
                artifact_path = item.get("path") if isinstance(item, dict) else None
                try:
                    destination = safe_artifact_destination(
                        root=root, artifact_path=artifact_path,
                    )
                except UnsafeArtifactPathError as error:
                    rejected.append({"path": repr(artifact_path), "reason": str(error)})
                    print(f"  WARNING: artifact {artifact_path!r} not saved: {error}")
                    continue
                content = self._with_retries(
                    action=lambda: self.api.download_artifact(
                        agent_id=result.agent_id, artifact_path=artifact_path,
                    ),
                    description=f"Downloading {artifact_path}",
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                downloaded.append({
                    "path": artifact_path,
                    "saved_as": str(destination),
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                })
        except CursorAPIError as error:
            result.status = STAGE_ARTIFACTS_FAILED
            result.error = f"The artifacts could not be collected: {error}"
            self.checkpoint()
            return False
        
        result.artifacts = {
            "collected_at": utc_now(),
            "directory": str(root),
            "downloaded": downloaded,
            "rejected": rejected,
        }
        result.error = None
        print(f"  Artifacts: {len(downloaded)} saved under {root}, {len(rejected)} rejected")
        return True
    
    def _archive_launch(self, result: StageResult) -> None:
        """Keep the record of the current launch among the round's launches, once."""
        if result.agent_id is None:
            return
        if any(launch["agent_id"] == result.agent_id for launch in result.launches):
            return
        result.launches.append({name: getattr(result, name) for name in LAUNCH_FIELDS})
    
    def _start_phase(self, result: StageResult, phase: str) -> bool:
        """Go on to the round's next launch. Nothing of the last launch is carried over."""
        defaults = StageResult(name=result.name)
        for name in LAUNCH_FIELDS:
            setattr(result, name, getattr(defaults, name))
        result.phase = phase
        result.status = STAGE_PENDING
        result.error = None
        self._reported_branches = None
        self.checkpoint()
        return True
    
    def _finish_launch(self, stage: StageConfig, result: StageResult) -> bool:
        """After a launch's output is checked: the round's next launch, or its end."""
        self._archive_launch(result=result)
        if result.phase == PHASE_PROPOSE:
            # A reviewer is paid for only when the candidate has the required form.
            if (
                stage.cross_model_enabled and result.origin == ORIGIN_PIPELINE
                and result.gate["passed"]
            ):
                return self._start_phase(result=result, phase=PHASE_BLIND)
        elif result.phase == PHASE_BLIND:
            if not result.cross_review["blind"]["problems"]:
                return self._start_phase(result=result, phase=PHASE_COMPARE)
        return self._close_round(stage=stage, result=result)
    
    def _round_outcome(self, stage: StageConfig, result: StageResult) -> str:
        """How a round ended, from its recorded checks."""
        gate = result.gate or {}
        if gate.get("problems"):
            return OUTCOME_CHECKS_FAILED
        review = result.cross_review
        if review is not None:
            comparison = review.get("comparison")
            if review["blind"]["problems"] or comparison is None or comparison["problems"]:
                return OUTCOME_REVIEW_INVALID
            verdict = comparison["verdict"]
            accepted = verdict["accept"] and (
                review["pass_policy"] == "reviewer_accept" or verdict["agree"]
            )
            if not accepted:
                return OUTCOME_DISAGREEMENT
        if gate.get("blockers"):
            return OUTCOME_BLOCKERS_OPEN
        return OUTCOME_PASSED
    
    def _sees_no_alternative(self, result: StageResult) -> bool:
        """True if the proposer or the reviewer stated that it sees no other route."""
        comparison = (result.cross_review or {}).get("comparison") or {}
        verdict = comparison.get("verdict") or {}
        return bool(
            (result.gate or {}).get("no_plausible_alternative")
            or verdict.get("plausible_alternative") is False
        )
    
    def _route(self, stage: StageConfig, result: StageResult) -> tuple[Optional[str], Optional[str]]:
        """Decide what follows a closed round.

        Returns:
            (the reason the round stops at the human gate, None), or
            (None, the kind of the next round) when the stage allows a
            round without the owner and one is possible. A round is never
            followed by a round of another stage.
        """
        outcome = result.outcome
        if result.origin == ORIGIN_ADOPTED:
            return STOP_ADOPTED, None
        if outcome == OUTCOME_PASSED:
            return STOP_PASSED, None
        if outcome in INFRASTRUCTURE_OUTCOMES:
            return STOP_INFRASTRUCTURE, None
        if outcome == OUTCOME_BLOCKERS_OPEN:
            return STOP_OWNER_DECISION, None
        if self._sees_no_alternative(result=result):
            return STOP_NO_ALTERNATIVE, None
        if result.round >= self.round_limit(stage=stage):
            return STOP_CAP_EXHAUSTED, None
        if outcome == OUTCOME_REVIEW_INVALID:
            return STOP_REVIEW_REQUIRED, None
        if outcome == OUTCOME_DISAGREEMENT:
            if stage.autonomous_fix and stage.cross_model["on_disagreement"] == "revise_next_round":
                return None, KIND_RECONCILE
            return STOP_DISAGREEMENT, None
        if stage.autonomous_fix:
            return None, KIND_FIX
        return STOP_REVIEW_REQUIRED, None
    
    def _next_round_revision(
        self, stage: StageConfig, result: StageResult, kind: str,
    ) -> dict:
        """The revision record of a round the orchestrator starts itself."""
        if kind == KIND_FIX:
            source = "checks"
            feedback = (
                f"The checks of round {result.round} found:\n"
                + "\n".join(f"- {problem}" for problem in result.gate["problems"])
            )
        else:
            source = "review"
            paths = self._review_paths(stage=stage, round_number=result.round)
            verdict = result.cross_review["comparison"]["verdict"]
            feedback = (
                f"The reviewer's verdict on round {result.round}: "
                f"{'accept' if verdict['accept'] else 'reject'}; its blind assessment "
                f"{'agrees' if verdict['agree'] else 'does not agree'} with the "
                f"candidate.\nBlocking differences:\n"
                + ("\n".join(f"- {item}" for item in verdict["blocking_differences"]) or "- none listed")
                + f"\nThe reviewer's rationale: {verdict['rationale']}\n"
                f"The blind assessment is {paths['blind']}; the comparison is "
                f"{paths['comparison']}."
            )
        return {
            "source": source,
            "decision_id": None,
            "requested_at": utc_now(),
            "replaces_round": result.round,
            "base_branch": result.output_branch,
            "base_commit": result.output_commit or result.input_commit,
            "feedback": feedback,
            "feedback_sha256": hashlib.sha256(feedback.encode("utf-8")).hexdigest(),
            "approve_batch": False,
        }
    
    def _close_round(self, stage: StageConfig, result: StageResult) -> bool:
        """End a round: record how it ended and why, then take the route that follows.

        The record is written to the trial journal before anything else
        is done: what was tried is in the events before it, and this event
        holds the result, what becomes of the candidate, and why the next
        step was chosen.
        """
        result.outcome = result.outcome or self._round_outcome(stage=stage, result=result)
        comparison = (result.cross_review or {}).get("comparison") or {}
        if comparison.get("contains_candidate_unchanged"):
            # The reviewer's commit is the candidate plus the review files.
            result.output_branch, result.output_commit = comparison["branch"], comparison["commit"]
        else:
            result.output_branch, result.output_commit = (
                result.candidate_branch, result.candidate_commit,
            )
        stop_reason, next_kind = self._route(stage=stage, result=result)
        gate = result.gate or {}
        
        if next_kind is not None:
            disposition = f"KEEP: commit {result.output_commit or result.input_commit} is the base of the next round"
            rationale = (
                "The reviewer did not agree with the candidate, and the stage is set to "
                "reconcile in a further round" if next_kind == KIND_RECONCILE else
                "The checks failed on points a further round can correct, and the "
                "stage allows a correction round without the owner"
            )
            next_route = (
                f"Round {result.round + 1} ({next_kind}) of the same stage, started by "
                f"the orchestrator"
                + ("; the two models change roles" if stage.cross_model_enabled else "")
            )
        else:
            disposition = (
                "UNRESOLVED: the output waits for the owner's decision"
                if result.output_commit else "ABANDON: the round produced no candidate"
            )
            rationale = STOP_EXPLANATIONS[stop_reason]
            next_route = "Human gate: local checkout, signed notice, then the owner's decision"
        self._record_event(
            stage=stage, round_number=result.round, kind="closed",
            facts={
                "What happened": f"Round {result.round} was closed",
                "Outcome": result.outcome,
                "Output": (
                    f"commit {result.output_commit} on branch '{result.output_branch}'"
                    if result.output_commit else "none"
                ),
                "Problems": gate.get("problems", []),
                "Open items for the owner": gate.get("blockers", []),
                "Disposition": disposition,
                "Rationale": rationale,
                "Next route": next_route,
            },
        )
        result.closed_at = result.closed_at or utc_now()
        print(f"  Round {result.round} closed: {result.outcome}. {rationale}.")
        
        if next_kind is not None:
            self._reserve_round(
                stage=stage,
                kind=next_kind,
                started_by="orchestrator",
                reason=f"round {result.round} ended with {result.outcome}; {rationale.lower()}",
                revision=self._next_round_revision(stage=stage, result=result, kind=next_kind),
            )
            return True
        
        result.stop_reason = stop_reason
        result.status = STAGE_SYNC_PENDING if result.output_commit else STAGE_NOTICE_PENDING
        self.checkpoint()
        return True
    
    # ------------------------------------------------------------------
    # The human gate
    # ------------------------------------------------------------------
    
    def _review_files(self, stage: StageConfig, result: StageResult) -> list[str]:
        """Paths of the files the reviewer reads for a round, as far as they are at its output commit."""
        paths = self._reviewed_files(stage=stage)
        if result.cross_review:
            paths.extend(self._review_paths(stage=stage, round_number=result.round).values())
        if self.trial_log_path:
            paths.append(self.trial_log_path)
        return [
            path for path in dict.fromkeys(paths)
            if self._file_bytes(path=path, commit=result.output_commit) is not None
        ]
    
    def _show_output_locally(self, stage: StageConfig, result: StageResult) -> bool:
        """Check the round's output commit out in the local checkout.

        On failure nothing is forced: the stage stays LOCAL_SYNC_PENDING
        with the reason, no notice is written, and a resume tries again
        without launching anything.
        """
        try:
            result.local_sync = self._local_review().show_commit(
                branch=result.output_branch,
                commit=result.output_commit,
                review_files=self._review_files(stage=stage, result=result),
            )
        except (LocalSyncError, APIError) as error:
            result.local_sync = {
                "verified": False,
                "checked_at": utc_now(),
                "branch": result.output_branch,
                "commit": result.output_commit,
                "problem": str(error),
            }
            result.error = (
                f"The round's output is on GitHub (commit {result.output_commit} on "
                f"branch '{result.output_branch}') but is NOT in the local checkout, "
                f"so no notice is written for it: {error}"
            )
            self.checkpoint()
            return False
        result.error = None
        result.status = STAGE_NOTICE_PENDING
        self.checkpoint()
        return True
    
    def _allowed_actions(self, stage: StageConfig, result: StageResult) -> list[str]:
        """The decisions the owner can take on a round at its gate."""
        actions = []
        if result.outcome == OUTCOME_PASSED and (result.gate or {}).get("acceptable"):
            actions.append(ACTION_ACCEPT)
        if result.round >= self.round_limit(stage=stage):
            actions.append(ACTION_RAISE_LIMIT)
        else:
            if result.output_commit:
                actions.append(ACTION_ITERATE)
            actions.append(ACTION_REDO)
        actions.append(ACTION_HALT)
        return actions
    
    def _build_notice(self, stage: StageConfig, result: StageResult, kind: str) -> dict:
        """Return the signed notice of a round at its gate (`review`) or of an acceptance (`accepted`).

        Raises:
            BridgeError: if it cannot be signed.
            APIError: if the trial log cannot be read on GitHub.
        """
        number = self.stage_number(stage=stage)
        sequence = result.notice_seq + 1
        if kind == "accepted":
            actions = [ACTION_START_STAGE] if number < len(self.stages) else []
        else:
            actions = self._allowed_actions(stage=stage, result=result)
        gate = result.gate or {}
        sync = result.local_sync or {}
        log_path = self.trial_log_path
        pending_entries = []
        if log_path and result.output_commit:
            present = (
                self._file_bytes(path=log_path, commit=result.output_commit) or b""
            ).decode("utf-8", errors="replace")
            pending_entries = [
                trial_log.render_entry(event=event)
                for event in trial_log.entries_missing_from(
                    log_text=present, events=self._project_events(),
                )
            ]
        notice = {
            "event_id": trial_log.event_id(
                project_id=self.manifest.project_id, stage_number=number,
                round_number=result.round, kind=f"notice-{sequence}",
            ),
            "kind": kind,
            "created_at": utc_now(),
            "project_id": self.manifest.project_id,
            "pipeline": self.manifest.pipeline_name,
            "repository": self.manifest.repo_url,
            "stage": {
                "number": number, "of": len(self.stages),
                "name": stage.name, "display_name": stage.display_name,
            },
            "round": result.round,
            "round_limit": self.round_limit(stage=stage),
            "round_kind": result.kind,
            "origin": result.origin,
            "outcome": result.outcome,
            "stop_reason": result.stop_reason,
            "commits": {
                "input": result.input_commit,
                "baseline": result.baseline_commit,
                "candidate": result.candidate_commit,
                "content": result.content_commit,
                "output": result.output_commit,
            },
            "branch": result.output_branch,
            "config_sha256": self.manifest.provenance["config_sha256"],
            "checks": {
                "passed": gate.get("passed", False),
                "problems": gate.get("problems", []),
                "open_items_for_owner": gate.get("blockers", []),
                "evidence": gate.get("evidence"),
                "note": CHECKS_NOTE,
            },
            "cross_review": result.cross_review,
            "local": {
                "checkout": sync.get("checkout"),
                "at_output_commit": bool(sync.get("verified")),
                "review_files": sync.get("review_files", []),
                "trial_log": (
                    str(Path(sync["checkout"]) / log_path)
                    if log_path and sync.get("verified") else None
                ),
                "trial_journal": str(self.journal.path),
                "manifest": str(self.manifest_path.resolve()),
            },
            "on_github": {
                "commit": (
                    self._commit_url(commit=result.output_commit)
                    if result.output_commit else None
                ),
                "trial_log": log_path,
            },
            # Journal entries that are not in the repository's trial log at
            # the output commit; the next launch of the project appends them.
            "trial_log_entries_not_yet_in_repository": pending_entries,
            "allowed_decisions": [{"action": action} for action in actions],
            "message": self._notice_message(stage=stage, result=result, kind=kind, actions=actions),
        }
        return self.bridge.sign_notice(notice=notice)
    
    def _notice_message(
        self, stage: StageConfig, result: StageResult, kind: str, actions: list[str],
    ) -> str:
        """The text the host posts in the main chat for a notice."""
        number = self.stage_number(stage=stage)
        head = (
            f"Stage {number} of {len(self.stages)} ({stage.display_name}), round "
            f"{result.round} of at most {self.round_limit(stage=stage)}"
        )
        if kind == "accepted":
            return (
                f"{head}: ACCEPTED at commit {result.output_commit}. Nothing was "
                f"launched. "
                + (
                    f"Stage {number + 1} starts only on a separate start_stage decision."
                    if actions else "It was the last stage."
                )
            )
        gate = result.gate or {}
        where = (
            f"Output: commit {result.output_commit} on branch '{result.output_branch}'"
            + (
                f", checked out in {result.local_sync['checkout']}."
                if (result.local_sync or {}).get("verified") else "."
            )
            if result.output_commit else "The round produced no output commit."
        )
        return (
            f"{head}: {result.outcome}. Stopped because: "
            f"{STOP_EXPLANATIONS[result.stop_reason]}. {where} "
            f"{len(gate.get('problems', []))} problem(s), "
            f"{len(gate.get('blockers', []))} open item(s) for the owner. "
            f"Possible decisions: {', '.join(actions)}. {CHECKS_NOTE}"
        )
    
    def _reopen_notice(self, result: StageResult) -> None:
        """Set a round back to NOTICE_PENDING: a new notice is written and must be acknowledged."""
        result.notice = None
        result.review = None
        result.status = STAGE_NOTICE_PENDING
    
    def _deliver_notice(self, stage: StageConfig, result: StageResult) -> bool:
        """Write the round's signed notice and look for the host's ack.

        The round is AWAITING_REVIEW only once a valid ack is in the
        inbox. Without it the round stays NOTICE_PENDING: what is printed
        on the console is never counted as delivery.
        """
        try:
            if result.notice is None:
                event = self._build_notice(stage=stage, result=result, kind="review")
                result.notice_seq += 1
                result.notice = {"event": event, "ack": None}
                self.checkpoint()
            event = result.notice["event"]
            path = self.bridge.emit(notice=event)
            ack = self.bridge.find_ack(notice=event)
        except (BridgeError, APIError) as error:
            result.error = f"The notice of this round could not be handed to the host: {error}"
            self.checkpoint()
            return False
        if ack is None:
            result.error = (
                f"The notice {path} is written. The host has not acknowledged posting "
                f"it in the main chat: {self.bridge.ack_path(event_id=event['event_id'])} "
                f"is not there. Until it is, the output is NOT announced for review."
            )
            self.checkpoint()
            return False
        self._record_event(
            stage=stage, round_number=result.round, kind=f"notice-{result.notice_seq}-acknowledged",
            facts={
                "What happened": "The host acknowledged posting the notice in the main chat",
                "Notice": event["event_id"],
                "Chat message": ack["chat"]["message_ref"],
                "Stop reason": result.stop_reason,
                "Possible decisions": [entry["action"] for entry in event["allowed_decisions"]],
            },
        )
        result.notice["ack"] = ack
        result.status = STAGE_AWAITING_REVIEW
        result.review = {"requested_at": utc_now()}
        result.error = None
        self.checkpoint()
        return True
    
    def _print_review_notice(self, stage: StageConfig, result: StageResult) -> None:
        """Print where a round at its gate stands. The console is not the delivery."""
        number = self.stage_number(stage=stage)
        event = result.notice["event"]
        gate = result.gate or {}
        line = "=" * 70
        print(f"\n  {line}")
        print(
            f"  AWAITING THE OWNER'S DECISION: stage {number} of {len(self.stages)}, "
            f"{stage.display_name} (round {result.round} of at most "
            f"{self.round_limit(stage=stage)})"
        )
        print(f"  {line}")
        print(f"  Outcome: {result.outcome}; stopped because: {STOP_EXPLANATIONS[result.stop_reason]}")
        if result.output_commit:
            print(f"  Output commit: {result.output_commit}")
            print(f"  Output branch: {result.output_branch}")
            print(f"  On GitHub:     {self._commit_url(commit=result.output_commit)}")
            for path in (result.local_sync or {}).get("review_files", []):
                print(f"    {path}")
        for problem in gate.get("problems", []):
            print(f"  Problem: {problem}")
        for blocker in gate.get("blockers", []):
            print(f"  Open item for the owner: {blocker}")
        print(f"  {CHECKS_NOTE}")
        print(f"  Notice: {self.bridge.notice_path(event_id=event['event_id'])}")
        print(f"  Posted in the main chat as: {result.notice['ack']['chat']['message_ref']}")
        print(
            f"  Possible decisions: "
            f"{', '.join(entry['action'] for entry in event['allowed_decisions'])}"
        )
        print(
            f"  Nothing further happens until a signed decision for this notice is "
            f"applied:\n    --resume {self.manifest_path} --apply-decision <decision file>"
        )
        print(f"  {line}")
    
    def _commit_url(self, commit: str) -> str:
        return f"https://github.com/{self.github.full_name}/tree/{commit}"
    
    # ------------------------------------------------------------------
    # The owner's decisions
    # ------------------------------------------------------------------
    
    def _issue_receipt(self, stage: StageConfig, result: StageResult) -> None:
        """Write the signed notice of an acceptance. It carries the token that starts the next stage.

        If it cannot be written now, the stage stays accepted and a resume
        writes it again; no decision can start the next stage before it is
        written.
        """
        try:
            if result.receipt is None:
                event = self._build_notice(stage=stage, result=result, kind="accepted")
                result.notice_seq += 1
                result.receipt = {"event": event}
                self.checkpoint()
            self.bridge.emit(notice=result.receipt["event"])
        except (BridgeError, APIError) as error:
            result.error = f"The notice of the acceptance could not be written: {error}"
            self.checkpoint()
            return
        result.error = None
    
    def _record_decision(
        self, stage: StageConfig, result: StageResult, decision: dict,
        disposition: str, next_route: str,
    ) -> None:
        """Record an owner's decision in the manifest's list and in the trial journal."""
        self._record_event(
            stage=stage, round_number=result.round,
            kind=f"decision-{decision['decision_id']}",
            facts={
                "What happened": f"The project owner's decision was applied: {decision['action']}",
                "Decision": decision["decision_id"],
                "Answers notice": decision["event_id"],
                "Bound to": f"round {decision['round']}, commit {decision['commit']}",
                "Owner's message": (
                    f"{decision['chat']['message_ref']}: "
                    f"\"{' '.join(decision['chat']['quote'].split())}\""
                ),
                "Parameters": decision["params"] or "none",
                "Disposition": disposition,
                "Next route": next_route,
            },
        )
        self.manifest.decisions.append(decision)
    
    def apply_decision(self, path: Path) -> None:
        """Apply a signed decision of the project owner.

        A decision is accepted only if it is signed with the shared
        secret, carries the token of the open notice and of its action,
        and names this project, the stage, the round, the exact output
        commit and the configuration of that notice. A decision that was
        applied before does nothing; a second decision for a notice that
        was decided is refused.

        Raises:
            ProjectStateError: if the decision cannot be trusted, does not
                answer the open notice, or what it asks for is not allowed.
            APIError: if GitHub cannot be read.
        """
        try:
            decision = self.bridge.read_decision(path=path)
        except BridgeError as error:
            raise ProjectStateError(str(error)) from error
        identifier = decision["decision_id"]
        if any(applied["decision_id"] == identifier for applied in self.manifest.decisions):
            print(f"\nDecision {identifier} was applied before. Nothing is done again.")
            return
        if self.manifest.halted:
            raise ProjectStateError(
                f"The project is halted ({self.manifest.halted['reason'] or 'no reason given'}). "
                f"No decision is applied until it is taken out of the halt with --unhalt."
            )
        
        stage = self.stage_named(name=decision.get("stage"))
        result = self.manifest.stages.get(stage.name)
        action = decision["action"]
        if action == ACTION_START_STAGE:
            notice = (result.receipt or {}).get("event") if (
                result is not None and result.status == STAGE_COMPLETE
            ) else None
            needed = "accepted, with the notice of its acceptance written"
        else:
            notice = (result.notice or {}).get("event") if (
                result is not None and result.status == STAGE_AWAITING_REVIEW
            ) else None
            needed = "awaiting review"
        if notice is None:
            state = "it has not been started" if result is None else f"its status is {result.status}"
            raise ProjectStateError(
                f"Decision {identifier} ({action}) is not applied: stage "
                f"'{stage.name}' is not {needed}; {state}."
            )
        
        expected = {
            "event_id": notice["event_id"],
            "event_payload_sha256": notice["payload_sha256"],
            "project_id": self.manifest.project_id,
            "round": result.round,
            "commit": result.output_commit,
            "config_sha256": self.manifest.provenance["config_sha256"],
        }
        differing = [
            f"{key} is {decision.get(key)!r}, the open notice has {value!r}"
            for key, value in expected.items() if decision.get(key) != value
        ]
        if differing:
            raise ProjectStateError(
                f"Decision {identifier} ({action}) does not answer the open notice of "
                f"stage '{stage.name}':\n  - " + "\n  - ".join(differing)
                + "\n  A decision holds only for the notice, round, commit and "
                "configuration it was made for."
            )
        allowed = [entry["action"] for entry in notice["allowed_decisions"]]
        if action not in allowed:
            raise ProjectStateError(
                f"Decision {identifier}: '{action}' is not among the decisions the "
                f"notice {notice['event_id']} allows ({', '.join(allowed) or 'none'})."
            )
        if any(applied["event_id"] == notice["event_id"] for applied in self.manifest.decisions):
            raise ProjectStateError(
                f"Decision {identifier} is refused: the notice {notice['event_id']} "
                f"was already answered by another decision."
            )
        
        handler = {
            ACTION_ACCEPT: self._decide_accept,
            ACTION_START_STAGE: self._decide_start_stage,
            ACTION_ITERATE: self._decide_iterate,
            ACTION_REDO: self._decide_redo,
            ACTION_RAISE_LIMIT: self._decide_raise_limit,
            ACTION_HALT: self._decide_halt,
        }[action]
        handler(stage=stage, result=result, decision=decision)
    
    def _require_gate_ready(self, what: str) -> None:
        try:
            self._check_gate_ready()
        except (LocalSyncError, BridgeError) as error:
            raise ProjectStateError(
                f"{what} is not started: its output could not be shown for review "
                f"or announced. {error}"
            ) from error
    
    def _decide_accept(self, stage: StageConfig, result: StageResult, decision: dict) -> None:
        """Accept a round's output. Launches nothing: the next stage needs its own decision."""
        gate = result.gate or {}
        if result.outcome != OUTCOME_PASSED or not gate.get("acceptable"):
            raise ProjectStateError(
                f"Stage '{stage.name}' cannot be accepted: round {result.round} ended "
                f"with {result.outcome}. Only a round that passed every required "
                f"check can be accepted."
            )
        head = self._branch_head(branch=result.output_branch)
        if head != result.output_commit:
            raise ProjectStateError(
                f"Stage '{stage.name}' is not accepted: branch '{result.output_branch}' "
                f"has moved from the reviewed commit {result.output_commit} to {head}. "
                f"The checks"
                + (" and the reviewer's verdict" if result.cross_review else "")
                + f" hold for the reviewed commit only. To have the new commit checked, "
                f"decide iterate with base_commit {head}."
            )
        try:
            state = self._local_review().state()
        except LocalSyncError as error:
            raise ProjectStateError(
                f"Stage '{stage.name}' is not accepted: the local checkout could "
                f"not be read. {error}"
            ) from error
        if state["changed_files"] or state["commit"] != result.output_commit:
            raise ProjectStateError(
                f"Stage '{stage.name}' is not accepted: the local checkout is at "
                f"commit {state['commit']} with {len(state['changed_files'])} "
                f"uncommitted change(s), not cleanly at the reviewed commit "
                f"{result.output_commit}. What is accepted is what is shown there."
            )
        
        self._record_decision(
            stage=stage, result=result, decision=decision,
            disposition=f"KEEP: commit {result.output_commit} is accepted by the owner",
            next_route=(
                "Nothing is launched. The next stage starts only on a separate "
                "start_stage decision"
            ),
        )
        result.review = {
            **(result.review or {}),
            "decision_id": decision["decision_id"],
            "accepted_at": utc_now(),
            "accepted_commit": result.output_commit,
        }
        result.status = STAGE_COMPLETE
        result.completed_at = utc_now()
        self.checkpoint()
        self._issue_receipt(stage=stage, result=result)
        print(
            f"\nStage '{stage.name}' is accepted at commit {result.output_commit}. "
            f"Nothing was launched."
        )
    
    def _decide_start_stage(self, stage: StageConfig, result: StageResult, decision: dict) -> None:
        """Start the first round of the stage after an accepted one."""
        number = self.stage_number(stage=stage)
        if number >= len(self.stages):
            raise ProjectStateError(f"Stage '{stage.name}' is the last stage; no stage follows it.")
        following = self.stages[number]
        existing = self.manifest.stages.get(following.name)
        if existing is not None and existing.round > 0:
            raise ProjectStateError(f"Stage '{following.name}' was already started.")
        self._require_gate_ready(what=f"Stage '{following.name}'")
        self._record_decision(
            stage=stage, result=result, decision=decision,
            disposition=f"KEEP: the accepted commit {result.output_commit} is the next stage's input",
            next_route=f"Round 1 of stage '{following.name}'",
        )
        self._reserve_round(
            stage=following, kind=KIND_INITIAL, started_by=decision["decision_id"],
            reason=f"the owner decided to start the stage (decision {decision['decision_id']})",
        )
    
    def _decide_iterate(self, stage: StageConfig, result: StageResult, decision: dict) -> None:
        """Start a round that revises the reviewed output as the owner's feedback asks."""
        params = decision["params"]
        feedback = params.get("feedback", "")
        approve_batch = params.get("approve_batch", False)
        base_commit = params.get("base_commit")
        if not isinstance(feedback, str) or not isinstance(approve_batch, bool) or not (
            base_commit is None
            or (isinstance(base_commit, str) and COMMIT_SHA_PATTERN.fullmatch(base_commit))
        ):
            raise ProjectStateError(
                "An iterate decision takes feedback (text), approve_batch (true or "
                "false) and base_commit (a full commit SHA)."
            )
        batches = (
            stage.verification is not None
            and stage.verification["kind"] == verification.KIND_PARAMETER_BATCHES
        )
        if approve_batch and not batches:
            raise ProjectStateError(
                f"approve_batch applies only to a stage that runs parameter batches; "
                f"stage '{stage.name}' does not."
            )
        if not approve_batch and not feedback.strip():
            raise ProjectStateError(
                "An iterate decision needs feedback: the text that says what to revise."
            )
        self._require_gate_ready(what=f"A further round of stage '{stage.name}'")
        head = self._branch_head(branch=result.output_branch)
        if (base_commit or result.output_commit) != head:
            raise ProjectStateError(
                f"The commit to revise must be the current head of branch "
                f"'{result.output_branch}', which is {head}"
                + (
                    "." if base_commit else
                    f" (the branch has moved since commit {result.output_commit} was "
                    f"reviewed; name the new head as base_commit to revise from it)."
                )
            )
        if approve_batch:
            spec_path = stage.verification["next_batch_spec"]
            spec = self._file_bytes(path=spec_path, commit=head)
            spec_problems = (
                [f"{spec_path} is not at commit {head}"] if spec is None else
                verification.batch_file_problems(
                    content=spec, path=spec_path,
                    limit=stage.verification["max_configurations"],
                )
            )
            if spec_problems:
                raise ProjectStateError(
                    "The batch cannot be approved: " + "; ".join(spec_problems)
                )
            approval = (
                f"The project owner approved the batch in {spec_path} as it is at "
                f"commit {head} (SHA-256 {hashlib.sha256(spec).hexdigest()}). Run "
                f"exactly that batch."
            )
            feedback = f"{approval}\n{feedback}".rstrip()
        
        self._record_decision(
            stage=stage, result=result, decision=decision,
            disposition=f"KEEP: commit {head} is the base of the next round",
            next_route=(
                f"Round {result.round + 1} of the same stage: "
                + ("run the approved batch" if approve_batch else "revise as the owner's feedback asks")
            ),
        )
        self._reserve_round(
            stage=stage, kind=KIND_REVISE, started_by=decision["decision_id"],
            reason=f"the owner asked for it (decision {decision['decision_id']})",
            revision={
                "source": "owner",
                "decision_id": decision["decision_id"],
                "requested_at": utc_now(),
                "replaces_round": result.round,
                "base_branch": result.output_branch,
                "base_commit": head,
                "feedback": feedback,
                "feedback_sha256": hashlib.sha256(feedback.encode("utf-8")).hexdigest(),
                "approve_batch": approve_batch,
            },
        )
    
    def _decide_redo(self, stage: StageConfig, result: StageResult, decision: dict) -> None:
        """Start a round from the stage's input again; the reviewed output is not used."""
        self._require_gate_ready(what=f"A further round of stage '{stage.name}'")
        self._record_decision(
            stage=stage, result=result, decision=decision,
            disposition="ABANDON: the owner asked for a new start from the stage's input",
            next_route=f"Round {result.round + 1} of the same stage, from the stage's input",
        )
        self._reserve_round(
            stage=stage, kind=KIND_REDO, started_by=decision["decision_id"],
            reason=f"the owner asked for a new start (decision {decision['decision_id']})",
        )
    
    def _decide_raise_limit(self, stage: StageConfig, result: StageResult, decision: dict) -> None:
        """Raise the stage's round limit. Starts nothing: the round's notice is written again."""
        by = decision["params"].get("by")
        if not (isinstance(by, int) and not isinstance(by, bool) and by >= 1):
            raise ProjectStateError("A raise_limit decision needs by: a whole number, 1 or more.")
        raises = self.manifest.round_limit_raises
        self._record_decision(
            stage=stage, result=result, decision=decision,
            disposition="UNRESOLVED: the output still waits for the owner's decision",
            next_route=(
                f"The stage's limit is raised by {by} to "
                f"{self.round_limit(stage=stage) + by} round(s). The round's notice is "
                f"written again with the decisions that are now possible"
            ),
        )
        raises[stage.name] = raises.get(stage.name, 0) + by
        self._reopen_notice(result=result)
        self.checkpoint()
        print(
            f"\nThe round limit of stage '{stage.name}' is now "
            f"{self.round_limit(stage=stage)}. Nothing was launched."
        )
    
    def _decide_halt(self, stage: StageConfig, result: StageResult, decision: dict) -> None:
        self._record_decision(
            stage=stage, result=result, decision=decision,
            disposition="UNRESOLVED: the project is halted",
            next_route="Nothing, until the project is taken out of the halt with --unhalt",
        )
        self._set_halt(reason=str(decision["params"].get("reason", "")), by=decision["decision_id"])
    
    def _set_halt(self, reason: str, by: str) -> None:
        self.manifest.halted = {"at": utc_now(), "reason": reason, "by": by}
        self.checkpoint()
        print("\nThe project is halted. Nothing is launched and no decision is applied.")
    
    def halt(self, reason: str) -> None:
        """Halt the project from the command line. Nothing is launched while it is halted."""
        if self.manifest.halted:
            raise ProjectStateError("The project is already halted.")
        number = 1 + sum(event["kind"].startswith("halt-") for event in self._project_events())
        self._record_event(
            stage=None, round_number=0, kind=f"halt-{number}",
            facts={"What happened": "The project was halted with --halt", "Reason": reason},
        )
        self._set_halt(reason=reason, by="command line")
    
    def unhalt(self) -> None:
        """Take the project out of a halt. A notice that the halt answered is written again."""
        if not self.manifest.halted:
            raise ProjectStateError("The project is not halted.")
        number = 1 + sum(event["kind"].startswith("unhalt-") for event in self._project_events())
        self._record_event(
            stage=None, round_number=0, kind=f"unhalt-{number}",
            facts={
                "What happened": "The project was taken out of its halt with --unhalt",
                "The halt": self.manifest.halted,
            },
        )
        answered = {decision["event_id"] for decision in self.manifest.decisions}
        for result in self.manifest.stages.values():
            if (
                result.status == STAGE_AWAITING_REVIEW
                and result.notice["event"]["event_id"] in answered
            ):
                self._reopen_notice(result=result)
        self.manifest.halted = None
        self.checkpoint()
        print("\nThe project is no longer halted.")
    
    # ------------------------------------------------------------------
    # Adoption of existing work
    # ------------------------------------------------------------------
    
    def adopt_stage(
        self,
        stage_name: str,
        branch: str,
        commit: str,
        input_commit: str,
        content_commit: Optional[str] = None,
    ) -> None:
        """Register work that already exists on a branch as a round of a stage. Launches nothing.

        The adopted commit is checked like a candidate an agent pushed,
        against the given input commit. The round counts against the
        stage's limit, its origin is recorded as adopted, and no agent,
        run or artifact is recorded for it. Evidence that is not there is
        a failed check. The round then goes to the human gate like any
        other; adoption never accepts anything and is never followed by
        an automatic round.

        Args:
            branch: The branch that holds the work.
            commit: Its current head: the commit that is adopted.
            input_commit: The commit the stage's work started from.
            content_commit: The commit that holds the stage's content,
                when the adopted commit is a later one (for example after
                the orchestrator itself was changed on the branch). The
                stage's own files must be the same in both.

        Raises:
            ProjectStateError: if any of that does not hold. Nothing is
                recorded then.
            APIError: if GitHub cannot be read.
        """
        stage = self.stage_named(name=stage_name)
        number = self.stage_number(stage=stage)
        for label, value in (
            ("--adopt-commit", commit), ("--adopt-input-commit", input_commit),
            ("--adopt-content-commit", content_commit),
        ):
            if value is not None and not COMMIT_SHA_PATTERN.fullmatch(value):
                raise ProjectStateError(f"{label} must be a full 40-character commit SHA.")
        existing = self.manifest.stages.get(stage.name)
        if existing is not None and existing.round > 0:
            raise ProjectStateError(
                f"Stage '{stage.name}' already has a round in this project; work is "
                f"adopted only for a stage that was not started."
            )
        if number == 1:
            expected_input = self.manifest.source_commit
            origin_of_input = "the project's source commit"
        else:
            prior = self.manifest.stages.get(self.stages[number - 2].name)
            if prior is None or prior.status != STAGE_COMPLETE:
                raise ProjectStateError(
                    f"Stage '{stage.name}' cannot be adopted: the stage before it is "
                    f"not accepted."
                )
            expected_input = prior.output_commit
            origin_of_input = f"the accepted output of stage '{prior.name}'"
        if input_commit != expected_input:
            raise ProjectStateError(
                f"The input commit given ({input_commit}) is not {origin_of_input} "
                f"({expected_input})."
            )
        head = self._branch_head(branch=branch)
        if head != commit:
            raise ProjectStateError(
                f"The commit to adopt must be the current head of branch '{branch}', "
                f"which is {head}, not {commit}."
            )
        content = content_commit or commit
        for label, base, later, accepted in (
            ("the adopted commit", input_commit, commit, ("ahead",)),
            ("the content commit", input_commit, content, ("ahead",)),
            ("the adopted commit", content, commit, ("ahead", "identical")),
        ):
            relation = self._compare(base=base, head=later)
            if relation["status"] not in accepted:
                raise ProjectStateError(
                    f"Nothing is adopted: {label} {later} does not build on {base} "
                    f"(GitHub reports '{relation['status']}')."
                )
        if content != commit:
            if relation["truncated"]:
                raise ProjectStateError(
                    "Nothing is adopted: too many files differ between the content "
                    "commit and the adopted commit for GitHub to list them all."
                )
            changed = [
                path for path in relation["files"]
                if path_is_under(path, self._stage_content_paths(stage=stage))
            ]
            if changed:
                raise ProjectStateError(
                    f"Nothing is adopted: the stage's own files differ between the "
                    f"content commit {content} and the adopted commit {commit}: "
                    f"{', '.join(changed)}."
                )
        
        self._require_gate_ready(what=f"The adoption for stage '{stage.name}'")
        
        draft = StageResult(
            name=stage.name, round=1, kind=KIND_ADOPTED, origin=ORIGIN_ADOPTED,
            input_commit=input_commit, baseline_commit=input_commit, content_commit=content,
        )
        gate = self._candidate_gate(stage=stage, result=draft, branch=branch, commit=commit)
        result = self._reserve_round(
            stage=stage, kind=KIND_ADOPTED, started_by="adopt", origin=ORIGIN_ADOPTED,
            reason="existing work on a branch was registered with --adopt; no agent was launched",
            fields_to_set={
                "status": STAGE_OUTPUT_CHECKED,
                "input_source": f"adopted work; input commit given: {origin_of_input}",
                "input_commit": input_commit,
                "baseline_commit": input_commit,
                "candidate_branch": branch,
                "candidate_commit": commit,
                "content_commit": content,
                "gate": gate,
            },
        )
        self._record_event(
            stage=stage, round_number=result.round, kind="adopted",
            facts={
                "What happened": "Existing work was adopted and checked; no agent was launched",
                "Origin": "adopted (no agent, run or artifact belongs to this round)",
                "Adopted": f"commit {commit}, the head of branch '{branch}'",
                "Stage content from": f"commit {content}",
                "Input": f"commit {input_commit}",
                "Result": "checks passed" if gate["passed"] else "checks FAILED",
                "Problems": gate["problems"],
                "Open items for the owner": gate["blockers"],
                "Note": (
                    "Adoption accepts nothing. Checks of form and cross-references, "
                    "not of correctness"
                ),
            },
        )
        self.checkpoint()
        print(
            f"  Adopted commit {commit} for stage '{stage.name}': checks "
            f"{'passed' if gate['passed'] else 'FAILED'} ({len(gate['problems'])} "
            f"problem(s), {len(gate['blockers'])} open item(s)). Nothing was launched "
            f"and nothing is accepted."
        )
    
    # ------------------------------------------------------------------
    # The pipeline
    # ------------------------------------------------------------------
    
    def run_pipeline(self) -> PipelineManifest:
        """
        Move the open project forward as far as it can go without the owner.
        
        A project is opened by start_new_project() (which asks for the
        model setup document) or by resume_project(). At most one stage is
        worked on: the first one that is not accepted, and only if a round
        was started for it. A round of a later stage is started only by
        apply_decision().
        
        Returns:
            PipelineManifest with all stage results
        
        Raises:
            RuntimeError: if no project is open.
        """
        if self.manifest is None:
            raise RuntimeError(
                "No project is open. Call start_new_project() or "
                "resume_project() before run_pipeline()."
            )
        
        print(f"\n=== Pipeline: {self.manifest.pipeline_name} ===")
        print(f"Project: {self.manifest.project_id}, started {self.manifest.started_at}")
        print(f"Repository: {self.manifest.repo_url}")
        print(f"Stages: {len(self.stages)}")
        print(f"Dry run: {self.dry_run}")
        
        if self.manifest.halted:
            print(
                f"\nThe project is halted ({self.manifest.halted['reason'] or 'no reason given'}). "
                f"Nothing is done."
            )
            return self.manifest
        
        for stage in self.stages:
            result = self.manifest.stages.get(stage.name)
            if self.dry_run:
                self.manifest.stages[stage.name] = StageResult(name=stage.name)
                self._run_stage(stage)
                continue
            if result is not None and result.status == STAGE_COMPLETE:
                print(f"\n--- Stage: {stage.display_name} [accepted] ---")
                self._issue_receipt(stage=stage, result=result)
                continue
            if (
                result is None and stage is self.stages[0]
                and self.manifest.provenance.get("created_by") == "execute"
            ):
                self._reserve_round(
                    stage=stage, kind=KIND_INITIAL, started_by="execute",
                    reason="the project was started with --execute",
                )
                result = self.manifest.stages[stage.name]
            if result is None or result.round == 0:
                print(
                    f"\n--- Stage: {stage.display_name} [not started: starts only on "
                    f"a start_stage decision of the owner] ---"
                )
                break
            self._run_stage(stage)
            break
        
        if all(
            stage.name in self.manifest.stages
            and self.manifest.stages[stage.name].status == STAGE_COMPLETE
            for stage in self.stages
        ):
            self.manifest.completed_at = self.manifest.completed_at or utc_now()
            final = self.manifest.stages[self.stages[-1].name]
            print(
                f"\nEvery stage is accepted. The accepted final output is commit "
                f"{final.output_commit} on branch '{final.output_branch}'. Nothing "
                f"was merged into any other branch."
            )
        self.checkpoint()
        return self.manifest
    
    @property
    def artifacts_root(self) -> Path:
        """Folder for this project's downloaded artifacts, beside its manifest."""
        return (
            self.manifest_path.parent
            / self.artifacts_config.get("local_output_dir", "collected_artifacts")
            / self.manifest_path.stem
        )
    
    def checkpoint(self) -> None:
        """Write the project manifest to its file now.

        Does nothing in a dry run: a dry run saves nothing.

        Raises:
            RuntimeError: if the project has no manifest file.
        """
        if self.manifest is None or self.dry_run:
            return
        if self.manifest_path is None:
            raise RuntimeError("The project has no manifest file. Set manifest_path.")
        self.save_manifest(self.manifest_path)
    
    def save_manifest(self, path: Path) -> None:
        """Save the manifest to a JSON file, replacing the file in one step."""
        if self.manifest is None:
            return
        temporary_path = path.with_name(path.name + ".tmp")
        with open(temporary_path, "w", encoding="utf-8") as f:
            json.dump(self.manifest.to_dict(), f, indent=2)
        os.replace(temporary_path, path)
    

STOP_EXPLANATIONS = {
    STOP_PASSED: "the round passed every required check and waits for the owner's review",
    STOP_REVIEW_REQUIRED: "the round did not pass, and a further round needs the owner's decision",
    STOP_CAP_EXHAUSTED: "the stage has used all the rounds it is allowed",
    STOP_NO_ALTERNATIVE: "the round did not pass and no other route was seen",
    STOP_OWNER_DECISION: "items are open that only the project owner can decide",
    STOP_DISAGREEMENT: "the reviewer did not agree with the candidate",
    STOP_INFRASTRUCTURE: "a launch failed; no launch is repeated without the owner",
    STOP_ADOPTED: "existing work was adopted; adoption accepts nothing",
}


def load_config(config_path: Path) -> dict:
    """Load configuration from YAML file."""
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_manifest(manifest_path: Path) -> PipelineManifest:
    """Load a manifest from JSON file."""
    with open(manifest_path, encoding="utf-8") as f:
        return PipelineManifest.from_dict(json.load(f))


NEXT_STEP_BY_STATUS = {
    STAGE_AWAITING_REVIEW: (
        "The notice is posted. Nothing happens until the owner's signed decision is "
        "applied:\n            --resume {manifest} --apply-decision <decision file>"
    ),
    STAGE_NOTICE_PENDING: (
        "The signed notice is in the outbox; the host has not acknowledged posting it "
        "in the main chat.\n            --resume {manifest} looks for the ack again. "
        "Nothing is launched, and the output is not announced."
    ),
    STAGE_SYNC_PENDING: (
        "The output is on GitHub but not in the local checkout, so no notice is "
        "written.\n            Fix the cause given above; --resume {manifest} then "
        "tries the local checkout again.\n            No new agent is launched."
    ),
    STAGE_BLOCKED: (
        "No agent was launched. Fix the cause given above; --resume {manifest} then "
        "launches this round."
    ),
    STAGE_LAUNCHING: (
        "It is not known whether the agent was created. --resume {manifest} first "
        "checks for the recorded agent and never creates a second one."
    ),
    STAGE_RUNNING: (
        "--resume {manifest} keeps waiting for the same run. No new agent is launched."
    ),
    STAGE_RUN_FINISHED: (
        "--resume {manifest} checks the output again. No new agent is launched."
    ),
    STAGE_ARTIFACTS_FAILED: (
        "--resume {manifest} tries the download again. No new agent is launched."
    ),
    STAGE_COMPLETE: (
        "Accepted. The next stage starts only on the owner's start_stage decision:\n"
        "            --resume {manifest} --apply-decision <decision file>"
    ),
}

EXIT_COMPLETE = 0
EXIT_FAILED = 1
EXIT_WAITING_FOR_OWNER = 2
EXIT_NOTICE_PENDING = 3


def build_local_review(config: dict, config_path: Path) -> LocalReviewCheckout:
    """Return the local checkout named by the config's `review` section.

    `review.local_checkout` is taken relative to the config file's folder.
    The orchestrator's own files, where they are inside that checkout, are
    registered as the running code: a commit that holds another version of
    them is not checked out.
    """
    checkout = (config_path.resolve().parent / config["review"]["local_checkout"]).resolve()
    code_folder = Path(__file__).resolve().parent
    own_files = [code_folder / name for name in ORCHESTRATOR_FILES] + [config_path.resolve()]
    return LocalReviewCheckout(
        path=checkout,
        repo_url=config["pipeline"]["repository"]["url"],
        remote=config["review"]["remote"],
        running_code_paths=[
            path.relative_to(checkout).as_posix()
            for path in own_files if path.is_relative_to(checkout)
        ],
    )


def main(
    argv: Optional[list[str]] = None,
    api_client: Optional[CursorAPIClient] = None,
    ask: Callable[[str], str] = input,
    github: Optional[GitHubRepoReader] = None,
    sleep: Callable[[float], None] = time.sleep,
    local_review: Optional[LocalReviewCheckout] = None,
) -> int:
    """Run the command line interface and return the process exit code.

    Exit codes: 0 when every stage is accepted (or a check passed), 2 when
    the project waits for a decision of the owner, 3 when a notice waits
    for the host's ack, 1 otherwise.

    Args:
        argv: Command line arguments (default: those of the process).
        api_client: API client to use (default: one built from CURSOR_API_KEY).
        ask: Called with a question, returns the user's answer (default: input).
        github: GitHub reader to use (default: one built from the config).
        sleep: Called to wait between polls and retries (default: time.sleep).
        local_review: Local checkout that shows stage outputs for review
            (default: the one named by the config's `review` section).
    """
    parser = argparse.ArgumentParser(
        description="Research Pipeline Orchestrator for Cursor Cloud Agents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python orchestrator.py --dry-run              # Offline check, no network
  python orchestrator.py --check-online         # API key, models, GitHub; launches nothing
  python orchestrator.py --execute              # Start a new project
  python orchestrator.py --resume manifest.json # Continue an existing project
  python orchestrator.py --resume manifest.json --apply-decision decision.json
  python orchestrator.py --adopt STAGE --adopt-branch BRANCH --adopt-commit SHA \\
                         --adopt-input-commit SHA [--adopt-content-commit SHA]

Rounds and the human gate:
  A stage is worked on in rounds, up to iteration.max_rounds. A round ends at
  the human gate: its output is checked out in the local Git checkout, a
  signed notice is written to the outbox, and the host of the main chat
  acknowledges posting it. Only then is the output awaiting review. Nothing
  further happens until the owner's signed decision for that notice is
  applied with --apply-decision: accept, iterate, redo, raise_limit, halt, and
  after an acceptance start_stage. Accepting launches nothing.

Model setup / equilibrium definition document:
  --execute, --adopt (new project) and --dry-run always ask for the location
  of this document (.qmd, .tex or .md). --resume uses the recorded one.

Environment:
  CURSOR_API_KEY    API key from Cursor Dashboard -> API Keys
  GITHUB_TOKEN      Only for a private repository: token that can read it
  The variable named in review.delivery.secret_env: the secret shared with
  the host of the main chat, used to sign notices and verify acks and decisions

Official API docs: https://cursor.com/docs/cloud-agent/api/endpoints
        """,
    )
    
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent / "config.yaml",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Offline: check the config, ask for the model setup document and "
             "print what each stage would do. No network request, no API key, "
             "no agent, nothing saved",
    )
    mode.add_argument(
        "--check-online",
        action="store_true",
        help="Check the API key, the configured models (GET /v1/models) and "
             "the starting ref on GitHub. Uses the network; launches nothing",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Start a new project: ask for the model setup document, then "
             "run the first round of the first stage (launches paid cloud agents)",
    )
    mode.add_argument(
        "--resume",
        type=Path,
        metavar="MANIFEST",
        help="Continue the project recorded in a manifest JSON file. Never "
             "launches a second agent for a launch that already has one",
    )
    
    parser.add_argument(
        "--output",
        type=Path,
        help="Manifest file for a new project (default: pipeline_manifest.json). "
             "Not allowed with --resume",
    )
    parser.add_argument(
        "--apply-decision",
        type=Path,
        metavar="FILE",
        help="With --resume: apply a signed decision file of the project owner "
             "(accept, start_stage, iterate, redo, raise_limit or halt)",
    )
    parser.add_argument(
        "--adopt",
        metavar="STAGE",
        help="Register work that already exists on a branch as a round of STAGE. "
             "Launches nothing and accepts nothing. Alone it starts a new project "
             "(first stage); with --resume it applies to the next stage",
    )
    parser.add_argument("--adopt-branch", metavar="BRANCH", help="With --adopt: the branch that holds the work")
    parser.add_argument("--adopt-commit", metavar="SHA", help="With --adopt: the commit to adopt, the branch's current head")
    parser.add_argument("--adopt-input-commit", metavar="SHA", help="With --adopt: the commit the work started from")
    parser.add_argument(
        "--adopt-content-commit",
        metavar="SHA",
        help="With --adopt: the commit that holds the stage's content, when the "
             "adopted commit is a later one with the same stage files",
    )
    parser.add_argument(
        "--halt",
        metavar="REASON",
        help="With --resume: halt the project. Nothing is launched and no decision "
             "is applied until --unhalt",
    )
    parser.add_argument("--unhalt", action="store_true", help="With --resume: end a halt")
    parser.add_argument(
        "--repin",
        action="store_true",
        help="With --resume: record a changed configuration for the project. Open "
             "notices are written again and must be answered again",
    )
    
    args = parser.parse_args(argv)
    
    usage_error = None
    modes = [args.dry_run, args.check_online, args.execute, bool(args.resume)]
    adopt_details = [
        flag for flag, value in (
            ("--adopt-branch", args.adopt_branch),
            ("--adopt-commit", args.adopt_commit),
            ("--adopt-input-commit", args.adopt_input_commit),
            ("--adopt-content-commit", args.adopt_content_commit),
        ) if value
    ]
    resume_only = [
        flag for flag, value in (
            ("--apply-decision", args.apply_decision),
            ("--halt", args.halt is not None),
            ("--unhalt", args.unhalt),
            ("--repin", args.repin),
        ) if value
    ]
    actions = resume_only[:3] + (["--adopt"] if args.adopt else [])
    if not any(modes) and not args.adopt:
        usage_error = "one of --dry-run, --check-online, --execute, --resume or --adopt is required"
    elif args.adopt and (args.dry_run or args.check_online or args.execute):
        usage_error = "--adopt can be combined only with --resume"
    elif adopt_details and not args.adopt:
        usage_error = f"{', '.join(adopt_details)} can be used only with --adopt"
    elif args.adopt and not (args.adopt_branch and args.adopt_commit and args.adopt_input_commit):
        usage_error = "--adopt needs --adopt-branch, --adopt-commit and --adopt-input-commit"
    elif resume_only and not args.resume:
        usage_error = f"{', '.join(resume_only)} can be used only with --resume"
    elif len([action for action in actions if action != "--repin"]) > 1:
        usage_error = f"{' and '.join(actions)} cannot be used together"
    elif args.resume and args.output:
        usage_error = (
            "--output cannot be used with --resume: a resumed project keeps "
            "writing to the manifest it was resumed from"
        )
    if usage_error:
        print(f"Error: {usage_error}")
        return EXIT_FAILED
    
    if not args.config.exists():
        print(f"Error: Config file not found: {args.config}")
        return EXIT_FAILED
    
    print(f"Loading config: {args.config}")
    config = load_config(args.config)
    
    orchestrator = Orchestrator(
        config=config,
        api_client=api_client,
        github=github,
        dry_run=args.dry_run,
        ask=ask,
        sleep=sleep,
    )
    
    if not orchestrator.run_config_validation():
        print("\nValidation FAILED. No agent was launched.")
        return EXIT_FAILED
    
    manifest_path = args.resume or args.output or Path(
        config.get("manifest", {}).get("output_file", "pipeline_manifest.json")
    )
    # A dry run runs no Git command and writes no file, so it gets no
    # local checkout and no manifest file.
    if not args.dry_run:
        orchestrator.local_review = local_review or build_local_review(
            config=config, config_path=args.config,
        )
        orchestrator.manifest_path = manifest_path
    
    if args.check_online:
        online_valid = orchestrator.run_online_validation()
        print("\nChecking the local checkout for review (changes nothing)...")
        try:
            orchestrator.local_review.check_ready()
        except LocalSyncError as error:
            online_valid = False
            print(f"  ERROR: {error}")
        else:
            print(f"  {orchestrator.local_review.path} can receive stage outputs")
        print("\nChecking the secret for signing review notices (writes nothing)...")
        try:
            orchestrator.bridge.check_secret()
        except BridgeError as error:
            online_valid = False
            print(f"  ERROR: {error}")
        else:
            print(
                "  The secret is set. Whether a host reads the outbox and posts in "
                "the main chat is NOT checked: that host is outside this program."
            )
        if not online_valid:
            print("\nOnline validation FAILED. No agent was launched.")
            return EXIT_FAILED
        print("\nOnline validation PASSED. No agent was launched.")
        return EXIT_COMPLETE
    
    new_project = bool(args.execute or (args.adopt and not args.resume))
    try:
        if args.resume:
            if not args.resume.exists():
                print(f"Error: Manifest file not found: {args.resume}")
                return EXIT_FAILED
            orchestrator.resume_project(
                manifest=load_manifest(args.resume), repin=args.repin,
            )
        elif new_project:
            if manifest_path.exists():
                print(
                    f"\nERROR: A project manifest already exists at {manifest_path}. "
                    f"A new project would overwrite that project's record.\n"
                    f"Use --resume {manifest_path} to continue that project, or "
                    f"--output <other file> to start a new project beside it."
                )
                print("No agent was launched.")
                return EXIT_FAILED
            orchestrator.start_new_project(
                created_by="adopt" if args.adopt else "execute",
                source_ref=args.adopt_input_commit if args.adopt else None,
            )
        else:
            orchestrator.start_new_project()
    except (ModelSetupDocumentError, ProjectStateError, APIError, TrialLogError) as error:
        print(f"\nERROR: {error}")
        print("No agent was launched.")
        return EXIT_FAILED
    
    if args.dry_run:
        manifest = orchestrator.run_pipeline()
        ready = all(
            stage.name in manifest.stages
            and manifest.stages[stage.name].status == STAGE_DRY_RUN
            for stage in orchestrator.stages
        )
        print(
            "\n[DRY RUN] Offline check only: no network request was made, no agent "
            "was launched, no Git command was run and nothing was saved.\nNo stage "
            "was carried out: nothing was derived, written, computed or checked "
            "out. The dry run shows only that the config is valid, the document "
            "can be read, and each stage's prompts can be built.\nNot checked: the "
            "API key, the models, GitHub access, the local checkout and the "
            "review notices (use --check-online). --execute starts a project and "
            "asks for the document again."
        )
        return EXIT_COMPLETE if ready else EXIT_FAILED
    
    # A command that can launch nothing needs no API key and no model check.
    launches_nothing = bool(args.adopt or args.halt is not None)
    if not orchestrator.run_online_validation(check_models=not launches_nothing):
        print("\nValidation FAILED. No agent was launched.")
        return EXIT_FAILED
    
    print("\nValidation PASSED")
    
    try:
        if args.adopt:
            # Nothing is saved for a new project unless the adoption is recorded.
            orchestrator.adopt_stage(
                stage_name=args.adopt,
                branch=args.adopt_branch,
                commit=args.adopt_commit,
                input_commit=args.adopt_input_commit,
                content_commit=args.adopt_content_commit,
            )
        else:
            # The project's record (the selected document and the source
            # commit) is on disk before any agent is launched.
            orchestrator.checkpoint()
        if args.apply_decision:
            orchestrator.apply_decision(path=args.apply_decision)
        if args.halt is not None:
            orchestrator.halt(reason=args.halt)
        if args.unhalt:
            orchestrator.unhalt()
        manifest = orchestrator.run_pipeline()
    except (ProjectStateError, APIError, BridgeError, TrialLogError) as error:
        print(f"\nERROR: {error}")
        print("Nothing further was done.")
        return EXIT_FAILED
    print(f"\nManifest saved to: {manifest_path}")
    
    print("\n=== Pipeline Summary ===")
    not_started = "NOT STARTED"
    statuses = [
        manifest.stages[stage.name].status
        if stage.name in manifest.stages and manifest.stages[stage.name].round > 0
        else not_started
        for stage in orchestrator.stages
    ]
    for position, (stage, status) in enumerate(zip(orchestrator.stages, statuses)):
        result = manifest.stages.get(stage.name)
        started = status != not_started
        print(
            f"  {stage.name}: {status}"
            + (
                f" (round {result.round} of at most {orchestrator.round_limit(stage=stage)}"
                + (f", {result.outcome}" if result.outcome else "") + ")"
                if started else ""
            )
        )
        if started and result.error:
            print(f"      {result.error}")
        # For an accepted stage the next step is named only where the project stands.
        waits_here = status != STAGE_COMPLETE or statuses[position + 1:position + 2] == [not_started]
        if status in NEXT_STEP_BY_STATUS and waits_here:
            print("      Next: " + NEXT_STEP_BY_STATUS[status].format(manifest=manifest_path))
    if manifest.halted:
        print(f"  The project is halted: {manifest.halted['reason'] or 'no reason given'}")
    
    if all(status == STAGE_COMPLETE for status in statuses):
        return EXIT_COMPLETE
    if STAGE_NOTICE_PENDING in statuses:
        return EXIT_NOTICE_PENDING
    waits_for_start = STAGE_COMPLETE in statuses and all(
        status in (STAGE_COMPLETE, not_started) for status in statuses
    )
    if manifest.halted or STAGE_AWAITING_REVIEW in statuses or waits_for_start:
        return EXIT_WAITING_FOR_OWNER
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
