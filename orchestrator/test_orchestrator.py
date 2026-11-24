#!/usr/bin/env python3
"""
Unit tests for the pipeline orchestrator.

All tests use mocked HTTP - no network calls are made. The Cursor API, the
GitHub API and the artifact download host are replaced by stand-ins written
from the public API documentation. No test launches an agent or reads a
real repository, so these tests do not show that the live services behave
as documented.

The local review checkout is replaced by a stand-in that runs no Git
command, except in the tests of LocalReviewCheckout itself: those run real
Git commands on throw-away repositories in a temporary folder, with a
local bare repository standing in for GitHub.

The host of the project's main chat is not part of this repository. FakeHost
stands in for it: it reads the signed notices the orchestrator writes and
writes signed acks and decisions, with a secret that exists only in these
tests. It posts nothing anywhere, so no test shows that a notice reaches a
chat. The stand-in agents write output of the required form with no
content: no test shows that a derivation, a trace, code or a result is right.
Run with: python -m pytest test_orchestrator.py -v
Or simply: python test_orchestrator.py
"""

import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, quote, unquote, urlsplit

import yaml

from api_client import (
    APIError,
    CursorAPIClient,
    CursorAPIError,
    GitHubAPIError,
    GitHubRepoReader,
    Model,
    RunStatus,
    TransportError,
    parse_github_repo_url,
)
import trial_log
import verification
from orchestrator import (
    AUTONOMOUS_RULES,
    EXIT_COMPLETE,
    EXIT_FAILED,
    EXIT_NOTICE_PENDING,
    EXIT_WAITING_FOR_OWNER,
    FIX_HEADING,
    GUARDRAILS_HEADING,
    KIND_ADOPTED,
    KIND_FIX,
    KIND_INITIAL,
    KIND_RECONCILE,
    KIND_REDO,
    KIND_REVISE,
    LOG_ENTRIES_BEGIN_LINE,
    LOG_ENTRIES_END_LINE,
    MANIFEST_FORMAT_VERSION,
    MODEL_SETUP_BEGIN_LINE,
    MODEL_SETUP_END_LINE,
    MODEL_SETUP_EXTENSIONS,
    MODEL_SETUP_FORMATS,
    MODEL_SETUP_HEADING,
    MODEL_SETUP_QUESTION,
    ORIGIN_ADOPTED,
    OUTCOME_BLOCKERS_OPEN,
    OUTCOME_CHECKS_FAILED,
    OUTCOME_DISAGREEMENT,
    OUTCOME_LAUNCH_REJECTED,
    OUTCOME_PASSED,
    OUTCOME_REVIEW_FAILED,
    OUTCOME_REVIEW_INVALID,
    OUTCOME_RUN_FAILED,
    OWN_ENTRY_BEGIN_LINE,
    OWN_ENTRY_END_LINE,
    PHASE_BLIND,
    PHASE_COMPARE,
    PHASE_PROPOSE,
    RECONCILE_HEADING,
    REVISION_HEADING,
    ROLE_PROPOSER,
    ROLE_REVIEWER,
    STAGE_ARTIFACTS_FAILED,
    STAGE_AWAITING_REVIEW,
    STAGE_BLOCKED,
    STAGE_COMPLETE,
    STAGE_DRY_RUN,
    STAGE_LAUNCHING,
    STAGE_NOTICE_PENDING,
    STAGE_PENDING,
    STAGE_RUN_FINISHED,
    STAGE_RUNNING,
    STAGE_SYNC_PENDING,
    STOP_ADOPTED,
    STOP_CAP_EXHAUSTED,
    STOP_DISAGREEMENT,
    STOP_INFRASTRUCTURE,
    STOP_NO_ALTERNATIVE,
    STOP_OWNER_DECISION,
    STOP_PASSED,
    STOP_REVIEW_REQUIRED,
    TRIAL_LOG_HEADING,
    WORKING_DOCUMENT_HEADING,
    LocalReviewCheckout,
    LocalSyncError,
    ModelSetupDocumentError,
    Orchestrator,
    PipelineManifest,
    ProjectStateError,
    StageConfig,
    StageResult,
    UnsafeArtifactPathError,
    WorkingDocumentError,
    build_stage_prompt,
    extract_stage_section,
    github_repository_of,
    load_config,
    load_manifest,
    load_model_setup_document,
    main,
    safe_artifact_destination,
    stage_section_markers,
    unsafe_relative_path_reason,
)
from review_bridge import (
    ACTION_ACCEPT,
    ACTION_HALT,
    ACTION_ITERATE,
    ACTION_RAISE_LIMIT,
    ACTION_REDO,
    ACTION_START_STAGE,
    BridgeError,
    SIGNATURE_FIELDS,
    HostReceiver,
    ReviewBridge,
    build_ack,
    build_decision,
    decision_id_of,
    decision_token,
    read_json,
    sign,
    verify,
    write_json_once,
)
from trial_log import TrialJournal, TrialLogError

ORCHESTRATOR_DIR = Path(__file__).resolve().parent
REAL_CONFIG_PATH = ORCHESTRATOR_DIR / "config.yaml"
DESIGN_DOC_PATH = ORCHESTRATOR_DIR.parent / "docs" / "pipeline-design.md"
DESIGN_DOC_GUARDRAILS_HEADING = "#### Reproducibility Guardrails (Stage 3)"
STAGE1_NAME = "equilibrium_derivation"
STAGE2_NAME = "equilibrium_to_pseudocode"
STAGE3_NAME = "pseudocode_to_implementation"
STAGE4_NAME = "parameterization_and_results"
ALL_STAGE_NAMES = (STAGE1_NAME, STAGE2_NAME, STAGE3_NAME, STAGE4_NAME)
EXPECTED_GUARDRAIL_COUNT = 12

DOCS_DIR = ORCHESTRATOR_DIR.parent / "docs"
WORKING_DOCUMENT_PATH = "docs/equilibrium-working-document.md"
SOURCE_DOCUMENT_PATH = "docs/intervention-incomplete-information-source.md"
# The original model setup document as the project owner supplied it.
SOURCE_DOCUMENT_SHA256 = "560b2ee1a7189ed8183dcc5ec8d6db9292f85ac645351bfe65a0a3292cad5960"
SOURCE_DOCUMENT_SIZE_BYTES = 79082
TRIAL_LOG_PATH = "docs/pipeline-trial-log.md"
# The secret the stand-in host and the orchestrator under test sign with.
# It exists only in these tests.
TEST_BRIDGE_SECRET = "test-only-bridge-secret"
BRIDGE_SECRET_ENV = "RMB_REVIEW_BRIDGE_SECRET"
os.environ[BRIDGE_SECRET_ENV] = TEST_BRIDGE_SECRET

# Stand-in document text for tests. It is a sentinel, not a model setup or
# an equilibrium definition.
SENTINEL_DOCUMENT_TEXT = (
    "TEST-ONLY SENTINEL DOCUMENT.\n"
    "It stands in for a model setup file and states no economics.\n"
)
OTHER_SENTINEL_DOCUMENT_TEXT = "TEST-ONLY SECOND SENTINEL DOCUMENT.\n"


