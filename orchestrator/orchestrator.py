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
    manual_review: bool = False


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
) -> str:
    """Return the prompt text sent to the cloud agent for a stage.

    The model setup document (for a stage that receives it) and the stage's
    guardrails are appended to the stage prompt, so the agent receives them
    in the same launch request as the task. The document's local path is
    left out: the cloud agent cannot open it.

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
    
    if stage.guardrails:
        numbered = "\n".join(
            f"{number}. {guardrail}"
            for number, guardrail in enumerate(stage.guardrails, start=1)
        )
        sections.append(f"{GUARDRAILS_HEADING}\n{numbered}")
    
    if len(sections) == 1:
        return stage.prompt
    return "\n\n".join(sections) + "\n"


STAGE_PENDING = "PENDING"
STAGE_BLOCKED = "BLOCKED"
STAGE_DRY_RUN = "DRY_RUN"
STAGE_LAUNCHING = "LAUNCHING"
STAGE_LAUNCH_REJECTED = "LAUNCH_REJECTED"
STAGE_RUNNING = "RUNNING"
STAGE_RUN_FAILED = "RUN_FAILED"
STAGE_RUN_FINISHED = "RUN_FINISHED"
STAGE_GATE_FAILED = "GATE_FAILED"
STAGE_OUTPUT_VERIFIED = "OUTPUT_VERIFIED"
STAGE_ARTIFACTS_FAILED = "ARTIFACTS_FAILED"
STAGE_AWAITING_REVIEW = "AWAITING_REVIEW"
STAGE_COMPLETE = "COMPLETE"

# Only a stage in one of these states can lead to a launch request. No agent
# exists for it yet, or (LAUNCHING) it is not known whether one does.
LAUNCHABLE_STATUSES = (
    STAGE_PENDING, STAGE_BLOCKED, STAGE_DRY_RUN, STAGE_LAUNCH_REJECTED, STAGE_LAUNCHING,
)

# An agent ran for a stage in one of these states, and its output is not
# (or not yet) accepted: the run failed, the output check failed, or the
# output waits for a review that may reject it. Launching again is a new
# paid run, so it happens only with --rerun-stage.
RERUNNABLE_STATUSES = (STAGE_RUN_FAILED, STAGE_GATE_FAILED, STAGE_AWAITING_REVIEW)

MANIFEST_FORMAT_VERSION = 2
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


@dataclass
class StageResult:
    """The recorded state of one stage of a project."""
    name: str
    status: str = STAGE_PENDING
    # 1 for the first launch; raised by each explicit --rerun-stage.
    attempt: int = 1
    agent_id: Optional[str] = None
    run_id: Optional[str] = None
    agent_url: Optional[str] = None
    run_status: Optional[str] = None
    # What the stage started from, and the exact commit sent as startingRef.
    input_source: Optional[str] = None
    input_commit: Optional[str] = None
    # The branch the agent pushed, and the commit that passed the output
    # check. Later stages start from this commit, not from the branch.
    output_branch: Optional[str] = None
    output_commit: Optional[str] = None
    gate: Optional[dict] = None
    artifacts: Optional[dict] = None
    review: Optional[dict] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass
class PipelineManifest:
    """Manifest tracking pipeline execution state."""
    pipeline_name: str
    repo_url: str
    started_at: str
    stages: dict[str, StageResult] = field(default_factory=dict)
    completed_at: Optional[str] = None
    # The document selected when this project was started: path, format,
    # size_bytes, sha256 and selected_at. The document's text is not stored.
    model_setup_document: Optional[dict] = None
    # The configured starting ref, and the commit it pointed at when the
    # project was started. The first stage starts from that commit.
    source_ref: Optional[str] = None
    source_commit: Optional[str] = None
    # Records of stage attempts replaced by an explicit --rerun-stage.
    previous_attempts: list[dict] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {
            "format_version": MANIFEST_FORMAT_VERSION,
            "pipeline_name": self.pipeline_name,
            "repo_url": self.repo_url,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "model_setup_document": self.model_setup_document,
            "source_ref": self.source_ref,
            "source_commit": self.source_commit,
            "stages": {name: asdict(result) for name, result in self.stages.items()},
            "previous_attempts": self.previous_attempts,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "PipelineManifest":
        """
        Raises:
            ProjectStateError: if the manifest has another format version
                or a stage record with unknown fields.
        """
        if data.get("format_version") != MANIFEST_FORMAT_VERSION:
            raise ProjectStateError(
                f"The manifest has format version {data.get('format_version')!r}, "
                f"but this orchestrator reads version {MANIFEST_FORMAT_VERSION}. "
                f"It cannot be resumed with this version of the orchestrator."
            )
        manifest = cls(
            pipeline_name=data["pipeline_name"],
            repo_url=data["repo_url"],
            started_at=data["started_at"],
            completed_at=data.get("completed_at"),
            model_setup_document=data.get("model_setup_document"),
            source_ref=data.get("source_ref"),
            source_commit=data.get("source_commit"),
            previous_attempts=list(data.get("previous_attempts", [])),
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


class Orchestrator:
    """
    Pipeline orchestrator that launches cloud agents for each stage.
    
    Key behaviors:
    - Asks for the model setup document whenever a new project is started
    - Validates models against GET /v1/models before launch
    - Runs the stages in order; each starts from the exact commit that
      passed the previous stage's output check
    - Creates each stage's agent at most once, under a recorded agent ID
    - Checks required output files, collects artifacts, pauses for review
    - Records every step in the manifest, so a project can be resumed
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
        
        self.pipeline_config = config.get("pipeline", {})
        self.repo_config = self.pipeline_config.get("repository", {})
        self.api_config = config.get("api", {})
        self.stages = self._parse_stages(config.get("stages", []))
        self.execution_config = config.get("execution", {})
        self.artifacts_config = config.get("artifacts", {})
        
        self.manifest: Optional[PipelineManifest] = None
        self.model_setup_document: Optional[ModelSetupDocument] = None
    
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
                manual_review=stage_data.get("manual_review", False),
            ))
        return stages
    
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
        """Check a stage's review and required-file settings."""
        prefix = f"Stage '{stage.name}'"
        errors = []
        
        if not isinstance(stage.manual_review, bool):
            errors.append(f"{prefix}: manual_review must be true or false")
        
        required_files = (
            stage.output_manifest.get("required_files", [])
            if isinstance(stage.output_manifest, dict) else None
        )
        if not isinstance(required_files, list):
            return errors + [f"{prefix}: output_manifest.required_files must be a list of paths"]
        for path in required_files:
            reason = unsafe_relative_path_reason(path)
            if reason:
                errors.append(
                    f"{prefix}: required file {path!r} is not a safe path relative "
                    f"to the repository root: {reason}"
                )
        return errors
    
    def _pipeline_setting_errors(self) -> list[str]:
        """Check the repository, retry and artifact settings."""
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
        return errors
    
    def _model_selection_errors(self, stage: StageConfig) -> list[str]:
        """Check that a stage's model settings can be written as the API's `model` field.

        The API takes `model.id` (a string) and `model.params` (a list of
        `{id, value}` items). Whether the API lists these IDs and values is
        checked separately, by validate_models().
        """
        prefix = f"Stage '{stage.name}'"
        errors = []
        
        if stage.model_id is not None and not (
            isinstance(stage.model_id, str) and stage.model_id.strip()
        ):
            errors.append(f"{prefix}: model_id must be a non-empty string or null")
        
        params = stage.model_params
        if not isinstance(params, list):
            return errors + [f"{prefix}: model_params must be a list of {{id, value}} items"]
        
        if params and stage.model_id is None:
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
    
    def start_new_project(self) -> PipelineManifest:
        """Start a new project: ask for its document, then open its manifest.

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
            model_setup_document={**document.to_record(), "selected_at": started_at},
            source_ref=self.repo_config.get("starting_ref", "main"),
        )
        return self.manifest
    
    def _is_stage_finished(self, manifest: PipelineManifest, stage: StageConfig) -> bool:
        existing = manifest.stages.get(stage.name)
        return existing is not None and existing.status == STAGE_COMPLETE
    
    def _may_still_launch(self, manifest: PipelineManifest, stage: StageConfig) -> bool:
        existing = manifest.stages.get(stage.name)
        return existing is None or existing.status in LAUNCHABLE_STATUSES
    
    def _reset_stage_for_rerun(self, manifest: PipelineManifest, stage_name: str) -> None:
        """Set a stage back to PENDING, keeping the replaced attempt's record.

        Raises:
            ProjectStateError: unless the stage's run failed, its output
                check failed, or its output is awaiting review.
        """
        result = manifest.stages.get(stage_name)
        if result is None or result.status not in RERUNNABLE_STATUSES:
            state = "it has not been run" if result is None else f"its status is {result.status}"
            raise ProjectStateError(
                f"Stage '{stage_name}' cannot be rerun: {state}. --rerun-stage "
                f"applies only to a stage whose run failed, whose output check "
                f"failed, or whose output is awaiting review "
                f"({', '.join(RERUNNABLE_STATUSES)})."
            )
        manifest.previous_attempts.append(asdict(result))
        manifest.stages[stage_name] = StageResult(name=stage_name, attempt=result.attempt + 1)
        print(
            f"\nStage '{stage_name}': attempt {result.attempt} is kept under "
            f"previous_attempts. A NEW paid agent will be launched as attempt "
            f"{result.attempt + 1}."
        )
    
    def resume_project(
        self,
        manifest: PipelineManifest,
        rerun_stage: Optional[str] = None,
    ) -> PipelineManifest:
        """Continue an existing project from its manifest, without asking.

        The document is the one recorded when the project was started. It
        is re-read only if a stage that receives it may still be launched,
        and it must then have the recorded content.

        Args:
            manifest: The project's manifest.
            rerun_stage: Name of a stage to launch again (a new paid run):
                one that failed, or one awaiting review whose output is
                rejected. The change is made in memory; checkpoint() saves it.

        Raises:
            ProjectStateError: if the manifest belongs to another repository
                or pipeline layout, or rerun_stage cannot be rerun.
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
        
        if rerun_stage is not None:
            self._reset_stage_for_rerun(manifest=manifest, stage_name=rerun_stage)
        
        document_still_needed = any(
            stage.receives_model_setup_document
            and self._may_still_launch(manifest=manifest, stage=stage)
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
    
    def validate_models(self) -> tuple[bool, list[str]]:
        """
        Validate all configured models against GET /v1/models.
        
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
            
            for stage in self.stages:
                if stage.model_id is None:
                    print(f"  Stage '{stage.name}': using configured default model")
                    continue
                
                is_valid, model = self.api.validate_model(stage.model_id)
                if not is_valid:
                    errors.append(
                        f"Stage '{stage.name}': model '{stage.model_id}' is not "
                        f"listed by GET /v1/models. Listed model IDs and "
                        f"aliases: {sorted(available_ids)}"
                    )
                    continue
                
                alias_note = "" if model.id == stage.model_id else f" (alias of '{model.id}')"
                print(f"  Stage '{stage.name}': model '{stage.model_id}' OK{alias_note}")
                
                if stage.model_params:
                    params_valid, param_errors = self.api.validate_model_params(
                        stage.model_id,
                        stage.model_params,
                    )
                    if not params_valid:
                        errors.extend(
                            f"Stage '{stage.name}': {e}" for e in param_errors
                        )
                        continue
                    
                    permitted = {
                        definition["id"]: [v["value"] for v in definition.get("values", [])]
                        for definition in model.parameters
                    }
                    for param in stage.model_params:
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
    
    def run_online_validation(self) -> bool:
        """
        Run the checks that need the network. Launches nothing.
        
        Checks the API key, the configured models (GET /v1/models) and that
        the project's starting point can be read on GitHub.
        
        Returns:
            True if all checks pass
        """
        print("\n=== Online Validation (uses the network, launches nothing) ===\n")
        all_valid = True
        
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
        
        print("\nChecking the starting point on GitHub...")
        source_valid, source_errors = self.validate_source()
        if not source_valid:
            all_valid = False
            for e in source_errors:
                print(f"  ERROR: {e}")
        
        return all_valid
    
    def validate_source(self) -> tuple[bool, list[str]]:
        """
        Check that the starting ref can be read on GitHub.
        
        For an open project the commit found is recorded as the project's
        source commit, so the first stage starts from exactly that commit.
        
        Returns:
            Tuple of (is_readable, list_of_errors)
        """
        if self.manifest is not None and self.manifest.source_commit:
            print(f"  Source commit recorded for this project: {self.manifest.source_commit}")
            return True, []
        
        ref = (
            self.manifest.source_ref
            if self.manifest is not None
            else self.repo_config.get("starting_ref", "main")
        )
        try:
            commit = self._resolve_ref(ref=ref)
        except GitHubAPIError as error:
            return False, [
                f"Could not read '{ref}' in {self.github.full_name} on GitHub: {error}. "
                f"A private repository needs a token in the GITHUB_TOKEN "
                f"environment variable."
            ]
        print(f"  '{ref}' in {self.github.full_name} is at commit {commit}")
        if self.manifest is not None:
            self.manifest.source_commit = commit
        return True, []
    
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
    
    def _resolve_input(self, stage: StageConfig) -> tuple[str, str]:
        """Return (description, commit SHA) of the snapshot a stage starts from.

        A stage with a dependency starts from the commit that passed the
        earlier stage's output check. The first stage starts from the
        project's source commit, which is read once and then kept.
        """
        if stage.depends_on:
            prior = self.manifest.stages[stage.depends_on]
            return f"output of stage '{stage.depends_on}'", prior.output_commit
        
        if not self.manifest.source_commit:
            self.manifest.source_commit = self._resolve_ref(ref=self.manifest.source_ref)
            self.checkpoint()
        return f"'{self.manifest.source_ref}'", self.manifest.source_commit
    
    def _run_stage(self, stage: StageConfig) -> StageResult:
        """Move a stage forward from its recorded state as far as it can go.

        The steps are: launch, wait for the run, check the output, collect
        artifacts, manual review. Each step is recorded in the manifest
        before the next one starts, so a later run continues from the
        recorded step and never launches a second agent for the same step.
        """
        result = self.manifest.stages.get(stage.name)
        if result is None:
            result = StageResult(name=stage.name)
            self.manifest.stages[stage.name] = result
        
        print(f"\n--- Stage: {stage.display_name} ---")
        print(f"  Recorded status: {result.status} (attempt {result.attempt})")
        
        self._advance_stage(stage=stage, result=result)
        
        if result.status == STAGE_AWAITING_REVIEW:
            print("  PAUSED for manual review. No later stage is launched until it is approved.")
            print(f"  Output to review: {self._commit_url(commit=result.output_commit)}")
        elif result.status == STAGE_COMPLETE:
            print("  Stage complete")
        elif result.status != STAGE_DRY_RUN:
            print(f"  Stopped with status {result.status}: {result.error}")
        return result
    
    def _advance_stage(self, stage: StageConfig, result: StageResult) -> None:
        """Take each step the stage's recorded status allows, until one stops it."""
        if result.status in LAUNCHABLE_STATUSES:
            if not self._launch(stage=stage, result=result):
                return
        
        reported_branches = None
        if result.status == STAGE_RUNNING:
            reported_branches = self._wait_for_run(result=result)
            if reported_branches is None:
                return
        
        if result.status in (STAGE_RUN_FINISHED, STAGE_GATE_FAILED):
            if not self._check_output(
                stage=stage, result=result, reported_branches=reported_branches,
            ):
                return
        
        if result.status in (STAGE_OUTPUT_VERIFIED, STAGE_ARTIFACTS_FAILED):
            if not self._collect_artifacts(stage=stage, result=result):
                return
            if stage.manual_review:
                result.status = STAGE_AWAITING_REVIEW
                result.review = {
                    "requested_at": utc_now(),
                    "approved_at": None,
                    "approved_commit": None,
                }
            else:
                result.status = STAGE_COMPLETE
                result.completed_at = utc_now()
            self.checkpoint()
    
    def _commit_url(self, commit: str) -> str:
        return f"https://github.com/{self.github.full_name}/tree/{commit}"
    
    def _launch(self, stage: StageConfig, result: StageResult) -> bool:
        """Launch the stage's agent. Returns True once an agent is known to exist."""
        print(f"  Model: {stage.model_id or '(default)'}")
        print(f"  Guardrails appended to prompt: {len(stage.guardrails)}")
        
        # The prompt is built before anything else, so a stage that receives
        # the model setup document cannot be launched (or reported as ready
        # by a dry run) when no document has been selected.
        try:
            prompt_text = build_stage_prompt(
                stage=stage,
                model_setup_document=self.model_setup_document,
            )
        except ModelSetupDocumentError as error:
            result.status = STAGE_BLOCKED
            result.error = str(error)
            return False
        
        if stage.receives_model_setup_document:
            print(
                f"  Model setup document in prompt: "
                f"{self.model_setup_document.file_name} "
                f"(SHA-256 {self.model_setup_document.sha256})"
            )
        
        if self.dry_run:
            source = (
                f"the output of stage '{stage.depends_on}'"
                if stage.depends_on
                else f"'{self.manifest.source_ref}'"
            )
            required = stage.output_manifest.get("required_files", [])
            print(f"  [DRY RUN] Would launch a cloud agent from {source}")
            print(f"  [DRY RUN] Would then check {len(required)} required output file(s)")
            if stage.manual_review:
                print("  [DRY RUN] Would then pause for manual review")
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
            try:
                result.input_source, result.input_commit = self._resolve_input(stage=stage)
            except GitHubAPIError as error:
                result.error = f"Could not read the starting commit on GitHub: {error}"
                self.checkpoint()
                return False
            result.agent_id = f"bc-{uuid.uuid4()}"
            result.status = STAGE_LAUNCHING
            result.error = None
            # The agent ID is on disk before the launch request is sent, so
            # an interrupted launch can be picked up under the same ID.
            self.checkpoint()
        
        print(f"  Starting from: {result.input_source}, commit {result.input_commit}")
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
        """Create the stage's agent at most once, under the agent ID recorded for it.

        A launch request starts a paid agent, so it is never simply sent
        again. When the outcome of a launch request is unknown (no response,
        429 or 5xx), the next try first asks the API whether the agent with
        the recorded ID exists. Only if it does not is the request sent
        again, with the same ID, which the API answers with
        409 agent_id_conflict instead of creating a second agent.

        Returns:
            The agent's run, or None. On None the result holds either
            LAUNCH_REJECTED (the API refused; no agent exists) or stays
            LAUNCHING (outcome unknown; a later resume checks again).
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
                    starting_ref=result.input_commit,
                    model_id=stage.model_id,
                    model_params=stage.model_params if stage.model_params else None,
                    name=f"Pipeline: {stage.display_name}",
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
                result.status = STAGE_LAUNCH_REJECTED
                result.error = f"The API refused the launch request; no agent was created: {error}"
                return None
        
        result.error = (
            f"The outcome of the launch is unknown after {self.retry_max_attempts} "
            f"tries: {last_error}. Agent ID {result.agent_id} is recorded; a resume "
            f"first checks whether that agent exists."
        )
        return None
    
    def _wait_for_run(self, result: StageResult) -> Optional[list[dict]]:
        """Poll the stage's run until it ends.

        Returns:
            The branches the run reported, if it finished successfully.
            None otherwise: the stage is then RUN_FAILED, or still RUNNING
            when the status could not be read or the polls ran out.
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
                return None
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
            return None
        
        result.run_status = run.status
        result.duration_ms = run.duration_ms
        if not RunStatus.is_success(run.status):
            result.status = STAGE_RUN_FAILED
            result.error = f"The run ended with status {run.status}: {run.result_text}"
            self.checkpoint()
            return None
        
        result.status = STAGE_RUN_FINISHED
        result.error = None
        self.checkpoint()
        return run.branches
    
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
    
    def _check_output(
        self,
        stage: StageConfig,
        result: StageResult,
        reported_branches: Optional[list[dict]],
    ) -> bool:
        """Check a finished stage's output and fix the commit it hands on.

        Reads the head commit of the branch the agent pushed and checks, at
        that commit, that the branch has moved on from the input commit and
        that every required file exists. Only a commit that passes is
        recorded as the stage's output commit. Nothing is launched, so this
        can be repeated freely after a failure.
        """
        required = list(stage.output_manifest.get("required_files", []))
        commit = None
        missing: list[str] = []
        try:
            if result.output_branch is None:
                if reported_branches is None:
                    reported_branches = self._with_retries(
                        action=lambda: self.api.get_run(
                            agent_id=result.agent_id, run_id=result.run_id,
                        ),
                        description="Reading the run",
                    ).branches
                result.output_branch = self._select_output_branch(
                    reported_branches=reported_branches,
                )
            commit = self._with_retries(
                action=lambda: self.github.get_branch_commit(branch=result.output_branch),
                description=f"Reading branch '{result.output_branch}'",
            )
            if commit == result.input_commit:
                problem = (
                    f"Branch '{result.output_branch}' is still at the input commit "
                    f"{commit}: the stage pushed no new commit"
                )
            else:
                missing = self._missing_required_files(stage=stage, commit=commit)
                problem = (
                    f"Required output file(s) missing at commit {commit} on branch "
                    f"'{result.output_branch}': {', '.join(missing)}"
                    if missing else None
                )
        except (OutputGateError, APIError) as error:
            problem = f"The output could not be checked: {error}"
        
        result.gate = {
            "checked_at": utc_now(),
            "branch": result.output_branch,
            "commit": commit,
            "required_files": required,
            "missing_files": missing,
            "passed": problem is None,
        }
        if problem is not None:
            result.status = STAGE_GATE_FAILED
            result.error = problem
            self.checkpoint()
            return False
        
        result.output_commit = commit
        result.status = STAGE_OUTPUT_VERIFIED
        result.error = None
        self.checkpoint()
        print(f"  Output branch: {result.output_branch}")
        print(f"  Output commit: {commit} ({len(required)} required file(s) present)")
        return True
    
    def _collect_artifacts(self, stage: StageConfig, result: StageResult) -> bool:
        """Download the agent's artifacts into the project's artifact folder.

        An artifact whose path is unsafe is not written; it is recorded
        under `rejected`. A failed listing or download stops the stage with
        ARTIFACTS_FAILED; a resume tries the download again and launches
        nothing.
        """
        if not self.artifacts_config.get("collect", False):
            return True
        
        # One folder per attempt: files from a replaced attempt's agent are
        # never mixed with those of the attempt that is handed on.
        root = self.artifacts_root / stage.name / f"attempt_{result.attempt}"
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
    
    def approve_stage(self, stage_name: str, commit: Optional[str] = None) -> None:
        """Record the manual approval of a stage that is awaiting review.

        Without `commit`, the commit that passed the output check is
        approved, and only if the stage's branch still points at it. If the
        branch has moved (for example, the reviewer pushed a correction),
        `commit` must name the branch's current head; the required files
        are then checked at that commit and it becomes the stage's output.

        Raises:
            ProjectStateError: if the stage is not awaiting review, the
                branch moved and no commit was named, the named commit is
                not the branch head, or required files are missing there.
            APIError: if GitHub cannot be read.
        """
        result = self.manifest.stages.get(stage_name)
        if result is None or result.status != STAGE_AWAITING_REVIEW:
            state = "it has not been run" if result is None else f"its status is {result.status}"
            raise ProjectStateError(
                f"Stage '{stage_name}' is not awaiting review: {state}."
            )
        
        head = self._with_retries(
            action=lambda: self.github.get_branch_commit(branch=result.output_branch),
            description=f"Reading branch '{result.output_branch}'",
        )
        if commit is None:
            if head != result.output_commit:
                raise ProjectStateError(
                    f"Branch '{result.output_branch}' has moved since stage "
                    f"'{stage_name}' was checked: the checked output is commit "
                    f"{result.output_commit}, the branch is now at {head}. To hand "
                    f"on the new commit, approve it by name: "
                    f"--approve {stage_name} --approve-commit {head}"
                )
        else:
            if commit != head:
                raise ProjectStateError(
                    f"--approve-commit must be the current head of branch "
                    f"'{result.output_branch}', which is {head}."
                )
            if commit != result.output_commit:
                stage = next(s for s in self.stages if s.name == stage_name)
                missing = self._missing_required_files(stage=stage, commit=commit)
                if missing:
                    raise ProjectStateError(
                        f"Commit {commit} cannot be approved: required output "
                        f"file(s) missing there: {', '.join(missing)}"
                    )
                result.gate = {
                    "checked_at": utc_now(),
                    "branch": result.output_branch,
                    "commit": commit,
                    "required_files": list(stage.output_manifest.get("required_files", [])),
                    "missing_files": [],
                    "passed": True,
                }
                result.review["replaced_commit"] = result.output_commit
                result.output_commit = commit
        
        result.review["approved_at"] = utc_now()
        result.review["approved_commit"] = result.output_commit
        result.status = STAGE_COMPLETE
        result.completed_at = utc_now()
        self.checkpoint()
        print(f"\nStage '{stage_name}' approved at commit {result.output_commit}")
    
    def run_pipeline(self) -> PipelineManifest:
        """
        Run the stages of the open project, in order, until one cannot go on.
        
        A project is opened by start_new_project() (which asks for the
        model setup document) or by resume_project().
        
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
        print(f"Project started: {self.manifest.started_at}")
        print(f"Repository: {self.manifest.repo_url}")
        print(f"Stages: {len(self.stages)}")
        print(f"Dry run: {self.dry_run}")
        
        done_statuses = (STAGE_COMPLETE, STAGE_DRY_RUN)
        for stage in self.stages:
            existing = self.manifest.stages.get(stage.name)
            if existing and existing.status == STAGE_COMPLETE:
                print(f"\n--- Stage: {stage.display_name} [already complete] ---")
                continue
            
            if stage.depends_on:
                dependency = self.manifest.stages.get(stage.depends_on)
                if not dependency or dependency.status not in done_statuses:
                    print(
                        f"\n--- Stage: {stage.display_name} "
                        f"[not started: waits for '{stage.depends_on}'] ---"
                    )
                    break
            
            result = self._run_stage(stage)
            if result.status not in done_statuses:
                break
        
        if all(
            stage.name in self.manifest.stages
            and self.manifest.stages[stage.name].status == STAGE_COMPLETE
            for stage in self.stages
        ):
            self.manifest.completed_at = self.manifest.completed_at or utc_now()
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


def load_config(config_path: Path) -> dict:
    """Load configuration from YAML file."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_manifest(manifest_path: Path) -> PipelineManifest:
    """Load a manifest from JSON file."""
    with open(manifest_path, encoding="utf-8") as f:
        return PipelineManifest.from_dict(json.load(f))


NEXT_STEP_BY_STATUS = {
    STAGE_AWAITING_REVIEW: (
        "Review the output, then approve it: --resume {manifest} --approve {stage}\n"
        "            (approving also launches the next stage). To reject it and launch "
        "a NEW paid\n            agent for this stage instead: "
        "--resume {manifest} --rerun-stage {stage}"
    ),
    STAGE_LAUNCHING: (
        "It is not known whether the agent was created. --resume {manifest} first "
        "checks for the recorded agent and never creates a second one."
    ),
    STAGE_RUNNING: (
        "--resume {manifest} keeps waiting for the same run. No new agent is launched."
    ),
    STAGE_LAUNCH_REJECTED: (
        "No agent was created. Fix the cause; --resume {manifest} then launches this stage."
    ),
    STAGE_RUN_FAILED: (
        "--resume {manifest} launches nothing for this stage. To launch a NEW paid "
        "agent for it: --resume {manifest} --rerun-stage {stage}"
    ),
    STAGE_GATE_FAILED: (
        "--resume {manifest} checks the output again and launches nothing. To launch "
        "a NEW paid agent instead: --resume {manifest} --rerun-stage {stage}"
    ),
    STAGE_ARTIFACTS_FAILED: (
        "--resume {manifest} tries the download again. No new agent is launched."
    ),
}


def main(
    argv: Optional[list[str]] = None,
    api_client: Optional[CursorAPIClient] = None,
    ask: Callable[[str], str] = input,
    github: Optional[GitHubRepoReader] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run the command line interface and return the process exit code.

    Exit codes: 0 when every stage is complete (or a check passed), 2 when
    the pipeline is paused for manual review, 1 otherwise.

    Args:
        argv: Command line arguments (default: those of the process).
        api_client: API client to use (default: one built from CURSOR_API_KEY).
        ask: Called with a question, returns the user's answer (default: input).
        github: GitHub reader to use (default: one built from the config).
        sleep: Called to wait between polls and retries (default: time.sleep).
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
  python orchestrator.py --resume manifest.json --approve STAGE
  python orchestrator.py --resume manifest.json --rerun-stage STAGE

Model setup / equilibrium definition document:
  --execute and --dry-run always ask for the location of this document
  (.qmd, .tex or .md). --resume uses the document recorded in the manifest.

Environment:
  CURSOR_API_KEY    API key from Cursor Dashboard -> API Keys
  GITHUB_TOKEN      Only for a private repository: token that can read it

Official API docs: https://cursor.com/docs/cloud-agent/api/endpoints
        """,
    )
    
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent / "config.yaml",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    
    mode = parser.add_mutually_exclusive_group(required=True)
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
             "run the pipeline (launches paid cloud agents)",
    )
    mode.add_argument(
        "--resume",
        type=Path,
        metavar="MANIFEST",
        help="Continue the project recorded in a manifest JSON file. Never "
             "launches a second agent for a stage that already has one",
    )
    
    parser.add_argument(
        "--output",
        type=Path,
        help="Manifest file for a new project (default: pipeline_manifest.json). "
             "Not allowed with --resume",
    )
    parser.add_argument(
        "--approve",
        metavar="STAGE",
        help="With --resume: approve a stage that is paused for manual "
             "review, then go on to the next stage",
    )
    parser.add_argument(
        "--approve-commit",
        metavar="SHA",
        help="With --approve: approve this commit, which must be the current "
             "head of the stage's branch (needed when the branch has moved)",
    )
    parser.add_argument(
        "--rerun-stage",
        metavar="STAGE",
        help="With --resume: launch a NEW paid agent for a stage whose run "
             "failed, whose output check failed, or whose output is awaiting "
             "review and is rejected",
    )
    
    args = parser.parse_args(argv)
    
    usage_error = None
    resume_only = [
        flag for flag, value in (
            ("--approve", args.approve),
            ("--approve-commit", args.approve_commit),
            ("--rerun-stage", args.rerun_stage),
        ) if value
    ]
    if resume_only and not args.resume:
        usage_error = f"{', '.join(resume_only)} can be used only with --resume"
    elif args.approve_commit and not args.approve:
        usage_error = "--approve-commit can be used only with --approve"
    elif args.approve and args.rerun_stage:
        usage_error = "--approve and --rerun-stage cannot be used together"
    elif args.resume and args.output:
        usage_error = (
            "--output cannot be used with --resume: a resumed project keeps "
            "writing to the manifest it was resumed from"
        )
    if usage_error:
        print(f"Error: {usage_error}")
        return 1
    
    if not args.config.exists():
        print(f"Error: Config file not found: {args.config}")
        return 1
    
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
        return 1
    
    if args.check_online:
        if not orchestrator.run_online_validation():
            print("\nOnline validation FAILED. No agent was launched.")
            return 1
        print("\nOnline validation PASSED. No agent was launched.")
        return 0
    
    manifest_path = args.resume or args.output or Path(
        config.get("manifest", {}).get("output_file", "pipeline_manifest.json")
    )
    
    try:
        if args.resume:
            if not args.resume.exists():
                print(f"Error: Manifest file not found: {args.resume}")
                return 1
            orchestrator.manifest_path = manifest_path
            orchestrator.resume_project(
                manifest=load_manifest(args.resume),
                rerun_stage=args.rerun_stage,
            )
        elif args.execute:
            if manifest_path.exists():
                print(
                    f"\nERROR: A project manifest already exists at {manifest_path}. "
                    f"A new project would overwrite that project's record.\n"
                    f"Use --resume {manifest_path} to continue that project, or "
                    f"--output <other file> to start a new project beside it."
                )
                print("No agent was launched.")
                return 1
            orchestrator.manifest_path = manifest_path
            orchestrator.start_new_project()
        else:
            orchestrator.start_new_project()
    except (ModelSetupDocumentError, ProjectStateError) as error:
        print(f"\nERROR: {error}")
        print("No agent was launched.")
        return 1
    
    if args.dry_run:
        manifest = orchestrator.run_pipeline()
        ready = all(
            stage.name in manifest.stages
            and manifest.stages[stage.name].status == STAGE_DRY_RUN
            for stage in orchestrator.stages
        )
        print(
            "\n[DRY RUN] Offline check only: no network request was made, no agent "
            "was launched and nothing was saved.\nNot checked: the API key, the "
            "models and GitHub access (use --check-online). --execute starts a "
            "project and asks for the document again."
        )
        return 0 if ready else 1
    
    if not orchestrator.run_online_validation():
        print("\nValidation FAILED. No agent was launched.")
        return 1
    
    print("\nValidation PASSED")
    
    # The project's record (the selected document, the source commit, and any
    # --rerun-stage reset) is on disk before any agent is launched.
    orchestrator.checkpoint()
    
    if args.approve:
        try:
            orchestrator.approve_stage(stage_name=args.approve, commit=args.approve_commit)
        except (ProjectStateError, APIError) as error:
            print(f"\nERROR: {error}")
            print("No agent was launched.")
            return 1
    
    manifest = orchestrator.run_pipeline()
    print(f"\nManifest saved to: {manifest_path}")
    
    print("\n=== Pipeline Summary ===")
    statuses = []
    for stage in orchestrator.stages:
        result = manifest.stages.get(stage.name)
        status = result.status if result else "NOT STARTED"
        statuses.append(status)
        print(f"  {stage.name}: {status}")
        if result and result.error:
            print(f"      {result.error}")
        if status in NEXT_STEP_BY_STATUS:
            print("      Next: " + NEXT_STEP_BY_STATUS[status].format(
                manifest=manifest_path, stage=stage.name,
            ))
    
    if all(status == STAGE_COMPLETE for status in statuses):
        return 0
    if STAGE_AWAITING_REVIEW in statuses:
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
