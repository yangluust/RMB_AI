#!/usr/bin/env python3
"""
Research Pipeline Orchestrator

Orchestrates a multi-stage research pipeline using the Cursor Cloud Agents API.
Each stage runs as a separate cloud agent with configurable model selection.

Usage:
    python orchestrator.py --dry-run              # Validate, launch nothing
    python orchestrator.py --execute              # Start a new project
    python orchestrator.py --resume manifest.json # Continue an existing project

Starting a new project (--execute) always asks for the location of the
model setup / equilibrium definition document (.qmd or .tex). Nothing is
launched until a valid document has been given.

Official API docs: https://cursor.com/docs/cloud-agent/api/endpoints
"""

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import yaml
except ImportError:
    print("Error: PyYAML required. Install with: pip install pyyaml")
    sys.exit(1)

from api_client import CursorAPIClient, CursorAPIError, RunStatus


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


GUARDRAILS_HEADING = "REPRODUCIBILITY GUARDRAILS (follow all of these):"

MODEL_SETUP_QUESTION = (
    "Location of the model setup / equilibrium definition document "
    "for this project (.qmd or .tex): "
)
MODEL_SETUP_FORMATS = {
    ".qmd": "Quarto Markdown (.qmd)",
    ".tex": "LaTeX (.tex)",
}
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
INCLUDE_DIRECTIVE_PATTERNS = {
    ".tex": re.compile(r"\\(?:input|include|subfile|import|subimport)(?![A-Za-z])"),
    ".qmd": re.compile(r"\{\{<\s*include\s"),
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

    The file is only read. It must exist, be a `.qmd` or `.tex` file, hold
    UTF-8 text, be non-empty and be self-contained. Whether it states a
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
            "definition document. Give the path of a .qmd or .tex file."
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
            f"The model setup document must be a .qmd or .tex file, "
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


@dataclass
class StageResult:
    """Result of a completed stage."""
    name: str
    status: str
    agent_id: Optional[str] = None
    run_id: Optional[str] = None
    agent_url: Optional[str] = None
    input_ref: Optional[str] = None
    output_branch: Optional[str] = None
    output_commit: Optional[str] = None
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
    
    def to_dict(self) -> dict:
        return {
            "pipeline_name": self.pipeline_name,
            "repo_url": self.repo_url,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "model_setup_document": self.model_setup_document,
            "stages": {
                name: {
                    "name": r.name,
                    "status": r.status,
                    "agent_id": r.agent_id,
                    "run_id": r.run_id,
                    "agent_url": r.agent_url,
                    "input_ref": r.input_ref,
                    "output_branch": r.output_branch,
                    "output_commit": r.output_commit,
                    "duration_ms": r.duration_ms,
                    "error": r.error,
                    "completed_at": r.completed_at,
                }
                for name, r in self.stages.items()
            },
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "PipelineManifest":
        manifest = cls(
            pipeline_name=data["pipeline_name"],
            repo_url=data["repo_url"],
            started_at=data["started_at"],
            completed_at=data.get("completed_at"),
            model_setup_document=data.get("model_setup_document"),
        )
        for name, stage_data in data.get("stages", {}).items():
            manifest.stages[name] = StageResult(
                name=stage_data["name"],
                status=stage_data["status"],
                agent_id=stage_data.get("agent_id"),
                run_id=stage_data.get("run_id"),
                agent_url=stage_data.get("agent_url"),
                input_ref=stage_data.get("input_ref"),
                output_branch=stage_data.get("output_branch"),
                output_commit=stage_data.get("output_commit"),
                duration_ms=stage_data.get("duration_ms"),
                error=stage_data.get("error"),
                completed_at=stage_data.get("completed_at"),
            )
        return manifest


class Orchestrator:
    """
    Pipeline orchestrator that launches cloud agents for each stage.
    
    Key behaviors:
    - Asks for the model setup document whenever a new project is started
    - Validates models against GET /v1/models before launch
    - Sequential stage execution with dependency tracking
    - Artifact handoff via Git branches
    - Manifest tracking for resume capability
    """
    
    def __init__(
        self,
        config: dict,
        api_client: Optional[CursorAPIClient] = None,
        dry_run: bool = True,
        ask: Callable[[str], str] = input,
    ):
        self.config = config
        self.dry_run = dry_run
        self.api = api_client or CursorAPIClient()
        # Called with the question text; returns the user's answer.
        self.ask = ask
        
        self.pipeline_config = config.get("pipeline", {})
        self.repo_config = self.pipeline_config.get("repository", {})
        self.api_config = config.get("api", {})
        self.stages = self._parse_stages(config.get("stages", []))
        self.execution_config = config.get("execution", {})
        
        self.manifest: Optional[PipelineManifest] = None
        self.model_setup_document: Optional[ModelSetupDocument] = None
    
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
        
        # The document asked for at project start has to reach the stage
        # that starts the pipeline, or asking for it would have no effect.
        if self.stages and self.stages[0].receives_model_setup_document is not True:
            errors.append(
                f"Stage '{self.stages[0].name}' is the first stage and must set "
                f"receives_model_setup_document: true"
            )
        
        return len(errors) == 0, errors
    
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
        )
        return self.manifest
    
    def _is_stage_finished(self, manifest: PipelineManifest, stage: StageConfig) -> bool:
        existing = manifest.stages.get(stage.name)
        return existing is not None and RunStatus.is_success(existing.status)
    
    def resume_project(self, manifest: PipelineManifest) -> PipelineManifest:
        """Continue an existing project from its manifest, without asking.

        The document is the one recorded when the project was started. It
        is re-read only if a stage that receives it still has to run, and
        it must then have the recorded content.

        Raises:
            ModelSetupDocumentError: if a stage still needs the document
                and the recorded document is missing or has changed.
        """
        self.manifest = None
        self.model_setup_document = None
        
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
    
    def validate_models(self) -> tuple[bool, list[str]]:
        """
        Validate all configured models against GET /v1/models.
        
        Returns:
            Tuple of (all_valid, list_of_errors)
        """
        errors = []
        
        try:
            models = self.api.list_models()
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
    
    def run_api_validation(self) -> bool:
        """
        Check the API key and the configured models (calls GET /v1/models).
        
        Returns:
            True if all checks pass
        """
        print("\n=== API Validation ===\n")
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
        
        return all_valid
    
    def _get_stage_input_ref(self, stage: StageConfig) -> str:
        """Determine the input ref for a stage."""
        if stage.depends_on and self.manifest:
            prior_result = self.manifest.stages.get(stage.depends_on)
            if prior_result and prior_result.output_branch:
                return prior_result.output_branch
        
        return self.repo_config.get("starting_ref", "main")
    
    def _run_stage(self, stage: StageConfig) -> StageResult:
        """Execute a single stage."""
        result = StageResult(name=stage.name, status="PENDING")
        
        input_ref = self._get_stage_input_ref(stage)
        result.input_ref = input_ref
        
        print(f"\n--- Stage: {stage.display_name} ---")
        print(f"  Input ref: {input_ref}")
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
            result.status = "BLOCKED"
            result.error = str(error)
            print(f"  BLOCKED: {error}")
            return result
        
        if stage.receives_model_setup_document:
            print(
                f"  Model setup document in prompt: "
                f"{self.model_setup_document.file_name} "
                f"(SHA-256 {self.model_setup_document.sha256})"
            )
        
        if self.dry_run:
            print("  [DRY RUN] Would launch cloud agent")
            result.status = "DRY_RUN"
            return result
        
        try:
            polling_config = self.api_config.get("polling", {})
            interval = polling_config.get("interval_seconds", 30)
            max_attempts = polling_config.get("max_attempts", 120)
            
            print("  Launching cloud agent...")
            run = self.api.create_agent(
                prompt_text=prompt_text,
                repo_url=self.repo_config["url"],
                starting_ref=input_ref,
                model_id=stage.model_id,
                model_params=stage.model_params if stage.model_params else None,
                name=f"Pipeline: {stage.display_name}",
                auto_create_pr=self.execution_config.get("auto_create_pr", False),
            )
            
            result.agent_id = run.agent_id
            result.run_id = run.run_id
            result.agent_url = run.agent_url
            
            print(f"  Agent ID: {run.agent_id}")
            print(f"  Run ID: {run.run_id}")
            if run.agent_url:
                print(f"  URL: {run.agent_url}")
            
            def on_status(status: str, attempt: int) -> None:
                print(f"  [{attempt}/{max_attempts}] Status: {status}")
            
            print("  Polling for completion...")
            final_run = self.api.poll_run_until_complete(
                agent_id=run.agent_id,
                run_id=run.run_id,
                interval_seconds=interval,
                max_attempts=max_attempts,
                on_status=on_status,
            )
            
            result.status = final_run.status
            result.duration_ms = final_run.duration_ms
            
            if final_run.branches:
                branch_info = final_run.branches[0]
                result.output_branch = branch_info.get("branch")
                print(f"  Output branch: {result.output_branch}")
            
            result.completed_at = datetime.now(timezone.utc).isoformat()
            
            if RunStatus.is_success(result.status):
                print(f"  Stage completed successfully")
            else:
                print(f"  Stage ended with status: {result.status}")
                result.error = final_run.result_text
        
        except CursorAPIError as e:
            result.status = "API_ERROR"
            result.error = str(e)
            print(f"  ERROR: {e}")
        
        return result
    
    def run_pipeline(self) -> PipelineManifest:
        """
        Run the stages of the open project.
        
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
        
        fail_fast = self.execution_config.get("fail_fast", True)
        
        def is_stage_complete(status: str) -> bool:
            """Check if a stage status counts as complete (for dependency tracking)."""
            return RunStatus.is_success(status) or status == "DRY_RUN"
        
        for stage in self.stages:
            existing = self.manifest.stages.get(stage.name)
            if existing and RunStatus.is_success(existing.status):
                print(f"\n--- Stage: {stage.display_name} [SKIPPED - already complete] ---")
                continue
            
            if stage.depends_on:
                dep_result = self.manifest.stages.get(stage.depends_on)
                if not dep_result or not is_stage_complete(dep_result.status):
                    print(f"\n--- Stage: {stage.display_name} [SKIPPED - dependency not met] ---")
                    self.manifest.stages[stage.name] = StageResult(
                        name=stage.name,
                        status="SKIPPED",
                        error=f"Dependency '{stage.depends_on}' not satisfied",
                    )
                    if fail_fast:
                        break
                    continue
            
            result = self._run_stage(stage)
            self.manifest.stages[stage.name] = result
            
            if fail_fast and result.status not in ("FINISHED", "DRY_RUN"):
                print(f"\nStopping pipeline (fail_fast=true)")
                break
        
        self.manifest.completed_at = datetime.now(timezone.utc).isoformat()
        return self.manifest
    
    def save_manifest(self, path: Path) -> None:
        """Save the manifest to a JSON file."""
        if self.manifest:
            with open(path, "w") as f:
                json.dump(self.manifest.to_dict(), f, indent=2)
            print(f"\nManifest saved to: {path}")


def load_config(config_path: Path) -> dict:
    """Load configuration from YAML file."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_manifest(manifest_path: Path) -> PipelineManifest:
    """Load a manifest from JSON file."""
    with open(manifest_path) as f:
        return PipelineManifest.from_dict(json.load(f))


def main(
    argv: Optional[list[str]] = None,
    api_client: Optional[CursorAPIClient] = None,
    ask: Callable[[str], str] = input,
) -> int:
    """Run the command line interface and return the process exit code.

    Args:
        argv: Command line arguments (default: those of the process).
        api_client: API client to use (default: one built from CURSOR_API_KEY).
        ask: Called with a question, returns the user's answer (default: input).
    """
    parser = argparse.ArgumentParser(
        description="Research Pipeline Orchestrator for Cursor Cloud Agents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python orchestrator.py --dry-run              # Validate, launch nothing
  python orchestrator.py --execute              # Start a new project
  python orchestrator.py --resume manifest.json # Continue an existing project

Model setup / equilibrium definition document:
  --execute and --dry-run always ask for the location of this document
  (.qmd or .tex). --resume uses the document recorded in the manifest.

Environment:
  CURSOR_API_KEY    API key from Cursor Dashboard -> API Keys

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
        help="Ask for the model setup document and validate everything "
             "without launching agents or saving a project",
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
        help="Continue the project recorded in a manifest JSON file",
    )
    
    parser.add_argument(
        "--output",
        type=Path,
        help="Path of the project manifest to write (default for a new "
             "project: pipeline_manifest.json; default for --resume: the "
             "manifest being resumed)",
    )
    
    args = parser.parse_args(argv)
    
    if not args.config.exists():
        print(f"Error: Config file not found: {args.config}")
        return 1
    
    print(f"Loading config: {args.config}")
    config = load_config(args.config)
    
    orchestrator = Orchestrator(
        config=config,
        api_client=api_client,
        dry_run=args.dry_run,
        ask=ask,
    )
    
    if not orchestrator.run_config_validation():
        print("\nValidation FAILED. No agent was launched.")
        return 1
    
    if args.resume:
        manifest_path = args.output or args.resume
    else:
        manifest_path = args.output or Path(
            config.get("manifest", {}).get("output_file", "pipeline_manifest.json")
        )
    
    try:
        if args.resume:
            if not args.resume.exists():
                print(f"Error: Manifest file not found: {args.resume}")
                return 1
            orchestrator.resume_project(manifest=load_manifest(args.resume))
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
            orchestrator.start_new_project()
        else:
            orchestrator.select_model_setup_document()
    except ModelSetupDocumentError as error:
        print(f"\nERROR: {error}")
        print("No agent was launched.")
        return 1
    
    if not orchestrator.run_api_validation():
        print("\nValidation FAILED. No agent was launched.")
        return 1
    
    print("\nValidation PASSED")
    
    if args.dry_run:
        print(
            "\n[DRY RUN] No agents launched and no project saved. "
            "Use --execute to start a project; it will ask for the document again."
        )
        return 0
    
    if args.execute:
        # The project's record, including which document was selected, is
        # written before the first agent is launched.
        orchestrator.save_manifest(manifest_path)
    
    manifest = orchestrator.run_pipeline()
    orchestrator.save_manifest(manifest_path)
    
    print("\n=== Pipeline Summary ===")
    all_success = True
    for stage_name, result in manifest.stages.items():
        status_icon = "✓" if RunStatus.is_success(result.status) else "✗"
        print(f"  {status_icon} {stage_name}: {result.status}")
        if result.error:
            print(f"      Error: {result.error}")
        if not RunStatus.is_success(result.status):
            all_success = False
    
    return 0 if all_success else 1


if __name__ == "__main__":
    sys.exit(main())