class MockHttpClient:
    """Mock HTTP client for testing without network calls."""
    
    def __init__(self):
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.responses: dict[str, tuple[int, dict]] = {}
    
    def add_response(self, method: str, url_pattern: str, status: int, body: dict):
        """Add a mock response for a URL pattern."""
        key = f"{method}:{url_pattern}"
        self.responses[key] = (status, body)
    
    def __call__(
        self,
        method: str,
        url: str,
        headers: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> tuple[int, dict]:
        """Handle a mock HTTP request."""
        self.calls.append((method, url, headers or {}, json_body or {}))
        
        for pattern, (status, body) in self.responses.items():
            req_method, url_pattern = pattern.split(":", 1)
            if method == req_method and url_pattern in url:
                return status, body
        
        return 404, {"error": {"code": "not_found", "message": f"No mock for {method} {url}"}}


MOCK_MODELS_RESPONSE = {
    "items": [
        {
            "id": "composer-2",
            "displayName": "Composer 2",
            "aliases": ["composer-latest", "composer"],
            "parameters": [
                {
                    "id": "fast",
                    "displayName": "Fast",
                    "values": [
                        {"value": "false"},
                        {"value": "true", "displayName": "Fast"},
                    ],
                }
            ],
            "variants": [
                {"params": [{"id": "fast", "value": "true"}], "displayName": "Composer 2", "isDefault": True},
                {"params": [{"id": "fast", "value": "false"}], "displayName": "Composer 2"},
            ],
        },
        {
            "id": "claude-4.6-sonnet-thinking",
            "displayName": "Claude 4.6 Sonnet (Thinking)",
            "variants": [
                {"params": [], "displayName": "Claude 4.6 Sonnet (Thinking)", "isDefault": True}
            ],
        },
        {
            "id": "gpt-5.6-sol-high",
            "displayName": "GPT 5.6 Sol (High)",
            "variants": [
                {"params": [], "displayName": "GPT 5.6 Sol (High)", "isDefault": True}
            ],
        },
    ]
}


# The `model` field each stage must send, in the shape the public API docs
# give for POST /v1/agents: {"id": ..., "params": [{"id": ..., "value": ...}]}.
EXPECTED_STAGE_MODELS = {
    STAGE1_NAME: {"id": "claude-fable-5-2", "params": [{"id": "effort", "value": "xhigh"}]},
    STAGE2_NAME: {"id": "claude-fable-5-2", "params": [{"id": "effort", "value": "xhigh"}]},
    STAGE3_NAME: {"id": "grok-4.7", "params": [{"id": "reasoning_effort", "value": "low"}]},
    STAGE4_NAME: {"id": "gpt-5.6-sol", "params": [{"id": "reasoning", "value": "high"}]},
}


def model_listing_entry(model_id: str, parameter_id: str, values: list[str]) -> dict:
    return {
        "id": model_id,
        "displayName": model_id,
        "parameters": [{"id": parameter_id, "values": [{"value": value} for value in values]}],
    }


# Stand-in for a GET /v1/models listing that holds the models the committed
# config asks for. It is NOT a copy of the live listing, which has not been
# fetched. It holds only the IDs, parameter names and values reported by the
# owner's tool smoke tests, laid out in the response shape the API docs give.
MOCK_PIPELINE_MODELS_RESPONSE = {
    "items": [
        model_listing_entry(
            model_id="claude-fable-5-2",
            parameter_id="effort",
            values=["low", "medium", "high", "xhigh", "max"],
        ),
        model_listing_entry(model_id="grok-4.7", parameter_id="reasoning_effort", values=["low"]),
        model_listing_entry(model_id="gpt-5.6-sol", parameter_id="reasoning", values=["high"]),
    ]
}


MOCK_CREATE_RESPONSE = {
    "agent": {
        "id": "bc-00000000-0000-0000-0000-000000000001",
        "name": "Test Agent",
        "status": "ACTIVE",
        "url": "https://cursor.com/agents/bc-00000000-0000-0000-0000-000000000001",
    },
    "run": {
        "id": "run-00000000-0000-0000-0000-000000000001",
        "agentId": "bc-00000000-0000-0000-0000-000000000001",
        "status": "CREATING",
    },
}


MOCK_RUN_FINISHED = {
    "id": "run-00000000-0000-0000-0000-000000000001",
    "agentId": "bc-00000000-0000-0000-0000-000000000001",
    "status": "FINISHED",
    "durationMs": 60000,
    "text": "Task completed successfully.",
    "git": {
        "branches": [
            {
                "repoUrl": "github.com/test/repo",
                "branch": "cursor/test-branch-a1b2",
            }
        ]
    },
}


MOCK_CONFIG = {
    "pipeline": {
        "name": "test-pipeline",
        "repository": {
            "url": "https://github.com/test/repo",
            "starting_ref": "main",
        },
    },
    "api": {
        "base_url": "https://api.cursor.com/v1",
        "polling": {
            "interval_seconds": 1,
            "max_attempts": 3,
        },
        "retries": {
            "max_attempts": 3,
            "backoff_seconds": [1, 2],
        },
    },
    "stages": [
        {
            "name": "stage1",
            "display_name": "Test Stage 1",
            "description": "First test stage",
            "model_id": "composer-2",
            "model_params": [{"id": "fast", "value": "true"}],
            "receives_model_setup_document": True,
            "prompt": "Do something for stage 1",
            "iteration": {"max_rounds": 2},
            "output_manifest": {"required_files": ["output1.txt"]},
        },
        {
            "name": "stage2",
            "display_name": "Test Stage 2",
            "description": "Second test stage",
            "model_id": None,
            "depends_on": "stage1",
            "prompt": "Do something for stage 2",
            "iteration": {"max_rounds": 2},
            "output_manifest": {"required_files": ["output2.txt"]},
        },
    ],
    "execution": {
        "auto_create_pr": False,
    },
    "review": {
        "local_checkout": "..",
        "remote": "origin",
        "delivery": {"directory": "review_bridge", "secret_env": BRIDGE_SECRET_ENV},
    },
}

# The smallest `api` and `review` sections a config needs to pass validation,
# and the smallest per-stage settings.
MINIMAL_API_CONFIG = {"retries": {"max_attempts": 1, "backoff_seconds": [0]}}
MINIMAL_REVIEW_CONFIG = MOCK_CONFIG["review"]
MINIMAL_ITERATION = {"max_rounds": 1}


def test_run_status_helpers():
    """Test RunStatus helper methods."""
    assert RunStatus.is_active("CREATING")
    assert RunStatus.is_active("RUNNING")
    assert RunStatus.is_active("WAITING_FOR_BACKGROUND_WORK")
    assert not RunStatus.is_active("FINISHED")
    assert not RunStatus.is_active("ERROR")
    
    assert RunStatus.is_terminal("FINISHED")
    assert RunStatus.is_terminal("ERROR")
    assert RunStatus.is_terminal("CANCELLED")
    assert RunStatus.is_terminal("EXPIRED")
    assert not RunStatus.is_terminal("RUNNING")
    
    assert RunStatus.is_success("FINISHED")
    assert not RunStatus.is_success("ERROR")
    print("✓ test_run_status_helpers passed")


def test_api_client_list_models():
    """Test that list_models parses the API response correctly."""
    mock_http = MockHttpClient()
    mock_http.add_response("GET", "/models", 200, MOCK_MODELS_RESPONSE)
    
    client = CursorAPIClient(api_key="test-key", http_client=mock_http)
    models = client.list_models()
    
    assert len(models) == 3
    assert models[0].id == "composer-2"
    assert "composer-latest" in models[0].aliases
    assert len(models[0].parameters) == 1
    assert models[0].parameters[0]["id"] == "fast"
    
    assert len(mock_http.calls) == 1
    assert mock_http.calls[0][0] == "GET"
    assert "/models" in mock_http.calls[0][1]
    print("✓ test_api_client_list_models passed")


def test_api_client_validate_model_valid():
    """Test model validation with a valid model ID."""
    mock_http = MockHttpClient()
    mock_http.add_response("GET", "/models", 200, MOCK_MODELS_RESPONSE)
    
    client = CursorAPIClient(api_key="test-key", http_client=mock_http)
    
    is_valid, model = client.validate_model("composer-2")
    assert is_valid
    assert model is not None
    assert model.id == "composer-2"
    
    is_valid, model = client.validate_model("composer-latest")
    assert is_valid
    assert model is not None
    assert model.id == "composer-2"
    print("✓ test_api_client_validate_model_valid passed")


def test_api_client_validate_model_invalid():
    """Test model validation with an invalid model ID."""
    mock_http = MockHttpClient()
    mock_http.add_response("GET", "/models", 200, MOCK_MODELS_RESPONSE)
    
    client = CursorAPIClient(api_key="test-key", http_client=mock_http)
    
    is_valid, model = client.validate_model("nonexistent-model")
    assert not is_valid
    assert model is None
    print("✓ test_api_client_validate_model_invalid passed")


def test_api_client_validate_params_valid():
    """Test parameter validation with valid params."""
    mock_http = MockHttpClient()
    mock_http.add_response("GET", "/models", 200, MOCK_MODELS_RESPONSE)
    
    client = CursorAPIClient(api_key="test-key", http_client=mock_http)
    
    is_valid, errors = client.validate_model_params(
        "composer-2",
        [{"id": "fast", "value": "true"}],
    )
    assert is_valid
    assert len(errors) == 0
    print("✓ test_api_client_validate_params_valid passed")


def test_api_client_validate_params_invalid():
    """Test parameter validation with invalid params."""
    mock_http = MockHttpClient()
    mock_http.add_response("GET", "/models", 200, MOCK_MODELS_RESPONSE)
    
    client = CursorAPIClient(api_key="test-key", http_client=mock_http)
    
    is_valid, errors = client.validate_model_params(
        "composer-2",
        [{"id": "fast", "value": "invalid"}],
    )
    assert not is_valid
    assert len(errors) == 1
    assert "invalid" in errors[0]
    
    is_valid, errors = client.validate_model_params(
        "composer-2",
        [{"id": "unknown_param", "value": "foo"}],
    )
    assert not is_valid
    assert len(errors) == 1
    assert "unknown_param" in errors[0]
    print("✓ test_api_client_validate_params_invalid passed")


def test_api_client_create_agent():
    """Test creating an agent."""
    mock_http = MockHttpClient()
    mock_http.add_response("POST", "/agents", 200, MOCK_CREATE_RESPONSE)
    
    client = CursorAPIClient(api_key="test-key", http_client=mock_http)
    
    run = client.create_agent(
        prompt_text="Test prompt",
        repo_url="https://github.com/test/repo",
        starting_ref="main",
        model_id="composer-2",
        model_params=[{"id": "fast", "value": "true"}],
        name="Test Agent",
    )
    
    assert run.agent_id == "bc-00000000-0000-0000-0000-000000000001"
    assert run.run_id == "run-00000000-0000-0000-0000-000000000001"
    assert run.status == "CREATING"
    
    assert len(mock_http.calls) == 1
    call_body = mock_http.calls[0][3]
    assert call_body["prompt"]["text"] == "Test prompt"
    assert call_body["model"]["id"] == "composer-2"
    assert call_body["model"]["params"] == [{"id": "fast", "value": "true"}]
    print("✓ test_api_client_create_agent passed")


def test_api_client_get_run():
    """Test getting run status."""
    mock_http = MockHttpClient()
    mock_http.add_response("GET", "/runs/", 200, MOCK_RUN_FINISHED)
    
    client = CursorAPIClient(api_key="test-key", http_client=mock_http)
    
    run = client.get_run(
        agent_id="bc-00000000-0000-0000-0000-000000000001",
        run_id="run-00000000-0000-0000-0000-000000000001",
    )
    
    assert run.status == "FINISHED"
    assert run.duration_ms == 60000
    assert len(run.branches) == 1
    assert run.branches[0]["branch"] == "cursor/test-branch-a1b2"
    print("✓ test_api_client_get_run passed")


def test_api_client_auth_error():
    """Test that missing API key raises error."""
    mock_http = MockHttpClient()
    
    old_key = os.environ.pop("CURSOR_API_KEY", None)
    try:
        client = CursorAPIClient(api_key=None, http_client=mock_http)
        
        try:
            client.list_models()
            assert False, "Should have raised CursorAPIError"
        except CursorAPIError as e:
            assert e.code == "auth_error"
    finally:
        if old_key:
            os.environ["CURSOR_API_KEY"] = old_key
    print("✓ test_api_client_auth_error passed")


def test_orchestrator_validate_config_valid():
    """Test config validation with valid config."""
    mock_http = MockHttpClient()
    mock_http.add_response("GET", "/models", 200, MOCK_MODELS_RESPONSE)
    
    client = CursorAPIClient(api_key="test-key", http_client=mock_http)
    orchestrator = Orchestrator(config=MOCK_CONFIG, api_client=client, dry_run=True)
    
    is_valid, errors = orchestrator.validate_config()
    assert is_valid
    assert len(errors) == 0
    print("✓ test_orchestrator_validate_config_valid passed")


def test_orchestrator_validate_config_missing_repo():
    """Test config validation with missing repo URL."""
    config = {
        "pipeline": {"repository": {}},
        "stages": [{"name": "s1", "prompt": "test"}],
    }
    
    orchestrator = Orchestrator(config=config, dry_run=True)
    is_valid, errors = orchestrator.validate_config()
    
    assert not is_valid
    assert any("repository.url" in e for e in errors)
    print("✓ test_orchestrator_validate_config_missing_repo passed")


def test_orchestrator_validate_config_bad_dependency():
    """Test config validation with invalid stage dependency."""
    config = {
        "pipeline": {"repository": {"url": "https://github.com/test/repo"}},
        "stages": [
            {"name": "s1", "prompt": "test", "depends_on": "nonexistent"},
        ],
    }
    
    orchestrator = Orchestrator(config=config, dry_run=True)
    is_valid, errors = orchestrator.validate_config()
    
    assert not is_valid
    assert any("nonexistent" in e for e in errors)
    print("✓ test_orchestrator_validate_config_bad_dependency passed")


def test_orchestrator_validate_models():
    """Test model validation through orchestrator."""
    mock_http = MockHttpClient()
    mock_http.add_response("GET", "/models", 200, MOCK_MODELS_RESPONSE)
    
    client = CursorAPIClient(api_key="test-key", http_client=mock_http)
    orchestrator = Orchestrator(config=MOCK_CONFIG, api_client=client, dry_run=True)
    
    is_valid, errors = orchestrator.validate_models()
    assert is_valid
    assert len(errors) == 0
    print("✓ test_orchestrator_validate_models passed")


def test_orchestrator_validate_models_invalid():
    """Test model validation with invalid model."""
    mock_http = MockHttpClient()
    mock_http.add_response("GET", "/models", 200, MOCK_MODELS_RESPONSE)
    
    config = dict(MOCK_CONFIG)
    config["stages"] = [
        {
            "name": "stage1",
            "display_name": "Test",
            "model_id": "invalid-model",
            "prompt": "test",
        }
    ]
    
    client = CursorAPIClient(api_key="test-key", http_client=mock_http)
    orchestrator = Orchestrator(config=config, api_client=client, dry_run=True)
    
    is_valid, errors = orchestrator.validate_models()
    assert not is_valid
    assert any("invalid-model" in e for e in errors)
    print("✓ test_orchestrator_validate_models_invalid passed")


def test_orchestrator_dry_run():
    """Test that dry run doesn't make API calls to create agents."""
    mock_http = MockHttpClient()
    mock_http.add_response("GET", "/models", 200, MOCK_MODELS_RESPONSE)
    
    config_no_deps = {
        "pipeline": MOCK_CONFIG["pipeline"],
        "api": MOCK_CONFIG["api"],
        "stages": [
            {
                "name": "stage1",
                "display_name": "Test Stage 1",
                "model_id": "composer-2",
                "receives_model_setup_document": True,
                "prompt": "Do something",
                "iteration": MINIMAL_ITERATION,
            },
        ],
    }
    
    client = CursorAPIClient(api_key="test-key", http_client=mock_http)
    with tempfile.TemporaryDirectory() as tmp:
        document_path = write_document(directory=Path(tmp), file_name="setup.qmd")
        orchestrator = Orchestrator(
            config=config_no_deps,
            api_client=client,
            dry_run=True,
            ask=answer_with(str(document_path)),
        )
        orchestrator.start_new_project()
        manifest = orchestrator.run_pipeline()
    
    assert manifest is not None
    assert len(manifest.stages) == 1
    
    assert mock_http.calls == [], "A dry run sends no request at all"
    assert manifest.stages["stage1"].status == STAGE_DRY_RUN
    print("✓ test_orchestrator_dry_run passed")


def test_pipeline_manifest_serialization():
    """A manifest survives a round trip; another format or an unknown field is refused."""
    manifest = PipelineManifest(
        pipeline_name="test",
        repo_url="https://github.com/test/repo",
        started_at="2026-11-24T00:00:00Z",
        project_id="prj-000000000000",
        provenance={"created_by": "execute", "config_sha256": "c" * 64, "code": {}, "repins": []},
        source_ref="main",
        source_commit="a" * 40,
        previous_rounds=[{"name": "stage1", "round": 1}],
        round_limit_raises={"stage1": 1},
    )
    manifest.stages["stage1"] = StageResult(
        name="stage1",
        status=STAGE_COMPLETE,
        round=2,
        kind=KIND_REVISE,
        agent_id="bc-123",
        input_commit="a" * 40,
        output_branch="cursor/test-a1b2",
        output_commit="b" * 40,
    )
    
    data = json.loads(json.dumps(manifest.to_dict()))
    assert data["format_version"] == MANIFEST_FORMAT_VERSION == 4
    assert data["stages"]["stage1"]["status"] == STAGE_COMPLETE
    
    restored = PipelineManifest.from_dict(data)
    assert restored == manifest
    
    # A manifest of an earlier version is refused, not converted: it does
    # not hold the signed records this version checks.
    for broken in (
        {**data, "format_version": 3},
        {**data, "format_version": 1},
        {key: value for key, value in data.items() if key != "format_version"},
        {**data, "stages": {"stage1": {**data["stages"]["stage1"], "input_ref": "main"}}},
        {**data, "stages": {"stage1": {**data["stages"]["stage1"], "attempt": 1}}},
    ):
        try:
            PipelineManifest.from_dict(broken)
        except ProjectStateError:
            pass
        else:
            raise AssertionError("A manifest this orchestrator cannot read must be refused")
    print("✓ test_pipeline_manifest_serialization passed")


def test_stage_input_is_the_recorded_output_commit_of_its_dependency():
    """A later stage starts from the commit recorded for the earlier stage, not its branch."""
    mock_http = MockHttpClient()
    client = CursorAPIClient(api_key="test-key", http_client=mock_http)
    orchestrator = Orchestrator(config=MOCK_CONFIG, api_client=client, dry_run=True)
    
    orchestrator.manifest = PipelineManifest(
        pipeline_name="test",
        repo_url="https://github.com/test/repo",
        started_at="2026-11-24T00:00:00Z",
        source_ref="main",
    )
    orchestrator.manifest.stages["stage1"] = StageResult(
        name="stage1",
        status=STAGE_COMPLETE,
        output_branch="cursor/stage1-output",
        output_commit="c" * 40,
    )
    
    stage2 = orchestrator.stages[1]
    source, commit = orchestrator._resolve_input(
        stage=stage2, result=StageResult(name=stage2.name, round=1, kind=KIND_INITIAL),
    )
    assert commit == "c" * 40
    assert "stage1" in source
    
    # A round that revises an earlier round starts from the commit recorded for it.
    source, commit = orchestrator._resolve_input(
        stage=stage2,
        result=StageResult(
            name=stage2.name, round=2, kind=KIND_REVISE,
            revision={"replaces_round": 1, "base_branch": "cursor/stage2", "base_commit": "d" * 40},
        ),
    )
    assert commit == "d" * 40
    assert "round 1" in source
    assert mock_http.calls == [], "The recorded commit is used; the branch is not read again"
    print("✓ test_stage_input_is_the_recorded_output_commit_of_its_dependency passed")


def read_design_doc_guardrails() -> list[str]:
    """Return the numbered items under the Stage 3 guardrails heading in the design doc."""
    lines = DESIGN_DOC_PATH.read_text(encoding="utf-8").splitlines()
    section_start = lines.index(DESIGN_DOC_GUARDRAILS_HEADING) + 1
    guardrails = []
    for line in lines[section_start:]:
        if line.startswith("#"):
            break
        numbered_item = re.match(r"^\d+\.\s+(.*\S)\s*$", line)
        if numbered_item:
            guardrails.append(numbered_item.group(1))
    return guardrails


def must_not_ask(question: str) -> str:
    raise AssertionError(f"The orchestrator asked a question it must not ask: {question}")


def answer_with(*answers: str) -> Callable[[str], str]:
    """Return an `ask` stand-in that gives the answers in order and records the questions."""
    remaining = list(answers)
    
    def ask(question: str) -> str:
        ask.questions.append(question)
        return remaining.pop(0)
    
    ask.questions = []
    return ask


def write_document(
    directory: Path,
    file_name: str,
    text: str = SENTINEL_DOCUMENT_TEXT,
) -> Path:
    """Write a stand-in model setup document and return its path."""
    path = directory / file_name
    path.write_bytes(text.encode("utf-8"))
    return path


def must_not_sleep(seconds: float) -> None:
    raise AssertionError(f"The orchestrator waited {seconds}s where it must not wait")


def must_not_download(url: str) -> tuple[int, bytes]:
    raise AssertionError(f"The orchestrator downloaded {url} where it must not download")


# Projects of tests that launch (fake) agents keep their manifest files here.
TEST_PROJECTS_DIR = tempfile.TemporaryDirectory()


def new_manifest_path() -> Path:
    """Return a manifest path in a fresh folder of its own."""
    return Path(tempfile.mkdtemp(dir=TEST_PROJECTS_DIR.name)) / "manifest.json"


class FakeLocalReview:
    """Stand-in for the local Git checkout that shows outputs for review.

    It runs no Git command. It keeps which branch and commit are "checked
    out" and what was asked of it. A test changes its behaviour through
    `problem` (the checkout cannot be used) and `changed_files`
    (uncommitted changes to tracked files).
    """
    
    def __init__(self):
        self.path = Path("stand-in-checkout")
        self.remote = "origin"
        self.branch: Optional[str] = "main"
        self.commit = "0" * 40
        self.changed_files: list[str] = []
        self.problem: Optional[str] = None
        self.shown: list[tuple[str, str]] = []
        self.ready_checks = 0
    
    def check_ready(self) -> None:
        self.ready_checks += 1
        if self.problem:
            raise LocalSyncError(self.problem)
        if self.changed_files:
            raise LocalSyncError(f"uncommitted changes: {', '.join(self.changed_files)}")
    
    def state(self) -> dict:
        return {
            "commit": self.commit,
            "branch": self.branch,
            "changed_files": list(self.changed_files),
        }
    
    def show_commit(self, branch: str, commit: str, review_files: list[str]) -> dict:
        self.check_ready()
        previous_branch, previous_commit = self.branch, self.commit
        self.branch, self.commit = branch, commit
        self.shown.append((branch, commit))
        return {
            "verified": True,
            "synced_at": "2026-11-24T00:00:00+00:00",
            "checkout": str(self.path),
            "remote": self.remote,
            "branch": branch,
            "commit": commit,
            "previous_branch": previous_branch,
            "previous_commit": previous_commit,
            "review_files": [str(self.path / path) for path in review_files],
        }


def load_real_orchestrator(
    mock_http: Any,
    dry_run: bool,
    ask: Callable[[str], str] = must_not_ask,
    manifest_path: Optional[Path] = None,
) -> Orchestrator:
    """Build an orchestrator from the committed config.yaml with mocked HTTP.

    Args:
        mock_http: A FakeServices (for tests that launch) or a MockHttpClient.
        manifest_path: The project's manifest file. A run that may launch
            gets a fresh one if none is given.

    The local checkout is the stand-in of the FakeServices (one checkout
    for all the orchestrators that use the same services, as on one
    machine), or a fresh stand-in. With a FakeServices the config is the
    one it was made with: the committed config, or a changed copy of it
    (see make_changed_mock).
    """
    is_fake_services = isinstance(mock_http, FakeServices)
    config = (
        copy.deepcopy(mock_http.config) if is_fake_services
        else load_config(config_path=REAL_CONFIG_PATH)
    )
    client = CursorAPIClient(
        api_key="test-key",
        http_client=mock_http,
        download_client=mock_http.download if is_fake_services else must_not_download,
    )
    if manifest_path is None and not dry_run:
        manifest_path = new_manifest_path()
    return Orchestrator(
        config=config,
        api_client=client,
        github=GitHubRepoReader(
            repo_url=config["pipeline"]["repository"]["url"], http_client=mock_http,
        ),
        dry_run=dry_run,
        ask=ask,
        sleep=mock_http.sleep if is_fake_services else must_not_sleep,
        manifest_path=manifest_path,
        local_review=mock_http.local_review if is_fake_services else FakeLocalReview(),
        bridge_secret=TEST_BRIDGE_SECRET,
    )


def run_cli(
    backend: "FakeServices",
    arguments: list[str],
    ask: Callable[[str], str] = must_not_ask,
    api_key: Optional[str] = "test-key",
) -> int:
    """Run the command line interface against the stand-in services."""
    return main(
        argv=arguments,
        api_client=CursorAPIClient(
            api_key=api_key, http_client=backend, download_client=backend.download,
        ),
        github=GitHubRepoReader(repo_url=backend.repo_url, http_client=backend),
        ask=ask,
        sleep=backend.sleep,
        local_review=backend.local_review,
    )


def get_stage(orchestrator: Orchestrator, name: str) -> StageConfig:
    matches = [stage for stage in orchestrator.stages if stage.name == name]
    assert len(matches) == 1, f"Expected exactly one stage named '{name}'"
    return matches[0]


def test_stage3_guardrails_in_config_match_design_doc():
    """config.yaml lists the same twelve guardrails, in order, as the design doc."""
    doc_guardrails = read_design_doc_guardrails()
    assert len(doc_guardrails) == EXPECTED_GUARDRAIL_COUNT, doc_guardrails
    
    orchestrator = load_real_orchestrator(mock_http=MockHttpClient(), dry_run=True)
    stage3 = get_stage(orchestrator=orchestrator, name=STAGE3_NAME)
    
    assert stage3.guardrails == doc_guardrails
    
    is_valid, errors = orchestrator.validate_config()
    assert is_valid, errors
    print("✓ test_stage3_guardrails_in_config_match_design_doc passed")


def test_stage3_launch_request_carries_all_guardrails():
    """The POST /v1/agents body for Stage 3 holds the task and all twelve guardrails."""
    mock_http = make_launch_mock()
    orchestrator = load_real_orchestrator(mock_http=mock_http, dry_run=False)
    open_project_by_hand(
        orchestrator=orchestrator, backend=mock_http, completed=(STAGE1_NAME, STAGE2_NAME),
    )
    stage3 = get_stage(orchestrator=orchestrator, name=STAGE3_NAME)
    
    result = run_stage(orchestrator=orchestrator, stage=stage3)
    assert result.status == STAGE_AWAITING_REVIEW
    
    launch_requests = [call for call in mock_http.calls if call[0] == "POST"]
    assert len(launch_requests) == 1
    prompt_text = launch_requests[0][3]["prompt"]["text"]
    
    assert "TASK: Translate the pseudocode into Python code" in prompt_text
    assert GUARDRAILS_HEADING in prompt_text
    doc_guardrails = read_design_doc_guardrails()
    assert len(doc_guardrails) == EXPECTED_GUARDRAIL_COUNT
    for number, guardrail in enumerate(doc_guardrails, start=1):
        assert f"{number}. {guardrail}" in prompt_text, f"Missing guardrail {number}"
    print("✓ test_stage3_launch_request_carries_all_guardrails passed")


def test_stage_without_guardrails_sends_prompt_unchanged():
    """A stage with no guardrails sends exactly its configured prompt."""
    stage = StageConfig(
        name="plain",
        display_name="Plain",
        description="",
        prompt="Do the task.\n",
    )
    assert build_stage_prompt(stage=stage) == "Do the task.\n"
    print("✓ test_stage_without_guardrails_sends_prompt_unchanged passed")


def test_validate_config_rejects_malformed_guardrails():
    """Guardrails must be a list of non-empty strings."""
    for malformed in (None, "one string", ["ok", ""], ["ok", 3]):
        config = {
            "pipeline": {"repository": {"url": "https://github.com/test/repo"}},
            "stages": [{"name": "s1", "prompt": "test", "guardrails": malformed}],
        }
        orchestrator = Orchestrator(config=config, dry_run=True)
        is_valid, errors = orchestrator.validate_config()
        assert not is_valid, f"Expected rejection for guardrails={malformed!r}"
        assert any("guardrails" in error for error in errors)
    print("✓ test_validate_config_rejects_malformed_guardrails passed")


# Scripted outcomes for FakeServices.script().
NO_RESPONSE = "no response: the request never reached the service"
LOST_RESPONSE = "lost response: the request took effect, but no response came back"

# Matches GET /v1/agents/{id} (the lookup of one agent) and nothing longer.
AGENT_LOOKUP = re.compile(r"/agents/bc-[0-9a-f-]{36}$")
AGENT_ID_PATTERN = re.compile(
    r"^bc-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# The name the orchestrator gives each agent: stage, round and, for a
# reviewer's launch, which of its two parts it is.
AGENT_NAME_PATTERN = re.compile(
    r"^Pipeline: (?P<stage>.+) \(round (?P<round>\d+)(?:, (?P<part>blind review|comparison))?\)$"
)
PHASE_BY_AGENT_NAME_PART = {None: PHASE_PROPOSE, "blind review": PHASE_BLIND, "comparison": PHASE_COMPARE}


def text_between(text: str, begin_line: str, end_line: str) -> str:
    """Return the text between the first pair of two marker lines."""
    start = text.index(begin_line) + len(begin_line)
    return text[start:text.index(end_line, start)].strip("\n")


def stand_in_evidence(
    stage: dict, round_number: int, phase: Optional[str], approved_spec: Optional[bytes],
) -> dict[str, bytes]:
    """Return the evidence files a stand-in agent writes for a stage.

    The files have the form the stage's evidence check asks for, and
    refer to the stand-in sections and code that FakeServices writes.
    They hold no derivation, no trace, no test result and no computation.
    """
    settings = stage["verification"]
    folder = settings["evidence_file"].rsplit("/", 1)[0]
    files: dict[str, bytes] = {}
    evidence: dict = {
        "stage": stage["name"],
        "round": round_number,
        "attempt": {"tried": "stand-in: nothing was done", "result": "stand-in: nothing was found"},
        "no_plausible_alternative": False,
    }
    kind = settings["kind"]
    if kind == verification.KIND_EQUILIBRIUM_AUDIT:
        files[f"{folder}/check.py"] = b"# stand-in: checks nothing\n"
        files[f"{folder}/check.log"] = b"stand-in: nothing was checked\n"
        evidence.update({
            "equations": [{
                "label": "E1", "source": "stand-in", "status": "exact",
                "check": {"method": "stand-in", "result": "pass"},
            }],
            "unknowns": ["x"],
            "residuals": ["E1"],
            "closures": [],
            "order_consistency": [{"item": "stand-in", "status": "consistent"}],
            "symbolic_checks": {
                "script": f"{folder}/check.py", "log": f"{folder}/check.log",
                "passed": 1, "failed": 0,
            },
        })
    elif kind == verification.KIND_TRACEABILITY:
        evidence.update({
            "conditions": [
                {"label": "E1", "kind": "equilibrium condition", "steps": ["P1"]},
                {"label": "x", "kind": "unknown", "steps": ["P1"]},
            ],
            "steps": [{"id": "P1", "origins": ["E1", "x"]}],
            "solve_blocks": [{"id": "B1", "unknowns": ["x"], "residuals": ["E1"]}],
            "open_choices": [],
        })
    elif kind == verification.KIND_TEST_MAP:
        files[f"{folder}/test_output.txt"] = b"stand-in: no test was run\n"
        evidence.update({
            "steps": [{"id": "P1", "functions": ["solve"], "tests": ["test_solve"]}],
            "tolerances": [{"name": "residual", "config_key": "solver.residual_tolerance"}],
            "residual_checks": [{"name": "E1", "tolerance": "residual", "test": "test_solve"}],
            "admissibility_checks": [{"name": "x > 0", "tolerance": "residual", "test": "test_solve"}],
            "test_run": {
                "command": "stand-in (nothing was run)", "exit_code": 0, "passed": 1,
                "failed": 0, "skipped": [], "log": f"{folder}/test_output.txt",
            },
            "deviations": [],
        })
    elif phase == verification.PHASE_DOMAIN_PROPOSAL:
        files[settings["next_batch_spec"]] = json.dumps({
            "domain": {"beta": {
                "min": 0.9, "max": 0.99, "origin": "illustrative", "reason": "stand-in",
            }},
            "max_configurations": 2,
            "seeds": [1],
            "solver": {"residual_tolerance": 1e-8, "max_iterations": 100},
        }).encode("utf-8")
        evidence.update({"phase": phase, "configurations": [], "boundary_claims": []})
    else:
        batch = f"{folder}/batch_{round_number:02d}"
        files[f"{batch}/batch_spec.json"] = approved_spec
        configurations = []
        for name, beta, classification in (
            ("c1", 0.95, "admissible"), ("c2", 0.97, "inadmissible"),
        ):
            files[f"{batch}/{name}_parameters.json"] = json.dumps(
                {"parameters": {"beta": beta}},
            ).encode("utf-8")
            files[f"{batch}/{name}_results.json"] = b"{}\n"
            configurations.append({
                "id": name,
                "parameters_file": f"{batch}/{name}_parameters.json",
                "results_file": f"{batch}/{name}_results.json",
                "classification": classification,
                **({"violated_condition": "x > 0"} if classification == "inadmissible" else {}),
            })
        evidence.update({
            "phase": phase,
            "approved_spec_sha256": hashlib.sha256(approved_spec).hexdigest(),
            "batch_spec_file": f"{batch}/batch_spec.json",
            "configurations": configurations,
            "boundary_claims": [],
        })
    files[settings["evidence_file"]] = json.dumps(evidence, indent=2).encode("utf-8")
    return files


class FakeServices:
    """Stand-in for the three services the orchestrator talks to.

    - the Cursor Cloud Agents API (models, agents, runs, artifacts),
    - the GitHub REST API (branch heads, commits, files at a commit, the
      comparison of two commits),
    - the host that serves presigned artifact download URLs.

    The request and response shapes follow the public documentation of the
    two APIs. They are NOT recordings of the live services.

    It keeps the state a launch would create. An agent launched from a commit
    pushes one new commit to a new branch. What that commit holds depends on
    the launch, which the stand-in reads from the agent's name and prompt:
    - a proposer: the files of the starting commit plus the stand-in output
      of its stage (required files, its section of the working document, its
      evidence files, the trial log entries it was given and its own entry);
    - a reviewer's blind assessment: one new file;
    - a reviewer's comparison: the candidate merged in, the comparison file
      and the trial log entries.
    The stand-in output has the required form and no content: it is not a
    derivation, code, a result or a review.
    Like the documented API, a second launch request with an agent ID
    already in use is answered with 409 agent_id_conflict and creates nothing.

    Tests change this behaviour through the attributes set in __init__ and
    through script(). Nothing here uses the network.
    """
    
    def __init__(self, config: dict, models_listing: dict):
        self.config = config
        self.repo_url = config["pipeline"]["repository"]["url"]
        self.owner, self.repo = parse_github_repo_url(self.repo_url)
        self.models_listing = models_listing
        self.stage_by_display_name = {
            stage.get("display_name", stage["name"]): stage for stage in config["stages"]
        }
        self.trial_log_path = (config.get("trial_log") or {}).get("path")
        # The one local checkout of the machine these services are used from.
        self.local_review = FakeLocalReview()
        
        # What the orchestrator did.
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.downloads: list[str] = []
        self.sleeps: list[float] = []
        
        # State of the stand-in repository and API. The starting branch
        # holds the committed copy of the stand-in model setup document.
        self.branches: dict[str, str] = {}
        self.files: dict[str, dict[str, bytes]] = {}
        self.parents: dict[str, list[str]] = {}
        self.agents: dict[str, dict] = {}
        self.add_commit(
            files={SOURCE_DOCUMENT_PATH: SENTINEL_DOCUMENT_TEXT.encode("utf-8")}, branch="main",
        )
        
        # Behaviour a test can change.
        self.withheld_files: set[str] = set()   # files agents do not write
        # How agents break the working document's rules: None, or one of
        # "no_section", "empty_section", "later_marker", "earlier_changed",
        # "source_changed".
        self.working_document_fault: Optional[str] = None
        # Called with (stage name, the evidence object); what it returns is
        # written as the stage's evidence file (a str is written as it is).
        self.edit_evidence: Optional[Callable[[str, dict], Any]] = None
        # Further files every proposer writes: path -> bytes.
        self.extra_files: dict[str, bytes] = {}
        # How agents break the trial log's rules: None, or one of
        # "rewritten", "entries_left_out", "no_own_entry", "bad_disposition".
        self.trial_log_fault: Optional[str] = None
        # What the reviewers' comparisons say, in turn (the last one repeats).
        self.review_verdicts: list[dict] = [{"verdict": "accept", "agreement": "agree"}]
        # How a reviewer breaks its rules: None, "blind_writes_more" or
        # "comparison_edits_candidate".
        self.review_fault: Optional[str] = None
        self.agents_report_branch = True         # False: runs report no pushed branch
        self.agents_push_commit = True           # False: the branch stays at the input commit
        self.run_statuses: dict[str, list[str]] = {}   # stage -> statuses reported in turn
        self.artifacts: dict[str, dict[str, bytes]] = {}   # stage -> artifact path -> bytes
        self.download_scheme = "https"
        self.before_launch: Optional[Callable[[dict], None]] = None
        self.scripted: list[tuple[str, Any, Any]] = []
    
    def add_commit(
        self,
        files: Any,
        parent: Optional[str] = None,
        branch: Optional[str] = None,
        removed: tuple[str, ...] = (),
        merged: Optional[str] = None,
    ) -> str:
        """Add a commit holding the parent's files plus `files`, minus `removed`.

        `files` is a list of paths (each gets stand-in content) or a dict
        of path -> bytes. `merged` is a second parent whose files are
        taken over, as a merge of it would.
        """
        if not isinstance(files, dict):
            files = {path: f"Stand-in content of {path}.\n".encode("utf-8") for path in files}
        commit = hashlib.sha1(f"fake commit {len(self.files)}".encode()).hexdigest()
        self.files[commit] = {
            path: content
            for path, content in {
                **self.files.get(parent, {}), **self.files.get(merged, {}), **files,
            }.items()
            if path not in removed
        }
        self.parents[commit] = [each for each in (parent, merged) if each is not None]
        if branch is not None:
            self.branches[branch] = commit
        return commit
    
    def ancestors(self, commit: str) -> set[str]:
        """The commit and every commit it builds on."""
        found, queue = set(), [commit]
        while queue:
            current = queue.pop()
            if current not in found:
                found.add(current)
                queue.extend(self.parents.get(current, []))
        return found
    
    def trial_log_after(self, before: Optional[bytes], prompt_text: str) -> bytes:
        """The trial log as an agent leaves it: the given entries and its own entry appended."""
        entries = text_between(prompt_text, LOG_ENTRIES_BEGIN_LINE, LOG_ENTRIES_END_LINE)
        own = re.sub(
            r"(?m)^- (Tried|Evidence|Result|Rationale|Next route): <.*>$",
            r"- \1: stand-in statement",
            text_between(prompt_text, OWN_ENTRY_BEGIN_LINE, OWN_ENTRY_END_LINE),
        )
        disposition = "PERHAPS" if self.trial_log_fault == "bad_disposition" else "UNRESOLVED"
        own = re.sub(r"(?m)^- Disposition: <.*>$", f"- Disposition: {disposition}", own)
        start = before if before is not None else b"# Stand-in trial log\n"
        if self.trial_log_fault == "rewritten":
            start = b"# Rewritten trial log\n"
        if self.trial_log_fault == "entries_left_out":
            entries = ""
        if self.trial_log_fault == "no_own_entry":
            own = ""
        return start + f"\n{entries}\n\n{own}\n".encode("utf-8")
    
    def stage_output_files(
        self,
        stage: dict,
        start_commit: str,
        prompt_text: Optional[str] = None,
        round_number: int = 1,
    ) -> dict[str, bytes]:
        """Return the stand-in output of a stage's proposer that starts from a commit.

        Without a prompt that holds the trial log's rules (a commit a test
        makes by hand) the trial log is left as it is, and a stage that
        runs parameter batches writes a domain proposal.
        """
        start_files = self.files[start_commit]
        output_manifest = stage.get("output_manifest", {})
        files = {
            path: f"Stand-in content of {path}.\n".encode("utf-8")
            for path in output_manifest.get("required_files", [])
        }
        settings = stage.get("verification")
        if settings and settings["kind"] == verification.KIND_TEST_MAP:
            files[settings["implementation_file"]] = b"def solve():\n    return 0\n"
            files[settings["test_file"]] = b"def test_solve():\n    assert solve() == 0\n"
        
        number = self.config["stages"].index(stage) + 1
        if "working_document" in self.config:
            begin, end = stage_section_markers(number=number, name=stage["name"])
            text = start_files.get(
                WORKING_DOCUMENT_PATH, b"# Stand-in working document\n",
            ).decode("utf-8")
            attempt = 1 + sum(
                agent["stage"] == stage["name"] and agent["phase"] == PHASE_PROPOSE
                for agent in self.agents.values()
            )
            section = (
                f"{begin}\nStand-in section of stage {number}, attempt {attempt}.\n"
                f"$$x - 1 = 0 \\tag{{E1}}$$\nStep P1: stand-in step.\n{end}\n"
            )
            fault = self.working_document_fault
            if fault == "no_section":
                section = ""
            elif fault == "empty_section":
                section = f"{begin}\n\n{end}\n"
            if begin in text:
                # A later round: the section is already there and is replaced.
                text = text[:text.index(begin)] + section + text[text.index(end) + len(end) + 1:]
            else:
                text += f"\n{section}"
            if fault == "later_marker":
                later = self.config["stages"][number]
                text += "\n" + stage_section_markers(number=number + 1, name=later["name"])[0] + "\n"
            elif fault == "earlier_changed":
                text = text.replace("Stand-in section of stage 1", "Changed section of stage 1")
            elif fault == "source_changed":
                files[SOURCE_DOCUMENT_PATH] = start_files[SOURCE_DOCUMENT_PATH] + b"An added line.\n"
            files[WORKING_DOCUMENT_PATH] = text.encode("utf-8")
        
        if settings:
            phase = None
            if settings["kind"] == verification.KIND_PARAMETER_BATCHES:
                phase = (
                    verification.PHASE_BATCH
                    if prompt_text and "THIS ROUND RUNS ONE APPROVED BATCH" in prompt_text
                    else verification.PHASE_DOMAIN_PROPOSAL
                )
            evidence_files = stand_in_evidence(
                stage=stage, round_number=round_number, phase=phase,
                approved_spec=start_files.get(settings.get("next_batch_spec")),
            )
            if self.edit_evidence is not None:
                path = settings["evidence_file"]
                edited = self.edit_evidence(stage["name"], json.loads(evidence_files[path]))
                evidence_files[path] = (
                    edited if isinstance(edited, str) else json.dumps(edited)
                ).encode("utf-8")
            files.update(evidence_files)
        files.update(self.extra_files)
        if self.trial_log_path and prompt_text and LOG_ENTRIES_BEGIN_LINE in prompt_text:
            files[self.trial_log_path] = self.trial_log_after(
                before=start_files.get(self.trial_log_path), prompt_text=prompt_text,
            )
        return {path: content for path, content in files.items() if path not in self.withheld_files}
    
    def blind_review_files(self, prompt_text: str) -> dict[str, bytes]:
        """What a reviewer's first launch writes: the blind assessment and nothing else."""
        path = re.search(r"Add only the file (\S+)\. Change no other file", prompt_text).group(1)
        files = {path: b"Stand-in blind assessment: nothing was assessed.\n"}
        if self.review_fault == "blind_writes_more":
            files["outputs/unasked_file.txt"] = b"not asked for\n"
        return files
    
    def comparison_files(self, prompt_text: str, candidate: str) -> dict[str, bytes]:
        """What a reviewer's second launch writes on top of the candidate it merged in."""
        path = re.search(r"3\. Write (\S+), a JSON object", prompt_text).group(1)
        said = self.review_verdicts.pop(0) if len(self.review_verdicts) > 1 else self.review_verdicts[0]
        comparison = {
            "candidate_commit": candidate,
            "candidate_files": json.loads(
                re.search(r'"candidate_files": (\{.*\})\n', prompt_text).group(1)
            ),
            "blind_commit": re.search(r'"blind_commit": "([0-9a-f]{40})"', prompt_text).group(1),
            "blind_assessment_sha256": re.search(
                r'"blind_assessment_sha256": "([0-9a-f]{64})"', prompt_text,
            ).group(1),
            "differences": (
                [] if said["agreement"] == "agree" and said["verdict"] == "accept" else
                [{
                    "item": "stand-in difference",
                    "severity": "minor" if said["verdict"] == "accept" else "blocking",
                    "detail": "stand-in",
                }]
            ),
            "plausible_alternative": True,
            "rationale": "stand-in rationale: nothing was compared",
            **said,
        }
        files = {path: json.dumps(comparison, indent=2).encode("utf-8")}
        if self.review_fault == "comparison_edits_candidate":
            files[WORKING_DOCUMENT_PATH] = self.files[candidate][WORKING_DOCUMENT_PATH] + b"edited\n"
        if self.trial_log_path:
            files[self.trial_log_path] = self.trial_log_after(
                before=self.files[candidate].get(self.trial_log_path), prompt_text=prompt_text,
            )
        return files
    
    def script(self, method: str, url_part: Any, *outcomes: Any) -> None:
        """Give the next matching requests these outcomes, in order.

        An outcome is NO_RESPONSE, LOST_RESPONSE or a (status, body) pair.
        `url_part` is a substring of the URL or a compiled pattern. Use the
        method "DOWNLOAD" for requests to the download host.
        """
        self.scripted.extend((method, url_part, outcome) for outcome in outcomes)
    
    def _take_scripted(self, method: str, url: str) -> Any:
        for index, (scripted_method, url_part, outcome) in enumerate(self.scripted):
            matches = (
                url_part.search(url) if isinstance(url_part, re.Pattern) else url_part in url
            )
            if scripted_method == method and matches:
                del self.scripted[index]
                return outcome
        return None
    
    def __call__(
        self,
        method: str,
        url: str,
        headers: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> tuple[int, Any]:
        self.calls.append((method, url, headers or {}, json_body or {}))
        if method == "POST" and self.before_launch is not None:
            self.before_launch(json_body)
        outcome = self._take_scripted(method=method, url=url)
        if outcome == NO_RESPONSE:
            raise TransportError("test: no response")
        if outcome == LOST_RESPONSE:
            self._respond(method=method, url=url, json_body=json_body)
            raise TransportError("test: the response was lost")
        if outcome is not None:
            return outcome
        return self._respond(method=method, url=url, json_body=json_body)
    
    def download(self, url: str) -> tuple[int, bytes]:
        self.downloads.append(url)
        outcome = self._take_scripted(method="DOWNLOAD", url=url)
        if outcome == NO_RESPONSE:
            raise TransportError("test: no response")
        if outcome is not None:
            return outcome
        agent_id, quoted_path = urlsplit(url).path.strip("/").split("/", 1)
        return 200, self.artifacts[self.agents[agent_id]["stage"]][unquote(quoted_path)]
    
    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
    
    def _respond(self, method: str, url: str, json_body: Optional[dict]) -> tuple[int, Any]:
        parts = urlsplit(url)
        segments = [unquote(segment) for segment in parts.path.strip("/").split("/")]
        query = parse_qs(parts.query)
        if parts.netloc == "api.github.com":
            assert method == "GET", "The orchestrator must only read from GitHub"
            return self._respond_github(segments=segments, query=query)
        if parts.netloc == "api.cursor.com":
            return self._respond_cursor(
                method=method, segments=segments[1:], query=query, json_body=json_body,
            )
        raise AssertionError(f"Request to an unexpected host: {method} {url}")
    
    def _respond_github(self, segments: list[str], query: dict) -> tuple[int, Any]:
        assert segments[:3] == ["repos", self.owner, self.repo], segments
        rest = segments[3:]
        not_found = (404, {"message": "Not Found"})
        if rest[:3] == ["git", "ref", "heads"]:
            branch = "/".join(rest[3:])
            if branch not in self.branches:
                return not_found
            return 200, {
                "ref": f"refs/heads/{branch}",
                "object": {"sha": self.branches[branch], "type": "commit"},
            }
        if rest[:2] == ["git", "commits"] and len(rest) == 3:
            return (200, {"sha": rest[2]}) if rest[2] in self.files else not_found
        if rest[:1] == ["compare"] and len(rest) == 2:
            base, _, head = rest[1].partition("...")
            if base not in self.files or head not in self.files:
                return not_found
            return 200, self._comparison(base=base, head=head)
        if rest[:1] == ["contents"]:
            path = "/".join(rest[1:])
            files = self.files.get(query["ref"][0], {})
            if path in files:
                return 200, {
                    "type": "file",
                    "path": path,
                    "size": len(files[path]),
                    "encoding": "base64",
                    "content": base64.encodebytes(files[path]).decode("ascii"),
                }
            if any(file.startswith(path + "/") for file in files):
                return 200, [{"type": "file", "path": file} for file in sorted(files)]
            return not_found
        raise AssertionError(f"The stand-in has no answer for GitHub path {rest}")
    
    def _comparison(self, base: str, head: str) -> dict:
        """Answer GET /compare/{base}...{head} in the shape the GitHub docs give."""
        behind = self.ancestors(base) - self.ancestors(head)
        ahead = self.ancestors(head) - self.ancestors(base)
        status = (
            "identical" if base == head else
            "ahead" if not behind else
            "behind" if not ahead else
            "diverged"
        )
        # GitHub lists the files that differ between the common ancestor
        # and head. The stand-in's histories are simple enough that the
        # common ancestor's files are those `base` shares with `head`'s start.
        common = self.files[base] if status == "ahead" else self.files[
            next(iter(sorted(
                self.ancestors(base) & self.ancestors(head),
                key=lambda commit: -len(self.ancestors(commit)),
            )))
        ]
        changed = sorted(
            path for path in set(common) | set(self.files[head])
            if common.get(path) != self.files[head].get(path)
        ) if status in ("ahead", "diverged") else []
        return {
            "status": status,
            "ahead_by": len(ahead),
            "behind_by": len(behind),
            "files": [{"filename": path, "status": "modified"} for path in changed],
        }
    
    def _respond_cursor(
        self, method: str, segments: list[str], query: dict, json_body: Optional[dict],
    ) -> tuple[int, Any]:
        if (method, segments) == ("GET", ["models"]):
            return 200, self.models_listing
        if (method, segments) == ("POST", ["agents"]):
            return self._create_agent(body=json_body)
        if method == "GET" and segments[:1] == ["agents"] and len(segments) >= 2:
            agent = self.agents.get(segments[1])
            if agent is None:
                return 404, {"error": {"code": "not_found", "message": "Agent not found"}}
            rest = segments[2:]
            if rest == []:
                return 200, {
                    "id": agent["id"],
                    "name": agent["name"],
                    "status": "ACTIVE",
                    "url": agent["url"],
                    "latestRunId": agent["run_id"],
                }
            if rest == ["runs", agent["run_id"]]:
                return 200, self._run_body(agent=agent)
            if rest == ["artifacts"]:
                return 200, {"items": [
                    {"path": path, "sizeBytes": len(content), "updatedAt": "2026-11-24T00:00:00Z"}
                    for path, content in self.artifacts.get(agent["stage"], {}).items()
                ]}
            if rest == ["artifacts", "download"]:
                return 200, {
                    "url": (
                        f"{self.download_scheme}://downloads.test/{agent['id']}/"
                        f"{quote(query['path'][0], safe='')}"
                    ),
                    "expiresAt": "2026-11-24T00:15:00Z",
                }
        raise AssertionError(f"The stand-in has no answer for {method} {'/'.join(segments)}")
    
    def _create_agent(self, body: dict) -> tuple[int, Any]:
        agent_id = body["agentId"]
        assert AGENT_ID_PATTERN.fullmatch(agent_id), agent_id
        if agent_id in self.agents:
            return 409, {"error": {
                "code": "agent_id_conflict", "message": "An agent with this ID already exists",
            }}
        name = AGENT_NAME_PATTERN.fullmatch(body["name"])
        assert name, f"Unexpected agent name: {body['name']}"
        stage = self.stage_by_display_name[name["stage"]]
        phase = PHASE_BY_AGENT_NAME_PART[name["part"]]
        prompt_text = body["prompt"]["text"]
        starting_ref = body["repos"][0]["startingRef"]
        start_commit = self.branches.get(starting_ref, starting_ref)
        if start_commit not in self.files:
            return 400, {"error": {"code": "invalid_ref", "message": "Unknown starting ref"}}
        
        number = len(self.agents) + 1
        branch = f"cursor/{stage['name']}-{number:04x}"
        merged = None
        if phase == PHASE_PROPOSE:
            files = self.stage_output_files(
                stage=stage, start_commit=start_commit, prompt_text=prompt_text,
                round_number=int(name["round"]),
            )
        elif phase == PHASE_BLIND:
            files = self.blind_review_files(prompt_text=prompt_text)
        else:
            merged = re.search(r"git merge --no-edit ([0-9a-f]{40})", prompt_text).group(1)
            files = self.comparison_files(prompt_text=prompt_text, candidate=merged)
        self.agents[agent_id] = {
            "id": agent_id,
            "name": body["name"],
            "url": f"https://cursor.com/agents/{agent_id}",
            "run_id": f"run-{number:04d}",
            "stage": stage["name"],
            "phase": phase,
            "round": int(name["round"]),
            "model": body.get("model"),
            "branch": branch,
            "starting_ref": starting_ref,
            "statuses": list(self.run_statuses.get(stage["name"], ["FINISHED"])),
        }
        if self.agents_push_commit:
            self.add_commit(files=files, parent=start_commit, branch=branch, merged=merged)
        else:
            self.branches[branch] = start_commit
        agent = self.agents[agent_id]
        return 200, {
            "agent": {
                "id": agent_id, "name": agent["name"], "status": "ACTIVE", "url": agent["url"],
            },
            "run": {"id": agent["run_id"], "agentId": agent_id, "status": "CREATING"},
        }
    
    def _run_body(self, agent: dict) -> dict:
        statuses = agent["statuses"]
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        branches = (
            [{"repoUrl": f"github.com/{self.owner}/{self.repo}", "branch": agent["branch"]}]
            if self.agents_report_branch else []
        )
        return {
            "id": agent["run_id"],
            "agentId": agent["id"],
            "status": status,
            "durationMs": 60000,
            "text": "Stand-in run text.",
            "git": {"branches": branches},
        }


class FakeHost:
    """Offline stand-in for the host of the project's main chat.

    The real host is outside this repository. This stand-in does what the
    review bridge asks of a host, with the same functions (HostReceiver,
    build_ack, build_decision), and posts nothing anywhere: `posted` is
    the list of notices it would have posted.
    """
    
    def __init__(self, secret: str = TEST_BRIDGE_SECRET):
        self.secret = secret
        self.receiver = HostReceiver(secret=secret)
        self.posted: list[dict] = []
        self.decisions = 0
    
    def bridge_of(self, manifest_path: Path) -> ReviewBridge:
        """The outbox and inbox of the project whose manifest is at a path."""
        return ReviewBridge(
            directory=manifest_path.resolve().parent / "review_bridge" / manifest_path.stem,
            secret=self.secret,
        )
    
    def post_new_notices(self, manifest_path: Path) -> list[dict]:
        """'Post' every notice in the outbox that was not posted before, and write its ack."""
        bridge = self.bridge_of(manifest_path=manifest_path)
        new = []
        for path in sorted(bridge.outbox.glob("*.json")):
            notice = read_json(path=path)
            if not self.receiver.receive(notice=notice):
                continue
            self.posted.append(notice)
            new.append(notice)
            write_json_once(
                path=bridge.ack_path(event_id=notice["event_id"]),
                record=build_ack(
                    notice=notice, secret=self.secret,
                    message_ref=f"main-chat-message-{len(self.posted)}",
                    posted_at="2026-11-24T00:00:00+00:00",
                ),
            )
        return new
    
    def write_decision(
        self,
        manifest_path: Path,
        notice: dict,
        action: str,
        params: Optional[dict] = None,
        owner_quote: str = "stand-in for the owner's written message",
    ) -> Path:
        """Write the signed decision file for a notice, as after the owner's message."""
        self.decisions += 1
        decision = build_decision(
            notice=notice, secret=self.secret, action=action, params=params,
            message_ref=f"main-chat-reply-{self.decisions}", quote=owner_quote,
            decided_at="2026-11-24T00:00:00+00:00",
        )
        path = self.bridge_of(manifest_path=manifest_path).inbox / (
            f"{decision['decision_id']}.decision.json"
        )
        write_json_once(path=path, record=decision)
        return path


def forged_decision(
    notice: dict,
    action: str,
    changes: Optional[dict] = None,
    secret: str = TEST_BRIDGE_SECRET,
    resign: bool = True,
) -> dict:
    """A decision that the reference host would not write for a notice.

    It names `action` (with the token of that action, as someone who holds
    the secret could compute it) and carries `changes`. With resign=False
    the changes are made after signing, as by someone without the secret.
    """
    record = build_decision(
        notice=notice, secret=secret, action=notice["allowed_decisions"][0]["action"],
        message_ref="forged", quote="forged", decided_at="2026-11-24T00:00:00+00:00",
    )
    signature = {name: record[name] for name in SIGNATURE_FIELDS}
    record = {name: value for name, value in record.items() if name not in SIGNATURE_FIELDS}
    if resign:
        record.update({
            "action": action,
            "token": decision_token(secret=secret, event_id=notice["event_id"], action=action),
            **(changes or {}),
        })
        record["decision_id"] = decision_id_of(decision=record)
        return sign(record=record, secret=secret)
    return {**record, **(changes or {}), **signature}


def write_record(directory: Path, record: dict, name: str = "forged.decision.json") -> Path:
    """Write a record as a JSON file and return its path."""
    path = directory / name
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def open_notice(orchestrator: Orchestrator, stage_name: str, action: str) -> dict:
    """The notice a decision on a stage answers: of its acceptance, or of its round."""
    result = orchestrator.manifest.stages[stage_name]
    record = result.receipt if action == ACTION_START_STAGE else result.notice
    assert record is not None, f"Stage '{stage_name}' has no notice for '{action}'"
    return record["event"]


def decide(
    orchestrator: Orchestrator,
    stage_name: str,
    action: str,
    params: Optional[dict] = None,
    host: Optional[FakeHost] = None,
) -> Path:
    """Have the stand-in host write the owner's decision on a stage, and apply it."""
    host = host or FakeHost()
    path = host.write_decision(
        manifest_path=orchestrator.manifest_path,
        notice=open_notice(orchestrator=orchestrator, stage_name=stage_name, action=action),
        action=action, params=params,
    )
    orchestrator.apply_decision(path=path)
    return path


def run_to_gate(orchestrator: Orchestrator, host: Optional[FakeHost] = None) -> PipelineManifest:
    """Run the pipeline; let the stand-in host post and acknowledge a pending notice; run again.

    For tests that are not about the notice itself. The orchestrator
    still stops at NOTICE_PENDING first; this helper does what the host
    would do, then resumes.
    """
    host = host or FakeHost()
    manifest = orchestrator.run_pipeline()
    if any(result.status == STAGE_NOTICE_PENDING for result in manifest.stages.values()):
        host.post_new_notices(manifest_path=orchestrator.manifest_path)
        manifest = orchestrator.run_pipeline()
    return manifest


def run_stage(orchestrator: Orchestrator, stage: StageConfig) -> StageResult:
    """Run one stage's current round to its gate, starting its first round if it has none.

    The first round is started here by hand, where a real project needs
    --execute or the owner's start_stage decision.
    """
    existing = orchestrator.manifest.stages.get(stage.name)
    if existing is None or existing.round == 0:
        orchestrator._reserve_round(
            stage=stage, kind=KIND_INITIAL, started_by="test", reason="started by a test",
        )
    result = orchestrator._run_stage(stage=stage)
    if result.status == STAGE_NOTICE_PENDING:
        FakeHost().post_new_notices(manifest_path=orchestrator.manifest_path)
        result = orchestrator._run_stage(stage=stage)
    return result


def approve_as_reviewer(
    orchestrator: Orchestrator, stage_name: str, host: Optional[FakeHost] = None,
) -> None:
    """Accept a stage's reviewed output and, if a stage follows, decide to start it.

    Two separate decisions of the owner: accepting launches nothing.
    """
    decide(orchestrator=orchestrator, stage_name=stage_name, action=ACTION_ACCEPT, host=host)
    if stage_name != orchestrator.stages[-1].name:
        decide(orchestrator=orchestrator, stage_name=stage_name, action=ACTION_START_STAGE, host=host)


def run_approving_every_stage(orchestrator: Orchestrator) -> PipelineManifest:
    """Run the pipeline to the end, deciding as the owner whenever a round is at its gate.

    For tests that are not about the review itself. Every stage still
    stops at its gate. The last stage here is one that runs parameter
    batches when the committed config is used: its first round is a domain
    proposal, which is approved, and its second round is accepted.
    """
    for _ in range(3 * len(orchestrator.stages)):
        manifest = run_to_gate(orchestrator=orchestrator)
        awaiting = [
            name for name, result in manifest.stages.items()
            if result.status == STAGE_AWAITING_REVIEW
        ]
        if not awaiting:
            return manifest
        allowed = [
            entry["action"]
            for entry in manifest.stages[awaiting[0]].notice["event"]["allowed_decisions"]
        ]
        if ACTION_ACCEPT in allowed:
            approve_as_reviewer(orchestrator=orchestrator, stage_name=awaiting[0])
        else:
            decide(
                orchestrator=orchestrator, stage_name=awaiting[0], action=ACTION_ITERATE,
                params={"approve_batch": True},
            )
    raise AssertionError("The pipeline did not come to an end")


def make_launch_mock() -> FakeServices:
    """Stand-in services for the committed config: launches succeed and runs finish."""
    return make_launch_mock_with_listing(models_listing=MOCK_PIPELINE_MODELS_RESPONSE)


def make_launch_mock_with_listing(models_listing: dict) -> FakeServices:
    """Like make_launch_mock, with a given stand-in for the GET /v1/models listing."""
    return FakeServices(
        config=load_config(config_path=REAL_CONFIG_PATH), models_listing=models_listing,
    )


def make_changed_mock(change: Callable[[dict], object]) -> FakeServices:
    """Stand-in services for a changed copy of the committed config.

    The change is applied in memory only. It is how tests switch on what
    the committed config leaves off (autonomous correction rounds, review
    by a second model); it says nothing about what the owner has chosen.
    """
    config = load_config(config_path=REAL_CONFIG_PATH)
    change(config)
    return FakeServices(config=config, models_listing=MOCK_PIPELINE_MODELS_RESPONSE)


def stage_settings(config: dict, name: str) -> dict:
    """The settings of one stage in a config."""
    return next(stage for stage in config["stages"] if stage["name"] == name)


def new_test_manifest(orchestrator: Orchestrator, source_commit: str) -> PipelineManifest:
    """A manifest made by hand for a test, with the IDs and provenance a real one has."""
    return PipelineManifest(
        pipeline_name="test",
        repo_url=orchestrator.repo_config["url"],
        started_at="2026-11-24T00:00:00Z",
        project_id="prj-000000000000",
        provenance={
            "created_by": "test", "config_sha256": orchestrator.config_sha256,
            "code": {}, "repins": [],
        },
        source_ref="main",
        source_commit=source_commit,
    )


def open_project_by_hand(
    orchestrator: Orchestrator,
    backend: FakeServices,
    completed: tuple[str, ...] = (),
) -> None:
    """Open a project with no document, with the given stages recorded as accepted.

    The manifest is made in memory, by hand. It would not pass the checks
    a manifest read from a file has to pass (it holds no signed decision);
    it serves tests of a later stage's own steps.
    """
    manifest = new_test_manifest(
        orchestrator=orchestrator, source_commit=backend.branches["main"],
    )
    commit = backend.branches["main"]
    for name in completed:
        stage = next(stage for stage in backend.config["stages"] if stage["name"] == name)
        commit = backend.add_commit(
            files=backend.stage_output_files(stage=stage, start_commit=commit),
            parent=commit,
            branch=f"cursor/{name}-by-hand",
        )
        manifest.stages[name] = StageResult(
            name=name, status=STAGE_COMPLETE, round=1, kind=KIND_INITIAL,
            outcome=OUTCOME_PASSED,
            output_branch=f"cursor/{name}-by-hand", output_commit=commit,
        )
    orchestrator.manifest = manifest


def start_project(
    backend: FakeServices,
    directory: Path,
) -> Orchestrator:
    """Start a project on the committed config, with its files in `directory`.

    Nothing has been sent when this returns. The manifest file is
    `directory / "manifest.json"`.
    """
    document_path = write_document(directory=directory, file_name="model_setup.qmd")
    orchestrator = load_real_orchestrator(
        mock_http=backend,
        dry_run=False,
        ask=answer_with(str(document_path)),
        manifest_path=directory / "manifest.json",
    )
    orchestrator.start_new_project()
    return orchestrator


def resume_from_file(backend: FakeServices, manifest_path: Path) -> Orchestrator:
    """Resume a project from its saved manifest file, as a new process would."""
    orchestrator = load_real_orchestrator(
        mock_http=backend,
        dry_run=False,
        manifest_path=manifest_path,
    )
    orchestrator.resume_project(manifest=load_manifest(manifest_path=manifest_path))
    return orchestrator


def launch_count(backend: FakeServices) -> int:
    """How many launch requests (POST /v1/agents) were sent."""
    return len([call for call in backend.calls if call[0] == "POST"])


def get_launch_requests(mock_http: MockHttpClient) -> list[dict]:
    """Return the JSON bodies of the agent launch requests sent so far."""
    return [call[3] for call in mock_http.calls if call[0] == "POST"]


def expect_document_error(action: Callable[[], object], expected_message_part: str) -> None:
    try:
        action()
    except ModelSetupDocumentError as error:
        assert expected_message_part in str(error), str(error)
    else:
        raise AssertionError(f"Expected an error mentioning '{expected_message_part}'")


def end_of_input(question: str) -> str:
    raise EOFError


def test_new_project_asks_for_the_document_before_any_request():
    """Starting a project asks the one question, sends nothing, and records the answer."""
    with tempfile.TemporaryDirectory() as tmp:
        document_path = write_document(directory=Path(tmp), file_name="model_setup.qmd")
        mock_http = make_launch_mock()
        ask = answer_with(str(document_path))
        orchestrator = load_real_orchestrator(mock_http=mock_http, dry_run=False, ask=ask)
        
        manifest = orchestrator.start_new_project()
        
        assert ask.questions == [MODEL_SETUP_QUESTION]
        assert mock_http.calls == [], "Nothing may be sent while the document is selected"
        record = manifest.model_setup_document
        assert record["path"] == str(document_path.resolve())
        assert record["format"] == "qmd"
        assert record["size_bytes"] == len(SENTINEL_DOCUMENT_TEXT.encode("utf-8"))
        assert record["sha256"] == hashlib.sha256(document_path.read_bytes()).hexdigest()
        assert record["selected_at"] == manifest.started_at
        assert set(record) == {"path", "format", "size_bytes", "sha256", "selected_at"}
    print("✓ test_new_project_asks_for_the_document_before_any_request passed")


def test_every_new_project_asks_again_and_never_reuses_a_path():
    """A second project asks again; a failed answer leaves no earlier document in place."""
    with tempfile.TemporaryDirectory() as tmp:
        first = write_document(directory=Path(tmp), file_name="first.qmd")
        second = write_document(
            directory=Path(tmp), file_name="second.tex", text=OTHER_SENTINEL_DOCUMENT_TEXT,
        )
        mock_http = make_launch_mock()
        ask = answer_with(str(first), str(second), "")
        orchestrator = load_real_orchestrator(mock_http=mock_http, dry_run=False, ask=ask)
        
        first_manifest = orchestrator.start_new_project()
        second_manifest = orchestrator.start_new_project()
        
        assert ask.questions == [MODEL_SETUP_QUESTION, MODEL_SETUP_QUESTION]
        assert first_manifest.model_setup_document["path"] == str(first.resolve())
        assert second_manifest.model_setup_document["path"] == str(second.resolve())
        assert orchestrator.model_setup_document.text == OTHER_SENTINEL_DOCUMENT_TEXT
        
        expect_document_error(
            action=orchestrator.start_new_project,
            expected_message_part="No location was given",
        )
        assert len(ask.questions) == 3
        assert orchestrator.model_setup_document is None
        assert orchestrator.manifest is None
        assert mock_http.calls == []
    print("✓ test_every_new_project_asks_again_and_never_reuses_a_path passed")


def test_unusable_locations_are_rejected_before_any_launch():
    """Blank, missing, non-file, wrong-type, empty and non-UTF-8 answers all stop the start."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        (directory / "folder.tex").mkdir()
        write_document(directory=directory, file_name="notes.txt")
        write_document(directory=directory, file_name="paper.pdf")
        write_document(directory=directory, file_name="no_extension")
        write_document(directory=directory, file_name="notes.markdown")
        write_document(directory=directory, file_name="notebook.Rmd")
        write_document(directory=directory, file_name="empty.tex", text="")
        write_document(directory=directory, file_name="blank.qmd", text="  \n\n")
        write_document(directory=directory, file_name="empty.md", text="")
        write_document(directory=directory, file_name="blank.md", text="\n \t\n")
        (directory / "folder.md").mkdir()
        (directory / "latin1.tex").write_bytes("caf\xe9".encode("latin-1"))
        (directory / "latin1.md").write_bytes("caf\xe9".encode("latin-1"))
        (directory / "utf16.md").write_bytes("Sentinel line.\n".encode("utf-16"))
        
        wrong_type = "must be a .qmd, .tex or .md file"
        cases = [
            ("", "No location was given"),
            ("   ", "No location was given"),
            ('""', "No location was given"),
            (str(directory / "missing.qmd"), "does not exist"),
            (str(directory / "missing.md"), "does not exist"),
            (str(directory / "folder.tex"), "is not a file"),
            (str(directory / "folder.md"), "is not a file"),
            (str(directory / "notes.txt"), wrong_type),
            (str(directory / "paper.pdf"), wrong_type),
            (str(directory / "no_extension"), wrong_type),
            (str(directory / "notes.markdown"), wrong_type),
            (str(directory / "notebook.Rmd"), wrong_type),
            (str(directory / "empty.tex"), "is empty"),
            (str(directory / "blank.qmd"), "is empty"),
            (str(directory / "empty.md"), "is empty"),
            (str(directory / "blank.md"), "is empty"),
            (str(directory / "latin1.tex"), "is not UTF-8 text"),
            (str(directory / "latin1.md"), "is not UTF-8 text"),
            (str(directory / "utf16.md"), "is not UTF-8 text"),
        ]
        for answer, expected_message_part in cases:
            mock_http = make_launch_mock()
            orchestrator = load_real_orchestrator(
                mock_http=mock_http, dry_run=False, ask=answer_with(answer),
            )
            expect_document_error(
                action=orchestrator.start_new_project,
                expected_message_part=expected_message_part,
            )
            assert orchestrator.manifest is None, answer
            try:
                orchestrator.run_pipeline()
            except RuntimeError as error:
                assert "No project is open" in str(error)
            else:
                raise AssertionError(f"run_pipeline must refuse after answer {answer!r}")
            assert mock_http.calls == [], f"A request was sent after answer {answer!r}"
    print("✓ test_unusable_locations_are_rejected_before_any_launch passed")


def test_end_of_input_when_asked_fails_before_any_launch():
    """With no interactive input (EOF), the start fails clearly and sends nothing."""
    mock_http = make_launch_mock()
    orchestrator = load_real_orchestrator(
        mock_http=mock_http, dry_run=False, ask=end_of_input,
    )
    expect_document_error(
        action=orchestrator.start_new_project,
        expected_message_part="input ended before an answer was given",
    )
    assert orchestrator.manifest is None
    assert mock_http.calls == []
    print("✓ test_end_of_input_when_asked_fails_before_any_launch passed")


def test_accepted_formats_are_qmd_tex_and_md():
    """The question, the messages and the format names all come from one mapping."""
    assert MODEL_SETUP_FORMATS == {
        ".qmd": "Quarto Markdown (.qmd)",
        ".tex": "LaTeX (.tex)",
        ".md": "Markdown (.md)",
    }
    assert MODEL_SETUP_EXTENSIONS == ".qmd, .tex or .md"
    assert MODEL_SETUP_QUESTION.endswith("for this project (.qmd, .tex or .md): ")
    expect_document_error(
        action=lambda: load_model_setup_document(location=""),
        expected_message_part="Give the path of a .qmd, .tex or .md file.",
    )
    print("✓ test_accepted_formats_are_qmd_tex_and_md passed")


def test_valid_qmd_tex_and_md_documents_reach_stage1_in_the_launch_request():
    """The Stage 1 launch body holds the selected file's text, not its local path."""
    cases = (
        ("model_setup.qmd", "", "qmd", "Quarto Markdown (.qmd)"),
        ("model_setup.tex", "", "tex", "LaTeX (.tex)"),
        ("MODEL.TEX", '"', "tex", "LaTeX (.tex)"),
        ("model_setup.md", "", "md", "Markdown (.md)"),
        ("NOTES.MD", '"', "md", "Markdown (.md)"),
    )
    for file_name, quote, recorded_format, format_name in cases:
        with tempfile.TemporaryDirectory() as tmp:
            document_path = write_document(directory=Path(tmp), file_name=file_name)
            bytes_before = document_path.read_bytes()
            sha256 = hashlib.sha256(bytes_before).hexdigest()
            mock_http = make_launch_mock()
            orchestrator = load_real_orchestrator(
                mock_http=mock_http,
                dry_run=False,
                ask=answer_with(f"{quote}{document_path}{quote}"),
            )
            orchestrator.start_new_project()
            record = orchestrator.manifest.model_setup_document
            assert record["format"] == recorded_format, file_name
            stage1 = get_stage(orchestrator=orchestrator, name=STAGE1_NAME)
            
            result = run_stage(orchestrator=orchestrator, stage=stage1)
            
            assert result.status == STAGE_AWAITING_REVIEW, file_name
            launch_requests = get_launch_requests(mock_http=mock_http)
            assert len(launch_requests) == 1
            prompt_text = launch_requests[0]["prompt"]["text"]
            assert f"Format: {format_name}\n" in prompt_text, file_name
            positions = [
                prompt_text.index("TASK: Derive the equilibrium"),
                prompt_text.index(MODEL_SETUP_HEADING),
                prompt_text.index(MODEL_SETUP_BEGIN_LINE),
                prompt_text.index(SENTINEL_DOCUMENT_TEXT.rstrip()),
                prompt_text.index(MODEL_SETUP_END_LINE),
            ]
            assert positions == sorted(positions), file_name
            assert f"File name: {file_name}" in prompt_text
            assert f"SHA-256: {sha256}" in prompt_text
            assert tmp not in prompt_text, "The local path must not be sent"
            assert "TODO(Yang)" not in prompt_text
            assert document_path.read_bytes() == bytes_before, "The file must not change"
            assert sorted(path.name for path in Path(tmp).iterdir()) == [file_name]
    print("✓ test_valid_qmd_tex_and_md_documents_reach_stage1_in_the_launch_request passed")


def test_stage1_is_blocked_when_no_document_was_selected():
    """Without a selected document, Stage 1 sends nothing, in a real run or a dry run."""
    for dry_run in (False, True):
        mock_http = make_launch_mock()
        orchestrator = load_real_orchestrator(mock_http=mock_http, dry_run=dry_run)
        open_project_by_hand(orchestrator=orchestrator, backend=mock_http)
        stage1 = get_stage(orchestrator=orchestrator, name=STAGE1_NAME)
        
        result = run_stage(orchestrator=orchestrator, stage=stage1)
        
        assert result.status == STAGE_BLOCKED, f"dry_run={dry_run}"
        assert "none has been selected" in result.error
        assert result.agent_id is None
        assert mock_http.calls == []
        expect_document_error(
            action=lambda: build_stage_prompt(stage=stage1),
            expected_message_part="none has been selected",
        )
    print("✓ test_stage1_is_blocked_when_no_document_was_selected passed")


def test_document_that_pulls_in_other_files_is_rejected():
    """Only the one file's text is sent, so a file that includes others is refused."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        tex_with_input = write_document(
            directory=directory,
            file_name="main.tex",
            text="Sentinel line.\n\\input{sections/setup}\n",
        )
        qmd_with_include = write_document(
            directory=directory,
            file_name="main.qmd",
            text="Sentinel line.\n\n{{< include _setup.qmd >}}\n",
        )
        self_contained = write_document(
            directory=directory,
            file_name="alone.tex",
            text=(
                "\\usepackage[utf8]{inputenc}\n"
                "% \\input{old_draft}\n"
                "\\includegraphics{figure}\n"
                "Sentinel line.\n"
            ),
        )
        md_with_includes = [
            write_document(
                directory=directory,
                file_name=file_name,
                text=f"# Sentinel heading\n\nSentinel line.\n\n{include_line}\n",
            )
            for file_name, include_line in (
                ("quarto_shortcode.md", "{{< include _setup.md >}}"),
                ("myst_backticks.md", "```{include} sections/setup.md\n```"),
                ("myst_colons.MD", ":::{include} sections/setup.md\n:::"),
                ("myst_indented.md", "  ~~~ {literalinclude} code/solver.py\n  ~~~"),
            )
        ]
        
        for path in (tex_with_input, qmd_with_include, *md_with_includes):
            try:
                load_model_setup_document(location=str(path))
            except ModelSetupDocumentError as error:
                assert "pulls in other files" in str(error), path.name
                assert "line " in str(error)
                if path in md_with_includes:
                    assert "line 5: " in str(error), path.name
            else:
                raise AssertionError(f"Expected {path.name} to be refused")
        
        document = load_model_setup_document(location=str(self_contained))
        assert document.format == "tex"
        
        md_text = (
            "# Sentinel heading\n\n"
            "Sentinel line. Prose may include the word include, {include} and \\input{x}.\n\n"
            "![Sentinel figure](figures/sentinel.png)\n\n"
            "[Sentinel link](other_notes.md)\n\n"
            "```python\nfrom sentinel import solve\n```\n\n"
            "$$\nx = y\n$$\n"
        )
        md_self_contained = write_document(
            directory=directory, file_name="alone.md", text=md_text,
        )
        document = load_model_setup_document(location=str(md_self_contained))
        assert document.format == "md"
        assert document.text == md_text
        
        # The Markdown-only directive check does not change what a .qmd file may hold.
        qmd_with_myst_directive = write_document(
            directory=directory,
            file_name="unchanged.qmd",
            text="Sentinel line.\n\n```{include} sections/setup.md\n```\n",
        )
        assert load_model_setup_document(location=str(qmd_with_myst_directive)).format == "qmd"
    print("✓ test_document_that_pulls_in_other_files_is_rejected passed")


def start_project_and_reload_manifest(document_path: Path) -> PipelineManifest:
    """Start a project with a document, and return its manifest as read back from JSON."""
    starter = load_real_orchestrator(
        mock_http=make_launch_mock(), dry_run=False, ask=answer_with(str(document_path)),
    )
    starter.start_new_project()
    return PipelineManifest.from_dict(json.loads(json.dumps(starter.manifest.to_dict())))


def test_resume_uses_the_recorded_document_without_asking():
    """Resuming re-reads the project's own document and never asks."""
    for file_name, recorded_format in (("model_setup.tex", "tex"), ("model_setup.md", "md")):
        with tempfile.TemporaryDirectory() as tmp:
            document_path = write_document(directory=Path(tmp), file_name=file_name)
            manifest = start_project_and_reload_manifest(document_path=document_path)
            assert manifest.model_setup_document["format"] == recorded_format
            
            mock_http = make_launch_mock()
            orchestrator = load_real_orchestrator(mock_http=mock_http, dry_run=False)
            orchestrator.resume_project(manifest=manifest)
            assert mock_http.calls == []
            
            result = run_stage(orchestrator=orchestrator, stage=get_stage(orchestrator=orchestrator, name=STAGE1_NAME),
            )
            
            assert result.status == STAGE_AWAITING_REVIEW, file_name
            prompt_text = get_launch_requests(mock_http=mock_http)[0]["prompt"]["text"]
            assert SENTINEL_DOCUMENT_TEXT.rstrip() in prompt_text
            assert orchestrator.manifest.model_setup_document == manifest.model_setup_document
    print("✓ test_resume_uses_the_recorded_document_without_asking passed")


def test_resume_fails_before_launch_when_the_recorded_document_is_unusable():
    """A changed, deleted or unrecorded document stops a resume that still needs it."""
    with tempfile.TemporaryDirectory() as tmp:
        document_path = write_document(directory=Path(tmp), file_name="model_setup.qmd")
        manifest = start_project_and_reload_manifest(document_path=document_path)
        
        def expect_resume_refused(resumed: PipelineManifest, expected_message_part: str) -> None:
            mock_http = make_launch_mock()
            orchestrator = load_real_orchestrator(mock_http=mock_http, dry_run=False)
            expect_document_error(
                action=lambda: orchestrator.resume_project(manifest=resumed),
                expected_message_part=expected_message_part,
            )
            assert orchestrator.manifest is None
            assert mock_http.calls == []
        
        write_document(
            directory=Path(tmp), file_name="model_setup.qmd", text=OTHER_SENTINEL_DOCUMENT_TEXT,
        )
        expect_resume_refused(resumed=manifest, expected_message_part="has changed")
        
        document_path.unlink()
        expect_resume_refused(resumed=manifest, expected_message_part="does not exist")
        
        unrecorded = copy.deepcopy(manifest)
        unrecorded.model_setup_document = None
        expect_resume_refused(
            resumed=unrecorded, expected_message_part="records no model setup",
        )
        
        unrecorded.model_setup_document = {"path": str(document_path)}
        expect_resume_refused(resumed=unrecorded, expected_message_part="missing: sha256")
    print("✓ test_resume_fails_before_launch_when_the_recorded_document_is_unusable passed")


def test_resume_after_stage1_finished_does_not_need_the_document():
    """Once the stage that receives the document is accepted, a resume neither asks nor reads it."""
    backend = make_launch_mock()
    with tempfile.TemporaryDirectory() as tmp:
        orchestrator = start_project(backend=backend, directory=Path(tmp))
        manifest_path = orchestrator.manifest_path
        run_to_gate(orchestrator=orchestrator)
        approve_as_reviewer(orchestrator=orchestrator, stage_name=STAGE1_NAME)
        stage1_commit = orchestrator.manifest.stages[STAGE1_NAME].output_commit
        (Path(tmp) / "model_setup.qmd").unlink()
        
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        result = run_approving_every_stage(orchestrator=resumed)
    
    launch_requests = get_launch_requests(mock_http=backend)
    assert [body["repos"][0]["startingRef"] for body in launch_requests[:2]] == [
        backend.branches["main"], stage1_commit,
    ]
    assert all(MODEL_SETUP_HEADING not in body["prompt"]["text"] for body in launch_requests[1:])
    assert result.stages[STAGE2_NAME].status == STAGE_COMPLETE
    print("✓ test_resume_after_stage1_finished_does_not_need_the_document passed")


def test_cli_execute_asks_records_the_document_and_resume_does_not_ask():
    """--execute asks before anything is sent; --resume continues without asking.

    Runs the committed config through the command line to the end. Every
    round stops twice: first until the host has acknowledged the notice
    (exit code 3), then until the owner's decision is applied (exit code
    2). Accepting a stage launches nothing; the next stage starts on a
    decision of its own. The last stage waits for its decision too.
    """
    with tempfile.TemporaryDirectory() as tmp:
        document_path = write_document(directory=Path(tmp), file_name="model_setup.qmd")
        manifest_path = Path(tmp) / "project_manifest.json"
        backend = make_launch_mock()
        host = FakeHost()
        execute_arguments = [
            "--execute", "--config", str(REAL_CONFIG_PATH), "--output", str(manifest_path),
        ]
        resume_arguments = ["--resume", str(manifest_path), "--config", str(REAL_CONFIG_PATH)]
        
        def stage_notice(stage_name: str, action: str) -> dict:
            result = load_manifest(manifest_path=manifest_path).stages[stage_name]
            return (result.receipt if action == ACTION_START_STAGE else result.notice)["event"]
        
        def decide_by_cli(stage_name: str, action: str, params: Optional[dict] = None) -> int:
            decision_path = host.write_decision(
                manifest_path=manifest_path, action=action, params=params,
                notice=stage_notice(stage_name=stage_name, action=action),
            )
            return run_cli(
                backend=backend,
                arguments=[*resume_arguments, "--apply-decision", str(decision_path)],
            )
        
        exit_code = run_cli(backend=backend, arguments=execute_arguments, ask=end_of_input)
        assert exit_code == EXIT_FAILED
        assert backend.calls == [], "No request may be sent when no document is given"
        assert not manifest_path.exists()
        
        ask = answer_with(str(document_path))
        exit_code = run_cli(backend=backend, arguments=execute_arguments, ask=ask)
        assert exit_code == EXIT_NOTICE_PENDING, "Without the host's ack the output is not awaiting review"
        assert ask.questions == [MODEL_SETUP_QUESTION]
        launch_requests = get_launch_requests(mock_http=backend)
        assert len(launch_requests) == 1
        assert SENTINEL_DOCUMENT_TEXT.rstrip() in launch_requests[0]["prompt"]["text"]
        
        saved_text = manifest_path.read_text(encoding="utf-8")
        assert SENTINEL_DOCUMENT_TEXT.rstrip() not in saved_text, "The text is not stored"
        assert TEST_BRIDGE_SECRET not in saved_text, "The secret is not stored"
        saved = load_manifest(manifest_path=manifest_path)
        assert saved.model_setup_document["path"] == str(document_path.resolve())
        assert saved.model_setup_document["sha256"] == hashlib.sha256(
            document_path.read_bytes()
        ).hexdigest()
        assert saved.stages[STAGE1_NAME].status == STAGE_NOTICE_PENDING
        
        exit_code = run_cli(backend=backend, arguments=execute_arguments)
        assert exit_code == EXIT_FAILED, "A new project must not overwrite an existing project's manifest"
        assert manifest_path.read_text(encoding="utf-8") == saved_text
        
        assert run_cli(backend=backend, arguments=resume_arguments) == EXIT_NOTICE_PENDING
        assert len(host.post_new_notices(manifest_path=manifest_path)) == 1
        exit_code = run_cli(backend=backend, arguments=resume_arguments)
        assert exit_code == EXIT_WAITING_FOR_OWNER, "A resume without a decision stays at the gate"
        assert load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME].status == (
            STAGE_AWAITING_REVIEW
        )
        assert launch_count(backend=backend) == 1
        
        for number, accepted in enumerate((STAGE1_NAME, STAGE2_NAME, STAGE3_NAME), start=1):
            exit_code = decide_by_cli(stage_name=accepted, action=ACTION_ACCEPT)
            assert exit_code == EXIT_WAITING_FOR_OWNER, f"Accepting {accepted} launches nothing"
            assert launch_count(backend=backend) == number
            assert run_cli(backend=backend, arguments=resume_arguments) == EXIT_WAITING_FOR_OWNER
            assert launch_count(backend=backend) == number, "A resume does not start the next stage"
            
            exit_code = decide_by_cli(stage_name=accepted, action=ACTION_START_STAGE)
            assert exit_code == EXIT_NOTICE_PENDING, "The next stage runs one round, to its notice"
            assert launch_count(backend=backend) == number + 1
            host.post_new_notices(manifest_path=manifest_path)
            assert run_cli(backend=backend, arguments=resume_arguments) == EXIT_WAITING_FOR_OWNER
        
        # The last stage's first round only proposes a parameter domain: it
        # cannot be accepted. The owner approves the batch; one more round runs it.
        paused = load_manifest(manifest_path=manifest_path)
        assert [paused.stages[name].status for name in ALL_STAGE_NAMES] == (
            [STAGE_COMPLETE] * 3 + [STAGE_AWAITING_REVIEW]
        )
        allowed = [
            entry["action"]
            for entry in paused.stages[STAGE4_NAME].notice["event"]["allowed_decisions"]
        ]
        assert ACTION_ACCEPT not in allowed and ACTION_ITERATE in allowed
        exit_code = decide_by_cli(
            stage_name=STAGE4_NAME, action=ACTION_ITERATE, params={"approve_batch": True},
        )
        assert exit_code == EXIT_NOTICE_PENDING
        host.post_new_notices(manifest_path=manifest_path)
        assert run_cli(backend=backend, arguments=resume_arguments) == EXIT_WAITING_FOR_OWNER
        assert load_manifest(manifest_path=manifest_path).completed_at is None, (
            "The last stage is not accepted by itself"
        )
        
        exit_code = decide_by_cli(stage_name=STAGE4_NAME, action=ACTION_ACCEPT)
        assert exit_code == EXIT_COMPLETE, "Only the acceptance of the last stage finishes the project"
        
        launch_requests = get_launch_requests(mock_http=backend)
        assert len(launch_requests) == 5
        assert all(
            MODEL_SETUP_HEADING not in body["prompt"]["text"] for body in launch_requests[1:]
        )
        finished = load_manifest(manifest_path=manifest_path)
        assert [finished.stages[name].status for name in ALL_STAGE_NAMES] == [STAGE_COMPLETE] * 4
        assert finished.completed_at is not None
        
        exit_code = run_cli(backend=backend, arguments=resume_arguments)
        assert exit_code == EXIT_COMPLETE
        assert launch_count(backend=backend) == 5, "Accepted stages are never launched again"
        host.post_new_notices(manifest_path=manifest_path)
        assert len(host.posted) == 5 + 4, "One notice per round and one per acceptance"
        assert host.posted[-1]["allowed_decisions"] == [], "Nothing follows the last stage"
    print("✓ test_cli_execute_asks_records_the_document_and_resume_does_not_ask passed")


def test_cli_dry_run_is_offline_and_saves_nothing():
    """--dry-run asks for the document, needs no API key, sends nothing and saves nothing."""
    old_key = os.environ.pop("CURSOR_API_KEY", None)
    try:
        for file_name in ("model_setup.tex", "model_setup.qmd", "model_setup.md"):
            with tempfile.TemporaryDirectory() as tmp:
                document_path = write_document(directory=Path(tmp), file_name=file_name)
                manifest_path = Path(tmp) / "project_manifest.json"
                dry_run_arguments = [
                    "--dry-run", "--config", str(REAL_CONFIG_PATH),
                    "--output", str(manifest_path),
                ]
                backend = make_launch_mock()
                
                exit_code = run_cli(
                    backend=backend, arguments=dry_run_arguments, ask=answer_with(""),
                    api_key=None,
                )
                assert exit_code == 1
                
                ask = answer_with(str(document_path))
                exit_code = run_cli(
                    backend=backend, arguments=dry_run_arguments, ask=ask, api_key=None,
                )
                assert exit_code == 0, f"A dry run needs no API key ({file_name})"
                assert ask.questions == [MODEL_SETUP_QUESTION]
                assert backend.calls == [], "A dry run sends no request to any service"
                assert backend.downloads == [] and backend.sleeps == []
                assert sorted(path.name for path in Path(tmp).iterdir()) == [file_name]
    finally:
        if old_key:
            os.environ["CURSOR_API_KEY"] = old_key
    print("✓ test_cli_dry_run_is_offline_and_saves_nothing passed")


def test_cli_check_online_validates_over_the_network_and_launches_nothing():
    """--check-online reads the models listing and the starting ref, asks and launches nothing."""
    check_arguments = ["--check-online", "--config", str(REAL_CONFIG_PATH)]
    used_backends = []
    
    def check(backend: FakeServices, api_key: Optional[str] = "test-key") -> int:
        used_backends.append(backend)
        return run_cli(backend=backend, arguments=check_arguments, api_key=api_key)
    
    backend = make_launch_mock()
    assert check(backend=backend) == 0
    assert [
        (method, urlsplit(url).netloc, urlsplit(url).path) for method, url, _, _ in backend.calls
    ] == [
        ("GET", "api.cursor.com", "/v1/models"),
        ("GET", "api.github.com", f"/repos/{backend.owner}/{backend.repo}/git/ref/heads/main"),
        ("GET", "api.github.com",
         f"/repos/{backend.owner}/{backend.repo}/contents/{SOURCE_DOCUMENT_PATH}"),
    ]
    assert backend.local_review.ready_checks == 1 and backend.local_review.shown == []
    
    unlisted = copy.deepcopy(MOCK_PIPELINE_MODELS_RESPONSE)
    unlisted["items"] = unlisted["items"][:1]
    assert check(backend=make_launch_mock_with_listing(models_listing=unlisted)) == 1
    
    backend = make_launch_mock()
    del backend.branches["main"]
    assert check(backend=backend) == 1, "A starting ref that cannot be read fails the check"
    
    backend = make_launch_mock()
    backend.add_commit(
        files=[], parent=backend.branches["main"], branch="main", removed=(SOURCE_DOCUMENT_PATH,),
    )
    assert check(backend=backend) == 1, "The source document must be at the starting ref"
    
    backend = make_launch_mock()
    backend.local_review.changed_files = ["docs/notes.md"]
    assert check(backend=backend) == 1, "A local checkout with uncommitted changes fails the check"
    
    old_key = os.environ.pop("CURSOR_API_KEY", None)
    try:
        backend = make_launch_mock()
        assert check(backend=backend, api_key=None) == 1
        assert all("api.cursor.com" not in url for _, url, _, _ in backend.calls)
    finally:
        if old_key:
            os.environ["CURSOR_API_KEY"] = old_key
    
    assert [launch_count(backend=used) for used in used_backends] == [0] * 6
    print("✓ test_cli_check_online_validates_over_the_network_and_launches_nothing passed")


def test_validate_config_requires_the_first_stage_to_receive_the_document():
    """The config cannot switch off sending the document to the first stage."""
    def validate(first_stage_extra: dict) -> tuple[bool, list[str]]:
        config = {
            "pipeline": {"repository": {"url": "https://github.com/test/repo"}},
            "api": MINIMAL_API_CONFIG,
            "review": MINIMAL_REVIEW_CONFIG,
            "stages": [{
                "name": "s1", "prompt": "test", "iteration": MINIMAL_ITERATION,
                **first_stage_extra,
            }],
        }
        return Orchestrator(config=config, dry_run=True).validate_config()
    
    assert validate({"receives_model_setup_document": True}) == (True, [])
    for rejected in ({}, {"receives_model_setup_document": False},
                     {"receives_model_setup_document": "yes"}):
        is_valid, errors = validate(rejected)
        assert not is_valid, rejected
        assert any("receives_model_setup_document" in error for error in errors)
    
    real = load_real_orchestrator(mock_http=MockHttpClient(), dry_run=True)
    assert [stage.receives_model_setup_document for stage in real.stages] == [
        True, False, False, False,
    ]
    assert real.validate_config() == (True, [])
    print("✓ test_validate_config_requires_the_first_stage_to_receive_the_document passed")


def test_committed_config_sets_the_requested_model_for_each_stage():
    """config.yaml holds the model ID and reasoning parameter requested for each stage."""
    orchestrator = load_real_orchestrator(mock_http=MockHttpClient(), dry_run=True)
    configured = {
        stage.name: {"id": stage.model_id, "params": stage.model_params}
        for stage in orchestrator.stages
    }
    assert configured == EXPECTED_STAGE_MODELS
    assert orchestrator.validate_config() == (True, [])
    print("✓ test_committed_config_sets_the_requested_model_for_each_stage passed")


def test_launch_requests_carry_each_stage_model_in_the_documented_rest_shape():
    """Each POST /v1/agents body has model.id and model.params as a list of {id, value}."""
    with tempfile.TemporaryDirectory() as tmp:
        document_path = write_document(directory=Path(tmp), file_name="model_setup.qmd")
        mock_http = make_launch_mock()
        orchestrator = load_real_orchestrator(
            mock_http=mock_http, dry_run=False, ask=answer_with(str(document_path)),
        )
        orchestrator.start_new_project()
        run_approving_every_stage(orchestrator=orchestrator)
    
    launch_requests = get_launch_requests(mock_http=mock_http)
    # The last stage has two rounds: its domain proposal and one approved batch.
    assert [body["model"] for body in launch_requests] == [
        EXPECTED_STAGE_MODELS[name] for name in (*ALL_STAGE_NAMES, STAGE4_NAME)
    ]
    assert json.dumps(launch_requests[0]["model"]) == (
        '{"id": "claude-fable-5-2", "params": [{"id": "effort", "value": "xhigh"}]}'
    )
    assert MODEL_SETUP_HEADING in launch_requests[0]["prompt"]["text"]
    assert GUARDRAILS_HEADING in launch_requests[2]["prompt"]["text"]
    print("✓ test_launch_requests_carry_each_stage_model_in_the_documented_rest_shape passed")


def test_model_validation_checks_configured_models_against_the_models_listing():
    """An ID, parameter or value the listing lacks fails validation, and nothing is launched."""
    def validate(models_listing: dict) -> tuple[bool, list[str]]:
        mock_http = make_launch_mock_with_listing(models_listing=models_listing)
        orchestrator = load_real_orchestrator(mock_http=mock_http, dry_run=False)
        outcome = orchestrator.validate_models()
        assert get_launch_requests(mock_http=mock_http) == []
        return outcome
    
    def listing_with(model_id: str, replacement: dict) -> dict:
        listing = copy.deepcopy(MOCK_PIPELINE_MODELS_RESPONSE)
        listing["items"] = [
            replacement if item["id"] == model_id else item for item in listing["items"]
        ]
        return listing
    
    assert validate(models_listing=MOCK_PIPELINE_MODELS_RESPONSE) == (True, [])
    
    is_valid, errors = validate(models_listing=listing_with(
        model_id="claude-fable-5-2",
        replacement=model_listing_entry(
            model_id="claude-fable-5-2",
            parameter_id="effort",
            values=["low", "medium", "high", "max"],
        ),
    ))
    assert not is_valid
    assert [STAGE1_NAME in errors[0], STAGE2_NAME in errors[1], len(errors)] == [True, True, 2]
    assert "Value 'xhigh' is not permitted for parameter 'effort'" in errors[0]
    assert "Permitted values: ['low', 'medium', 'high', 'max']" in errors[0]
    
    is_valid, errors = validate(models_listing=listing_with(
        model_id="grok-4.7",
        replacement=model_listing_entry(
            model_id="grok-4.7", parameter_id="effort", values=["low"],
        ),
    ))
    assert not is_valid and len(errors) == 1
    assert STAGE3_NAME in errors[0]
    assert "Parameter 'reasoning_effort' is not listed for model 'grok-4.7'" in errors[0]
    assert "Parameters listed for this model: ['effort']" in errors[0]
    
    is_valid, errors = validate(models_listing=listing_with(
        model_id="gpt-5.6-sol",
        replacement=model_listing_entry(
            model_id="gpt-5.6-sol", parameter_id="reasoning", values=[],
        ),
    ))
    assert not is_valid and len(errors) == 1
    assert "Value 'high' is not permitted" in errors[0] and "Permitted values: []" in errors[0]
    
    reasoning_in_the_id = listing_with(
        model_id="gpt-5.6-sol",
        replacement={"id": "gpt-5.6-sol-high", "displayName": "gpt-5.6-sol-high"},
    )
    is_valid, errors = validate(models_listing=reasoning_in_the_id)
    assert not is_valid and len(errors) == 1
    assert STAGE4_NAME in errors[0]
    assert "model 'gpt-5.6-sol' is not listed by GET /v1/models" in errors[0]
    assert "'gpt-5.6-sol-high'" in errors[0], "The error must show what the API lists"
    
    with tempfile.TemporaryDirectory() as tmp:
        document_path = write_document(directory=Path(tmp), file_name="model_setup.qmd")
        manifest_path = Path(tmp) / "project_manifest.json"
        mock_http = make_launch_mock_with_listing(models_listing=reasoning_in_the_id)
        exit_code = run_cli(
            backend=mock_http,
            arguments=[
                "--execute", "--config", str(REAL_CONFIG_PATH), "--output", str(manifest_path),
            ],
            ask=answer_with(str(document_path)),
        )
        assert exit_code == 1
        assert get_launch_requests(mock_http=mock_http) == []
        assert not manifest_path.exists()
    print("✓ test_model_validation_checks_configured_models_against_the_models_listing passed")


def test_validate_config_rejects_model_settings_the_api_field_cannot_carry():
    """model_params must be a list of {id, value} strings, and needs a model_id."""
    def validate(model_settings: dict) -> tuple[bool, list[str]]:
        config = {
            "pipeline": {"repository": {"url": "https://github.com/test/repo"}},
            "api": MINIMAL_API_CONFIG,
            "review": MINIMAL_REVIEW_CONFIG,
            "stages": [{
                "name": "s1",
                "prompt": "test",
                "receives_model_setup_document": True,
                "iteration": MINIMAL_ITERATION,
                **model_settings,
            }],
        }
        return Orchestrator(config=config, dry_run=True).validate_config()
    
    effort = {"id": "effort", "value": "xhigh"}
    assert validate({"model_id": "some-model", "model_params": [effort]}) == (True, [])
    assert validate({"model_id": None, "model_params": []}) == (True, [])
    
    rejected = [
        ({"model_id": None, "model_params": [effort]}, "model_id is null"),
        ({"model_id": "", "model_params": []}, "model_id must be"),
        ({"model_id": "some-model", "model_params": {"effort": "xhigh"}}, "must be a list"),
        ({"model_id": "some-model", "model_params": [{"effort": "xhigh"}]}, "item 1"),
        ({"model_id": "some-model", "model_params": [{"id": "fast", "value": True}]}, "item 1"),
        ({"model_id": "some-model", "model_params": [effort, {"id": "effort", "value": "low"}]},
         "more than once"),
    ]
    for model_settings, expected_message_part in rejected:
        is_valid, errors = validate(model_settings)
        assert not is_valid, model_settings
        assert len(errors) == 1 and expected_message_part in errors[0], errors
    print("✓ test_validate_config_rejects_model_settings_the_api_field_cannot_carry passed")


def test_design_doc_records_the_configured_model_for_each_stage():
    """The design doc shows, for every stage, the `model` field config.yaml produces."""
    doc_text = DESIGN_DOC_PATH.read_text(encoding="utf-8")
    orchestrator = load_real_orchestrator(mock_http=MockHttpClient(), dry_run=True)
    for stage in orchestrator.stages:
        rest_model = json.dumps({"id": stage.model_id, "params": stage.model_params})
        assert doc_text.count(f"`{rest_model}`") >= 1, f"{stage.name}: {rest_model} not in doc"
    assert doc_text.count(f"`{json.dumps(EXPECTED_STAGE_MODELS[STAGE1_NAME])}`") >= 2
    print("✓ test_design_doc_records_the_configured_model_for_each_stage passed")


def expect_state_error(action: Callable[[], object], expected_message_part: str) -> None:
    try:
        action()
    except ProjectStateError as error:
        assert expected_message_part in str(error), str(error)
    else:
        raise AssertionError(f"Expected an error mentioning '{expected_message_part}'")


def run_stage1(backend: FakeServices, directory: Path) -> tuple[Orchestrator, StageResult]:
    """Start a project in `directory` and run Stage 1 of the committed config."""
    orchestrator = start_project(backend=backend, directory=directory)
    result = run_stage(
        orchestrator=orchestrator, stage=get_stage(orchestrator=orchestrator, name=STAGE1_NAME),
    )
    return orchestrator, result


def allowed_decisions(result: StageResult) -> list[str]:
    """The decisions the notice of a round at its gate allows."""
    return [entry["action"] for entry in result.notice["event"]["allowed_decisions"]]


def test_each_stage_starts_from_an_exact_commit_and_hands_on_a_verified_commit():
    """startingRef is always a commit SHA: the source commit, then each accepted output commit."""
    names = (STAGE1_NAME, STAGE2_NAME, STAGE3_NAME, STAGE4_NAME)
    with tempfile.TemporaryDirectory() as tmp:
        backend = make_launch_mock()
        source_commit = backend.branches["main"]
        started, stage1 = run_stage1(backend=backend, directory=Path(tmp))
        assert stage1.status == STAGE_AWAITING_REVIEW
        stage1_commit = stage1.output_commit
        assert stage1_commit == backend.branches[stage1.output_branch]
        approve_as_reviewer(orchestrator=started, stage_name=STAGE1_NAME)
        assert stage1.status == STAGE_COMPLETE
        
        # After the handoff is recorded, both the source branch and the
        # Stage 1 branch move on. The recorded commits must still be used.
        backend.add_commit(files=["later_on_main.txt"], parent=source_commit, branch="main")
        backend.add_commit(
            files=["later_on_stage1.txt"], parent=stage1_commit, branch=stage1.output_branch,
        )
        
        manifest_path = Path(tmp) / "manifest.json"
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        manifest = run_approving_every_stage(orchestrator=resumed)
        assert load_manifest(manifest_path=manifest_path) == manifest, "Every step is on disk"
    
    bodies = get_launch_requests(mock_http=backend)
    results = [manifest.stages[name] for name in names]
    # Four first rounds, and the last stage's second round (its approved batch).
    assert len(bodies) == 5
    assert (manifest.source_ref, manifest.source_commit) == ("main", source_commit)
    assert results[0].output_commit == stage1_commit
    
    stage_inputs = [source_commit] + [result.output_commit for result in results[:3]]
    proposal = manifest.previous_rounds[-1]
    assert (proposal["name"], proposal["round"]) == (STAGE4_NAME, 1)
    assert [body["repos"][0]["startingRef"] for body in bodies] == [
        *stage_inputs, proposal["output_commit"],
    ]
    assert [result.baseline_commit for result in results] == stage_inputs
    assert [result.input_commit for result in results[:3]] == stage_inputs[:3]
    assert results[3].input_commit == proposal["output_commit"]
    for body, result in zip([*bodies[:3], bodies[4]], results):
        assert result.status == STAGE_COMPLETE
        assert re.fullmatch(r"[0-9a-f]{40}", body["repos"][0]["startingRef"])
        assert body["agentId"] == result.agent_id
        assert AGENT_ID_PATTERN.fullmatch(result.agent_id)
        assert result.gate["passed"] is True
        assert result.gate["commit"] == result.output_commit
        assert result.review["accepted_commit"] == result.output_commit
    assert len({body["agentId"] for body in bodies}) == 5
    assert len({result.output_commit for result in results}) == 4
    print("✓ test_each_stage_starts_from_an_exact_commit_and_hands_on_a_verified_commit passed")


def test_round_with_failed_checks_goes_to_the_owner_and_cannot_be_accepted():
    """A required file missing at the pushed commit: the round is counted, shown, and not acceptable."""
    missing = WORKING_DOCUMENT_PATH
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / "manifest.json"
        backend = make_launch_mock()
        host = FakeHost()
        backend.withheld_files = {missing}
        orchestrator = start_project(backend=backend, directory=Path(tmp))
        
        manifest = run_to_gate(orchestrator=orchestrator, host=host)
        stage1 = manifest.stages[STAGE1_NAME]
        assert (stage1.status, stage1.round) == (STAGE_AWAITING_REVIEW, 1)
        assert (stage1.outcome, stage1.stop_reason) == (OUTCOME_CHECKS_FAILED, STOP_REVIEW_REQUIRED)
        assert stage1.gate["passed"] is False
        assert stage1.gate["missing_files"] == [missing]
        assert any(missing in problem for problem in stage1.gate["problems"])
        assert STAGE2_NAME not in manifest.stages
        assert launch_count(backend=backend) == 1
        
        # The failed output is shown to the owner like any other, and the
        # notice does not offer to accept it.
        failed_commit = stage1.output_commit
        assert backend.local_review.shown == [(stage1.output_branch, failed_commit)]
        notice = stage1.notice["event"]
        assert allowed_decisions(result=stage1) == [ACTION_ITERATE, ACTION_REDO, ACTION_HALT]
        assert notice["checks"]["passed"] is False and notice["checks"]["problems"]
        
        # A decision to accept it, even one the host signed, is refused.
        forged = write_record(
            directory=Path(tmp), record=forged_decision(notice=notice, action=ACTION_ACCEPT),
        )
        expect_state_error(
            action=lambda: orchestrator.apply_decision(path=forged),
            expected_message_part="is not among the decisions",
        )
        assert stage1.status == STAGE_AWAITING_REVIEW and orchestrator.manifest.decisions == []
        
        manifest = resume_from_file(backend=backend, manifest_path=manifest_path).run_pipeline()
        assert manifest.stages[STAGE1_NAME].status == STAGE_AWAITING_REVIEW
        assert launch_count(backend=backend) == 1, "A resume launches nothing: no automatic retry"
        
        # The owner asks for a revision: one more counted round, from the failed output.
        backend.withheld_files = set()
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        decide(
            orchestrator=resumed, stage_name=STAGE1_NAME, action=ACTION_ITERATE, host=host,
            params={"feedback": "The working document is missing. Write it."},
        )
        manifest = run_to_gate(orchestrator=resumed, host=host)
        stage1 = manifest.stages[STAGE1_NAME]
        assert (stage1.status, stage1.round, stage1.kind) == (STAGE_AWAITING_REVIEW, 2, KIND_REVISE)
        assert (stage1.outcome, stage1.stop_reason) == (OUTCOME_PASSED, STOP_PASSED)
        assert launch_count(backend=backend) == 2
        second_launch = get_launch_requests(mock_http=backend)[1]
        assert second_launch["repos"][0]["startingRef"] == failed_commit
        assert REVISION_HEADING in second_launch["prompt"]["text"]
        assert "The working document is missing. Write it." in second_launch["prompt"]["text"]
        assert [
            (previous["round"], previous["outcome"], previous["output_commit"])
            for previous in manifest.previous_rounds
        ] == [(1, OUTCOME_CHECKS_FAILED, failed_commit)], "The failed round is kept, whole"
        
        assert ACTION_ACCEPT in allowed_decisions(result=stage1)
        decide(orchestrator=resumed, stage_name=STAGE1_NAME, action=ACTION_ACCEPT, host=host)
        assert stage1.status == STAGE_COMPLETE
        assert launch_count(backend=backend) == 2
    print("✓ test_round_with_failed_checks_goes_to_the_owner_and_cannot_be_accepted passed")


def test_output_check_fails_without_a_branch_a_new_commit_or_a_readable_repository():
    """No pushed branch or no new commit ends the round; an unreadable repository does not."""
    rate_limited = (403, {"message": "API rate limit exceeded"})
    scenarios = [
        (lambda backend: setattr(backend, "agents_report_branch", False), "reported no pushed branch"),
        (lambda backend: setattr(backend, "agents_push_commit", False), "pushed no new commit"),
    ]
    for change, expected_message_part in scenarios:
        with tempfile.TemporaryDirectory() as tmp:
            backend = make_launch_mock()
            change(backend)
            orchestrator = start_project(backend=backend, directory=Path(tmp))
            
            manifest = run_to_gate(orchestrator=orchestrator)
            
            stage1 = manifest.stages[STAGE1_NAME]
            assert stage1.status == STAGE_AWAITING_REVIEW, expected_message_part
            assert stage1.outcome == OUTCOME_CHECKS_FAILED
            assert any(expected_message_part in problem for problem in stage1.gate["problems"])
            assert stage1.output_commit is None, "There is no commit to show or to hand on"
            assert backend.local_review.shown == []
            assert allowed_decisions(result=stage1) == [ACTION_REDO, ACTION_HALT]
            assert STAGE2_NAME not in manifest.stages
            assert launch_count(backend=backend) == 1
    
    # GitHub cannot be read: nothing is recorded as checked, the round is
    # not closed, and a resume checks the same output again.
    with tempfile.TemporaryDirectory() as tmp:
        backend = make_launch_mock()
        backend.script("GET", f"/contents/{WORKING_DOCUMENT_PATH}", rate_limited)
        orchestrator, result = run_stage1(backend=backend, directory=Path(tmp))
        assert result.status == STAGE_RUN_FINISHED
        assert "API rate limit exceeded" in result.error
        assert (result.outcome, result.gate, result.output_commit) == (None, None, None)
        assert backend.sleeps == [], "A refusal (HTTP 403) is not tried again"
        
        again = run_stage(
            orchestrator=orchestrator, stage=get_stage(orchestrator=orchestrator, name=STAGE1_NAME),
        )
        assert (again.status, again.round, again.outcome) == (STAGE_AWAITING_REVIEW, 1, OUTCOME_PASSED)
        assert launch_count(backend=backend) == 1
    print("✓ test_output_check_fails_without_a_branch_a_new_commit_or_a_readable_repository passed")


def test_failed_run_ends_its_round_and_is_launched_again_only_by_the_owner():
    """A failed run is never launched again by itself; the owner's redo starts a counted round."""
    with tempfile.TemporaryDirectory() as tmp:
        document_path = write_document(directory=Path(tmp), file_name="model_setup.qmd")
        manifest_path = Path(tmp) / "project_manifest.json"
        backend = make_launch_mock()
        host = FakeHost()
        backend.run_statuses[STAGE1_NAME] = ["RUNNING", "ERROR"]
        resume_arguments = ["--resume", str(manifest_path), "--config", str(REAL_CONFIG_PATH)]
        
        exit_code = run_cli(
            backend=backend,
            arguments=[
                "--execute", "--config", str(REAL_CONFIG_PATH), "--output", str(manifest_path),
            ],
            ask=answer_with(str(document_path)),
        )
        assert exit_code == EXIT_NOTICE_PENDING
        failed = load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME]
        assert (failed.status, failed.run_status) == (STAGE_NOTICE_PENDING, "ERROR")
        assert (failed.outcome, failed.stop_reason) == (OUTCOME_RUN_FAILED, STOP_INFRASTRUCTURE)
        assert failed.output_commit is None
        assert launch_count(backend=backend) == 1
        
        assert run_cli(backend=backend, arguments=resume_arguments) == EXIT_NOTICE_PENDING
        host.post_new_notices(manifest_path=manifest_path)
        assert run_cli(backend=backend, arguments=resume_arguments) == EXIT_WAITING_FOR_OWNER
        assert launch_count(backend=backend) == 1, "A resume launches nothing for a failed run"
        waiting = load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME]
        assert allowed_decisions(result=waiting) == [ACTION_REDO, ACTION_HALT]
        
        # The commands of earlier versions no longer exist.
        for removed in ("--rerun-stage", "--approve", "--revise"):
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    run_cli(backend=backend, arguments=[*resume_arguments, removed, STAGE1_NAME])
            except SystemExit as error:
                assert error.code == 2, removed
            else:
                raise AssertionError(f"{removed} must not be accepted")
        decision_path = host.write_decision(
            manifest_path=manifest_path, notice=waiting.notice["event"], action=ACTION_REDO,
        )
        refused = [
            ["--execute", "--config", str(REAL_CONFIG_PATH),
             "--output", str(Path(tmp) / "new.json"), "--apply-decision", str(decision_path)],
            [*resume_arguments, "--apply-decision", str(decision_path),
             "--output", str(Path(tmp) / "other.json")],
            [*resume_arguments, "--apply-decision", str(decision_path), "--halt", "why"],
            [*resume_arguments, "--adopt", STAGE1_NAME],
        ]
        for arguments in refused:
            assert run_cli(backend=backend, arguments=arguments) == EXIT_FAILED, arguments
            assert launch_count(backend=backend) == 1, arguments
        assert load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME] == waiting
        assert not (Path(tmp) / "new.json").exists() and not (Path(tmp) / "other.json").exists()
        
        backend.run_statuses[STAGE1_NAME] = ["FINISHED"]
        exit_code = run_cli(
            backend=backend, arguments=[*resume_arguments, "--apply-decision", str(decision_path)],
        )
        assert exit_code == EXIT_NOTICE_PENDING, "The new round finished; its notice waits for the host"
        manifest = load_manifest(manifest_path=manifest_path)
        redone = manifest.stages[STAGE1_NAME]
        assert launch_count(backend=backend) == 2
        assert (redone.round, redone.kind, redone.outcome) == (2, KIND_REDO, OUTCOME_PASSED)
        assert redone.agent_id != failed.agent_id
        assert redone.input_commit == failed.input_commit, "A redo starts from the stage's input"
        assert [
            (previous["round"], previous["outcome"], previous["agent_id"])
            for previous in manifest.previous_rounds
        ] == [(1, OUTCOME_RUN_FAILED, failed.agent_id)]
        
        # The same decision file applied again starts nothing.
        exit_code = run_cli(
            backend=backend, arguments=[*resume_arguments, "--apply-decision", str(decision_path)],
        )
        assert exit_code == EXIT_NOTICE_PENDING
        assert launch_count(backend=backend) == 2
        assert load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME].round == 2
    print("✓ test_failed_run_ends_its_round_and_is_launched_again_only_by_the_owner passed")


