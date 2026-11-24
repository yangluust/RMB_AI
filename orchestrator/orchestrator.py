#!/usr/bin/env python3
"""
Research Pipeline Orchestrator

Orchestrates a multi-stage research pipeline using the Cursor Cloud Agents API.
Each stage runs as a separate cloud agent with configurable model selection.

Usage:
    python orchestrator.py --dry-run              # Validate config, no execution
    python orchestrator.py --execute              # Run the pipeline
    python orchestrator.py --resume manifest.json # Resume from last checkpoint

Official API docs: https://cursor.com/docs/cloud-agent/api/endpoints
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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
    
    def to_dict(self) -> dict:
        return {
            "pipeline_name": self.pipeline_name,
            "repo_url": self.repo_url,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
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
    ):
        self.config = config
        self.dry_run = dry_run
        self.api = api_client or CursorAPIClient()
        
        self.pipeline_config = config.get("pipeline", {})
        self.repo_config = self.pipeline_config.get("repository", {})
        self.api_config = config.get("api", {})
        self.stages = self._parse_stages(config.get("stages", []))
        self.execution_config = config.get("execution", {})
        
        self.manifest: Optional[PipelineManifest] = None
    
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
        
        return len(errors) == 0, errors
    
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
                        f"Stage '{stage.name}': model '{stage.model_id}' not available. "
                        f"Use GET /v1/models to list available models."
                    )
                    continue
                
                print(f"  Stage '{stage.name}': model '{stage.model_id}' OK")
                
                if stage.model_params:
                    params_valid, param_errors = self.api.validate_model_params(
                        stage.model_id,
                        stage.model_params,
                    )
                    if not params_valid:
                        errors.extend(
                            f"Stage '{stage.name}': {e}" for e in param_errors
                        )
        
        except CursorAPIError as e:
            errors.append(f"Failed to fetch models: {e}")
        
        return len(errors) == 0, errors
    
    def validate_api_key(self) -> tuple[bool, list[str]]:
        """Check that API key is configured."""
        if not os.environ.get("CURSOR_API_KEY") and not self.api.api_key:
            return False, ["CURSOR_API_KEY environment variable not set"]
        return True, []
    
    def run_validation(self) -> bool:
        """
        Run all validation checks.
        
        Returns:
            True if all validations pass
        """
        print("\n=== Validation ===\n")
        all_valid = True
        
        print("Checking configuration...")
        config_valid, config_errors = self.validate_config()
        if not config_valid:
            all_valid = False
            for e in config_errors:
                print(f"  ERROR: {e}")
        else:
            print("  Configuration OK")
        
        print("\nChecking API key...")
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
                prompt_text=stage.prompt,
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
    
    def run_pipeline(
        self,
        resume_manifest: Optional[PipelineManifest] = None,
    ) -> PipelineManifest:
        """
        Execute the full pipeline.
        
        Args:
            resume_manifest: Optional manifest to resume from
            
        Returns:
            PipelineManifest with all stage results
        """
        if resume_manifest:
            self.manifest = resume_manifest
            print(f"\nResuming pipeline from manifest (started {resume_manifest.started_at})")
        else:
            self.manifest = PipelineManifest(
                pipeline_name=self.pipeline_config.get("name", "unnamed"),
                repo_url=self.repo_config.get("url", ""),
                started_at=datetime.now(timezone.utc).isoformat(),
            )
        
        print(f"\n=== Pipeline: {self.manifest.pipeline_name} ===")
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


def main():
    parser = argparse.ArgumentParser(
        description="Research Pipeline Orchestrator for Cursor Cloud Agents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python orchestrator.py --dry-run              # Validate only
  python orchestrator.py --execute              # Run pipeline
  python orchestrator.py --resume manifest.json # Resume from checkpoint

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
        help="Validate config and models without launching agents",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Execute the pipeline (launches paid cloud agents)",
    )
    mode.add_argument(
        "--resume",
        type=Path,
        metavar="MANIFEST",
        help="Resume from a previous manifest JSON file",
    )
    
    parser.add_argument(
        "--output",
        type=Path,
        help="Path to write the output manifest (default: pipeline_manifest.json)",
    )
    
    args = parser.parse_args()
    
    if not args.config.exists():
        print(f"Error: Config file not found: {args.config}")
        sys.exit(1)
    
    print(f"Loading config: {args.config}")
    config = load_config(args.config)
    
    dry_run = args.dry_run or args.resume is None and not args.execute
    orchestrator = Orchestrator(config=config, dry_run=dry_run)
    
    if not orchestrator.run_validation():
        print("\nValidation FAILED")
        sys.exit(1)
    
    print("\nValidation PASSED")
    
    if args.dry_run:
        print("\n[DRY RUN] No agents launched. Use --execute to run the pipeline.")
        sys.exit(0)
    
    resume_manifest = None
    if args.resume:
        if not args.resume.exists():
            print(f"Error: Manifest file not found: {args.resume}")
            sys.exit(1)
        resume_manifest = load_manifest(args.resume)
    
    manifest = orchestrator.run_pipeline(resume_manifest=resume_manifest)
    
    output_path = args.output or Path(
        config.get("manifest", {}).get("output_file", "pipeline_manifest.json")
    )
    orchestrator.save_manifest(output_path)
    
    print("\n=== Pipeline Summary ===")
    all_success = True
    for stage_name, result in manifest.stages.items():
        status_icon = "✓" if RunStatus.is_success(result.status) else "✗"
        print(f"  {status_icon} {stage_name}: {result.status}")
        if result.error:
            print(f"      Error: {result.error}")
        if not RunStatus.is_success(result.status) and result.status != "DRY_RUN":
            all_success = False
    
    sys.exit(0 if all_success else 1)


if __name__ == "__main__":
    main()