def test_read_requests_are_retried_a_bounded_number_of_times_with_backoff():
    """Transient read failures are retried up to max_attempts; refusals are not retried."""
    unavailable = (503, {"error": {"code": "unavailable", "message": "try later"}})
    
    def validate_models(*outcomes: Any, retries: Optional[dict] = None) -> tuple[bool, FakeServices]:
        backend = make_launch_mock()
        backend.script("GET", "/models", *outcomes)
        orchestrator = load_real_orchestrator(mock_http=backend, dry_run=False)
        if retries is not None:
            orchestrator.api_config["retries"] = retries
        is_valid, _ = orchestrator.validate_models()
        return is_valid, backend
    
    is_valid, backend = validate_models(unavailable, NO_RESPONSE)
    assert is_valid, "Two transient failures, then an answer"
    assert (len(backend.calls), backend.sleeps) == (3, [5, 15])
    
    is_valid, backend = validate_models(*[NO_RESPONSE] * 5)
    assert not is_valid
    assert len(backend.calls) == 3, "max_attempts counts every try, the first included"
    assert backend.sleeps == [5, 15], "There is no wait after the last try"
    
    is_valid, backend = validate_models(
        (401, {"error": {"code": "unauthorized", "message": "bad key"}}),
    )
    assert not is_valid
    assert (len(backend.calls), backend.sleeps) == (1, []), "A refusal is not tried again"
    
    is_valid, backend = validate_models(
        *[unavailable] * 5, retries={"max_attempts": 5, "backoff_seconds": [1, 2]},
    )
    assert not is_valid
    assert (len(backend.calls), backend.sleeps) == (5, [1, 2, 2, 2]), "The last wait repeats"
    
    backend = make_launch_mock()
    backend.script("GET", "/git/ref/heads/main", (502, {"message": "Bad Gateway"}))
    orchestrator = load_real_orchestrator(mock_http=backend, dry_run=False)
    assert orchestrator.validate_source() == (True, [])
    assert (len(backend.calls), backend.sleeps) == (3, [5]), (
        "The branch twice (502, then OK), then the committed source document"
    )
    
    transient = [
        APIError(code=code, message="", status_code=status_code).is_transient
        for code, status_code in (
            ("network_error", 0), ("x", 429), ("x", 500), ("x", 503),
            ("x", 400), ("x", 401), ("x", 403), ("x", 404), ("x", 409),
        )
    ]
    assert transient == [True] * 4 + [False] * 5
    print("✓ test_read_requests_are_retried_a_bounded_number_of_times_with_backoff passed")


def test_launch_with_an_unknown_outcome_never_creates_a_second_agent():
    """After a launch request with no clear answer, the agent is looked up before any resend."""
    stage1_of = lambda orchestrator: get_stage(orchestrator=orchestrator, name=STAGE1_NAME)
    
    # The agent was created, but the response was lost.
    with tempfile.TemporaryDirectory() as tmp:
        backend = make_launch_mock()
        backend.script("POST", "/agents", LOST_RESPONSE)
        _, result = run_stage1(backend=backend, directory=Path(tmp))
        assert result.status == STAGE_AWAITING_REVIEW
        assert launch_count(backend=backend) == 1, "The launch request is not sent again"
        assert list(backend.agents) == [result.agent_id]
        assert result.run_id == backend.agents[result.agent_id]["run_id"]
        assert backend.sleeps == [5]
    
    # The request never arrived: it is sent again under the same agent ID.
    with tempfile.TemporaryDirectory() as tmp:
        backend = make_launch_mock()
        backend.script("POST", "/agents", NO_RESPONSE)
        _, result = run_stage1(backend=backend, directory=Path(tmp))
        bodies = get_launch_requests(mock_http=backend)
        assert result.status == STAGE_AWAITING_REVIEW
        assert len(bodies) == 2 and bodies[0] == bodies[1]
        assert bodies[0]["agentId"] == result.agent_id
        assert list(backend.agents) == [result.agent_id]
    
    # The agent was created, the response was lost, and the lookup keeps
    # failing: the stage stays LAUNCHING, and a resume finds the agent.
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / "manifest.json"
        backend = make_launch_mock()
        backend.script("POST", "/agents", LOST_RESPONSE)
        backend.script("GET", AGENT_LOOKUP, NO_RESPONSE, NO_RESPONSE)
        _, result = run_stage1(backend=backend, directory=Path(tmp))
        assert result.status == STAGE_LAUNCHING
        assert "unknown" in result.error
        assert launch_count(backend=backend) == 1
        saved = load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME]
        assert (saved.status, saved.agent_id) == (STAGE_LAUNCHING, result.agent_id)
        
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        again = run_stage(orchestrator=resumed, stage=stage1_of(resumed))
        assert again.status == STAGE_AWAITING_REVIEW
        assert again.agent_id == result.agent_id
        assert launch_count(backend=backend) == 1, "The resume found the agent; it sent no launch"
        assert list(backend.agents) == [result.agent_id]
    
    # The lookup wrongly says "no such agent": the resend is answered with
    # 409 agent_id_conflict, and the agent is then found.
    with tempfile.TemporaryDirectory() as tmp:
        backend = make_launch_mock()
        backend.script("POST", "/agents", LOST_RESPONSE)
        backend.script(
            "GET", AGENT_LOOKUP,
            (404, {"error": {"code": "not_found", "message": "not visible yet"}}),
        )
        _, result = run_stage1(backend=backend, directory=Path(tmp))
        bodies = get_launch_requests(mock_http=backend)
        assert result.status == STAGE_AWAITING_REVIEW
        assert len(bodies) == 2 and bodies[0]["agentId"] == bodies[1]["agentId"]
        assert list(backend.agents) == [result.agent_id], "The same ID cannot create two agents"
    
    # The API refuses the request: no agent exists, and nothing is retried.
    with tempfile.TemporaryDirectory() as tmp:
        backend = make_launch_mock()
        backend.script(
            "POST", "/agents", (400, {"error": {"code": "invalid_request", "message": "bad field"}}),
        )
        orchestrator, result = run_stage1(backend=backend, directory=Path(tmp))
        assert (result.status, result.outcome) == (STAGE_AWAITING_REVIEW, OUTCOME_LAUNCH_REJECTED)
        assert result.stop_reason == STOP_INFRASTRUCTURE
        assert "bad field" in result.gate["problems"][0]
        assert (launch_count(backend=backend), backend.agents, backend.sleeps) == (1, {}, [])
        refused_agent_id = result.agent_id
        
        # The round is over. Nothing is sent again without the owner's decision.
        again = run_stage(orchestrator=orchestrator, stage=stage1_of(orchestrator))
        assert (again.status, again.round) == (STAGE_AWAITING_REVIEW, 1)
        assert launch_count(backend=backend) == 1
        
        decide(orchestrator=orchestrator, stage_name=STAGE1_NAME, action=ACTION_REDO)
        again = run_stage(orchestrator=orchestrator, stage=stage1_of(orchestrator))
        assert (again.status, again.round, again.outcome) == (STAGE_AWAITING_REVIEW, 2, OUTCOME_PASSED)
        assert again.agent_id != refused_agent_id
        assert launch_count(backend=backend) == 2 and len(backend.agents) == 1
    
    # The record of the launch is on disk before the request is sent.
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / "manifest.json"
        seen_on_disk = []
        
        def read_record(body: dict) -> None:
            saved = load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME]
            seen_on_disk.append((
                saved.status,
                saved.agent_id == body["agentId"],
                saved.input_commit == body["repos"][0]["startingRef"],
            ))
        
        backend = make_launch_mock()
        backend.before_launch = read_record
        run_stage1(backend=backend, directory=Path(tmp))
        assert seen_on_disk == [(STAGE_LAUNCHING, True, True)]
    
    # Without a manifest file there is nowhere to record a launch: none is sent.
    with tempfile.TemporaryDirectory() as tmp:
        backend = make_launch_mock()
        orchestrator = start_project(backend=backend, directory=Path(tmp))
        orchestrator.manifest_path = None
        try:
            run_stage(orchestrator=orchestrator, stage=stage1_of(orchestrator))
        except RuntimeError as error:
            assert "manifest file" in str(error)
        else:
            raise AssertionError("A launch without a manifest file must be refused")
        assert backend.calls == []
    print("✓ test_launch_with_an_unknown_outcome_never_creates_a_second_agent passed")


def test_run_that_is_still_going_is_waited_for_again_and_never_relaunched():
    """Polls that run out, or a status that cannot be read, leave the stage RUNNING."""
    short_polling = {"interval_seconds": 30, "max_attempts": 3}
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / "manifest.json"
        backend = make_launch_mock()
        backend.run_statuses[STAGE1_NAME] = [
            "CREATING", "RUNNING", "RUNNING", "RUNNING", "FINISHED",
        ]
        orchestrator = start_project(backend=backend, directory=Path(tmp))
        orchestrator.api_config["polling"] = short_polling
        stage1 = get_stage(orchestrator=orchestrator, name=STAGE1_NAME)
        
        result = run_stage(orchestrator=orchestrator, stage=stage1)
        assert result.status == STAGE_RUNNING
        assert "still going after 3 polls" in result.error
        assert backend.sleeps == [30, 30]
        
        backend.sleeps.clear()
        backend.script("GET", "/runs/", NO_RESPONSE, NO_RESPONSE, NO_RESPONSE)
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        result = run_stage(orchestrator=resumed, stage=stage1)
        assert result.status == STAGE_RUNNING
        assert "Could not read the run status" in result.error
        assert backend.sleeps == [5, 15]
        
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        result = run_stage(orchestrator=resumed, stage=stage1)
        assert result.status == STAGE_AWAITING_REVIEW
        assert launch_count(backend=backend) == 1, "The same run was waited for; none was launched"
        assert len(backend.agents) == 1
    print("✓ test_run_that_is_still_going_is_waited_for_again_and_never_relaunched passed")


def test_nothing_goes_on_without_a_signed_decision_for_the_exact_notice_and_commit():
    """Accepting needs the open notice's own decision and the reviewed commit; it launches nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        manifest_path = directory / "manifest.json"
        backend = make_launch_mock()
        host = FakeHost()
        orchestrator = start_project(backend=backend, directory=directory)
        
        manifest = run_to_gate(orchestrator=orchestrator, host=host)
        stage1 = manifest.stages[STAGE1_NAME]
        assert stage1.status == STAGE_AWAITING_REVIEW
        assert stage1.review == {"requested_at": stage1.review["requested_at"]}
        assert STAGE2_NAME not in manifest.stages
        assert launch_count(backend=backend) == 1
        notice = stage1.notice["event"]
        reviewed_commit = stage1.output_commit
        
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        resumed.run_pipeline()
        assert launch_count(backend=backend) == 1, "A resume without a decision launches nothing"
        stage1 = resumed.manifest.stages[STAGE1_NAME]
        
        def refused(record: dict, expected_message_part: str) -> None:
            path = write_record(directory=directory, record=record)
            expect_state_error(
                action=lambda: resumed.apply_decision(path=path),
                expected_message_part=expected_message_part,
            )
            assert stage1.status == STAGE_AWAITING_REVIEW and resumed.manifest.decisions == []
            assert launch_count(backend=backend) == 1
        
        genuine = build_decision(
            notice=notice, secret=TEST_BRIDGE_SECRET, action=ACTION_ACCEPT,
            message_ref="main-chat-reply-1", quote="accept", decided_at="2026-11-24T00:00:00+00:00",
        )
        # Changed after signing; signed with another secret; not signed at all.
        refused(
            record={**genuine, "commit": "a" * 40},
            expected_message_part="does not match its payload_sha256",
        )
        refused(
            record=build_decision(
                notice=notice, secret="another-secret-of-16-chars", action=ACTION_ACCEPT,
                message_ref="x", quote="x", decided_at="2026-11-24T00:00:00+00:00",
            ),
            expected_message_part="signature is not valid",
        )
        refused(
            record={key: value for key, value in genuine.items() if key not in SIGNATURE_FIELDS},
            expected_message_part="not signed",
        )
        # Validly signed by a holder of the secret, but for another commit,
        # round, project or configuration, or with another action's token.
        for changes, expected_message_part in (
            ({"commit": "a" * 40}, "commit is"),
            ({"round": 2}, "round is"),
            ({"project_id": "prj-000000000000"}, "project_id is"),
            ({"config_sha256": "0" * 64}, "config_sha256 is"),
            ({"event_id": notice["event_id"].replace("notice-1", "notice-2")}, "token"),
            ({"token": decision_token(
                secret=TEST_BRIDGE_SECRET, event_id=notice["event_id"], action=ACTION_REDO,
            )}, "token"),
            ({"stage": STAGE2_NAME}, "is not awaiting review"),
            ({"action": "merge"}, "action"),
        ):
            refused(
                record=forged_decision(notice=notice, action=ACTION_ACCEPT, changes=changes),
                expected_message_part=expected_message_part,
            )
        # A decision that only starts the next stage cannot be had from a review notice.
        refused(
            record=forged_decision(notice=notice, action=ACTION_START_STAGE),
            expected_message_part="is not accepted",
        )
        
        # The reviewer pushes a correction to the stage's branch: the checks
        # hold for the reviewed commit only, so it cannot be accepted.
        accept_path = write_record(directory=directory, record=genuine, name="accept.json")
        corrected_commit = backend.add_commit(
            files=["outputs/stage1_audit/reviewer_note.md"],
            parent=reviewed_commit, branch=stage1.output_branch,
        )
        expect_state_error(
            action=lambda: resumed.apply_decision(path=accept_path),
            expected_message_part=f"decide iterate with base_commit {corrected_commit}",
        )
        backend.branches[stage1.output_branch] = reviewed_commit
        
        # The local checkout must show, cleanly, exactly what is accepted.
        local = backend.local_review
        local.changed_files = [" M " + WORKING_DOCUMENT_PATH]
        expect_state_error(
            action=lambda: resumed.apply_decision(path=accept_path),
            expected_message_part="not cleanly at the reviewed commit",
        )
        local.changed_files = []
        local.branch, local.commit = "main", backend.branches["main"]
        expect_state_error(
            action=lambda: resumed.apply_decision(path=accept_path),
            expected_message_part="not cleanly at the reviewed commit",
        )
        assert stage1.status == STAGE_AWAITING_REVIEW and resumed.manifest.decisions == []
        local.branch, local.commit = stage1.output_branch, reviewed_commit
        
        resumed.apply_decision(path=accept_path)
        assert stage1.status == STAGE_COMPLETE
        assert stage1.review["accepted_commit"] == reviewed_commit
        assert stage1.review["decision_id"] == genuine["decision_id"]
        assert [decision["decision_id"] for decision in resumed.manifest.decisions] == [
            genuine["decision_id"]
        ]
        assert load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME] == stage1
        
        # Accepting launches nothing, however often the project is run again.
        resumed.run_pipeline()
        again = resume_from_file(backend=backend, manifest_path=manifest_path)
        manifest = again.run_pipeline()
        assert launch_count(backend=backend) == 1
        assert STAGE2_NAME not in manifest.stages
        
        # The same decision again does nothing; another decision for the answered notice is refused.
        again.apply_decision(path=accept_path)
        assert len(again.manifest.decisions) == 1
        other = write_record(
            directory=directory, name="redo.json",
            record=build_decision(
                notice=notice, secret=TEST_BRIDGE_SECRET, action=ACTION_REDO,
                message_ref="main-chat-reply-2", quote="redo", decided_at="2026-11-24T00:00:00+00:00",
            ),
        )
        expect_state_error(
            action=lambda: again.apply_decision(path=other),
            expected_message_part="is not awaiting review",
        )
        
        # The next stage starts only on the decision that answers the notice of the acceptance.
        receipt = again.manifest.stages[STAGE1_NAME].receipt["event"]
        assert receipt["kind"] == "accepted"
        assert [entry["action"] for entry in receipt["allowed_decisions"]] == [ACTION_START_STAGE]
        assert receipt["commits"]["output"] == reviewed_commit
        expect_state_error(
            action=lambda: again.apply_decision(path=write_record(
                directory=directory, name="start-from-review-notice.json",
                record=forged_decision(notice=notice, action=ACTION_START_STAGE),
            )),
            expected_message_part="event_id is",
        )
        decide(orchestrator=again, stage_name=STAGE1_NAME, action=ACTION_START_STAGE, host=host)
        assert launch_count(backend=backend) == 1, "The decision is recorded before anything is sent"
        assert again.manifest.stages[STAGE2_NAME].round == 1
        
        manifest = run_to_gate(orchestrator=again, host=host)
        stage2 = manifest.stages[STAGE2_NAME]
        assert stage2.status == STAGE_AWAITING_REVIEW
        assert stage2.input_commit == reviewed_commit
        assert launch_count(backend=backend) == 2
        launched_from = get_launch_requests(mock_http=backend)[1]["repos"][0]["startingRef"]
        assert launched_from == reviewed_commit
        assert STAGE3_NAME not in manifest.stages
    print("✓ test_nothing_goes_on_without_a_signed_decision_for_the_exact_notice_and_commit passed")


def test_round_limit_is_a_hard_stop_that_only_the_owner_can_raise():
    """Every round is counted before it starts; at the limit nothing more starts until it is raised."""
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / "manifest.json"
        backend = make_launch_mock()
        host = FakeHost()
        orchestrator = start_project(backend=backend, directory=Path(tmp))
        stage1_config = get_stage(orchestrator=orchestrator, name=STAGE1_NAME)
        assert [stage.max_rounds for stage in orchestrator.stages] == [4, 3, 4, 6]
        
        # The count is on disk before the launch request of its round is sent.
        counted_before_launch = []
        backend.before_launch = lambda body: counted_before_launch.append(
            load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME].round
        )
        
        first = run_to_gate(orchestrator=orchestrator, host=host).stages[STAGE1_NAME]
        assert (first.status, first.round, first.kind) == (STAGE_AWAITING_REVIEW, 1, KIND_INITIAL)
        first_commit, first_agent = first.output_commit, first.agent_id
        
        # Rounds 2 to 4: redo, iterate, redo. Each one is counted, whatever started it.
        for expected_round, action, params in (
            (2, ACTION_REDO, None),
            (3, ACTION_ITERATE, {"feedback": "Stand-in feedback."}),
            (4, ACTION_REDO, None),
        ):
            orchestrator = resume_from_file(backend=backend, manifest_path=manifest_path)
            decide(
                orchestrator=orchestrator, stage_name=STAGE1_NAME, action=action,
                params=params, host=host,
            )
            current = run_to_gate(orchestrator=orchestrator, host=host).stages[STAGE1_NAME]
            assert (current.status, current.round) == (STAGE_AWAITING_REVIEW, expected_round)
        manifest = orchestrator.manifest
        assert counted_before_launch == [1, 2, 3, 4]
        assert launch_count(backend=backend) == 4
        assert [previous["round"] for previous in manifest.previous_rounds] == [1, 2, 3]
        assert manifest.previous_rounds[0]["agent_id"] == first_agent
        assert manifest.previous_rounds[0]["output_commit"] == first_commit
        redone = manifest.previous_rounds[1]
        assert (redone["kind"], redone["input_commit"]) == (KIND_REDO, first.input_commit)
        assert redone["output_commit"] != first_commit
        
        # At the limit: the notice says so and offers no further round.
        fourth = manifest.stages[STAGE1_NAME]
        assert fourth.notice["event"]["round_limit"] == 4
        assert allowed_decisions(result=fourth) == [ACTION_ACCEPT, ACTION_RAISE_LIMIT, ACTION_HALT]
        for action, params in ((ACTION_REDO, {}), (ACTION_ITERATE, {"feedback": "x"})):
            forged = write_record(
                directory=Path(tmp),
                record=forged_decision(
                    notice=fourth.notice["event"], action=action, changes={"params": params},
                ),
            )
            expect_state_error(
                action=lambda: orchestrator.apply_decision(path=forged),
                expected_message_part="is not among the decisions",
            )
        assert orchestrator.round_limit(stage=stage1_config) == 4
        assert launch_count(backend=backend) == 4
        
        # Raising the limit starts nothing. The round is announced again,
        # and only a decision for that new notice starts a round.
        old_notice = fourth.notice["event"]
        for bad in ({}, {"by": 0}, {"by": True}, {"by": "2"}):
            path = write_record(
                directory=Path(tmp),
                record=forged_decision(
                    notice=old_notice, action=ACTION_RAISE_LIMIT, changes={"params": bad},
                ),
            )
            expect_state_error(
                action=lambda: orchestrator.apply_decision(path=path),
                expected_message_part="a whole number, 1 or more",
            )
        decide(
            orchestrator=orchestrator, stage_name=STAGE1_NAME, action=ACTION_RAISE_LIMIT,
            params={"by": 1}, host=host,
        )
        assert orchestrator.manifest.round_limit_raises == {STAGE1_NAME: 1}
        assert (fourth.status, fourth.round) == (STAGE_NOTICE_PENDING, 4)
        assert launch_count(backend=backend) == 4
        
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        assert resumed.round_limit(stage=stage1_config) == 5, "The raise is kept across a resume"
        fourth = run_to_gate(orchestrator=resumed, host=host).stages[STAGE1_NAME]
        new_notice = fourth.notice["event"]
        assert new_notice["event_id"] != old_notice["event_id"] and new_notice["round_limit"] == 5
        assert allowed_decisions(result=fourth) == [
            ACTION_ACCEPT, ACTION_ITERATE, ACTION_REDO, ACTION_HALT,
        ]
        stale = write_record(
            directory=Path(tmp),
            record=forged_decision(notice=old_notice, action=ACTION_REDO),
        )
        expect_state_error(
            action=lambda: resumed.apply_decision(path=stale),
            expected_message_part="event_id is",
        )
        
        decide(orchestrator=resumed, stage_name=STAGE1_NAME, action=ACTION_REDO, host=host)
        fifth = run_to_gate(orchestrator=resumed, host=host).stages[STAGE1_NAME]
        assert (fifth.status, fifth.round) == (STAGE_AWAITING_REVIEW, 5)
        assert allowed_decisions(result=fifth) == [ACTION_ACCEPT, ACTION_RAISE_LIMIT, ACTION_HALT]
        assert launch_count(backend=backend) == 5
        
        # An accepted stage takes no further decision.
        decide(orchestrator=resumed, stage_name=STAGE1_NAME, action=ACTION_ACCEPT, host=host)
        expect_state_error(
            action=lambda: resumed.apply_decision(path=write_record(
                directory=Path(tmp),
                record=forged_decision(notice=fifth.notice["event"], action=ACTION_RAISE_LIMIT),
            )),
            expected_message_part="is not awaiting review; its status is COMPLETE",
        )
        assert launch_count(backend=backend) == 5
    print("✓ test_round_limit_is_a_hard_stop_that_only_the_owner_can_raise passed")


def test_artifacts_are_downloaded_only_to_safe_paths_inside_the_project_folder():
    """Safe artifacts are saved and recorded; unsafe paths are recorded and never written."""
    safe = {
        "artifacts/plot.png": b"\x89PNG stand-in bytes",
        "artifacts/tables/result.csv": b"a,b\n1,2\n",
    }
    unsafe = [
        "../escape.txt",
        "artifacts/../../escape.txt",
        "/abs/escape.txt",
        "artifacts\\escape.txt",
        "C:/escape.txt",
        "artifacts/NUL.txt",
        "artifacts/trailing./x.txt",
        "",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        manifest_path = directory / "manifest.json"
        backend = make_launch_mock()
        backend.artifacts[STAGE1_NAME] = {
            **safe, **{path: b"must not be written" for path in unsafe},
        }
        
        # A download URL that is not https is refused, and nothing is fetched.
        backend.download_scheme = "http"
        orchestrator, result = run_stage1(backend=backend, directory=directory)
        assert result.status == STAGE_ARTIFACTS_FAILED
        assert "not an https URL" in result.error
        assert backend.downloads == []
        
        # A resume collects again (one transient download failure on the way).
        backend.download_scheme = "https"
        backend.script("DOWNLOAD", "plot.png", (503, b""))
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        result = run_stage(orchestrator=resumed, stage=get_stage(orchestrator=resumed, name=STAGE1_NAME))
        assert result.status == STAGE_AWAITING_REVIEW
        assert launch_count(backend=backend) == 1, "Collecting again launches no agent"
        
        saved_folder = f"collected_artifacts/manifest/{STAGE1_NAME}/round_1/{PHASE_PROPOSE}"
        files_on_disk = {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()
        }
        notice_id = result.notice["event"]["event_id"]
        assert set(files_on_disk) == {
            "model_setup.qmd", "manifest.json", "manifest.trial-journal.jsonl",
            f"review_bridge/manifest/outbox/{notice_id}.json",
            f"review_bridge/manifest/inbox/{notice_id}.ack.json",
            *(f"{saved_folder}/{path}" for path in safe),
        }, "Nothing else was written anywhere in the project folder"
        for path, content in safe.items():
            assert files_on_disk[f"{saved_folder}/{path}"] == content
        
        assert [
            (item["path"], item["size_bytes"], item["sha256"])
            for item in result.artifacts["downloaded"]
        ] == [
            (path, len(content), hashlib.sha256(content).hexdigest())
            for path, content in safe.items()
        ]
        assert [item["path"] for item in result.artifacts["rejected"]] == [
            repr(path) for path in unsafe
        ]
        asked_for = [
            parse_qs(urlsplit(url).query)["path"][0]
            for _, url, _, _ in backend.calls if "/artifacts/download" in url
        ]
        assert set(asked_for) == set(safe), "No download URL is requested for an unsafe path"
        assert len(backend.downloads) == 3, "plot.png twice (503, then OK), result.csv once"
        
        for path in [*unsafe, None, 3, "a/./b", "a//b", "con", "LPT1.txt", "a/b ", "tab\tname"]:
            assert unsafe_relative_path_reason(path) is not None, repr(path)
        for path in (*safe, "a/b/c.d.e", "console.txt", "file name.txt", "ünïcode.png"):
            assert unsafe_relative_path_reason(path) is None, path
        
        root = directory / "download_root"
        root.mkdir()
        try:
            os.symlink(directory, root / "link", target_is_directory=True)
        except OSError:
            print("  (symbolic links cannot be made here; that check is skipped)")
        else:
            try:
                safe_artifact_destination(root=root, artifact_path="link/escape.txt")
            except UnsafeArtifactPathError as error:
                assert "outside the download folder" in str(error)
            else:
                raise AssertionError("A path that leaves the folder through a link must be refused")
    print("✓ test_artifacts_are_downloaded_only_to_safe_paths_inside_the_project_folder passed")


def test_validate_config_checks_retry_review_path_and_repository_settings():
    """Retry, review, required-file, artifact and repository settings are checked offline."""
    def validate(change: Callable[[dict], object]) -> list[str]:
        config = copy.deepcopy(MOCK_CONFIG)
        change(config)
        return Orchestrator(config=config, dry_run=True).validate_config()[1]
    
    assert validate(lambda config: None) == []
    
    cases = [
        (lambda c: c["api"].pop("retries"), ["max_attempts", "backoff_seconds"]),
        (lambda c: c["api"]["retries"].update(max_attempts=0), ["max_attempts"]),
        (lambda c: c["api"]["retries"].update(max_attempts=True), ["max_attempts"]),
        (lambda c: c["api"]["retries"].update(backoff_seconds=[]), ["backoff_seconds"]),
        (lambda c: c["api"]["retries"].update(backoff_seconds=[5, -1]), ["backoff_seconds"]),
        (lambda c: c["stages"][0].update(manual_review=False), ["manual_review is not a setting"]),
        (lambda c: c["stages"][0].update(manual_review=True), ["manual_review is not a setting"]),
        (lambda c: c.pop("review"), ["review.local_checkout", "review.remote", "review.delivery"]),
        (lambda c: c["review"].update(remote=""), ["review.remote"]),
        (lambda c: c["review"].pop("delivery"), ["review.delivery"]),
        (lambda c: c["review"]["delivery"].update(directory="../outside"), ["review.delivery.directory"]),
        (lambda c: c["review"]["delivery"].update(secret_env="not a name"), ["review.delivery.secret_env"]),
        (lambda c: c["review"]["delivery"].update(secret="the secret itself"), ["review.delivery"]),
        (lambda c: c.update(working_document={"path": "docs/w.md"}),
         ["working_document.source_document"]),
        (lambda c: c.update(working_document={"path": "../w.md", "source_document": "s.md"}),
         ["working_document.path"]),
        (lambda c: c["stages"][0]["output_manifest"].update(completion_record="/abs.json"),
         ["completion_record"]),
        (lambda c: c["stages"][0].update(output_manifest={"required_files": ["../secret.txt"]}),
         ["required_files entry '../secret.txt'"]),
        (lambda c: c["stages"][0].update(output_manifest={"required_files": "output1.txt"}),
         ["must be a list"]),
        (lambda c: c["pipeline"]["repository"].update(url="https://gitlab.com/test/repo"),
         ["github.com"]),
        (lambda c: c.update(artifacts={"collect": True}), ["local_output_dir"]),
        (lambda c: c.update(artifacts={"collect": True, "local_output_dir": "../outside"}),
         ["local_output_dir"]),
        (lambda c: c.update(artifacts={"collect": "yes"}), ["artifacts.collect"]),
    ]
    for number, (change, expected_parts) in enumerate(cases, start=1):
        errors = validate(change)
        assert len(errors) == len(expected_parts), f"case {number}: {errors}"
        for expected_part, error in zip(expected_parts, errors):
            assert expected_part in error, f"case {number}: {error}"
    
    committed = load_config(config_path=REAL_CONFIG_PATH)
    assert all("manual_review" not in stage for stage in committed["stages"])
    assert committed["working_document"] == {
        "path": WORKING_DOCUMENT_PATH, "source_document": SOURCE_DOCUMENT_PATH,
    }
    assert committed["review"] == {
        "local_checkout": "..", "remote": "origin",
        "delivery": {"directory": "review_bridge", "secret_env": BRIDGE_SECRET_ENV},
    }
    assert committed["trial_log"] == {"path": TRIAL_LOG_PATH}
    assert committed["execution"] == {"auto_create_pr": False}
    assert committed["artifacts"]["collect"] is True
    print("✓ test_validate_config_checks_retry_review_path_and_repository_settings passed")


def test_clients_send_the_documented_requests():
    """The GitHub reader only reads; the API client sends agentId and can look an agent up."""
    backend = make_launch_mock()
    reader = GitHubRepoReader(repo_url=backend.repo_url, token="test-token", http_client=backend)
    
    main_commit = reader.get_branch_commit(branch="main")
    assert main_commit == backend.branches["main"]
    assert reader.get_commit(commit=main_commit) == main_commit
    
    branch_commit = backend.add_commit(
        files=["outputs/a/file.txt"], parent=main_commit, branch="cursor/with/slashes-01",
    )
    assert reader.get_branch_commit(branch="cursor/with/slashes-01") == branch_commit
    assert backend.calls[-1][1].endswith("/git/ref/heads/cursor/with/slashes-01")
    assert reader.file_exists(path="outputs/a/file.txt", commit=branch_commit) is True
    assert reader.file_exists(path="outputs/a", commit=branch_commit) is False, "A folder is not a file"
    assert reader.file_exists(path="outputs/a/file.txt", commit=main_commit) is False
    assert reader.get_file_bytes(path="outputs/a/file.txt", commit=branch_commit) == (
        backend.files[branch_commit]["outputs/a/file.txt"]
    )
    assert reader.get_file_bytes(path="outputs/a/file.txt", commit=main_commit) is None
    assert reader.get_file_bytes(path="outputs/a", commit=branch_commit) is None
    
    # GET /compare/{base}...{head}: how two commits relate and which files differ.
    other_commit = backend.add_commit(files=["other.txt"], parent=main_commit)
    assert reader.compare(base=main_commit, head=branch_commit) == {
        "status": "ahead", "files": ["outputs/a/file.txt"], "truncated": False,
    }
    assert backend.calls[-1][1].endswith(f"/compare/{main_commit}...{branch_commit}")
    assert reader.compare(base=main_commit, head=main_commit)["status"] == "identical"
    assert reader.compare(base=branch_commit, head=main_commit)["status"] == "behind"
    assert reader.compare(base=other_commit, head=branch_commit)["status"] == "diverged"
    backend.script("GET", "/compare/", (200, {
        "status": "ahead",
        "files": [{"filename": f"f{number}.txt"} for number in range(reader.COMPARE_FILE_LIMIT)],
    }))
    assert reader.compare(base=main_commit, head=branch_commit)["truncated"] is True, (
        "A full page of files may not be the whole list"
    )
    
    for read_missing in (
        lambda: reader.get_branch_commit(branch="no-such-branch"),
        lambda: reader.get_commit(commit="f" * 40),
    ):
        try:
            read_missing()
        except GitHubAPIError as error:
            assert error.status_code == 404 and not error.is_transient
        else:
            raise AssertionError("Reading something that does not exist must fail")
    
    assert all(method == "GET" for method, _, _, _ in backend.calls)
    assert all(headers["Authorization"] == "Bearer test-token" for _, _, headers, _ in backend.calls)
    assert reader.is_same_repo(repo_url="github.com/YangLuUST/rmb_ai.git")
    assert not reader.is_same_repo(repo_url="github.com/someone-else/RMB_AI")
    
    client = CursorAPIClient(
        api_key="test-key", http_client=backend, download_client=backend.download,
    )
    agent_id = "bc-00000000-0000-4000-8000-000000000001"
    launch = dict(
        prompt_text="Stand-in prompt.",
        repo_url=backend.repo_url,
        starting_ref=main_commit,
        name="Pipeline: Equilibrium Derivation (round 1)",
        agent_id=agent_id,
    )
    run = client.create_agent(**launch)
    body = backend.calls[-1][3]
    assert body["agentId"] == agent_id
    assert body["repos"] == [{"url": backend.repo_url, "startingRef": main_commit}]
    
    found = client.get_agent(agent_id=agent_id)
    assert (found.agent_id, found.run_id) == (agent_id, run.run_id)
    
    for request, expected_status, expected_code in (
        (lambda: client.create_agent(**launch), 409, "agent_id_conflict"),
        (lambda: client.get_agent(agent_id="bc-00000000-0000-4000-8000-00000000ffff"),
         404, "not_found"),
    ):
        try:
            request()
        except CursorAPIError as error:
            assert (error.status_code, error.code) == (expected_status, expected_code)
        else:
            raise AssertionError(f"Expected HTTP {expected_status}")
    assert len(backend.agents) == 1
    print("✓ test_clients_send_the_documented_requests passed")


def test_no_stage_can_be_configured_to_skip_its_review():
    """Every stage of the committed config stops at the human gate, the last one included."""
    backend = make_launch_mock()
    with tempfile.TemporaryDirectory() as tmp:
        orchestrator = start_project(backend=backend, directory=Path(tmp))
        assert [stage.name for stage in orchestrator.stages] == list(ALL_STAGE_NAMES)
        assert orchestrator.validate_config() == (True, [])
        assert not hasattr(orchestrator.stages[0], "manual_review")
        assert [stage.autonomous_fix for stage in orchestrator.stages] == [False] * 4
        assert [stage.cross_model_enabled for stage in orchestrator.stages] == [False] * 4
        
        for number, name in enumerate(ALL_STAGE_NAMES[:3], start=1):
            manifest = run_to_gate(orchestrator=orchestrator)
            assert manifest.stages[name].status == STAGE_AWAITING_REVIEW
            assert launch_count(backend=backend) == number
            approve_as_reviewer(orchestrator=orchestrator, stage_name=name)
        
        # The last stage: a domain proposal, then one approved batch.
        last = run_to_gate(orchestrator=orchestrator).stages[STAGE4_NAME]
        assert last.status == STAGE_AWAITING_REVIEW and ACTION_ACCEPT not in allowed_decisions(last)
        decide(
            orchestrator=orchestrator, stage_name=STAGE4_NAME, action=ACTION_ITERATE,
            params={"approve_batch": True},
        )
        manifest = run_to_gate(orchestrator=orchestrator)
        last = manifest.stages[STAGE4_NAME]
        assert last.status == STAGE_AWAITING_REVIEW, "The last stage waits for a decision too"
        assert last.review == {"requested_at": last.review["requested_at"]}
        assert orchestrator.run_pipeline().completed_at is None
        assert launch_count(backend=backend) == 5, "Running again launches nothing"
        
        decide(orchestrator=orchestrator, stage_name=STAGE4_NAME, action=ACTION_ACCEPT)
        manifest = orchestrator.run_pipeline()
        assert last.status == STAGE_COMPLETE and manifest.completed_at is not None
        assert last.review["accepted_commit"] == last.output_commit
        assert launch_count(backend=backend) == 5
    print("✓ test_no_stage_can_be_configured_to_skip_its_review passed")


def test_working_document_rules_are_in_each_prompt_and_checked_at_the_pushed_commit():
    """Each stage is told its section's marker lines; the output check enforces the same rules."""
    with tempfile.TemporaryDirectory() as tmp:
        document_path = write_document(directory=Path(tmp), file_name="model_setup.md")
        backend = make_launch_mock()
        orchestrator = load_real_orchestrator(
            mock_http=backend, dry_run=False, ask=answer_with(str(document_path)),
        )
        orchestrator.start_new_project()
        manifest = run_approving_every_stage(orchestrator=orchestrator)
    
    # The fifth launch is the last stage's second round.
    prompts = [body["prompt"]["text"] for body in get_launch_requests(mock_http=backend)][:4]
    assert len(prompts) == 4
    markers = [
        stage_section_markers(number=number, name=name)
        for number, name in enumerate(ALL_STAGE_NAMES, start=1)
    ]
    for number, prompt_text in enumerate(prompts, start=1):
        assert WORKING_DOCUMENT_HEADING in prompt_text
        assert f"Path in the repository: {WORKING_DOCUMENT_PATH}" in prompt_text
        assert f"This is stage {number} of 4." in prompt_text
        for other_number, (begin, end) in enumerate(markers, start=1):
            in_prompt = begin in prompt_text and end in prompt_text
            assert in_prompt == (other_number == number), (number, other_number)
        assert ("stay byte-for-byte as they are in the commit you start from" in prompt_text) == (
            number > 1
        )
        assert ("no marker line of a later stage" in prompt_text) == (number < 4)
        assert f"The file {SOURCE_DOCUMENT_PATH}" in prompt_text
    
    # The final working document holds the four sections, in order, each
    # as the stage that wrote it left it.
    final_commit = manifest.stages[STAGE4_NAME].output_commit
    final_text = backend.files[final_commit][WORKING_DOCUMENT_PATH].decode("utf-8")
    for number, name in enumerate(ALL_STAGE_NAMES, start=1):
        section = extract_stage_section(text=final_text, number=number, name=name)
        assert section.strip().startswith(
            f"Stand-in section of stage {number}, attempt {2 if number == 4 else 1}."
        )
        written_at = manifest.stages[name].output_commit
        assert section == extract_stage_section(
            text=backend.files[written_at][WORKING_DOCUMENT_PATH].decode("utf-8"),
            number=number, name=name,
        )
    assert backend.files[final_commit][SOURCE_DOCUMENT_PATH] == (
        backend.files[backend.branches["main"]][SOURCE_DOCUMENT_PATH]
    )
    
    # A stage that breaks a rule fails the output check: the round is shown
    # to the owner as failed and cannot be accepted.
    faults = [
        (STAGE1_NAME, "no_section", "the section of stage 1 must be enclosed"),
        (STAGE1_NAME, "empty_section", "the section of stage 1 in"),
        (STAGE1_NAME, "later_marker", "holds a marker of stage 2"),
        (STAGE1_NAME, "source_changed", f"the source document {SOURCE_DOCUMENT_PATH} was changed"),
        (STAGE2_NAME, "earlier_changed", "the section of stage 1 ('equilibrium_derivation')"),
        (STAGE2_NAME, "later_marker", "holds a marker of stage 3"),
    ]
    for stage_name, fault, expected_message_part in faults:
        backend = make_launch_mock()
        orchestrator = load_real_orchestrator(mock_http=backend, dry_run=False)
        earlier = ALL_STAGE_NAMES[:ALL_STAGE_NAMES.index(stage_name)]
        open_project_by_hand(orchestrator=orchestrator, backend=backend, completed=earlier)
        orchestrator.model_setup_document = load_model_setup_document(
            location=str(DOCS_DIR / Path(SOURCE_DOCUMENT_PATH).name),
        )
        backend.working_document_fault = fault
        result = run_stage(
            orchestrator=orchestrator, stage=get_stage(orchestrator=orchestrator, name=stage_name),
        )
        assert (result.status, result.outcome) == (STAGE_AWAITING_REVIEW, OUTCOME_CHECKS_FAILED), fault
        assert any(expected_message_part in problem for problem in result.gate["problems"]), (
            result.gate["problems"]
        )
        assert result.gate["passed"] is False and result.gate["missing_files"] == []
        assert ACTION_ACCEPT not in allowed_decisions(result=result), fault
    
    for text, expected_message_part in (
        ("no markers", "found 0 and 0"),
        (f"{markers[0][0]}\nx\n{markers[0][1]}\n{markers[0][0]}\n", "found 2 and 1"),
        (f"{markers[0][1]}\nx\n{markers[0][0]}\n", "comes before its begin marker"),
        (f"text {markers[0][0]}\nx\n{markers[0][1]}\n", "found 0 and 1"),
    ):
        try:
            extract_stage_section(text=text, number=1, name=STAGE1_NAME)
        except WorkingDocumentError as error:
            assert expected_message_part in str(error), str(error)
        else:
            raise AssertionError(f"Expected {text!r} to be refused")
    crlf_text = f"Intro\r\n{markers[0][0]}\r\nline one\r\n{markers[0][1]}\r\n"
    assert extract_stage_section(text=crlf_text, number=1, name=STAGE1_NAME) == "\nline one\r\n"
    print("✓ test_working_document_rules_are_in_each_prompt_and_checked_at_the_pushed_commit passed")


def test_stage4_proposes_a_domain_first_and_computes_only_batches_the_owner_approved():
    """Round 1 computes nothing; a batch runs only after approve_batch, inside the approved domain."""
    spec_path = "outputs/stage4_results/next_batch_spec.json"
    
    def stage4_project(
        edit_batch: Optional[Callable[[dict], Any]] = None,
        batch_files: Optional[dict] = None,
    ) -> tuple[Orchestrator, FakeServices, StageConfig]:
        """A project whose first three stages are recorded as accepted, by hand."""
        backend = make_launch_mock()
        if edit_batch is not None:
            backend.edit_evidence = lambda name, evidence: (
                edit_batch(evidence) if evidence.get("phase") == verification.PHASE_BATCH
                else evidence
            )
        orchestrator = load_real_orchestrator(mock_http=backend, dry_run=False)
        open_project_by_hand(
            orchestrator=orchestrator, backend=backend, completed=ALL_STAGE_NAMES[:3],
        )
        return orchestrator, backend, get_stage(orchestrator=orchestrator, name=STAGE4_NAME)
    
    def approve_batch(orchestrator: Orchestrator) -> None:
        decide(
            orchestrator=orchestrator, stage_name=STAGE4_NAME, action=ACTION_ITERATE,
            params={"approve_batch": True},
        )
    
    orchestrator, backend, stage4 = stage4_project()
    assert stage4.verification["max_configurations"] == 25
    assert stage4.autonomous_fix is False
    
    # Round 1: a domain proposal. It passes its checks and is not acceptable.
    proposal = run_stage(orchestrator=orchestrator, stage=stage4)
    assert (proposal.status, proposal.outcome) == (STAGE_AWAITING_REVIEW, OUTCOME_PASSED)
    assert proposal.gate["passed"] is True and proposal.gate["acceptable"] is False
    assert proposal.gate["evidence"]["summary"]["phase"] == verification.PHASE_DOMAIN_PROPOSAL
    assert allowed_decisions(result=proposal) == [ACTION_ITERATE, ACTION_REDO, ACTION_HALT]
    prompts = [body["prompt"]["text"] for body in get_launch_requests(mock_http=backend)]
    assert "THIS ROUND IS A DOMAIN PROPOSAL" in prompts[0]
    assert "at most 25 configurations" in prompts[0]
    proposal_commit = proposal.output_commit
    assert spec_path in backend.files[proposal_commit]
    assert not any("/batch_0" in path for path in backend.files[proposal_commit])
    expect_state_error(
        action=lambda: orchestrator.apply_decision(path=write_record(
            directory=orchestrator.manifest_path.parent,
            record=forged_decision(notice=proposal.notice["event"], action=ACTION_ACCEPT),
        )),
        expected_message_part="is not among the decisions",
    )
    
    # Feedback without approve_batch: the next round is a proposal again.
    decide(
        orchestrator=orchestrator, stage_name=STAGE4_NAME, action=ACTION_ITERATE,
        params={"feedback": "Stand-in feedback on the proposed domain."},
    )
    second = run_stage(orchestrator=orchestrator, stage=stage4)
    assert second.gate["evidence"]["summary"]["phase"] == verification.PHASE_DOMAIN_PROPOSAL
    assert ACTION_ACCEPT not in allowed_decisions(result=second)
    
    # approve_batch: the round runs the batch as it is at the approved commit.
    approved_spec = backend.files[second.output_commit][spec_path]
    approve_batch(orchestrator=orchestrator)
    batch = run_stage(orchestrator=orchestrator, stage=stage4)
    assert (batch.round, batch.outcome) == (3, OUTCOME_PASSED)
    assert batch.gate["acceptable"] is True
    summary = batch.gate["evidence"]["summary"]
    assert (summary["phase"], summary["configurations"]) == (verification.PHASE_BATCH, 2)
    assert summary["by_classification"] == {
        "admissible": 1, "inadmissible": 1, "numerical_failure": 0, "outside_domain": 0,
    }
    assert allowed_decisions(result=batch)[0] == ACTION_ACCEPT
    batch_prompt = get_launch_requests(mock_http=backend)[2]["prompt"]["text"]
    assert "THIS ROUND RUNS ONE APPROVED BATCH" in batch_prompt
    assert f"SHA-256 {hashlib.sha256(approved_spec).hexdigest()}" in batch_prompt
    assert batch.revision["approve_batch"] is True
    assert batch.baseline_commit == proposal.input_commit, "Checked against the stage's own input"
    assert launch_count(backend=backend) == 3
    
    # A batch round that breaks a rule fails its checks.
    def with_third_run(evidence: dict) -> dict:
        evidence["configurations"].append({**evidence["configurations"][0], "id": "c3"})
        return evidence
    
    def with_failure(evidence: dict, **fields: Any) -> dict:
        evidence["configurations"][1] = {
            **evidence["configurations"][1], "classification": "numerical_failure", **fields,
        }
        return evidence
    
    def with_claim(evidence: dict, **fields: Any) -> dict:
        evidence["boundary_claims"] = [{
            "claim": "stand-in claim", "status": "evidence_not_proof",
            "admissible": ["c1"], "inadmissible": ["c2"], "reproduced_with": ["c1"], **fields,
        }]
        return evidence
    
    outside = {
        "outputs/stage4_results/batch_02/c1_parameters.json": b'{"parameters": {"beta": 0.5}}',
    }
    batch_faults = [
        (with_third_run, None, "3 configurations were run; the approved batch allows 2"),
        (None, outside, "c1 is outside the approved domain: beta = 0.5 is outside [0.9, 0.99]"),
        (lambda evidence: with_failure(evidence), None, 'a numerical failure needs a "failure_reason"'),
        (lambda evidence: with_failure(with_claim(evidence), failure_reason="no convergence"),
         None, "a numerical failure is not evidence of a boundary"),
        (lambda evidence: with_claim(evidence, status="proof"), None, "not a proof that no equilibrium exists"),
        (lambda evidence: with_claim(evidence, reproduced_with=[]), None, 'needs "reproduced_with"'),
        (lambda evidence: {**evidence, "approved_spec_sha256": "0" * 64}, None,
         "is not the SHA-256 of the batch the owner approved"),
        (lambda evidence: {**evidence, "phase": verification.PHASE_DOMAIN_PROPOSAL}, None,
         '"phase" must be "batch" in this round'),
    ]
    for edit_batch, batch_files, expected_message_part in batch_faults:
        orchestrator, backend, stage4 = stage4_project(edit_batch=edit_batch)
        run_stage(orchestrator=orchestrator, stage=stage4)
        approve_batch(orchestrator=orchestrator)
        backend.extra_files = batch_files or {}
        failed = run_stage(orchestrator=orchestrator, stage=stage4)
        assert failed.outcome == OUTCOME_CHECKS_FAILED, expected_message_part
        assert any(expected_message_part in problem for problem in failed.gate["problems"]), (
            failed.gate["problems"]
        )
        assert ACTION_ACCEPT not in allowed_decisions(result=failed)
    
    # A boundary claim with an admissible and an inadmissible run, repeated,
    # passes the check. It stays evidence: the check is of form.
    orchestrator, backend, stage4 = stage4_project(edit_batch=with_claim)
    run_stage(orchestrator=orchestrator, stage=stage4)
    approve_batch(orchestrator=orchestrator)
    assert run_stage(orchestrator=orchestrator, stage=stage4).outcome == OUTCOME_PASSED
    
    # A proposal that computes, or proposes more runs than the limit, fails.
    too_many = json.dumps({
        "domain": {"beta": {"values": [0.95], "origin": "source", "reason": "stand-in"}},
        "max_configurations": 26, "seeds": [1],
        "solver": {"residual_tolerance": 1e-8, "max_iterations": 10},
    }).encode("utf-8")
    proposal_faults = [
        (lambda name, evidence: {**evidence, "configurations": [{"id": "c1"}]}, {},
         "a domain proposal computes nothing"),
        (None, {spec_path: too_many}, '"max_configurations" must be a whole number from 1 to 25'),
    ]
    for edit_evidence, extra_files, expected_message_part in proposal_faults:
        orchestrator, backend, stage4 = stage4_project()
        backend.edit_evidence, backend.extra_files = edit_evidence, extra_files
        failed = run_stage(orchestrator=orchestrator, stage=stage4)
        assert failed.outcome == OUTCOME_CHECKS_FAILED, expected_message_part
        assert any(expected_message_part in problem for problem in failed.gate["problems"]), (
            failed.gate["problems"]
        )
        # A batch that fails its own check cannot be approved either.
        if extra_files:
            expect_state_error(
                action=lambda: approve_batch(orchestrator=orchestrator),
                expected_message_part="The batch cannot be approved",
            )
            assert launch_count(backend=backend) == 1
    
    stage3 = get_stage(orchestrator=orchestrator, name=STAGE3_NAME)
    assert "outputs/stage3_implementation/solve_equilibrium.py" in (
        stage3.output_manifest["required_files"]
    ), "Stage 3 must hand on Python code, not only a README"
    assert "outputs/stage3_implementation/test_solve_equilibrium.py" in (
        stage3.output_manifest["required_files"]
    )
    print("✓ test_stage4_proposes_a_domain_first_and_computes_only_batches_the_owner_approved passed")


def test_output_is_awaiting_review_only_after_the_local_checkout_and_the_hosts_ack():
    """Fail closed: no notice without the local checkout, no review without the host's signed ack."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        manifest_path = directory / "manifest.json"
        backend = make_launch_mock()
        local = backend.local_review
        host = FakeHost()
        bridge = host.bridge_of(manifest_path=manifest_path)
        
        # Before a launch: an unusable checkout stops the paid run from starting.
        local.changed_files = ["docs/my_notes.md"]
        orchestrator, result = run_stage1(backend=backend, directory=directory)
        assert result.status == STAGE_BLOCKED
        assert "No agent was launched" in result.error and "docs/my_notes.md" in result.error
        assert launch_count(backend=backend) == 0
        local.changed_files = []
        stage1 = get_stage(orchestrator=orchestrator, name=STAGE1_NAME)
        
        # So does a missing secret: its notice could not be signed afterwards.
        orchestrator.bridge_secret = "too short"
        result = orchestrator._run_stage(stage=stage1)
        assert result.status == STAGE_BLOCKED and BRIDGE_SECRET_ENV in result.error
        assert launch_count(backend=backend) == 0
        orchestrator.bridge_secret = TEST_BRIDGE_SECRET
        
        # After the run: the output passes its checks on GitHub, but the
        # checkout cannot show it. No notice is written.
        def fail_after_launch(body: dict) -> None:
            local.problem = "stand-in problem: the checkout cannot be used"
        
        backend.before_launch = fail_after_launch
        result = orchestrator._run_stage(stage=stage1)
        assert (result.status, result.round) == (STAGE_SYNC_PENDING, 1)
        assert "is NOT in the local checkout" in result.error
        assert "stand-in problem" in result.error
        assert result.output_commit is not None and result.gate["passed"] is True
        assert result.notice is None and result.review is None, "It is not announced"
        assert result.local_sync["verified"] is False
        assert local.shown == [] and list(bridge.outbox.glob("*")) == []
        
        # A resume tries the checkout again. It launches nothing.
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        manifest = resumed.run_pipeline()
        result = manifest.stages[STAGE1_NAME]
        assert result.status == STAGE_SYNC_PENDING and STAGE2_NAME not in manifest.stages
        assert launch_count(backend=backend) == 1
        
        # The checkout works again: the notice is written, signed, and waits for the host.
        local.problem = None
        manifest = resume_from_file(backend=backend, manifest_path=manifest_path).run_pipeline()
        result = manifest.stages[STAGE1_NAME]
        assert result.status == STAGE_NOTICE_PENDING
        assert "has not acknowledged" in result.error and result.review is None
        assert local.shown == [(result.output_branch, result.output_commit)]
        assert (local.branch, local.commit) == (result.output_branch, result.output_commit)
        assert result.local_sync["verified"] is True
        assert result.local_sync["commit"] == result.output_commit
        assert result.local_sync["review_files"] == [
            str(local.path / path) for path in (
                WORKING_DOCUMENT_PATH, "outputs/stage1_audit/audit.json", TRIAL_LOG_PATH,
            )
        ]
        notice_files = list(bridge.outbox.glob("*.json"))
        assert len(notice_files) == 1 and list(bridge.inbox.glob("*")) == []
        notice = read_json(path=notice_files[0])
        verify(record=notice, secret=TEST_BRIDGE_SECRET, expected_type="notice")
        assert notice == result.notice["event"]
        assert TEST_BRIDGE_SECRET not in notice_files[0].read_text(encoding="utf-8")
        
        # What the notice tells the owner: where the output and the trial
        # log are, locally and on GitHub, the evidence, and why it stopped.
        assert notice["commits"]["output"] == result.output_commit
        assert notice["branch"] == result.output_branch
        assert notice["local"]["at_output_commit"] is True
        assert notice["local"]["review_files"] == result.local_sync["review_files"]
        assert notice["local"]["trial_log"] == str(local.path / TRIAL_LOG_PATH)
        assert notice["local"]["trial_journal"] == str(manifest_path.with_name("manifest.trial-journal.jsonl"))
        assert notice["on_github"] == {
            "commit": f"https://github.com/yangluust/RMB_AI/tree/{result.output_commit}",
            "trial_log": TRIAL_LOG_PATH,
        }
        assert (notice["outcome"], notice["stop_reason"]) == (OUTCOME_PASSED, STOP_PASSED)
        assert (notice["round"], notice["round_limit"]) == (1, 4)
        assert notice["checks"]["passed"] is True and notice["checks"]["evidence"]["present"] is True
        assert "not a proof" in notice["checks"]["note"]
        assert result.output_commit in notice["message"] and "not a proof" in notice["message"]
        assert [entry["action"] for entry in notice["allowed_decisions"]] == [
            ACTION_ACCEPT, ACTION_ITERATE, ACTION_REDO, ACTION_HALT,
        ]
        assert len({entry["token"] for entry in notice["allowed_decisions"]}) == 4
        
        # What is printed on the console is not delivery: running again changes nothing.
        saved_notice = notice_files[0].read_bytes()
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        assert resumed.run_pipeline().stages[STAGE1_NAME].status == STAGE_NOTICE_PENDING
        assert [path.read_bytes() for path in bridge.outbox.glob("*.json")] == [saved_notice]
        
        # No decision is applied before the notice was acknowledged.
        early = host.write_decision(manifest_path=manifest_path, notice=notice, action=ACTION_ACCEPT)
        expect_state_error(
            action=lambda: resumed.apply_decision(path=early),
            expected_message_part="is not awaiting review; its status is NOTICE_PENDING",
        )
        
        # An ack that cannot be trusted is not an ack.
        ack_path = bridge.ack_path(event_id=notice["event_id"])
        genuine_ack = build_ack(
            notice=notice, secret=TEST_BRIDGE_SECRET, message_ref="main-chat-message-1",
            posted_at="2026-11-24T00:00:00+00:00",
        )
        other_notice = sign(record={**notice, "round": 2}, secret=TEST_BRIDGE_SECRET)
        untrusted = [
            (build_ack(notice=notice, secret="another-secret-of-16-chars", message_ref="m",
                       posted_at="2026-11-24T00:00:00+00:00"), "signature is not valid"),
            ({**genuine_ack, "event_id": "another-notice"}, "does not match its payload_sha256"),
            (build_ack(notice=other_notice, secret=TEST_BRIDGE_SECRET, message_ref="m",
                       posted_at="2026-11-24T00:00:00+00:00"), "acknowledges other content"),
            (build_ack(notice=notice, secret=TEST_BRIDGE_SECRET, message_ref=" ",
                       posted_at="2026-11-24T00:00:00+00:00"), "names no message in the main chat"),
        ]
        for record, expected_message_part in untrusted:
            ack_path.write_text(json.dumps(record), encoding="utf-8")
            result = resumed.run_pipeline().stages[STAGE1_NAME]
            assert result.status == STAGE_NOTICE_PENDING, expected_message_part
            assert expected_message_part in result.error, result.error
            ack_path.unlink()
        
        # The host posts the notice and writes its ack: now it is awaiting review.
        assert host.post_new_notices(manifest_path=manifest_path) == [notice]
        result = resumed.run_pipeline().stages[STAGE1_NAME]
        assert result.status == STAGE_AWAITING_REVIEW and result.error is None
        assert result.notice["ack"]["chat"]["message_ref"] == "main-chat-message-1"
        assert launch_count(backend=backend) == 1
        
        # The host receives the same notice again: it is not posted twice.
        # The same ID with other content is refused, not posted.
        assert host.post_new_notices(manifest_path=manifest_path) == []
        assert host.receiver.receive(notice=notice) is False
        try:
            host.receiver.receive(notice=other_notice)
        except BridgeError as error:
            assert "already received with other content" in str(error)
        else:
            raise AssertionError("A notice ID with other content must be refused")
        try:
            HostReceiver(secret=TEST_BRIDGE_SECRET).receive(notice={**notice, "round": 9})
        except BridgeError as error:
            assert "does not match its payload_sha256" in str(error)
        else:
            raise AssertionError("A changed notice must be refused")
        assert len(host.posted) == 1
        
        resumed.apply_decision(path=early)
        assert resumed.manifest.stages[STAGE1_NAME].status == STAGE_COMPLETE
    print("✓ test_output_is_awaiting_review_only_after_the_local_checkout_and_the_hosts_ack passed")


def test_revision_starts_from_the_reviewed_output_and_comes_back_for_review():
    """An iterate decision starts one counted round with the feedback, from the reviewed commit."""
    feedback_text = "TEST-ONLY FEEDBACK: stand-in request to revise the stand-in section."
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        manifest_path = directory / "manifest.json"
        backend = make_launch_mock()
        host = FakeHost()
        orchestrator = start_project(backend=backend, directory=directory)
        first = run_to_gate(orchestrator=orchestrator, host=host).stages[STAGE1_NAME]
        first_commit, first_branch, first_agent = first.output_commit, first.output_branch, first.agent_id
        saved_text = manifest_path.read_text(encoding="utf-8")
        
        refused = [
            ({}, "needs feedback"),
            ({"feedback": " \n"}, "needs feedback"),
            ({"feedback": 3}, "An iterate decision takes feedback (text)"),
            ({"feedback": feedback_text, "base_commit": "abc123"}, "a full commit SHA"),
            ({"feedback": feedback_text, "base_commit": "a" * 40}, "must be the current head"),
            ({"feedback": feedback_text, "approve_batch": True}, "only to a stage that runs parameter batches"),
        ]
        for params, expected_message_part in refused:
            path = write_record(
                directory=directory,
                record=forged_decision(
                    notice=first.notice["event"], action=ACTION_ITERATE, changes={"params": params},
                ),
            )
            expect_state_error(
                action=lambda: orchestrator.apply_decision(path=path),
                expected_message_part=expected_message_part,
            )
            assert launch_count(backend=backend) == 1, params
            assert manifest_path.read_text(encoding="utf-8") == saved_text, params
        
        # Uncommitted review edits are never left behind by a new round.
        backend.local_review.changed_files = [" M " + WORKING_DOCUMENT_PATH]
        expect_state_error(
            action=lambda: decide(
                orchestrator=orchestrator, stage_name=STAGE1_NAME, action=ACTION_ITERATE,
                params={"feedback": feedback_text}, host=host,
            ),
            expected_message_part="is not started",
        )
        assert manifest_path.read_text(encoding="utf-8") == saved_text
        backend.local_review.changed_files = []
        
        decision_path = decide(
            orchestrator=orchestrator, stage_name=STAGE1_NAME, action=ACTION_ITERATE,
            params={"feedback": feedback_text}, host=host,
        )
        manifest = run_to_gate(orchestrator=orchestrator, host=host)
        second = manifest.stages[STAGE1_NAME]
        assert (second.round, second.kind, second.status) == (2, KIND_REVISE, STAGE_AWAITING_REVIEW)
        assert second.input_commit == first_commit, "It starts from the reviewed output"
        assert second.baseline_commit == backend.branches["main"], "It is checked against the stage's input"
        assert second.output_commit != first_commit
        assert second.agent_id != first_agent
        decision_id = read_json(path=decision_path)["decision_id"]
        assert second.started_by == decision_id
        assert (second.revision["source"], second.revision["decision_id"]) == ("owner", decision_id)
        assert second.revision["replaces_round"] == 1
        assert second.revision["feedback"] == feedback_text
        assert (second.revision["base_branch"], second.revision["base_commit"]) == (
            first_branch, first_commit,
        )
        assert [previous["agent_id"] for previous in manifest.previous_rounds] == [first_agent]
        assert manifest.previous_rounds[0]["output_commit"] == first_commit
        
        bodies = get_launch_requests(mock_http=backend)
        assert len(bodies) == 2
        assert bodies[1]["repos"][0]["startingRef"] == first_commit
        first_prompt, revision_prompt = (body["prompt"]["text"] for body in bodies)
        task = get_stage(orchestrator=orchestrator, name=STAGE1_NAME).prompt.rstrip()
        assert task in first_prompt and task in revision_prompt, "The whole task is given again"
        assert REVISION_HEADING in revision_prompt and feedback_text in revision_prompt
        assert REVISION_HEADING not in first_prompt
        assert "This is round 1 of at most 4" in first_prompt
        assert "This is round 2 of at most 4" in revision_prompt
        assert SENTINEL_DOCUMENT_TEXT.rstrip() in revision_prompt
        revised_text = backend.files[second.output_commit][WORKING_DOCUMENT_PATH].decode("utf-8")
        assert extract_stage_section(
            text=revised_text, number=1, name=STAGE1_NAME,
        ).strip().startswith("Stand-in section of stage 1, attempt 2.")
        assert backend.local_review.shown[-1] == (second.output_branch, second.output_commit)
        
        assert orchestrator.run_pipeline().stages[STAGE1_NAME].round == 2
        assert launch_count(backend=backend) == 2, "Running again launches nothing"
        
        # The reviewer pushes edits of their own: a revision must name that commit.
        edited_commit = backend.add_commit(
            files=["outputs/stage1_audit/reviewer_note.md"], parent=second.output_commit,
            branch=second.output_branch,
        )
        expect_state_error(
            action=lambda: decide(
                orchestrator=orchestrator, stage_name=STAGE1_NAME, action=ACTION_ITERATE,
                params={"feedback": feedback_text}, host=host,
            ),
            expected_message_part="name the new head as base_commit",
        )
        assert launch_count(backend=backend) == 2
        decide(
            orchestrator=orchestrator, stage_name=STAGE1_NAME, action=ACTION_ITERATE,
            params={"feedback": feedback_text, "base_commit": edited_commit}, host=host,
        )
        third = run_to_gate(orchestrator=orchestrator, host=host).stages[STAGE1_NAME]
        assert (third.round, third.input_commit, third.outcome) == (3, edited_commit, OUTCOME_PASSED)
        assert "outputs/stage1_audit/reviewer_note.md" in backend.files[third.output_commit]
        assert launch_count(backend=backend) == 3
        
        approve_as_reviewer(orchestrator=orchestrator, stage_name=STAGE1_NAME)
        accepted = run_to_gate(orchestrator=orchestrator, host=host)
        assert accepted.stages[STAGE1_NAME].status == STAGE_COMPLETE
        assert accepted.stages[STAGE2_NAME].input_commit == third.output_commit
        assert len(accepted.previous_rounds) == 2
        assert load_manifest(manifest_path=manifest_path) == accepted
    print("✓ test_revision_starts_from_the_reviewed_output_and_comes_back_for_review passed")


def run_git(folder: Path, *arguments: str) -> str:
    """Run a Git command in a test repository and return its output."""
    completed = subprocess.run(
        ["git", "-C", str(folder), *arguments],
        capture_output=True, text=True, encoding="utf-8",
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        },
    )
    assert completed.returncode == 0, f"git {arguments}: {completed.stderr}"
    return completed.stdout.strip()


def commit_file(folder: Path, path: str, text: str, message: str) -> str:
    """Write a file in a test repository, commit it and return the commit SHA."""
    target = folder / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))
    run_git(folder, "add", "--", path)
    run_git(folder, "commit", "--quiet", "-m", message)
    return run_git(folder, "rev-parse", "HEAD")


def make_git_repositories(root: Path) -> tuple[Path, Path, str]:
    """Make a bare repository standing in for GitHub, a clone that pushes to
    it as cloud agents do, and a clone that is the owner's local checkout.

    The local checkout's remote is recorded with a github.com URL, which
    Git is told to read from the bare repository instead.

    Returns:
        (the agents' clone, the local checkout, the commit of `main`)
    """
    github_url = "https://github.com/test/repo"
    bare = root / "github.git"
    run_git(root, "init", "--quiet", "--bare", "--initial-branch=main", str(bare))
    agents = root / "agents"
    run_git(root, "clone", "--quiet", str(bare), str(agents))
    main_commit = commit_file(
        folder=agents, path="orchestrator/orchestrator.py", text="# stand-in v1\n",
        message="Stand-in orchestrator",
    )
    run_git(agents, "push", "--quiet", "origin", "HEAD:main")
    local = root / "local"
    run_git(root, "clone", "--quiet", str(bare), str(local))
    run_git(local, "config", f"url.{bare}.insteadOf", github_url)
    run_git(local, "config", "remote.origin.url", github_url)
    return agents, local, main_commit


def push_stage_branch(agents: Path, branch: str, start: str, files: dict[str, str]) -> str:
    """Push a new branch from `start` with the given files, as a stage's agent would."""
    run_git(agents, "checkout", "--quiet", "-B", branch, start)
    commit = None
    for path, text in files.items():
        commit = commit_file(folder=agents, path=path, text=text, message=f"Write {path}")
    run_git(agents, "push", "--quiet", "origin", f"{branch}:{branch}")
    return commit


def test_local_review_checkout_shows_the_exact_commit_and_never_forces():
    """Real Git, throw-away repositories: the checkout is safe, exact and refuses to overwrite."""
    document = WORKING_DOCUMENT_PATH
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        agents, local, main_commit = make_git_repositories(root=root)
        checkout = LocalReviewCheckout(
            path=local, repo_url="https://github.com/Test/Repo.git", remote="origin",
            running_code_paths=["orchestrator/orchestrator.py"],
        )
        
        def expect_refused(action: Callable[[], object], expected_message_part: str) -> None:
            before = (run_git(local, "rev-parse", "HEAD"), run_git(local, "status", "--porcelain"))
            try:
                action()
            except LocalSyncError as error:
                assert expected_message_part in str(error), str(error)
            else:
                raise AssertionError(f"Expected a refusal mentioning '{expected_message_part}'")
            after = (run_git(local, "rev-parse", "HEAD"), run_git(local, "status", "--porcelain"))
            assert after == before, "A refusal changes nothing in the checkout"
        
        # Stage 1 pushes a branch; later the branch moves on. The checkout
        # shows the checked commit, not the branch's later head.
        stage1 = "cursor/stage1-0001"
        stage1_commit = push_stage_branch(
            agents=agents, branch=stage1, start=main_commit,
            files={document: "Stand-in section of stage 1.\n"},
        )
        commit_file(folder=agents, path="later.txt", text="later\n", message="Later commit")
        run_git(agents, "push", "--quiet", "origin", f"{stage1}:{stage1}")
        (local / "pipeline_manifest.json").write_text("{}", encoding="utf-8")
        
        checkout.check_ready()
        record = checkout.show_commit(branch=stage1, commit=stage1_commit, review_files=[document])
        assert record["verified"] is True
        assert (record["branch"], record["commit"]) == (stage1, stage1_commit)
        assert (record["previous_branch"], record["previous_commit"]) == ("main", main_commit)
        assert record["review_files"] == [str(local / document)]
        assert run_git(local, "rev-parse", "HEAD") == stage1_commit
        assert checkout.state() == {"commit": stage1_commit, "branch": stage1, "changed_files": []}
        assert (local / document).read_text(encoding="utf-8") == "Stand-in section of stage 1.\n"
        assert not (local / "later.txt").exists()
        assert (local / "pipeline_manifest.json").read_text(encoding="utf-8") == "{}", (
            "Untracked files stay where they are"
        )
        assert run_git(local, "rev-parse", "main") == main_commit, "main is not moved or merged into"
        assert run_git(local, "rev-parse", "--abbrev-ref", f"{stage1}@{{upstream}}") == (
            f"origin/{stage1}"
        )
        # Showing the same commit again changes nothing.
        assert checkout.show_commit(
            branch=stage1, commit=stage1_commit, review_files=[document],
        )["previous_commit"] == stage1_commit
        
        # Uncommitted review edits: nothing is checked out over them.
        stage2 = "cursor/stage2-0002"
        stage2_commit = push_stage_branch(
            agents=agents, branch=stage2, start=stage1_commit,
            files={document: "Stand-in sections of stages 1 and 2.\n"},
        )
        (local / document).write_text("My review edit.\n", encoding="utf-8")
        show_stage2 = lambda: checkout.show_commit(
            branch=stage2, commit=stage2_commit, review_files=[document],
        )
        expect_refused(action=show_stage2, expected_message_part="uncommitted changes")
        expect_refused(action=checkout.check_ready, expected_message_part=document)
        assert (local / document).read_text(encoding="utf-8") == "My review edit.\n"
        assert checkout.state()["changed_files"] == [f" M {document}"]
        
        # The edit is committed locally and not pushed: the next stage's
        # branch is another branch, so it can be shown; the edit stays on
        # the Stage 1 branch.
        run_git(local, "add", "--", document)
        run_git(
            local, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "--quiet", "-m", "My review edit",
        )
        edit_commit = run_git(local, "rev-parse", "HEAD")
        # Showing the Stage 1 output again would lose sight of the local commit: refused.
        expect_refused(
            action=lambda: checkout.show_commit(
                branch=stage1, commit=stage1_commit, review_files=[document],
            ),
            expected_message_part="has commits of your own",
        )
        assert show_stage2()["commit"] == stage2_commit
        assert run_git(local, "rev-parse", stage1) == edit_commit, "The local commit is kept"
        assert (local / document).read_text(encoding="utf-8") == (
            "Stand-in sections of stages 1 and 2.\n"
        )
        
        # A local branch of the same name that has diverged is not overwritten.
        stage3 = "cursor/stage3-0003"
        stage3_commit = push_stage_branch(
            agents=agents, branch=stage3, start=stage2_commit, files={"code.py": "# stand-in\n"},
        )
        run_git(local, "branch", stage3, edit_commit)
        expect_refused(
            action=lambda: checkout.show_commit(
                branch=stage3, commit=stage3_commit, review_files=[document, "code.py"],
            ),
            expected_message_part="have diverged",
        )
        run_git(local, "branch", "-m", stage3, "my-own-work")
        assert checkout.show_commit(
            branch=stage3, commit=stage3_commit, review_files=[document, "code.py"],
        )["commit"] == stage3_commit
        
        # A local branch that is behind is fast-forwarded, never reset.
        stage3_next = commit_file(
            folder=agents, path="code.py", text="# stand-in, corrected\n", message="Correction",
        )
        run_git(agents, "push", "--quiet", "origin", f"{stage3}:{stage3}")
        run_git(local, "checkout", "--quiet", "main")
        assert checkout.show_commit(
            branch=stage3, commit=stage3_next, review_files=["code.py"],
        )["previous_branch"] == "main"
        assert run_git(local, "rev-parse", "HEAD") == stage3_next
        
        # A file to review that is not in the commit: not reported as ready.
        expect_refused(
            action=lambda: checkout.show_commit(
                branch=stage3, commit=stage3_next, review_files=["missing.md"],
            ),
            expected_message_part="missing.md",
        )
        
        # A commit that holds another version of the running orchestrator.
        stage4 = "cursor/stage4-0004"
        stage4_commit = push_stage_branch(
            agents=agents, branch=stage4, start=stage3_next,
            files={"orchestrator/orchestrator.py": "# stand-in v2\n"},
        )
        expect_refused(
            action=lambda: checkout.show_commit(
                branch=stage4, commit=stage4_commit, review_files=[document],
            ),
            expected_message_part="another version of the orchestrator",
        )
        
        # A remote branch that was rewritten is not accepted.
        run_git(agents, "checkout", "--quiet", "-B", stage3, stage2_commit)
        rewritten = commit_file(folder=agents, path="code.py", text="# rewritten\n", message="Rewrite")
        run_git(agents, "push", "--quiet", "--force", "origin", f"{stage3}:{stage3}")
        expect_refused(
            action=lambda: checkout.show_commit(
                branch=stage3, commit=rewritten, review_files=["code.py"],
            ),
            expected_message_part="could not be fetched",
        )
        
        # A commit that is not on the named branch; a branch that does not exist.
        expect_refused(
            action=lambda: checkout.show_commit(
                branch=stage2, commit=stage3_next, review_files=[document],
            ),
            expected_message_part="is not on branch",
        )
        expect_refused(
            action=lambda: checkout.show_commit(
                branch="cursor/no-such-branch", commit=stage2_commit, review_files=[document],
            ),
            expected_message_part="could not be fetched",
        )
        
        # The checkout of another repository, a subfolder, and a folder that is no checkout.
        other = LocalReviewCheckout(
            path=local, repo_url="https://github.com/test/another", remote="origin",
        )
        expect_refused(action=other.check_ready, expected_message_part="not at the pipeline's repository")
        no_remote = LocalReviewCheckout(
            path=local, repo_url="https://github.com/test/repo", remote="upstream",
        )
        expect_refused(action=no_remote.check_ready, expected_message_part="no remote named 'upstream'")
        subfolder = LocalReviewCheckout(
            path=local / "docs", repo_url="https://github.com/test/repo", remote="origin",
        )
        expect_refused(action=subfolder.check_ready, expected_message_part="not its top folder")
        plain_folder = root / "plain"
        plain_folder.mkdir()
        try:
            LocalReviewCheckout(
                path=plain_folder, repo_url="https://github.com/test/repo", remote="origin",
            ).check_ready()
        except LocalSyncError as error:
            assert "is not a Git checkout" in str(error)
        else:
            raise AssertionError("A folder that is no checkout must be refused")
        
        assert run_git(local, "rev-parse", "main") == main_commit
        assert run_git(local, "rev-parse", "my-own-work") == edit_commit
        assert run_git(local, "stash", "list") == "", "Nothing was ever stashed"
    
    assert github_repository_of("git@github.com:YangLuUST/RMB_AI.git") == ("yangluust", "rmb_ai")
    assert github_repository_of("ssh://git@github.com/yangluust/RMB_AI") == ("yangluust", "rmb_ai")
    assert github_repository_of("https://github.com/yangluust/RMB_AI.git") == ("yangluust", "rmb_ai")
    assert github_repository_of("D:\\RMB_AI") is None
    print("✓ test_local_review_checkout_shows_the_exact_commit_and_never_forces passed")


def test_source_document_in_docs_is_the_unchanged_original_and_reaches_stage1_whole():
    """docs/ holds the owner's original byte-for-byte; a dry run sends its whole text to Stage 1.

    This reads the real document and builds the real prompts. It derives
    nothing: no stage is carried out.
    """
    source_path = DOCS_DIR / Path(SOURCE_DOCUMENT_PATH).name
    source_bytes = source_path.read_bytes()
    assert len(source_bytes) == SOURCE_DOCUMENT_SIZE_BYTES
    assert hashlib.sha256(source_bytes).hexdigest() == SOURCE_DOCUMENT_SHA256
    assert source_bytes.count(b"\r\n") == 1700, "The original's CRLF line endings are kept"
    
    document = load_model_setup_document(location=str(source_path))
    assert (document.format, document.sha256) == ("md", SOURCE_DOCUMENT_SHA256)
    assert document.text == source_bytes.decode("utf-8")
    
    old_key = os.environ.pop("CURSOR_API_KEY", None)
    try:
        backend = make_launch_mock()
        ask = answer_with(str(source_path))
        exit_code = run_cli(
            backend=backend,
            arguments=["--dry-run", "--config", str(REAL_CONFIG_PATH)],
            ask=ask,
            api_key=None,
        )
    finally:
        if old_key:
            os.environ["CURSOR_API_KEY"] = old_key
    assert exit_code == 0 and ask.questions == [MODEL_SETUP_QUESTION]
    assert backend.calls == [] and backend.local_review.ready_checks == 0
    
    orchestrator = load_real_orchestrator(mock_http=MockHttpClient(), dry_run=True)
    orchestrator.model_setup_document = document
    prompts = {
        stage.name: build_stage_prompt(
            stage=stage,
            model_setup_document=document,
            working_document_rules=orchestrator.working_document_rules(stage=stage),
        )
        for stage in orchestrator.stages
    }
    stage1_prompt = prompts[STAGE1_NAME]
    assert document.text.rstrip() in stage1_prompt, "The whole document is in the Stage 1 prompt"
    assert f"SHA-256: {SOURCE_DOCUMENT_SHA256}" in stage1_prompt
    for expected in (
        "The target is Section 1 of that document",
        "equation (12) in Section 4",
        "The other sections are secondary reference only",
        "NOT IN THIS STAGE: no pseudocode, no solver, no parameter values",
        "# 1. Environment, information, and equilibrium",
        "# 4. From delayed intervention to daily fixing responsiveness",
    ):
        assert expected in stage1_prompt, expected
    for name in ALL_STAGE_NAMES[1:]:
        assert MODEL_SETUP_HEADING not in prompts[name]
        assert WORKING_DOCUMENT_PATH in prompts[name]
    assert "ILLUSTRATIVE" in prompts[STAGE4_NAME]
    assert "Never present an illustrative choice as the source's" in prompts[STAGE4_NAME]
    
    # The committed working document holds sections only for stages that
    # were carried out, without a gap, each enclosed by its marker lines.
    working_text = (DOCS_DIR / Path(WORKING_DOCUMENT_PATH).name).read_text(encoding="utf-8")
    present = [
        any(marker in working_text for marker in stage_section_markers(number=number, name=name))
        for number, name in enumerate(ALL_STAGE_NAMES, start=1)
    ]
    assert present == sorted(present, reverse=True), "No section of a later stage without the earlier ones"
    for number, name in enumerate(ALL_STAGE_NAMES, start=1):
        if present[number - 1]:
            assert extract_stage_section(text=working_text, number=number, name=name).strip()
    print("✓ test_source_document_in_docs_is_the_unchanged_original_and_reaches_stage1_whole passed")


def fail_stage1_check(until_round: int = 99) -> Callable[[str, dict], dict]:
    """An edit for FakeServices.edit_evidence: Stage 1's audit reports a failed check.

    The stand-in agent of rounds up to `until_round` reports that the
    check of its one equation failed. It stands in for a derivation with
    an error the audit caught; nothing is derived or checked.
    """
    def edit(name: str, evidence: dict) -> dict:
        if name == STAGE1_NAME and evidence["round"] <= until_round:
            evidence["equations"][0]["check"] = {"method": "stand-in", "result": "fail"}
        return evidence
    return edit


def journal_kinds(orchestrator: Orchestrator) -> list[str]:
    """The kinds of the events in a project's trial journal, in order, with their round."""
    return [f"r{event['round']}:{event['kind']}" for event in orchestrator.journal.events()]


def test_autonomous_fix_is_off_by_default_and_when_on_stays_inside_the_stage():
    """Correction rounds without the owner: only where configured, counted, and never past the gate."""
    autonomous = lambda config: stage_settings(config, STAGE1_NAME)["iteration"].update(
        autonomous_fix=True,
    )
    
    # Off (the committed config): a failed round goes to the owner; nothing follows by itself.
    with tempfile.TemporaryDirectory() as tmp:
        backend = make_launch_mock()
        backend.edit_evidence = fail_stage1_check()
        orchestrator = start_project(backend=backend, directory=Path(tmp))
        stage1 = run_to_gate(orchestrator=orchestrator).stages[STAGE1_NAME]
        assert (stage1.round, stage1.outcome) == (1, OUTCOME_CHECKS_FAILED)
        assert (stage1.status, stage1.stop_reason) == (STAGE_AWAITING_REVIEW, STOP_REVIEW_REQUIRED)
        assert launch_count(backend=backend) == 1
        assert AUTONOMOUS_RULES not in get_launch_requests(mock_http=backend)[0]["prompt"]["text"]
    
    # On: (a) a round fails, the next one passes. One gate, at the passing round.
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / "manifest.json"
        backend = make_changed_mock(change=autonomous)
        backend.edit_evidence = fail_stage1_check(until_round=1)
        host = FakeHost()
        orchestrator = start_project(backend=backend, directory=Path(tmp))
        manifest = run_to_gate(orchestrator=orchestrator, host=host)
        stage1 = manifest.stages[STAGE1_NAME]
        assert (stage1.round, stage1.kind, stage1.started_by) == (2, KIND_FIX, "orchestrator")
        assert (stage1.status, stage1.outcome, stage1.stop_reason) == (
            STAGE_AWAITING_REVIEW, OUTCOME_PASSED, STOP_PASSED,
        )
        assert launch_count(backend=backend) == 2
        failed_round = manifest.previous_rounds[0]
        assert (failed_round["round"], failed_round["outcome"]) == (1, OUTCOME_CHECKS_FAILED)
        assert failed_round["notice"] is None and failed_round["local_sync"] is None, (
            "The failed round was not announced: the stage went on by itself"
        )
        assert [notice["round"] for notice in host.posted] == [2]
        assert backend.local_review.shown == [(stage1.output_branch, stage1.output_commit)]
        
        first, second = get_launch_requests(mock_http=backend)
        assert second["repos"][0]["startingRef"] == failed_round["output_commit"]
        assert stage1.revision["source"] == "checks"
        assert "(E1): the stage reports that its check failed" in stage1.revision["feedback"]
        fix_prompt = second["prompt"]["text"]
        assert FIX_HEADING in fix_prompt and stage1.revision["feedback"] in fix_prompt
        for rule in (
            "Do not invent an assumption",
            "Do not change the model",
            "Do not weaken, remove or skip a check",
            "stays open. Do not close it",
        ):
            assert rule in fix_prompt, rule
        assert AUTONOMOUS_RULES in fix_prompt and FIX_HEADING not in first["prompt"]["text"]
        
        # Why the next round was started is recorded before it starts.
        kinds = journal_kinds(orchestrator=orchestrator)
        assert kinds[:6] == [
            "r1:reserved", "r1:launch-propose", "r1:checked-propose", "r1:closed",
            "r2:reserved", "r2:launch-propose",
        ]
        closed = orchestrator.journal.events()[3]["facts"]
        assert closed["Outcome"] == OUTCOME_CHECKS_FAILED
        assert closed["Disposition"].startswith("KEEP") and "Round 2 (fix)" in closed["Next route"]
        
        # It never goes past the gate: the next stage is not started, and
        # the passing round still needs the owner's decision.
        assert STAGE2_NAME not in manifest.stages
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        assert resumed.run_pipeline().stages[STAGE1_NAME].status == STAGE_AWAITING_REVIEW
        assert launch_count(backend=backend) == 2
    
    # On: (b) every round fails. It stops at the limit, which a resume does not reset.
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / "manifest.json"
        backend = make_changed_mock(change=autonomous)
        backend.edit_evidence = fail_stage1_check()
        orchestrator = start_project(backend=backend, directory=Path(tmp))
        stage1 = run_to_gate(orchestrator=orchestrator).stages[STAGE1_NAME]
        assert (stage1.round, stage1.outcome, stage1.stop_reason) == (
            4, OUTCOME_CHECKS_FAILED, STOP_CAP_EXHAUSTED,
        )
        assert launch_count(backend=backend) == 4
        assert allowed_decisions(result=stage1) == [ACTION_RAISE_LIMIT, ACTION_HALT]
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        assert resumed.run_pipeline().stages[STAGE1_NAME].round == 4
        assert launch_count(backend=backend) == 4
    
    # On: (c) the stage states that it sees no other route. It stops at once.
    with tempfile.TemporaryDirectory() as tmp:
        backend = make_changed_mock(change=autonomous)
        backend.edit_evidence = lambda name, evidence: {
            **fail_stage1_check()(name, evidence), "no_plausible_alternative": True,
        }
        orchestrator = start_project(backend=backend, directory=Path(tmp))
        stage1 = run_to_gate(orchestrator=orchestrator).stages[STAGE1_NAME]
        assert (stage1.round, stage1.stop_reason) == (1, STOP_NO_ALTERNATIVE)
        assert launch_count(backend=backend) == 1
    
    # On: an item that needs the owner's choice is not a bug to correct.
    # The round stops for the owner, and cannot be accepted while it is open.
    def with_closure(status: str, **fields: Any) -> Callable[[str, dict], dict]:
        def edit(name: str, evidence: dict) -> dict:
            evidence["closures"] = [{
                "id": "C1", "title": "stand-in open item", "kind": "owner_choice",
                "status": status, **fields,
            }]
            return evidence
        return edit
    
    with tempfile.TemporaryDirectory() as tmp:
        backend = make_changed_mock(change=autonomous)
        backend.edit_evidence = with_closure(status="open")
        orchestrator = start_project(backend=backend, directory=Path(tmp))
        stage1 = run_to_gate(orchestrator=orchestrator).stages[STAGE1_NAME]
        assert (stage1.round, stage1.outcome, stage1.stop_reason) == (
            1, OUTCOME_BLOCKERS_OPEN, STOP_OWNER_DECISION,
        )
        assert stage1.gate["passed"] is True, "The checks of form pass; the open item is reported"
        assert stage1.gate["blockers"] == ["open item C1: stand-in open item"]
        assert stage1.notice["event"]["checks"]["open_items_for_owner"] == stage1.gate["blockers"]
        assert ACTION_ACCEPT not in allowed_decisions(result=stage1)
        assert launch_count(backend=backend) == 1
    
    # A stage cannot close such an item itself: without the ID of a
    # recorded decision of the owner, "resolved" is a failed check.
    for fields in ({}, {"decision_ref": "dec-made-up-by-the-agent"}):
        with tempfile.TemporaryDirectory() as tmp:
            backend = make_launch_mock()
            backend.edit_evidence = with_closure(status="resolved", **fields)
            orchestrator = start_project(backend=backend, directory=Path(tmp))
            stage1 = run_to_gate(orchestrator=orchestrator).stages[STAGE1_NAME]
            assert stage1.outcome == OUTCOME_CHECKS_FAILED
            assert any("a stage cannot close it itself" in p for p in stage1.gate["problems"])
    
    # On: a failed run is not a reason for another launch.
    with tempfile.TemporaryDirectory() as tmp:
        backend = make_changed_mock(change=autonomous)
        backend.run_statuses[STAGE1_NAME] = ["ERROR"]
        orchestrator = start_project(backend=backend, directory=Path(tmp))
        stage1 = run_to_gate(orchestrator=orchestrator).stages[STAGE1_NAME]
        assert (stage1.round, stage1.outcome, stage1.stop_reason) == (
            1, OUTCOME_RUN_FAILED, STOP_INFRASTRUCTURE,
        )
        assert launch_count(backend=backend) == 1
    print("✓ test_autonomous_fix_is_off_by_default_and_when_on_stays_inside_the_stage passed")


SECOND_MODEL = {"id": "gpt-5.6-sol", "params": [{"id": "reasoning", "value": "high"}]}


def with_cross_model(**settings: Any) -> Callable[[dict], None]:
    """A config change that switches on review by a second model for Stage 1.

    The second model is one of the stand-in models listing, chosen for the
    test only. The committed config names none.
    """
    def change(config: dict) -> None:
        stage1 = stage_settings(config, STAGE1_NAME)
        iteration = settings.pop("iteration", None)
        if iteration:
            stage1["iteration"].update(iteration)
        stage1["cross_model"].update({
            "enabled": True,
            "secondary_model_id": SECOND_MODEL["id"],
            "secondary_model_params": SECOND_MODEL["params"],
            **settings,
        })
    return change


def launches_of(backend: FakeServices) -> list[tuple[int, str, str]]:
    """(round, phase, model ID) of every agent the stand-in created, in order."""
    return [
        (agent["round"], agent["phase"], agent["model"]["id"]) for agent in backend.agents.values()
    ]


def test_cross_model_round_counts_once_and_reviews_blind_before_comparing():
    """One round, three launches: propose, blind assessment, comparison with the exact candidate."""
    first_model = EXPECTED_STAGE_MODELS[STAGE1_NAME]
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        manifest_path = directory / "manifest.json"
        backend = make_changed_mock(change=with_cross_model())
        host = FakeHost()
        orchestrator = start_project(backend=backend, directory=directory)
        assert orchestrator.validate_config() == (True, [])
        assert orchestrator.validate_models() == (True, [])
        
        # The reviewer's first launch gets lost on the way: the round is
        # picked up on a resume, and no launch is sent twice or counted twice.
        interrupted = []
        
        def lose_the_blind_launch(body: dict) -> None:
            if "blind review" in body["name"] and not interrupted:
                interrupted.append(body["agentId"])
                backend.script("POST", "/agents", LOST_RESPONSE)
                backend.script("GET", AGENT_LOOKUP, NO_RESPONSE, NO_RESPONSE)
        
        backend.before_launch = lose_the_blind_launch
        stage1 = orchestrator.run_pipeline().stages[STAGE1_NAME]
        assert (stage1.status, stage1.phase, stage1.round) == (STAGE_LAUNCHING, PHASE_BLIND, 1)
        assert stage1.agent_id == interrupted[0] and len(stage1.launches) == 1
        
        orchestrator = resume_from_file(backend=backend, manifest_path=manifest_path)
        manifest = run_to_gate(orchestrator=orchestrator, host=host)
        stage1 = manifest.stages[STAGE1_NAME]
        assert (stage1.status, stage1.round, stage1.outcome) == (
            STAGE_AWAITING_REVIEW, 1, OUTCOME_PASSED,
        )
        assert launch_count(backend=backend) == 3 and len(backend.agents) == 3
        assert launches_of(backend=backend) == [
            (1, PHASE_PROPOSE, first_model["id"]),
            (1, PHASE_BLIND, SECOND_MODEL["id"]),
            (1, PHASE_COMPARE, SECOND_MODEL["id"]),
        ]
        assert [(launch["phase"], launch["role"], launch["model"]) for launch in stage1.launches] == [
            (PHASE_PROPOSE, ROLE_PROPOSER, first_model),
            (PHASE_BLIND, ROLE_REVIEWER, SECOND_MODEL),
            (PHASE_COMPARE, ROLE_REVIEWER, SECOND_MODEL),
        ]
        assert stage1.launches[1]["agent_id"] == interrupted[0]
        assert len({launch["agent_id"] for launch in stage1.launches}) == 3
        assert manifest.previous_rounds == [], "Three launches are one round"
        
        # Blind: the reviewer's first launch starts from the round's input,
        # is not given the candidate, and adds one file.
        propose, blind, compare = get_launch_requests(mock_http=backend)
        candidate, candidate_branch = stage1.candidate_commit, stage1.candidate_branch
        review = stage1.cross_review
        assert blind["repos"][0]["startingRef"] == stage1.input_commit == propose["repos"][0]["startingRef"]
        blind_prompt = blind["prompt"]["text"]
        assert candidate not in blind_prompt and candidate_branch not in blind_prompt
        assert "You are NOT shown the candidate" in blind_prompt
        assert get_stage(orchestrator=orchestrator, name=STAGE1_NAME).prompt.rstrip() in blind_prompt
        assert SENTINEL_DOCUMENT_TEXT.rstrip() in blind_prompt, "The same inputs as the proposer"
        blind_path = "outputs/stage1_review/round_01/blind_assessment.md"
        assert review["blind"]["path"] == blind_path and review["blind"]["problems"] == []
        blind_commit = review["blind"]["commit"]
        assert backend.parents[blind_commit] == [stage1.input_commit]
        assert set(backend.files[blind_commit]) - set(backend.files[stage1.input_commit]) == {blind_path}
        assert review["blind"]["sha256"] == hashlib.sha256(
            backend.files[blind_commit][blind_path]
        ).hexdigest()
        
        # Compare: launched from the blind assessment's commit, bound to the
        # exact candidate, which it leaves unchanged.
        assert compare["repos"][0]["startingRef"] == blind_commit
        assert f"git merge --no-edit {candidate}" in compare["prompt"]["text"]
        comparison = review["comparison"]
        assert comparison["problems"] == [] and comparison["contains_candidate_unchanged"] is True
        assert comparison["verdict"]["accept"] is True and comparison["verdict"]["agree"] is True
        written = json.loads(backend.files[comparison["commit"]][comparison["path"]])
        assert written["candidate_commit"] == candidate
        assert written["candidate_files"][WORKING_DOCUMENT_PATH] == hashlib.sha256(
            backend.files[candidate][WORKING_DOCUMENT_PATH]
        ).hexdigest()
        
        # What is shown and accepted is the reviewer's commit: the candidate plus the review files.
        assert stage1.output_commit == comparison["commit"] != candidate
        for path in (WORKING_DOCUMENT_PATH, "outputs/stage1_audit/audit.json"):
            assert backend.files[stage1.output_commit][path] == backend.files[candidate][path]
        shown_files = stage1.local_sync["review_files"]
        assert str(backend.local_review.path / blind_path) in shown_files
        assert str(backend.local_review.path / comparison["path"]) in shown_files
        notice = stage1.notice["event"]
        assert notice["commits"]["candidate"] == candidate
        assert notice["cross_review"]["comparison"]["verdict"]["accept"] is True
        
        kinds = journal_kinds(orchestrator=orchestrator)
        assert kinds == [
            "r1:reserved", "r1:launch-propose", "r1:checked-propose", "r1:launch-blind_review",
            "r1:checked-blind_review", "r1:launch-compare", "r1:checked-compare", "r1:closed",
            "r1:notice-1-acknowledged",
        ], "The lost launch is recorded once"
        
        # A later edit is not covered by the reviewer's verdict: it cannot be accepted.
        edited = backend.add_commit(
            files=["outputs/stage1_audit/later_note.md"], parent=stage1.output_commit,
            branch=stage1.output_branch,
        )
        expect_state_error(
            action=lambda: decide(
                orchestrator=orchestrator, stage_name=STAGE1_NAME, action=ACTION_ACCEPT, host=host,
            ),
            expected_message_part="The checks and the reviewer's verdict hold for the reviewed commit only",
        )
        assert stage1.status == STAGE_AWAITING_REVIEW
        
        # The next round: the two models change roles.
        decide(
            orchestrator=orchestrator, stage_name=STAGE1_NAME, action=ACTION_ITERATE, host=host,
            params={"feedback": "Stand-in feedback.", "base_commit": edited},
        )
        second = run_to_gate(orchestrator=orchestrator, host=host).stages[STAGE1_NAME]
        assert (second.round, second.outcome) == (2, OUTCOME_PASSED)
        assert launches_of(backend=backend)[3:] == [
            (2, PHASE_PROPOSE, SECOND_MODEL["id"]),
            (2, PHASE_BLIND, first_model["id"]),
            (2, PHASE_COMPARE, first_model["id"]),
        ]
        assert get_launch_requests(mock_http=backend)[3]["model"] == SECOND_MODEL
        assert get_launch_requests(mock_http=backend)[4]["model"] == first_model
        assert second.cross_review["blind"]["path"] == (
            "outputs/stage1_review/round_02/blind_assessment.md"
        )
        decide(orchestrator=orchestrator, stage_name=STAGE1_NAME, action=ACTION_ACCEPT, host=host)
        assert second.status == STAGE_COMPLETE and launch_count(backend=backend) == 6
    print("✓ test_cross_model_round_counts_once_and_reviews_blind_before_comparing passed")


def test_cross_model_pass_policies_disagreement_and_reviews_that_cannot_be_used():
    """Who must agree for a pass, what follows a disagreement, and what an unusable review costs."""
    accept_agree = {"verdict": "accept", "agreement": "agree"}
    accept_differ = {"verdict": "accept", "agreement": "disagree"}
    reject = {"verdict": "reject", "agreement": "disagree"}
    
    def run_stage1_with(
        settings: dict, verdicts: list[dict], prepare: Optional[Callable[[FakeServices], None]] = None,
    ) -> tuple[StageResult, FakeServices, Orchestrator]:
        with tempfile.TemporaryDirectory() as tmp:
            backend = make_changed_mock(change=with_cross_model(**settings))
            backend.review_verdicts = list(verdicts)
            if prepare:
                prepare(backend)
            orchestrator = start_project(backend=backend, directory=Path(tmp))
            is_valid, errors = orchestrator.validate_config()
            assert is_valid, errors
            manifest = run_to_gate(orchestrator=orchestrator)
            return manifest.stages[STAGE1_NAME], backend, orchestrator
    
    # reviewer_accept: the reviewer's acceptance is enough.
    stage1, backend, _ = run_stage1_with(
        settings={"pass_policy": "reviewer_accept"}, verdicts=[accept_differ],
    )
    assert (stage1.round, stage1.outcome) == (1, OUTCOME_PASSED)
    assert ACTION_ACCEPT in allowed_decisions(result=stage1)
    
    # both_agree: the reviewer must accept AND its blind assessment must
    # agree. Otherwise it is a disagreement; with human_review it goes to the owner.
    stage1, backend, _ = run_stage1_with(
        settings={"pass_policy": "both_agree", "on_disagreement": "human_review"},
        verdicts=[accept_differ],
    )
    assert (stage1.round, stage1.outcome, stage1.stop_reason) == (
        1, OUTCOME_DISAGREEMENT, STOP_DISAGREEMENT,
    )
    assert stage1.gate["passed"] is True, "The candidate has the required form; the reviewer differs"
    assert ACTION_ACCEPT not in allowed_decisions(result=stage1)
    assert launch_count(backend=backend) == 3
    
    # Both policies need every stage check: a candidate that fails them is
    # not reviewed at all (no reviewer is paid for), whatever a reviewer would say.
    stage1, backend, _ = run_stage1_with(
        settings={"pass_policy": "reviewer_accept"}, verdicts=[accept_agree],
        prepare=lambda backend: setattr(backend, "edit_evidence", fail_stage1_check()),
    )
    assert (stage1.outcome, stage1.cross_review) == (OUTCOME_CHECKS_FAILED, None)
    assert launch_count(backend=backend) == 1
    
    # revise_next_round: a disagreement starts the next counted round with
    # a reconciliation prompt and the roles reversed. No consensus is forced.
    reconcile = {
        "on_disagreement": "revise_next_round", "iteration": {"autonomous_fix": True},
    }
    stage1, backend, orchestrator = run_stage1_with(settings=reconcile, verdicts=[reject, accept_agree])
    assert (stage1.round, stage1.kind, stage1.outcome) == (2, KIND_RECONCILE, OUTCOME_PASSED)
    first_model = EXPECTED_STAGE_MODELS[STAGE1_NAME]["id"]
    assert [model for _, _, model in launches_of(backend=backend)] == (
        [first_model, SECOND_MODEL["id"], SECOND_MODEL["id"]]
        + [SECOND_MODEL["id"], first_model, first_model]
    )
    reconciliation = get_launch_requests(mock_http=backend)[3]
    assert RECONCILE_HEADING in reconciliation["prompt"]["text"]
    assert "stand-in difference" in reconciliation["prompt"]["text"]
    assert "stand-in rationale" in reconciliation["prompt"]["text"]
    assert stage1.revision["source"] == "review"
    first_round = orchestrator.manifest.previous_rounds[0]
    assert first_round["outcome"] == OUTCOME_DISAGREEMENT
    assert reconciliation["repos"][0]["startingRef"] == first_round["output_commit"]
    assert first_round["cross_review"]["comparison"]["verdict"]["blocking_differences"] == [
        "stand-in difference"
    ]
    
    # The two never agree: it ends at the limit, with the owner.
    stage1, backend, _ = run_stage1_with(settings=reconcile, verdicts=[reject])
    assert (stage1.round, stage1.outcome, stage1.stop_reason) == (
        4, OUTCOME_DISAGREEMENT, STOP_CAP_EXHAUSTED,
    )
    assert launch_count(backend=backend) == 12, "Four rounds of three launches"
    assert allowed_decisions(result=stage1) == [ACTION_RAISE_LIMIT, ACTION_HALT]
    
    # The reviewer sees no other route: it ends at once, with the owner.
    stage1, backend, _ = run_stage1_with(
        settings=reconcile, verdicts=[{**reject, "plausible_alternative": False}],
    )
    assert (stage1.round, stage1.stop_reason) == (1, STOP_NO_ALTERNATIVE)
    assert launch_count(backend=backend) == 3
    
    # A review that breaks its rules is no review. The candidate is shown
    # as it is, and cannot be accepted on it.
    stage1, backend, _ = run_stage1_with(
        settings={}, verdicts=[accept_agree],
        prepare=lambda backend: setattr(backend, "review_fault", "blind_writes_more"),
    )
    assert (stage1.outcome, stage1.stop_reason) == (OUTCOME_REVIEW_INVALID, STOP_REVIEW_REQUIRED)
    assert "must add only" in stage1.cross_review["blind"]["problems"][0]
    assert "comparison" not in stage1.cross_review and launch_count(backend=backend) == 2
    assert stage1.output_commit == stage1.candidate_commit
    assert ACTION_ACCEPT not in allowed_decisions(result=stage1)
    
    stage1, backend, _ = run_stage1_with(
        settings={}, verdicts=[accept_agree],
        prepare=lambda backend: setattr(backend, "review_fault", "comparison_edits_candidate"),
    )
    assert stage1.outcome == OUTCOME_REVIEW_INVALID
    comparison = stage1.cross_review["comparison"]
    assert any("changed file(s) other than its own" in problem for problem in comparison["problems"])
    assert any('"candidate_files" must give' not in problem for problem in comparison["problems"])
    assert stage1.output_commit == stage1.candidate_commit, "The reviewer's commit is not handed on"
    assert ACTION_ACCEPT not in allowed_decisions(result=stage1)
    
    # A reviewer's run that fails ends the round; nothing is launched again.
    def fail_the_reviewer(backend: FakeServices) -> None:
        def before_launch(body: dict) -> None:
            backend.run_statuses[STAGE1_NAME] = (
                ["ERROR"] if "blind review" in body["name"] else ["FINISHED"]
            )
        backend.before_launch = before_launch
    
    stage1, backend, _ = run_stage1_with(settings=reconcile, verdicts=[accept_agree], prepare=fail_the_reviewer)
    assert (stage1.round, stage1.outcome, stage1.stop_reason) == (
        1, OUTCOME_REVIEW_FAILED, STOP_INFRASTRUCTURE,
    )
    assert launch_count(backend=backend) == 2
    assert stage1.gate["passed"] is True and ACTION_ACCEPT not in allowed_decisions(result=stage1)
    print("✓ test_cross_model_pass_policies_disagreement_and_reviews_that_cannot_be_used passed")


def test_validate_config_checks_round_evidence_and_cross_model_settings():
    """The settings of rounds, evidence and review by a second model are checked offline."""
    def validate(change: Callable[[dict], object]) -> list[str]:
        config = load_config(config_path=REAL_CONFIG_PATH)
        change(config)
        return Orchestrator(config=config, dry_run=True).validate_config()[1]
    
    stage = lambda config, number: config["stages"][number - 1]
    assert validate(lambda config: None) == []
    
    # What the committed config sets, and leaves off.
    committed = load_config(config_path=REAL_CONFIG_PATH)
    assert [s["iteration"] for s in committed["stages"]] == [
        {"max_rounds": limit, "autonomous_fix": False} for limit in (4, 3, 4, 6)
    ]
    assert [s["verification"]["kind"] for s in committed["stages"]] == list(verification.KINDS)
    for number, settings in enumerate(committed["stages"], start=1):
        assert settings["cross_model"] == {
            "enabled": False, "secondary_model_id": None, "secondary_model_params": [],
            "pass_policy": "both_agree", "on_disagreement": "human_review",
            "review_dir": f"outputs/stage{number}_review",
        }
        assert "completion_record" not in settings["output_manifest"]
    assert committed["stages"][3]["verification"]["max_configurations"] == 25
    
    enable = lambda config: stage(config, 1)["cross_model"].update(
        enabled=True, secondary_model_id=SECOND_MODEL["id"],
        secondary_model_params=SECOND_MODEL["params"],
    )
    assert validate(enable) == []
    
    def enabled_with(**settings: Any) -> Callable[[dict], None]:
        def change(config: dict) -> None:
            enable(config)
            stage(config, 1)["cross_model"].update(settings)
        return change
    
    cases = [
        (lambda c: stage(c, 1).pop("iteration"), "There is no default"),
        (lambda c: stage(c, 1).update(iteration=4), "iteration must be a mapping with max_rounds"),
        (lambda c: stage(c, 1)["iteration"].pop("max_rounds"), "There is no default"),
        (lambda c: stage(c, 1)["iteration"].update(max_rounds=0), "max_rounds must be a whole number"),
        (lambda c: stage(c, 1)["iteration"].update(max_rounds=True), "max_rounds must be a whole number"),
        (lambda c: stage(c, 1)["iteration"].update(autonomous_fix="yes"), "autonomous_fix must be true or false"),
        (lambda c: stage(c, 1)["iteration"].update(retries=2), "unknown setting(s): ['retries']"),
        (lambda c: stage(c, 4)["iteration"].update(autonomous_fix=True),
         "every batch needs the owner's approval"),
        (lambda c: stage(c, 1)["verification"].update(kind="proof"), "verification.kind must be one of"),
        (lambda c: stage(c, 1)["verification"].update(evidence_file="../audit.json"), "evidence_file"),
        (lambda c: stage(c, 4)["verification"].update(max_configurations=0), "max_configurations"),
        (lambda c: stage(c, 4)["verification"].pop("next_batch_spec"), "next_batch_spec"),
        (lambda c: stage(c, 3)["verification"].pop("test_file"), "test_file"),
        (lambda c: stage(c, 1)["cross_model"].update(enabled="yes"), "enabled must be true or false"),
        (lambda c: stage(c, 1)["cross_model"].update(enabled=True), "must both name a model"),
        (enabled_with(secondary_model_id=EXPECTED_STAGE_MODELS[STAGE1_NAME]["id"]),
         "the reviewer must be a different model"),
        (enabled_with(secondary_model_params={"reasoning": "high"}), "must be a list"),
        (enabled_with(pass_policy="majority"), "pass_policy must be one of both_agree, reviewer_accept"),
        (enabled_with(on_disagreement="retry"), "on_disagreement must be one of"),
        (enabled_with(on_disagreement="revise_next_round"), "needs iteration.autonomous_fix: true"),
        (enabled_with(review_dir="/abs"), "review_dir"),
        (enabled_with(rounds=3), "unknown setting(s): ['rounds']"),
        (lambda c: c["trial_log"].update(path="../log.md"), "trial_log.path"),
        (lambda c: stage(c, 1)["output_manifest"].update(allowed_paths=["../x"]), "allowed_paths"),
        (lambda c: stage(c, 4)["output_manifest"].update(completion_record="a.json"),
         "completion_record is not a setting"),
    ]
    for number, (change, expected_part) in enumerate(cases, start=1):
        errors = validate(change)
        assert len(errors) == 1 and expected_part in errors[0], f"case {number}: {errors}"
    
    # The second model is checked against the models listing like the stage's own.
    backend = make_changed_mock(change=enabled_with(secondary_model_id="no-such-model"))
    orchestrator = load_real_orchestrator(mock_http=backend, dry_run=False)
    is_valid, errors = orchestrator.validate_models()
    assert not is_valid and len(errors) == 1 and "no-such-model" in errors[0]
    assert launch_count(backend=backend) == 0
    print("✓ test_validate_config_checks_round_evidence_and_cross_model_settings passed")


def push_direct_work(
    backend: FakeServices, branch: str = "cursor/direct-stage1", without: tuple[str, ...] = (),
) -> tuple[str, str]:
    """Push stand-in Stage 1 work to a branch, as written by hand with no agent.

    Returns:
        (the input commit, the commit that holds the work)
    """
    input_commit = backend.branches["main"]
    files = backend.stage_output_files(
        stage=backend.config["stages"][0], start_commit=input_commit,
    )
    commit = backend.add_commit(
        files={path: content for path, content in files.items() if path not in without},
        parent=input_commit, branch=branch,
    )
    return input_commit, commit


def test_adoption_registers_existing_work_for_review_and_launches_nothing():
    """--adopt checks work that is already on a branch and takes it to the gate, never past it."""
    branch = "cursor/direct-stage1"
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        document_path = write_document(directory=directory, file_name="model_setup.md")
        manifest_path = directory / "adopted.json"
        backend = make_launch_mock()
        host = FakeHost()
        input_commit, content_commit = push_direct_work(backend=backend, branch=branch)
        # The branch then moved on: a later commit changes the orchestrator
        # itself and none of the stage's files.
        head = backend.add_commit(
            files={"orchestrator/orchestrator.py": b"# a later version\n"},
            parent=content_commit, branch=branch,
        )
        
        def adopt(*details: str, stage: str = STAGE1_NAME, resume: bool = False) -> int:
            return run_cli(
                backend=backend,
                arguments=[
                    *(["--resume", str(manifest_path)] if resume else ["--output", str(manifest_path)]),
                    "--config", str(REAL_CONFIG_PATH), "--adopt", stage, *details,
                ],
                ask=must_not_ask if resume else answer_with(str(document_path)),
                api_key=None,
            )
        
        def details(commit: str = head, input: str = input_commit, content: Optional[str] = content_commit,
                    of_branch: str = branch) -> list[str]:
            return [
                "--adopt-branch", of_branch, "--adopt-commit", commit, "--adopt-input-commit", input,
                *(["--adopt-content-commit", content] if content else []),
            ]
        
        # Refused, with nothing saved and nothing launched.
        other_commit = backend.add_commit(files=["unrelated.txt"], parent=input_commit, branch="other")
        edited_later = backend.add_commit(
            files={WORKING_DOCUMENT_PATH: b"changed after the content commit\n"},
            parent=content_commit, branch="cursor/edited-later",
        )
        old_key = os.environ.pop("CURSOR_API_KEY", None)
        try:
            refusals = [
                (["--adopt-branch", branch, "--adopt-commit", head], None),
                (details(commit=head[:12]), None),
                (details(commit=content_commit, content=None), None),            # not the branch head
                (details(input=content_commit), None),                             # not the project's input
                (details(of_branch="no-such-branch"), None),
                (details(commit=other_commit, of_branch="other"), None),           # does not hold the content
                (details(content=other_commit), None),
                (details(commit=edited_later, of_branch="cursor/edited-later"), None),
                (details(), STAGE2_NAME),                                           # its predecessor is not accepted
                (details(), "no_such_stage"),
            ]
            for arguments, stage in refusals:
                exit_code = adopt(*arguments, stage=stage or STAGE1_NAME)
                assert exit_code == EXIT_FAILED, arguments
                assert not manifest_path.exists(), arguments
                assert sorted(path.name for path in directory.rglob("*")) == ["model_setup.md"], arguments
            assert launch_count(backend=backend) == 0
            
            # Adopted: checked like an agent's candidate, then taken to the gate.
            exit_code = adopt(*details())
            assert exit_code == EXIT_NOTICE_PENDING, "It is not announced until the host acknowledged"
            assert all("api.cursor.com" not in url for _, url, _, _ in backend.calls), (
                "Adoption needs no API key and sends nothing to the Cursor API"
            )
        finally:
            if old_key:
                os.environ["CURSOR_API_KEY"] = old_key
        manifest = load_manifest(manifest_path=manifest_path)
        adopted = manifest.stages[STAGE1_NAME]
        assert manifest.provenance["created_by"] == "adopt"
        assert (manifest.source_ref, manifest.source_commit) == (input_commit, input_commit)
        assert (adopted.round, adopted.kind, adopted.origin) == (1, KIND_ADOPTED, ORIGIN_ADOPTED)
        assert (adopted.status, adopted.stop_reason) == (STAGE_NOTICE_PENDING, STOP_ADOPTED)
        # No agent, run or artifact is recorded for work no agent of this project produced.
        for name in ("agent_id", "run_id", "agent_url", "run_status", "prompt_sha256", "artifacts", "model"):
            assert getattr(adopted, name) is None, name
        assert adopted.launches == []
        assert (adopted.input_commit, adopted.content_commit) == (input_commit, content_commit)
        assert (adopted.candidate_commit, adopted.output_commit) == (head, head)
        assert adopted.gate["passed"] is True and adopted.outcome == OUTCOME_PASSED, adopted.gate
        assert backend.local_review.shown == [(branch, head)]
        
        host.post_new_notices(manifest_path=manifest_path)
        resume_arguments = ["--resume", str(manifest_path), "--config", str(REAL_CONFIG_PATH)]
        assert run_cli(backend=backend, arguments=resume_arguments) == EXIT_WAITING_FOR_OWNER
        adopted = load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME]
        assert adopted.status == STAGE_AWAITING_REVIEW, "Adopted work is never accepted by adoption"
        notice = adopted.notice["event"]
        assert (notice["origin"], notice["stop_reason"]) == (ORIGIN_ADOPTED, STOP_ADOPTED)
        assert notice["commits"] == {
            "input": input_commit, "baseline": input_commit, "candidate": head,
            "content": content_commit, "output": head,
        }
        assert launch_count(backend=backend) == 0
        
        orchestrator = resume_from_file(backend=backend, manifest_path=manifest_path)
        assert [event["kind"] for event in orchestrator.journal.events()] == [
            "reserved", "adopted", "closed", "notice-1-acknowledged",
        ]
        assert "no agent was launched" in orchestrator.journal.events()[1]["facts"]["What happened"]
        
        # A stage that has a round cannot be adopted again.
        assert adopt(*details(), resume=True) == EXIT_FAILED
        
        # The adopted round counts as round 1. A further round is an
        # ordinary launched round, from the adopted commit.
        decide(
            orchestrator=orchestrator, stage_name=STAGE1_NAME, action=ACTION_ITERATE, host=host,
            params={"feedback": "Stand-in feedback on the adopted work."},
        )
        second = run_to_gate(orchestrator=orchestrator, host=host).stages[STAGE1_NAME]
        assert (second.round, second.origin, second.input_commit) == (2, "pipeline", head)
        assert second.agent_id is not None and launch_count(backend=backend) == 1
        assert second.notice["event"]["round_limit"] == 4
    
    # Evidence that is not there is a failed check, not a pass. The work is
    # still shown to the owner, and cannot be accepted. Nothing is launched
    # to repair it, even where the stage may correct itself.
    autonomous = lambda config: stage_settings(config, STAGE1_NAME)["iteration"].update(
        autonomous_fix=True,
    )
    for make_backend in (make_launch_mock, lambda: make_changed_mock(change=autonomous)):
        with tempfile.TemporaryDirectory() as tmp:
            backend = make_backend()
            input_commit, commit = push_direct_work(
                backend=backend, without=("outputs/stage1_audit/audit.json",),
            )
            document_path = write_document(directory=Path(tmp), file_name="model_setup.md")
            orchestrator = load_real_orchestrator(
                mock_http=backend, dry_run=False, ask=answer_with(str(document_path)),
                manifest_path=Path(tmp) / "manifest.json",
            )
            orchestrator.start_new_project(created_by="adopt", source_ref=input_commit)
            assert orchestrator.validate_source() == (True, [])
            orchestrator.adopt_stage(
                stage_name=STAGE1_NAME, branch=branch, commit=commit, input_commit=input_commit,
            )
            adopted = run_to_gate(orchestrator=orchestrator).stages[STAGE1_NAME]
            assert (adopted.status, adopted.outcome) == (STAGE_AWAITING_REVIEW, OUTCOME_CHECKS_FAILED)
            assert adopted.stop_reason == STOP_ADOPTED and adopted.round == 1
            assert any(
                "outputs/stage1_audit/audit.json is not at the pushed commit" in problem
                for problem in adopted.gate["problems"]
            )
            assert allowed_decisions(result=adopted) == [ACTION_ITERATE, ACTION_REDO, ACTION_HALT]
            assert launch_count(backend=backend) == 0
    
    # Adopted work that changed the source document fails its check.
    with tempfile.TemporaryDirectory() as tmp:
        backend = make_launch_mock()
        backend.working_document_fault = "source_changed"
        input_commit, commit = push_direct_work(backend=backend)
        document_path = write_document(directory=Path(tmp), file_name="model_setup.md")
        orchestrator = load_real_orchestrator(
            mock_http=backend, dry_run=False, ask=answer_with(str(document_path)),
            manifest_path=Path(tmp) / "manifest.json",
        )
        orchestrator.start_new_project(created_by="adopt", source_ref=input_commit)
        orchestrator.validate_source()
        orchestrator.adopt_stage(
            stage_name=STAGE1_NAME, branch=branch, commit=commit, input_commit=input_commit,
        )
        adopted = orchestrator.manifest.stages[STAGE1_NAME]
        assert any("source document" in problem for problem in adopted.gate["problems"])
        
        # An adopted round is never launched, whatever its record is made to say.
        adopted.status = STAGE_PENDING
        try:
            orchestrator.run_pipeline()
        except RuntimeError as error:
            assert "an adopted round is never launched" in str(error)
        else:
            raise AssertionError("An adopted round must never be launched")
        assert launch_count(backend=backend) == 0
    print("✓ test_adoption_registers_existing_work_for_review_and_launches_nothing passed")


def test_adoption_of_a_later_stage_needs_the_stage_before_it_accepted():
    """With --resume, work for the next stage is adopted from the accepted output, and reviewed."""
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / "manifest.json"
        backend = make_launch_mock()
        host = FakeHost()
        orchestrator = start_project(backend=backend, directory=Path(tmp))
        stage1 = run_to_gate(orchestrator=orchestrator, host=host).stages[STAGE1_NAME]
        stage2_work = backend.add_commit(
            files=backend.stage_output_files(
                stage=backend.config["stages"][1], start_commit=stage1.output_commit,
            ),
            parent=stage1.output_commit, branch="cursor/direct-stage2",
        )
        adoption = dict(
            stage_name=STAGE2_NAME, branch="cursor/direct-stage2", commit=stage2_work,
            input_commit=stage1.output_commit,
        )
        expect_state_error(
            action=lambda: orchestrator.adopt_stage(**adoption),
            expected_message_part="the stage before it is not accepted",
        )
        
        decide(orchestrator=orchestrator, stage_name=STAGE1_NAME, action=ACTION_ACCEPT, host=host)
        expect_state_error(
            action=lambda: orchestrator.adopt_stage(**{**adoption, "input_commit": backend.branches["main"]}),
            expected_message_part="is not the accepted output of stage",
        )
        orchestrator.adopt_stage(**adoption)
        stage2 = run_to_gate(orchestrator=orchestrator, host=host).stages[STAGE2_NAME]
        assert (stage2.status, stage2.origin, stage2.outcome) == (
            STAGE_AWAITING_REVIEW, ORIGIN_ADOPTED, OUTCOME_PASSED,
        )
        assert launch_count(backend=backend) == 1, "Only Stage 1 was launched"
        
        # The record passes the checks of a resume without a start_stage
        # decision: its first round is adopted, not launched.
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        assert resumed.manifest.stages[STAGE2_NAME].status == STAGE_AWAITING_REVIEW
        decide(orchestrator=resumed, stage_name=STAGE2_NAME, action=ACTION_ACCEPT, host=host)
        assert resumed.manifest.stages[STAGE2_NAME].status == STAGE_COMPLETE
        assert launch_count(backend=backend) == 1
    print("✓ test_adoption_of_a_later_stage_needs_the_stage_before_it_accepted passed")


def test_manifest_written_by_hand_cannot_stand_in_for_a_launch_a_notice_or_a_decision():
    """A resume checks every status against the signed records that must have led to it."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        manifest_path = directory / "manifest.json"
        backend = make_launch_mock()
        host = FakeHost()
        orchestrator = start_project(backend=backend, directory=directory)
        stage1 = run_to_gate(orchestrator=orchestrator, host=host).stages[STAGE1_NAME]
        awaiting = json.loads(manifest_path.read_text(encoding="utf-8"))
        accept_path = decide(
            orchestrator=orchestrator, stage_name=STAGE1_NAME, action=ACTION_ACCEPT, host=host,
        )
        accepted = json.loads(manifest_path.read_text(encoding="utf-8"))
        launches_so_far = launch_count(backend=backend)
        
        def planted(data: dict, change: Callable[[dict], object]) -> Path:
            data = copy.deepcopy(data)
            change(data)
            path = directory / "planted.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return path
        
        def refused(data: dict, change: Callable[[dict], object], expected_message_part: str) -> None:
            path = planted(data=data, change=change)
            fresh = load_real_orchestrator(mock_http=backend, dry_run=False, manifest_path=path)
            expect_state_error(
                action=lambda: fresh.resume_project(manifest=load_manifest(manifest_path=path)),
                expected_message_part=expected_message_part,
            )
            assert fresh.manifest is None
            assert run_cli(
                backend=backend, arguments=["--resume", str(path), "--config", str(REAL_CONFIG_PATH)],
            ) == EXIT_FAILED
            assert launch_count(backend=backend) == launches_so_far
        
        stage = lambda data, name=STAGE1_NAME: data["stages"][name]
        
        # The records as the orchestrator wrote them are accepted.
        for data in (awaiting, accepted):
            path = planted(data=data, change=lambda data: None)
            load_real_orchestrator(
                mock_http=backend, dry_run=False, manifest_path=path,
            ).resume_project(manifest=load_manifest(manifest_path=path))
        
        # A manifest of an earlier format is refused, not converted.
        refused(awaiting, lambda d: d.update(format_version=3), "format version 3")
        refused(awaiting, lambda d: d.update(project_id=""), "no project ID")
        refused(awaiting, lambda d: d.update(provenance={}), "no recorded configuration")
        
        # "Accepted" without the owner's signed decision for that commit.
        refused(awaiting, lambda d: stage(d).update(status=STAGE_COMPLETE),
                "no recorded decision of the owner accepts its output commit")
        refused(accepted, lambda d: d.update(decisions=[]),
                "no recorded decision of the owner accepts its output commit")
        refused(accepted, lambda d: stage(d).update(output_commit="a" * 40),
                "no recorded decision of the owner accepts its output commit")
        refused(accepted, lambda d: stage(d).update(outcome=OUTCOME_CHECKS_FAILED),
                "its round did not pass its checks")
        refused(accepted, lambda d: d["decisions"][0].update(commit="a" * 40),
                "a recorded decision cannot be verified")
        refused(accepted, lambda d: d["decisions"][0].update(
            sign(record={**d["decisions"][0], "action": ACTION_HALT}, secret="another-secret-of-16-chars"),
        ), "a recorded decision cannot be verified")
        
        # "Awaiting review" without the signed notice and the host's signed ack.
        refused(awaiting, lambda d: stage(d).update(notice=None), "there is no 'notice' record")
        refused(awaiting, lambda d: stage(d)["notice"].update(ack=None), "there is no 'ack' record")
        refused(awaiting, lambda d: stage(d)["notice"]["event"].update(outcome=OUTCOME_PASSED, round=7),
                "does not match its payload_sha256")
        refused(awaiting, lambda d: stage(d)["notice"]["ack"].update(event_id="another"),
                "does not match its payload_sha256")
        
        # A launched round without an agent; a round past the limit; a later
        # stage without the stage before it, or without the decision to start it.
        refused(awaiting, lambda d: stage(d).update(agent_id=None, launches=[]), "records no agent")
        refused(awaiting, lambda d: stage(d).update(round=5), "is past the stage's limit of 4 round(s)")
        refused(awaiting, lambda d: stage(d).update(round=0), "has status AWAITING_REVIEW but no round")
        refused(
            awaiting,
            lambda d: d["stages"].update({STAGE2_NAME: {**stage(d), "name": STAGE2_NAME}}),
            "has a round although the stage before it is not accepted",
        )
        refused(
            accepted,
            lambda d: d["stages"].update({STAGE2_NAME: {
                "name": STAGE2_NAME, "round": 1, "kind": KIND_INITIAL, "status": STAGE_PENDING,
                "started_by": "someone",
            }}),
            "was started without a recorded decision of the owner to start it",
        )
        # Adopted work that claims an agent.
        refused(awaiting, lambda d: stage(d).update(origin=ORIGIN_ADOPTED), "is adopted work but records an agent")
        
        # Another repository, a stage the config does not have, another configuration.
        refused(awaiting, lambda d: d.update(repo_url="https://github.com/other/repo"), "is for repository")
        refused(awaiting, lambda d: d["stages"].update(extra={"name": "extra"}), "does not define")
        refused(awaiting, lambda d: d["provenance"].update(config_sha256="0" * 64),
                "The configuration has changed since this project recorded it")
        
        # A decision file is not taken on the word of its file name either.
        assert read_json(path=accept_path)["action"] == ACTION_ACCEPT
        assert stage1.status == STAGE_COMPLETE and launch_count(backend=backend) == launches_so_far
    print("✓ test_manifest_written_by_hand_cannot_stand_in_for_a_launch_a_notice_or_a_decision passed")


def test_halt_and_changed_configuration_stop_everything_until_the_owner_acts():
    """--halt, the halt decision and a changed config: nothing is launched or decided meanwhile."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        document_path = write_document(directory=directory, file_name="model_setup.qmd")
        manifest_path = directory / "manifest.json"
        changed_config_path = directory / "changed_config.yaml"
        backend = make_launch_mock()
        host = FakeHost()
        
        def cli(*arguments: str, config_path: Path = REAL_CONFIG_PATH, ask=must_not_ask) -> int:
            return run_cli(backend=backend, arguments=["--config", str(config_path), *arguments], ask=ask)
        
        resume = ("--resume", str(manifest_path))
        assert cli("--execute", "--output", str(manifest_path), ask=answer_with(str(document_path))) == (
            EXIT_NOTICE_PENDING
        )
        host.post_new_notices(manifest_path=manifest_path)
        assert cli(*resume) == EXIT_WAITING_FOR_OWNER
        first_notice = load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME].notice["event"]
        redo_path = host.write_decision(
            manifest_path=manifest_path, notice=first_notice, action=ACTION_REDO,
        )
        
        # Halted from the command line: no decision is applied, nothing is launched.
        assert cli(*resume, "--halt", "stand-in reason") == EXIT_WAITING_FOR_OWNER
        assert load_manifest(manifest_path=manifest_path).halted["reason"] == "stand-in reason"
        assert cli(*resume, "--halt", "again") == EXIT_FAILED
        assert cli(*resume, "--apply-decision", str(redo_path)) == EXIT_FAILED
        assert cli(*resume) == EXIT_WAITING_FOR_OWNER
        assert launch_count(backend=backend) == 1
        assert load_manifest(manifest_path=manifest_path).decisions == []
        
        assert cli(*resume, "--unhalt") == EXIT_WAITING_FOR_OWNER
        assert cli(*resume, "--unhalt") == EXIT_FAILED
        assert load_manifest(manifest_path=manifest_path).halted is None
        
        # The configuration changes while the round is at its gate: the
        # project is refused until the change is recorded with --repin.
        stage_settings(backend.config, STAGE1_NAME)["iteration"]["max_rounds"] = 5
        changed_config_path.write_text(
            yaml.safe_dump(backend.config, sort_keys=False), encoding="utf-8",
        )
        assert cli(*resume, config_path=changed_config_path) == EXIT_FAILED
        assert cli(*resume, "--apply-decision", str(redo_path), config_path=changed_config_path) == (
            EXIT_FAILED
        )
        assert launch_count(backend=backend) == 1
        
        # --repin records the new configuration. The notice is written again
        # for it; the decision made for the old one no longer applies.
        assert cli(*resume, "--repin", config_path=changed_config_path) == EXIT_NOTICE_PENDING
        repinned = load_manifest(manifest_path=manifest_path)
        assert len(repinned.provenance["repins"]) == 1
        assert repinned.provenance["repins"][0]["from"] == first_notice["config_sha256"]
        assert repinned.provenance["config_sha256"] != first_notice["config_sha256"]
        host.post_new_notices(manifest_path=manifest_path)
        assert cli(*resume, config_path=changed_config_path) == EXIT_WAITING_FOR_OWNER
        second_notice = load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME].notice["event"]
        assert second_notice["event_id"] != first_notice["event_id"]
        assert second_notice["config_sha256"] == repinned.provenance["config_sha256"]
        assert second_notice["round_limit"] == 5
        assert cli(*resume, "--apply-decision", str(redo_path), config_path=changed_config_path) == (
            EXIT_FAILED
        )
        assert launch_count(backend=backend) == 1
        
        # The owner's halt decision: recorded, and nothing follows it.
        halt_path = host.write_decision(
            manifest_path=manifest_path, notice=second_notice, action=ACTION_HALT,
            params={"reason": "stand-in halt by the owner"},
        )
        assert cli(*resume, "--apply-decision", str(halt_path), config_path=changed_config_path) == (
            EXIT_WAITING_FOR_OWNER
        )
        halted = load_manifest(manifest_path=manifest_path)
        assert halted.halted["by"] == read_json(path=halt_path)["decision_id"]
        assert [decision["action"] for decision in halted.decisions] == [ACTION_HALT]
        
        # Taken out of the halt, the round is announced again: the notice
        # the halt answered cannot be answered a second time.
        assert cli(*resume, "--unhalt", config_path=changed_config_path) == EXIT_NOTICE_PENDING
        host.post_new_notices(manifest_path=manifest_path)
        assert cli(*resume, config_path=changed_config_path) == EXIT_WAITING_FOR_OWNER
        third_notice = load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME].notice["event"]
        assert len({n["event_id"] for n in (first_notice, second_notice, third_notice)}) == 3
        assert launch_count(backend=backend) == 1
        assert [notice["event_id"] for notice in host.posted] == [
            first_notice["event_id"], second_notice["event_id"], third_notice["event_id"],
        ]
        
        # A configuration is not changed in the middle of a round.
        accept_path = host.write_decision(
            manifest_path=manifest_path, notice=third_notice, action=ACTION_REDO,
        )
        backend.run_statuses[STAGE1_NAME] = ["RUNNING"]
        orchestrator = resume_from_file(backend=backend, manifest_path=manifest_path)
        orchestrator.api_config["polling"] = {"interval_seconds": 1, "max_attempts": 1}
        orchestrator.apply_decision(path=accept_path)
        assert orchestrator.run_pipeline().stages[STAGE1_NAME].status == STAGE_RUNNING
        assert cli(*resume, "--repin") == EXIT_FAILED
        assert launch_count(backend=backend) == 2
    print("✓ test_halt_and_changed_configuration_stop_everything_until_the_owner_acts passed")


def test_trial_journal_is_append_only_idempotent_and_blocks_work_when_it_cannot_be_written():
    """The journal's own rules, and the check of the trial log in the repository."""
    with tempfile.TemporaryDirectory() as tmp:
        journal = TrialJournal(path=Path(tmp) / "nested" / "project.trial-journal.jsonl")
        assert journal.events() == []
        event = {
            "event_id": trial_log.event_id(
                project_id="prj-000000000001", stage_number=1, round_number=2, kind="reserved",
            ),
            "project_id": "prj-000000000001", "at": "2026-01-01T00:00:00+00:00",
            "stage": STAGE1_NAME, "stage_number": 1, "round": 2, "kind": "reserved",
            "facts": {"Why": "stand-in  reason\nover two lines", "Problems": ["a", "b"], "Open": []},
        }
        assert event["event_id"] == "prj-000000000001-s1-r02-reserved"
        assert journal.record(event=event) is True
        # The same event again (a step retaken after an interruption) adds nothing.
        assert journal.record(event={**event, "at": "2026-01-02T00:00:00+00:00"}) is False
        assert journal.events() == [event] and journal.find(event_id=event["event_id"]) == event
        # The same ID with other facts is never written over.
        try:
            journal.record(event={**event, "facts": {"Why": "another reason"}})
        except TrialLogError as error:
            assert "with other facts" in str(error)
        else:
            raise AssertionError("An event must never be rewritten")
        assert len(journal.path.read_bytes().splitlines()) == 1
        
        # A damaged journal is an error, not an empty journal.
        with open(journal.path, "ab") as handle:
            handle.write(b"not a JSON line\n")
        for use in (journal.events, lambda: journal.record(event={**event, "event_id": "other"})):
            try:
                use()
            except TrialLogError as error:
                assert "Line 2" in str(error) and "never repaired" in str(error)
            else:
                raise AssertionError("A damaged journal must not be read as valid")
    
    # The entry of the repository log: one line per fact, the zone in the time.
    entry = trial_log.render_entry(event=event)
    assert entry.splitlines() == [
        "### prj-000000000001-s1-r02-reserved",
        "",
        "- Recorded by: orchestrator",
        "- Time: 2026-01-01T00:00:00+00:00",
        f"- Stage: 1 ({STAGE1_NAME}), round 2",
        "- Event: reserved",
        "- Why: stand-in reason over two lines",
        "- Problems: a; b",
        "- Open: none",
    ]
    
    # The check of the repository log at a pushed commit.
    report_id = "prj-000000000001-s1-r02-report-propose"
    own = trial_log.report_template(report_id=report_id, role="proposer", model="stand-in")
    filled = re.sub(r"<one of [^>]*>", "ABANDON", own)
    filled = re.sub(r"<[^>]*>", "stand-in statement", filled)
    before = trial_log.LOG_HEADER.encode("utf-8")
    good = before + f"\n{entry}\n{filled}".encode("utf-8")
    
    def problems(after: Optional[bytes], given: Optional[bytes] = before) -> list[str]:
        return trial_log.repository_log_problems(
            before=given, after=after, path=TRIAL_LOG_PATH, expected_entries=[entry],
            report_id=report_id,
        )
    
    assert problems(after=good) == []
    assert problems(after=good.replace(b"\n", b"\r\n"), given=None) == [], "Line ends do not count"
    assert trial_log.entry_ids(good.decode("utf-8")) == [event["event_id"], report_id]
    assert trial_log.entries_missing_from(log_text=good.decode("utf-8"), events=[event]) == []
    assert trial_log.entries_missing_from(log_text=trial_log.LOG_HEADER, events=[event]) == [event]
    cases = [
        (None, "is not at the pushed commit"),
        (good[10:], "was changed or shortened"),
        (before + filled.encode("utf-8"), "does not hold the orchestrator's entry"),
        (good.replace(b"stand-in reason", b"a kinder reason"), "does not hold the orchestrator's entry"),
        (before + f"\n{entry}".encode("utf-8"), f"has no entry '### {report_id}'"),
        (good + b"\n### prj-000000000001-s1-r01-closed\n\n- Made up: yes\n", "that the orchestrator did not give"),
        (good + f"\n{entry}".encode("utf-8"), "more than once"),
        (good.replace(b"- Disposition: ABANDON", b"- Disposition: PERHAPS"), "it must be one of KEEP, ABANDON, UNRESOLVED"),
        (good.replace(b"- Next route: stand-in statement", b"- Next route:"), "has no line '- Next route: ...' with text"),
        (good.replace(b"- Rationale: stand-in statement\n", b""), "has no line '- Rationale: ...' with text"),
        (b"\xff\xfe" + good, "was changed or shortened"),
    ]
    for number, (after, expected_part) in enumerate(cases, start=1):
        found = problems(after=after)
        assert found and expected_part in found[0], f"case {number}: {found}"
    
    # The log in the repository holds no entry: no stage has run. Its text
    # is the fixed heading, which a later round only appends to.
    committed = (ORCHESTRATOR_DIR.parent / TRIAL_LOG_PATH).read_bytes()
    assert committed.decode("utf-8") == trial_log.LOG_HEADER
    assert trial_log.entry_ids(committed.decode("utf-8")) == []
    assert trial_log.repository_log_problems(
        before=None, after=committed, path=TRIAL_LOG_PATH, expected_entries=[], report_id=None,
    ) == []
    assert load_config(config_path=REAL_CONFIG_PATH)["trial_log"] == {"path": TRIAL_LOG_PATH}
    print("✓ test_trial_journal_is_append_only_idempotent_and_blocks_work_when_it_cannot_be_written passed")


def test_trial_log_records_every_round_before_the_next_step_and_survives_interruptions():
    """Journal first, then the step; the repository log is the journal's entries plus each agent's own."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        manifest_path = directory / "manifest.json"
        backend = make_launch_mock()
        host = FakeHost()
        
        # What is in the journal at the moment a launch request is sent.
        journal_at_launch = []
        
        def note_the_journal(body: dict) -> None:
            path = directory / "manifest.trial-journal.jsonl"
            journal_at_launch.append([
                json.loads(line)["kind"] for line in path.read_text(encoding="utf-8").splitlines()
            ])
        
        backend.before_launch = note_the_journal
        orchestrator = start_project(backend=backend, directory=directory)
        assert orchestrator.journal.path == directory / "manifest.trial-journal.jsonl"
        stage1 = run_to_gate(orchestrator=orchestrator, host=host).stages[STAGE1_NAME]
        assert journal_at_launch == [["reserved", "launch-propose"]], (
            "The round and the launch are on disk before the request is sent"
        )
        
        events = orchestrator.journal.events()
        project_id = orchestrator.manifest.project_id
        assert [event["event_id"] for event in events] == [
            f"{project_id}-s1-r01-{kind}" for kind in (
                "reserved", "launch-propose", "checked-propose", "closed", "notice-1-acknowledged",
            )
        ]
        for event in events:
            assert event["project_id"] == project_id and (event["stage"], event["round"]) == (STAGE1_NAME, 1)
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?\+00:00", event["at"]), event["at"]
        launch, checked, closed = events[1]["facts"], events[2]["facts"], events[3]["facts"]
        model = EXPECTED_STAGE_MODELS[STAGE1_NAME]
        assert launch["Model"] == model["id"] and "proposer" in launch["What happened"]
        assert launch["Model parameters"] == [f"{p['id']}={p['value']}" for p in model["params"]]
        assert stage1.input_commit in launch["Starts from"] and launch["Agent ID"] == stage1.agent_id
        assert checked["Prompt SHA-256"] == stage1.prompt_sha256
        assert stage1.candidate_commit in checked["Candidate"]
        assert checked["Evidence file"] == "outputs/stage1_audit/audit.json"
        assert checked["Result"] == "checks passed" and "not of correctness" in checked["Note"]
        # A passed round is not kept by the orchestrator: that is the owner's decision.
        assert closed["Outcome"] == OUTCOME_PASSED and closed["Disposition"].startswith("UNRESOLVED")
        assert "the owner's decision" in closed["Next route"] and closed["Rationale"]
        assert events[4]["facts"]["Stop reason"] == STOP_PASSED
        
        # The repository log at the pushed commit: the journal's entries the
        # agent was given, word for word, then the agent's own entry.
        log = backend.files[stage1.output_commit][TRIAL_LOG_PATH].decode("utf-8")
        report_id = f"{project_id}-s1-r01-report-propose"
        assert trial_log.entry_ids(log) == [
            f"{project_id}-s1-r01-reserved", f"{project_id}-s1-r01-launch-propose", report_id,
        ]
        for event in events[:2]:
            assert trial_log.render_entry(event=event).rstrip("\n") in log
        assert stage1.log_event_ids == [event["event_id"] for event in events[:2]]
        prompt = get_launch_requests(mock_http=backend)[0]["prompt"]["text"]
        assert TRIAL_LOG_HEADING in prompt and f"### {report_id}" in prompt
        assert "no private reasoning notes" in prompt
        assert str(backend.local_review.path / TRIAL_LOG_PATH) in stage1.local_sync["review_files"]
        assert stage1.notice["event"]["local"]["trial_log"] == str(backend.local_review.path / TRIAL_LOG_PATH)
        notice = stage1.notice["event"]
        assert notice["on_github"]["trial_log"] == TRIAL_LOG_PATH
        assert stage1.output_commit in notice["on_github"]["commit"]
        assert notice["local"]["trial_journal"] == str(orchestrator.journal.path)
        # The events recorded after the launch are not in the repository yet; the notice gives them.
        assert [entry_id for entry in notice["trial_log_entries_not_yet_in_repository"]
                for entry_id in trial_log.entry_ids(entry)] == [
            f"{project_id}-s1-r01-checked-propose", f"{project_id}-s1-r01-closed",
        ]
        
        # Running it again from the record adds no event and launches nothing.
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        resumed.run_pipeline()
        assert resumed.journal.events() == events and launch_count(backend=backend) == 1
        
        # The next round appends: what the log held stays, and the events
        # recorded since (check, close, notice, the owner's decision) are added.
        decide(
            orchestrator=resumed, stage_name=STAGE1_NAME, action=ACTION_ITERATE, host=host,
            params={"feedback": "Stand-in feedback."},
        )
        second = run_to_gate(orchestrator=resumed, host=host).stages[STAGE1_NAME]
        later_log = backend.files[second.output_commit][TRIAL_LOG_PATH].decode("utf-8")
        assert later_log.startswith(log)
        added = trial_log.entry_ids(later_log[len(log):])
        decision_id = resumed.manifest.decisions[0]["decision_id"]
        assert added == [
            f"{project_id}-s1-r01-checked-propose", f"{project_id}-s1-r01-closed",
            f"{project_id}-s1-r01-notice-1-acknowledged", f"{project_id}-s1-r01-decision-{decision_id}",
            f"{project_id}-s1-r02-reserved", f"{project_id}-s1-r02-launch-propose",
            f"{project_id}-s1-r02-report-propose",
        ]
        decision_event = resumed.journal.find(event_id=f"{project_id}-s1-r01-decision-{decision_id}")
        facts = decision_event["facts"]
        assert facts["Decision"] == decision_id and facts["What happened"].endswith(ACTION_ITERATE)
        assert facts["Parameters"]["feedback"] == "Stand-in feedback."
        assert stage1.output_commit in facts["Bound to"] and facts["Disposition"] and facts["Next route"]
    
    # A failed launch and a failed run are recorded too, with no repository commit.
    with tempfile.TemporaryDirectory() as tmp:
        backend = make_launch_mock()
        backend.run_statuses[STAGE1_NAME] = ["ERROR"]
        orchestrator = start_project(backend=backend, directory=Path(tmp))
        run_to_gate(orchestrator=orchestrator)
        kinds = journal_kinds(orchestrator=orchestrator)
        assert kinds == [
            "r1:reserved", "r1:launch-propose", "r1:failed-propose", "r1:closed",
            "r1:notice-1-acknowledged",
        ]
        failed = orchestrator.journal.events()[2]["facts"]
        assert "ERROR" in failed["Detail"] and failed["Agent ID"]
        closed = orchestrator.journal.events()[3]["facts"]
        assert closed["Outcome"] == OUTCOME_RUN_FAILED and closed["Output"] == "none"
        assert closed["Disposition"] == "ABANDON: the round produced no candidate"
    
    # An agent that breaks the log's rules fails its output check.
    for fault, expected_part in (
        ("rewritten", "was changed or shortened"),
        ("entries_left_out", "does not hold the orchestrator's entry"),
        ("no_own_entry", "has no entry '###"),
        ("bad_disposition", "it must be one of KEEP, ABANDON, UNRESOLVED"),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            backend = make_launch_mock()
            # Stage 1 starts from a commit that already holds a log.
            backend.files[backend.branches["main"]][TRIAL_LOG_PATH] = trial_log.LOG_HEADER.encode("utf-8")
            backend.trial_log_fault = fault
            orchestrator = start_project(backend=backend, directory=Path(tmp))
            stage1 = run_to_gate(orchestrator=orchestrator).stages[STAGE1_NAME]
            assert stage1.outcome == OUTCOME_CHECKS_FAILED, fault
            assert any(expected_part in problem for problem in stage1.gate["problems"]), (
                fault, stage1.gate["problems"],
            )
            assert ACTION_ACCEPT not in allowed_decisions(result=stage1)
    
    # The journal cannot be written: nothing is launched.
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        backend = make_launch_mock()
        (directory / "manifest.trial-journal.jsonl").mkdir()
        orchestrator = start_project(backend=backend, directory=directory)
        try:
            orchestrator.run_pipeline()
        except TrialLogError as error:
            assert "could not be read" in str(error) or "could not be written" in str(error)
        else:
            raise AssertionError("Without the journal nothing may be done")
        assert launch_count(backend=backend) == 0
        assert not (directory / "manifest.json").exists()
        
        document_path = write_document(directory=directory, file_name="model_setup.md")
        exit_code = run_cli(
            backend=backend,
            arguments=["--execute", "--config", str(REAL_CONFIG_PATH), "--output", str(directory / "manifest.json")],
            ask=answer_with(str(document_path)),
        )
        assert exit_code == EXIT_FAILED and launch_count(backend=backend) == 0
    
    # The journal stops being writable after the launch: the result is not
    # taken further, and the next run goes on from the record without a second launch.
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        manifest_path = directory / "manifest.json"
        backend = make_launch_mock()
        orchestrator = start_project(backend=backend, directory=directory)
        real_record = TrialJournal.record
        
        def record_until_checked(self: TrialJournal, event: dict) -> bool:
            if event["kind"].startswith("checked-"):
                raise TrialLogError("stand-in: the disk is full")
            return real_record(self, event=event)
        
        TrialJournal.record = record_until_checked
        try:
            orchestrator.run_pipeline()
        except TrialLogError as error:
            assert "the disk is full" in str(error)
        else:
            raise AssertionError("A journal that cannot be written must stop the work")
        finally:
            TrialJournal.record = real_record
        assert launch_count(backend=backend) == 1
        saved = load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME]
        assert saved.status in (STAGE_RUNNING, STAGE_RUN_FINISHED) and saved.outcome is None
        assert backend.local_review.shown == []
        
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        stage1 = run_to_gate(orchestrator=resumed).stages[STAGE1_NAME]
        assert stage1.status == STAGE_AWAITING_REVIEW and launch_count(backend=backend) == 1
        kinds = journal_kinds(orchestrator=resumed)
        assert kinds == [
            "r1:reserved", "r1:launch-propose", "r1:checked-propose", "r1:closed",
            "r1:notice-1-acknowledged",
        ], "No event twice, none made up for the interruption"
    print("✓ test_trial_log_records_every_round_before_the_next_step_and_survives_interruptions passed")


def test_evidence_checks_of_stages_1_to_3_refuse_gaps_and_do_not_certify_content():
    """Audit, trace and test map: what each check refuses. A pass is a pass of form only."""
    def gate_of(stage_name: str, change: Callable[[dict], object],
                prepare: Optional[Callable[[FakeServices], None]] = None) -> dict:
        """Run the stages before `stage_name` with good stand-in evidence, then that stage with a change."""
        def edit(name: str, evidence: dict) -> dict:
            if name == stage_name:
                change(evidence)
            return evidence
        
        with tempfile.TemporaryDirectory() as tmp:
            backend = make_launch_mock()
            backend.edit_evidence = edit
            if prepare:
                prepare(backend)
            host = FakeHost()
            orchestrator = start_project(backend=backend, directory=Path(tmp))
            for stage in orchestrator.stages:
                result = run_to_gate(orchestrator=orchestrator, host=host).stages[stage.name]
                if stage.name == stage_name:
                    assert result.status == STAGE_AWAITING_REVIEW
                    assert (result.outcome == OUTCOME_PASSED) == (not result.gate["problems"] and not result.gate["blockers"])
                    assert ACTION_ACCEPT in allowed_decisions(result=result) or result.outcome != OUTCOME_PASSED
                    return result.gate
                approve_as_reviewer(orchestrator=orchestrator, stage_name=stage.name, host=host)
        raise AssertionError(stage_name)
    
    def item(key: str, position: int = 0) -> Callable[[dict], dict]:
        return lambda evidence: evidence[key][position]
    
    cases = {
        STAGE1_NAME: [
            (lambda e: e.update(stage="another_stage"), f'"stage" must be "{STAGE1_NAME}"'),
            (lambda e: e.update(round=3), '"round" must be 1, the round that was launched'),
            (lambda e: e.pop("attempt"), '"attempt" must be an object'),
            (lambda e: e.update(no_plausible_alternative="no"), '"no_plausible_alternative" must be true or false'),
            # Equation audit.
            (lambda e: e.update(equations=[]), '"equations" must not be empty'),
            (lambda e: e.update(equations=[]), "the displayed equation (E1) of the section is not audited"),
            (lambda e: e["equations"].append({**e["equations"][0], "label": "E9"}),
             "the equation (E9) is not in this stage's section"),
            (lambda e: e["equations"].append(dict(e["equations"][0])), "the equation (E1) is listed more than once"),
            (lambda e: item("equations")(e).pop("source"), 'every equation needs a "label" and the "source"'),
            (lambda e: item("equations")(e).update(status="probably exact"), '"status" must be "exact" or "first_order"'),
            (lambda e: item("equations")(e).update(check={"result": "fail"}), "(E1): the stage reports that its check failed"),
            (lambda e: item("equations")(e).update(check={"result": "not_checked"}), 'a check that was not made needs a "reason"'),
            (lambda e: item("equations")(e).pop("check"), '"check.result" must be "pass", "fail" or "not_checked"'),
            # Unknown audit.
            (lambda e: e["unknowns"].append("y"), "2 unknowns but 1 residual conditions"),
            (lambda e: e.update(residuals=["E7"]), "the residual (E7) is not among the audited equations"),
            (lambda e: e.update(unknowns=[]), '"unknowns" and "residuals" must be non-empty lists'),
            # Closure audit.
            (lambda e: e.pop("closures"), '"closures" must be given'),
            (lambda e: e.update(closures=[{"id": "C1", "title": "t", "status": "done"}]),
             '"status" must be "open", "resolved" or "scoped"'),
            (lambda e: e.update(closures=[{"id": "C1", "title": "t", "status": "scoped"}]),
             "a stage cannot close it itself"),
            (lambda e: e.update(closures=[{"status": "open"}]), 'every closure needs an "id" and a "title"'),
            # Exact versus first order.
            (lambda e: e.pop("order_consistency"), '"order_consistency" must be a list'),
            (lambda e: item("order_consistency")(e).update(status="inconsistent"),
             "is inconsistent and is not tied to a listed open item (closure_ref)"),
            (lambda e: item("order_consistency")(e).update(status="fine"),
             '"status" must be "consistent", "inconsistent" or "unresolved"'),
            # Symbolic checks.
            (lambda e: e.pop("symbolic_checks"), '"symbolic_checks" must be an object'),
            (lambda e: e["symbolic_checks"].update(log="outputs/stage1_audit/no_such.log"),
             '"symbolic_checks.log" must name a file at the pushed commit'),
            (lambda e: e["symbolic_checks"].update(failed=2), "2 symbolic check(s) are reported as failed"),
            (lambda e: e["symbolic_checks"].update(passed="all"), "must be whole numbers"),
        ],
        STAGE2_NAME: [
            # From the equilibrium to the pseudocode ...
            (lambda e: e["conditions"].pop(0), "(E1) of the equilibrium conditions is mapped to no step"),
            (lambda e: e["conditions"].pop(1), "(x) of the equilibrium conditions is mapped to no step"),
            (lambda e: item("conditions")(e).update(steps=[]), "the condition (E1) is mapped to no step"),
            (lambda e: item("conditions")(e).update(steps=["P7"]), "names the step P7, which is not listed"),
            (lambda e: item("conditions")(e).pop("kind"), 'every condition needs a "label" and a "kind"'),
            (lambda e: e["conditions"].append(dict(e["conditions"][0])), "the condition (E1) is listed more than once"),
            # ... and from the pseudocode back.
            (lambda e: e["steps"].append({"id": "P9", "origins": ["E1"]}), "the step P9 is not in this stage's section"),
            (lambda e: item("steps")(e).update(origins=[]), "the step P1 has no origin in the equilibrium conditions"),
            (lambda e: item("steps")(e).update(origins=["E5"]), "names the origin (E5), which is not listed"),
            (lambda e: e["steps"].append(dict(e["steps"][0])), "the step P1 is listed more than once"),
            (lambda e: item("solve_blocks")(e).update(unknowns=["x", "y"]),
             "B1 solves for 2 unknowns with 1 residuals"),
            (lambda e: e.pop("solve_blocks"), '"solve_blocks" must be a list'),
            (lambda e: e.pop("open_choices"), '"open_choices" must be given'),
            (lambda e: e.update(open_choices=[{"id": "Q1", "title": "t", "status": "resolved"}]),
             "choice Q1 is marked resolved without the ID of a recorded decision"),
            (lambda e: e.update(open_choices=[{"id": "Q1", "title": "t", "status": "chosen"}]),
             '"status" must be "open", "named_input" or "resolved"'),
        ],
        STAGE3_NAME: [
            (lambda e: e.update(steps=[]), "the pseudocode step P1 is mapped to no function and test"),
            (lambda e: item("steps")(e).update(functions=[]), 'the step P1 has no entry in "functions"'),
            (lambda e: item("steps")(e).update(tests=[]), 'the step P1 has no entry in "tests"'),
            (lambda e: item("steps")(e).update(functions=["no_such_function"]),
             "the step P1 names 'no_such_function', which is not defined in"),
            (lambda e: item("steps")(e).update(tests=["test_not_written"]),
             "the step P1 names 'test_not_written', which is not defined in"),
            (lambda e: item("tolerances")(e).pop("config_key"), 'every tolerance needs a "name" and the "config_key"'),
            (lambda e: item("residual_checks")(e).update(tolerance="1e-8"), "residual_checks: E1 names no listed tolerance"),
            (lambda e: item("residual_checks")(e).update(test="test_other"), "residual_checks: E1 names no test defined in"),
            (lambda e: e.update(admissibility_checks=[]), '"admissibility_checks" must not be empty'),
            (lambda e: item("admissibility_checks")(e).update(test=None), "admissibility_checks: x > 0 names no test defined in"),
            (lambda e: e.pop("test_run"), '"test_run" must be an object'),
            (lambda e: e["test_run"].update(command=""), '"test_run.command" must be the exact command'),
            (lambda e: e["test_run"].update(log="outputs/stage3_implementation/none.txt"),
             '"test_run.log" must name the printed test output'),
            (lambda e: e["test_run"].update(failed=1), "the stage reports a failing test run (exit code 0, 1 failed)"),
            (lambda e: e["test_run"].update(exit_code=1), "the stage reports a failing test run (exit code 1, 0 failed)"),
            (lambda e: e["test_run"].update(passed=0), "the stage reports no passing test"),
            (lambda e: e["test_run"].update(passed="many"), "must be whole numbers"),
        ],
    }
    for stage_name, stage_cases in cases.items():
        good = gate_of(stage_name=stage_name, change=lambda evidence: None)
        assert good["passed"] is True and good["problems"] == [] and good["blockers"] == []
        # What a pass means is stated with the record of the checks.
        assert "A passed check is not a proof" in good["note"]
        assert good["evidence"]["summary"]["attempt"]["tried"] == "stand-in: nothing was done"
        for number, (change, expected_part) in enumerate(stage_cases, start=1):
            gate = gate_of(stage_name=stage_name, change=change)
            assert gate["passed"] is False, (stage_name, number)
            assert any(expected_part in problem for problem in gate["problems"]), (
                stage_name, number, gate["problems"],
            )
    
    # An open item or an open choice is not a failed check: it is reported
    # for the owner and keeps the round from being accepted.
    for stage_name, change, blocker in (
        (STAGE1_NAME,
         lambda e: (
             e.update(closures=[{"id": "C2", "title": "stand-in  gap", "status": "open"}]),
             item("order_consistency")(e).update(status="unresolved", closure_ref="C2"),
         ),
         "open item C2: stand-in gap"),
        (STAGE2_NAME,
         lambda e: e.update(open_choices=[{"id": "Q1", "title": "stand-in choice", "status": "open"}]),
         "open choice Q1: stand-in choice"),
    ):
        gate = gate_of(stage_name=stage_name, change=change)
        assert gate["passed"] is True and gate["blockers"] == [blocker], gate
    gate = gate_of(
        stage_name=STAGE2_NAME,
        change=lambda e: e.update(open_choices=[{"id": "Q1", "title": "t", "status": "named_input"}]),
    )
    assert gate["passed"] is True and gate["blockers"] == []
    
    # The evidence file itself: missing, or not a JSON object.
    for stage_name, evidence_path in (
        (STAGE1_NAME, "outputs/stage1_audit/audit.json"),
        (STAGE2_NAME, "outputs/stage2_trace/trace.json"),
        (STAGE3_NAME, "outputs/stage3_implementation/test_map.json"),
    ):
        gate = gate_of(
            stage_name=stage_name, change=lambda evidence: None,
            prepare=lambda backend: backend.withheld_files.add(evidence_path),
        )
        assert gate["evidence"]["present"] is False
        assert gate["problems"] == [f"the evidence file {evidence_path} is not at the pushed commit"]
    for content, expected_part in ((b"{not json", "is not valid JSON"), (b"[]", "must be a JSON object")):
        evidence, found = verification.load_evidence(content=content, path="a.json")
        assert evidence is None and expected_part in found[0]
    print("✓ test_evidence_checks_of_stages_1_to_3_refuse_gaps_and_do_not_certify_content passed")


def run_all_tests():
    """Run every test_* function in this module."""
    print("Running orchestrator tests (stand-in services, no network)...")
    print("-" * 50)
    
    tests = [
        function
        for name, function in globals().items()
        if name.startswith("test_") and callable(function)
    ]
    for test in tests:
        test()
    
    print("-" * 50)
    print(f"All {len(tests)} tests passed!")


if __name__ == "__main__":
    run_all_tests()
