#!/usr/bin/env python3
"""
Unit tests for the pipeline orchestrator.

All tests use mocked HTTP - no network calls are made. The Cursor API, the
GitHub API and the artifact download host are replaced by stand-ins written
from the public API documentation. No test launches an agent or reads a
real repository, so these tests do not show that the live services behave
as documented.
Run with: python -m pytest test_orchestrator.py -v
Or simply: python test_orchestrator.py
"""

import copy
import hashlib
import json
import os
import re
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
from orchestrator import (
    GUARDRAILS_HEADING,
    MANIFEST_FORMAT_VERSION,
    MODEL_SETUP_BEGIN_LINE,
    MODEL_SETUP_END_LINE,
    MODEL_SETUP_HEADING,
    MODEL_SETUP_QUESTION,
    STAGE_ARTIFACTS_FAILED,
    STAGE_AWAITING_REVIEW,
    STAGE_BLOCKED,
    STAGE_COMPLETE,
    STAGE_DRY_RUN,
    STAGE_GATE_FAILED,
    STAGE_LAUNCH_REJECTED,
    STAGE_LAUNCHING,
    STAGE_RUN_FAILED,
    STAGE_RUNNING,
    ModelSetupDocumentError,
    Orchestrator,
    PipelineManifest,
    ProjectStateError,
    StageConfig,
    StageResult,
    UnsafeArtifactPathError,
    build_stage_prompt,
    load_config,
    load_manifest,
    load_model_setup_document,
    main,
    safe_artifact_destination,
    unsafe_relative_path_reason,
)

ORCHESTRATOR_DIR = Path(__file__).resolve().parent
REAL_CONFIG_PATH = ORCHESTRATOR_DIR / "config.yaml"
DESIGN_DOC_PATH = ORCHESTRATOR_DIR.parent / "docs" / "pipeline-design.md"
DESIGN_DOC_GUARDRAILS_HEADING = "#### Reproducibility Guardrails (Stage 3)"
STAGE1_NAME = "equilibrium_derivation"
STAGE2_NAME = "equilibrium_to_pseudocode"
STAGE3_NAME = "pseudocode_to_implementation"
STAGE4_NAME = "academic_writing"
EXPECTED_GUARDRAIL_COUNT = 12

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
            "output_manifest": {"required_files": ["output1.txt"]},
        },
        {
            "name": "stage2",
            "display_name": "Test Stage 2",
            "description": "Second test stage",
            "model_id": None,
            "depends_on": "stage1",
            "prompt": "Do something for stage 2",
            "output_manifest": {"required_files": ["output2.txt"]},
        },
    ],
    "execution": {
        "auto_create_pr": False,
    },
}

# The smallest `api` section a config needs to pass validation.
MINIMAL_API_CONFIG = {"retries": {"max_attempts": 1, "backoff_seconds": [0]}}


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
        source_ref="main",
        source_commit="a" * 40,
    )
    manifest.stages["stage1"] = StageResult(
        name="stage1",
        status=STAGE_COMPLETE,
        agent_id="bc-123",
        input_commit="a" * 40,
        output_branch="cursor/test-a1b2",
        output_commit="b" * 40,
    )
    
    data = json.loads(json.dumps(manifest.to_dict()))
    assert data["format_version"] == MANIFEST_FORMAT_VERSION
    assert data["stages"]["stage1"]["status"] == STAGE_COMPLETE
    
    restored = PipelineManifest.from_dict(data)
    assert restored == manifest
    
    for broken in (
        {**data, "format_version": 1},
        {key: value for key, value in data.items() if key != "format_version"},
        {**data, "stages": {"stage1": {**data["stages"]["stage1"], "input_ref": "main"}}},
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
    
    source, commit = orchestrator._resolve_input(stage=orchestrator.stages[1])
    
    assert commit == "c" * 40
    assert "stage1" in source
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


def load_real_orchestrator(
    mock_http: Any,
    dry_run: bool,
    ask: Callable[[str], str] = must_not_ask,
    manual_review: bool = False,
    manifest_path: Optional[Path] = None,
) -> Orchestrator:
    """Build an orchestrator from the committed config.yaml with mocked HTTP.

    Args:
        mock_http: A FakeServices (for tests that launch) or a MockHttpClient.
        manual_review: False switches off the review pauses of the committed
            config, for tests that are not about review.
        manifest_path: The project's manifest file. A run that may launch
            gets a fresh one if none is given.
    """
    config = load_config(config_path=REAL_CONFIG_PATH)
    if not manual_review:
        for stage in config["stages"]:
            stage["manual_review"] = False
    is_fake_services = isinstance(mock_http, FakeServices)
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
    
    result = orchestrator._run_stage(stage=stage3)
    assert result.status == STAGE_COMPLETE
    
    launch_requests = [call for call in mock_http.calls if call[0] == "POST"]
    assert len(launch_requests) == 1
    prompt_text = launch_requests[0][3]["prompt"]["text"]
    
    assert "TASK: Implement the pseudocode in Python" in prompt_text
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


class FakeServices:
    """Stand-in for the three services the orchestrator talks to.

    - the Cursor Cloud Agents API (models, agents, runs, artifacts),
    - the GitHub REST API (branch heads, commits, files at a commit),
    - the host that serves presigned artifact download URLs.

    The request and response shapes follow the public documentation of the
    two APIs. They are NOT recordings of the live services.

    It keeps the state a launch would create. An agent launched from a commit
    pushes one new commit to a new branch; that commit holds the files of the
    starting commit plus the required files of the agent's stage. Like the
    documented API, a second launch request with an agent ID already in use
    is answered with 409 agent_id_conflict and creates nothing.

    Tests change this behaviour through the attributes set in __init__ and
    through script(). Nothing here uses the network.
    """
    
    def __init__(self, config: dict, models_listing: dict):
        self.repo_url = config["pipeline"]["repository"]["url"]
        self.owner, self.repo = parse_github_repo_url(self.repo_url)
        self.models_listing = models_listing
        self.stage_by_agent_name = {
            f"Pipeline: {stage.get('display_name', stage['name'])}": stage
            for stage in config["stages"]
        }
        
        # What the orchestrator did.
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.downloads: list[str] = []
        self.sleeps: list[float] = []
        
        # State of the stand-in repository and API.
        self.branches: dict[str, str] = {}
        self.files: dict[str, set[str]] = {}
        self.agents: dict[str, dict] = {}
        self.add_commit(files=[], branch="main")
        
        # Behaviour a test can change.
        self.withheld_files: set[str] = set()   # required files agents do not write
        self.agents_report_branch = True         # False: runs report no pushed branch
        self.agents_push_commit = True           # False: the branch stays at the input commit
        self.run_statuses: dict[str, list[str]] = {}   # stage -> statuses reported in turn
        self.artifacts: dict[str, dict[str, bytes]] = {}   # stage -> artifact path -> bytes
        self.download_scheme = "https"
        self.before_launch: Optional[Callable[[dict], None]] = None
        self.scripted: list[tuple[str, Any, Any]] = []
    
    def add_commit(
        self,
        files: list[str],
        parent: Optional[str] = None,
        branch: Optional[str] = None,
        removed: tuple[str, ...] = (),
    ) -> str:
        """Add a commit holding the parent's files plus `files`, minus `removed`."""
        commit = hashlib.sha1(f"fake commit {len(self.files)}".encode()).hexdigest()
        self.files[commit] = (self.files.get(parent, set()) | set(files)) - set(removed)
        if branch is not None:
            self.branches[branch] = commit
        return commit
    
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
        if rest[:1] == ["contents"]:
            path = "/".join(rest[1:])
            files = self.files.get(query["ref"][0], set())
            if path in files:
                return 200, {"type": "file", "path": path}
            if any(file.startswith(path + "/") for file in files):
                return 200, [{"type": "file", "path": file} for file in sorted(files)]
            return not_found
        raise AssertionError(f"The stand-in has no answer for GitHub path {rest}")
    
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
        stage = self.stage_by_agent_name[body["name"]]
        starting_ref = body["repos"][0]["startingRef"]
        start_commit = self.branches.get(starting_ref, starting_ref)
        if start_commit not in self.files:
            return 400, {"error": {"code": "invalid_ref", "message": "Unknown starting ref"}}
        
        number = len(self.agents) + 1
        branch = f"cursor/{stage['name']}-{number:04x}"
        if self.agents_push_commit:
            written = [
                path
                for path in stage.get("output_manifest", {}).get("required_files", [])
                if path not in self.withheld_files
            ]
            self.add_commit(files=written, parent=start_commit, branch=branch)
        else:
            self.branches[branch] = start_commit
        self.agents[agent_id] = {
            "id": agent_id,
            "name": body["name"],
            "url": f"https://cursor.com/agents/{agent_id}",
            "run_id": f"run-{number:04d}",
            "stage": stage["name"],
            "branch": branch,
            "starting_ref": starting_ref,
            "statuses": list(self.run_statuses.get(stage["name"], ["FINISHED"])),
        }
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


def make_launch_mock() -> FakeServices:
    """Stand-in services for the committed config: launches succeed and runs finish."""
    return make_launch_mock_with_listing(models_listing=MOCK_PIPELINE_MODELS_RESPONSE)


def make_launch_mock_with_listing(models_listing: dict) -> FakeServices:
    """Like make_launch_mock, with a given stand-in for the GET /v1/models listing."""
    return FakeServices(
        config=load_config(config_path=REAL_CONFIG_PATH), models_listing=models_listing,
    )


def open_project_by_hand(
    orchestrator: Orchestrator,
    backend: FakeServices,
    completed: tuple[str, ...] = (),
) -> None:
    """Open a project with no document, with the given stages recorded as complete."""
    manifest = PipelineManifest(
        pipeline_name="test",
        repo_url=orchestrator.repo_config["url"],
        started_at="2026-11-24T00:00:00Z",
        source_ref="main",
        source_commit=backend.branches["main"],
    )
    commit = backend.branches["main"]
    for name in completed:
        commit = backend.add_commit(files=[], parent=commit, branch=f"cursor/{name}-by-hand")
        manifest.stages[name] = StageResult(
            name=name, status=STAGE_COMPLETE,
            output_branch=f"cursor/{name}-by-hand", output_commit=commit,
        )
    orchestrator.manifest = manifest


def start_project(
    backend: FakeServices,
    directory: Path,
    manual_review: bool = False,
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
        manual_review=manual_review,
        manifest_path=directory / "manifest.json",
    )
    orchestrator.start_new_project()
    return orchestrator


def resume_from_file(
    backend: FakeServices,
    manifest_path: Path,
    manual_review: bool = False,
    rerun_stage: Optional[str] = None,
) -> Orchestrator:
    """Resume a project from its saved manifest file, as a new process would."""
    orchestrator = load_real_orchestrator(
        mock_http=backend,
        dry_run=False,
        manual_review=manual_review,
        manifest_path=manifest_path,
    )
    orchestrator.resume_project(
        manifest=load_manifest(manifest_path=manifest_path), rerun_stage=rerun_stage,
    )
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
        write_document(directory=directory, file_name="empty.tex", text="")
        write_document(directory=directory, file_name="blank.qmd", text="  \n\n")
        (directory / "latin1.tex").write_bytes("caf\xe9".encode("latin-1"))
        
        cases = [
            ("", "No location was given"),
            ("   ", "No location was given"),
            ('""', "No location was given"),
            (str(directory / "missing.qmd"), "does not exist"),
            (str(directory / "folder.tex"), "is not a file"),
            (str(directory / "notes.txt"), "must be a .qmd or .tex file"),
            (str(directory / "paper.pdf"), "must be a .qmd or .tex file"),
            (str(directory / "no_extension"), "must be a .qmd or .tex file"),
            (str(directory / "empty.tex"), "is empty"),
            (str(directory / "blank.qmd"), "is empty"),
            (str(directory / "latin1.tex"), "is not UTF-8 text"),
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


def test_valid_qmd_and_tex_documents_reach_stage1_in_the_launch_request():
    """The Stage 1 launch body holds the selected file's text, not its local path."""
    for file_name, quote in (("model_setup.qmd", ""), ("model_setup.tex", ""), ("MODEL.TEX", '"')):
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
            stage1 = get_stage(orchestrator=orchestrator, name=STAGE1_NAME)
            
            result = orchestrator._run_stage(stage=stage1)
            
            assert result.status == STAGE_COMPLETE, file_name
            launch_requests = get_launch_requests(mock_http=mock_http)
            assert len(launch_requests) == 1
            prompt_text = launch_requests[0]["prompt"]["text"]
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
    print("✓ test_valid_qmd_and_tex_documents_reach_stage1_in_the_launch_request passed")


def test_stage1_is_blocked_when_no_document_was_selected():
    """Without a selected document, Stage 1 sends nothing, in a real run or a dry run."""
    for dry_run in (False, True):
        mock_http = make_launch_mock()
        orchestrator = load_real_orchestrator(mock_http=mock_http, dry_run=dry_run)
        open_project_by_hand(orchestrator=orchestrator, backend=mock_http)
        stage1 = get_stage(orchestrator=orchestrator, name=STAGE1_NAME)
        
        result = orchestrator._run_stage(stage=stage1)
        
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
        
        for path in (tex_with_input, qmd_with_include):
            try:
                load_model_setup_document(location=str(path))
            except ModelSetupDocumentError as error:
                assert "pulls in other files" in str(error)
                assert "line " in str(error)
            else:
                raise AssertionError(f"Expected {path.name} to be refused")
        
        document = load_model_setup_document(location=str(self_contained))
        assert document.format == "tex"
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
    with tempfile.TemporaryDirectory() as tmp:
        document_path = write_document(directory=Path(tmp), file_name="model_setup.tex")
        manifest = start_project_and_reload_manifest(document_path=document_path)
        
        mock_http = make_launch_mock()
        orchestrator = load_real_orchestrator(mock_http=mock_http, dry_run=False)
        orchestrator.resume_project(manifest=manifest)
        assert mock_http.calls == []
        
        result = orchestrator._run_stage(
            stage=get_stage(orchestrator=orchestrator, name=STAGE1_NAME),
        )
        
        assert result.status == STAGE_COMPLETE
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
        
        unrecorded = PipelineManifest(
            pipeline_name="test", repo_url=manifest.repo_url,
            started_at="2026-11-24T00:00:00Z", source_ref="main",
        )
        expect_resume_refused(
            resumed=unrecorded, expected_message_part="records no model setup",
        )
        
        unrecorded.model_setup_document = {"path": str(document_path)}
        expect_resume_refused(resumed=unrecorded, expected_message_part="missing: sha256")
    print("✓ test_resume_fails_before_launch_when_the_recorded_document_is_unusable passed")


def test_resume_after_stage1_finished_does_not_need_the_document():
    """Once the stage that receives the document is done, a resume neither asks nor reads it."""
    with tempfile.TemporaryDirectory() as tmp:
        document_path = write_document(directory=Path(tmp), file_name="model_setup.qmd")
        manifest = start_project_and_reload_manifest(document_path=document_path)
        document_path.unlink()
    
    mock_http = make_launch_mock()
    stage1_commit = mock_http.add_commit(files=[], parent=mock_http.branches["main"])
    manifest.stages[STAGE1_NAME] = StageResult(
        name=STAGE1_NAME, status=STAGE_COMPLETE,
        output_branch="cursor/stage1-output", output_commit=stage1_commit,
    )
    orchestrator = load_real_orchestrator(mock_http=mock_http, dry_run=False)
    orchestrator.resume_project(manifest=manifest)
    
    result = orchestrator.run_pipeline()
    
    launch_requests = get_launch_requests(mock_http=mock_http)
    assert len(launch_requests) == 3
    assert launch_requests[0]["repos"][0]["startingRef"] == stage1_commit
    assert all(MODEL_SETUP_HEADING not in body["prompt"]["text"] for body in launch_requests)
    assert result.stages[STAGE2_NAME].status == STAGE_COMPLETE
    print("✓ test_resume_after_stage1_finished_does_not_need_the_document passed")


def test_cli_execute_asks_records_the_document_and_resume_does_not_ask():
    """--execute asks before anything is sent; --resume continues without asking.

    Runs the committed config through the command line to the end: each of
    the first three stages pauses for review and is approved in turn.
    """
    with tempfile.TemporaryDirectory() as tmp:
        document_path = write_document(directory=Path(tmp), file_name="model_setup.qmd")
        manifest_path = Path(tmp) / "project_manifest.json"
        backend = make_launch_mock()
        execute_arguments = [
            "--execute", "--config", str(REAL_CONFIG_PATH), "--output", str(manifest_path),
        ]
        resume_arguments = ["--resume", str(manifest_path), "--config", str(REAL_CONFIG_PATH)]
        
        exit_code = run_cli(backend=backend, arguments=execute_arguments, ask=end_of_input)
        assert exit_code == 1
        assert backend.calls == [], "No request may be sent when no document is given"
        assert not manifest_path.exists()
        
        ask = answer_with(str(document_path))
        exit_code = run_cli(backend=backend, arguments=execute_arguments, ask=ask)
        assert exit_code == 2, "The committed config pauses for review after Stage 1"
        assert ask.questions == [MODEL_SETUP_QUESTION]
        launch_requests = get_launch_requests(mock_http=backend)
        assert len(launch_requests) == 1
        assert SENTINEL_DOCUMENT_TEXT.rstrip() in launch_requests[0]["prompt"]["text"]
        
        saved_text = manifest_path.read_text(encoding="utf-8")
        assert SENTINEL_DOCUMENT_TEXT.rstrip() not in saved_text, "The text is not stored"
        saved = load_manifest(manifest_path=manifest_path)
        assert saved.model_setup_document["path"] == str(document_path.resolve())
        assert saved.model_setup_document["sha256"] == hashlib.sha256(
            document_path.read_bytes()
        ).hexdigest()
        assert saved.stages[STAGE1_NAME].status == STAGE_AWAITING_REVIEW
        
        exit_code = run_cli(backend=backend, arguments=execute_arguments)
        assert exit_code == 1, "A new project must not overwrite an existing project's manifest"
        assert manifest_path.read_text(encoding="utf-8") == saved_text
        
        exit_code = run_cli(backend=backend, arguments=resume_arguments)
        assert exit_code == 2, "A resume without an approval stays paused"
        assert launch_count(backend=backend) == 1
        
        for approved, expected_exit_code in (
            (STAGE1_NAME, 2), (STAGE2_NAME, 2), (STAGE3_NAME, 0),
        ):
            exit_code = run_cli(
                backend=backend, arguments=[*resume_arguments, "--approve", approved],
            )
            assert exit_code == expected_exit_code, approved
        
        launch_requests = get_launch_requests(mock_http=backend)
        assert len(launch_requests) == 4
        assert all(
            MODEL_SETUP_HEADING not in body["prompt"]["text"] for body in launch_requests[1:]
        )
        finished = load_manifest(manifest_path=manifest_path)
        assert [finished.stages[name].status for name in (
            STAGE1_NAME, STAGE2_NAME, STAGE3_NAME, STAGE4_NAME,
        )] == [STAGE_COMPLETE] * 4
        assert finished.completed_at is not None
        
        exit_code = run_cli(backend=backend, arguments=resume_arguments)
        assert exit_code == 0
        assert launch_count(backend=backend) == 4, "Complete stages are never launched again"
    print("✓ test_cli_execute_asks_records_the_document_and_resume_does_not_ask passed")


def test_cli_dry_run_is_offline_and_saves_nothing():
    """--dry-run asks for the document, needs no API key, sends nothing and saves nothing."""
    old_key = os.environ.pop("CURSOR_API_KEY", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            document_path = write_document(directory=Path(tmp), file_name="model_setup.tex")
            manifest_path = Path(tmp) / "project_manifest.json"
            dry_run_arguments = [
                "--dry-run", "--config", str(REAL_CONFIG_PATH), "--output", str(manifest_path),
            ]
            backend = make_launch_mock()
            
            exit_code = run_cli(
                backend=backend, arguments=dry_run_arguments, ask=answer_with(""), api_key=None,
            )
            assert exit_code == 1
            
            ask = answer_with(str(document_path))
            exit_code = run_cli(
                backend=backend, arguments=dry_run_arguments, ask=ask, api_key=None,
            )
            assert exit_code == 0, "A dry run needs no API key"
            assert ask.questions == [MODEL_SETUP_QUESTION]
            assert backend.calls == [], "A dry run sends no request to any service"
            assert backend.downloads == [] and backend.sleeps == []
            assert sorted(path.name for path in Path(tmp).iterdir()) == ["model_setup.tex"]
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
    ]
    
    unlisted = copy.deepcopy(MOCK_PIPELINE_MODELS_RESPONSE)
    unlisted["items"] = unlisted["items"][:1]
    assert check(backend=make_launch_mock_with_listing(models_listing=unlisted)) == 1
    
    backend = make_launch_mock()
    del backend.branches["main"]
    assert check(backend=backend) == 1, "A starting ref that cannot be read fails the check"
    
    old_key = os.environ.pop("CURSOR_API_KEY", None)
    try:
        backend = make_launch_mock()
        assert check(backend=backend, api_key=None) == 1
        assert all("api.cursor.com" not in url for _, url, _, _ in backend.calls)
    finally:
        if old_key:
            os.environ["CURSOR_API_KEY"] = old_key
    
    assert [launch_count(backend=used) for used in used_backends] == [0, 0, 0, 0]
    print("✓ test_cli_check_online_validates_over_the_network_and_launches_nothing passed")


def test_validate_config_requires_the_first_stage_to_receive_the_document():
    """The config cannot switch off sending the document to the first stage."""
    def validate(first_stage_extra: dict) -> tuple[bool, list[str]]:
        config = {
            "pipeline": {"repository": {"url": "https://github.com/test/repo"}},
            "api": MINIMAL_API_CONFIG,
            "stages": [{"name": "s1", "prompt": "test", **first_stage_extra}],
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
        orchestrator.run_pipeline()
    
    launch_requests = get_launch_requests(mock_http=mock_http)
    assert [body["model"] for body in launch_requests] == [
        EXPECTED_STAGE_MODELS[name]
        for name in (STAGE1_NAME, STAGE2_NAME, STAGE3_NAME, STAGE4_NAME)
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
            "stages": [{
                "name": "s1",
                "prompt": "test",
                "receives_model_setup_document": True,
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
    result = orchestrator._run_stage(
        stage=get_stage(orchestrator=orchestrator, name=STAGE1_NAME),
    )
    return orchestrator, result


def test_each_stage_starts_from_an_exact_commit_and_hands_on_a_verified_commit():
    """startingRef is always a commit SHA: the source commit, then each checked output commit."""
    names = (STAGE1_NAME, STAGE2_NAME, STAGE3_NAME, STAGE4_NAME)
    with tempfile.TemporaryDirectory() as tmp:
        backend = make_launch_mock()
        source_commit = backend.branches["main"]
        _, stage1 = run_stage1(backend=backend, directory=Path(tmp))
        assert stage1.status == STAGE_COMPLETE
        stage1_commit = stage1.output_commit
        assert stage1_commit == backend.branches[stage1.output_branch]
        
        # After the handoff is recorded, both the source branch and the
        # Stage 1 branch move on. The recorded commits must still be used.
        backend.add_commit(files=["later_on_main.txt"], parent=source_commit, branch="main")
        backend.add_commit(
            files=["later_on_stage1.txt"], parent=stage1_commit, branch=stage1.output_branch,
        )
        
        manifest_path = Path(tmp) / "manifest.json"
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        manifest = resumed.run_pipeline()
        assert load_manifest(manifest_path=manifest_path) == manifest, "Every step is on disk"
    
    bodies = get_launch_requests(mock_http=backend)
    results = [manifest.stages[name] for name in names]
    assert len(bodies) == 4
    assert (manifest.source_ref, manifest.source_commit) == ("main", source_commit)
    assert results[0].output_commit == stage1_commit
    
    expected_inputs = [source_commit] + [result.output_commit for result in results[:3]]
    assert [body["repos"][0]["startingRef"] for body in bodies] == expected_inputs
    assert [result.input_commit for result in results] == expected_inputs
    for body, result in zip(bodies, results):
        assert result.status == STAGE_COMPLETE
        assert re.fullmatch(r"[0-9a-f]{40}", body["repos"][0]["startingRef"])
        assert body["agentId"] == result.agent_id
        assert AGENT_ID_PATTERN.fullmatch(result.agent_id)
        assert result.gate["passed"] is True
        assert result.gate["commit"] == result.output_commit
    assert len({result.agent_id for result in results}) == 4
    assert len({result.output_commit for result in results}) == 4
    print("✓ test_each_stage_starts_from_an_exact_commit_and_hands_on_a_verified_commit passed")


def test_missing_output_file_stops_the_pipeline_until_the_file_is_there():
    """A required file missing at the pushed commit stops the chain; re-checking costs no launch."""
    missing = "outputs/stage1_derivation/summary.md"
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / "manifest.json"
        backend = make_launch_mock()
        backend.withheld_files = {missing}
        orchestrator = start_project(backend=backend, directory=Path(tmp))
        
        manifest = orchestrator.run_pipeline()
        stage1 = manifest.stages[STAGE1_NAME]
        assert stage1.status == STAGE_GATE_FAILED
        assert missing in stage1.error
        assert stage1.gate["passed"] is False
        assert stage1.gate["missing_files"] == [missing]
        assert stage1.output_commit is None, "A commit that failed the check is not handed on"
        assert STAGE2_NAME not in manifest.stages
        assert launch_count(backend=backend) == 1
        
        manifest = resume_from_file(backend=backend, manifest_path=manifest_path).run_pipeline()
        assert manifest.stages[STAGE1_NAME].status == STAGE_GATE_FAILED
        assert launch_count(backend=backend) == 1, "Checking again launches nothing"
        
        # The missing file is pushed to the stage's branch.
        branch = stage1.output_branch
        fixed_commit = backend.add_commit(
            files=[missing], parent=backend.branches[branch], branch=branch,
        )
        manifest = resume_from_file(backend=backend, manifest_path=manifest_path).run_pipeline()
        stage1 = manifest.stages[STAGE1_NAME]
        assert stage1.status == STAGE_COMPLETE
        assert stage1.output_commit == fixed_commit
        assert launch_count(backend=backend) == 4
        launched_stage2_from = get_launch_requests(mock_http=backend)[1]["repos"][0]["startingRef"]
        assert launched_stage2_from == fixed_commit
    print("✓ test_missing_output_file_stops_the_pipeline_until_the_file_is_there passed")


def test_output_check_fails_without_a_branch_a_new_commit_or_a_readable_repository():
    """No pushed branch, no new commit, or an unreadable repository: nothing is handed on."""
    rate_limited = (403, {"message": "API rate limit exceeded"})
    scenarios = [
        (lambda backend: setattr(backend, "agents_report_branch", False), "reported no pushed branch"),
        (lambda backend: setattr(backend, "agents_push_commit", False), "pushed no new commit"),
        (lambda backend: backend.script("GET", "/contents/", rate_limited), "API rate limit exceeded"),
    ]
    for change, expected_message_part in scenarios:
        with tempfile.TemporaryDirectory() as tmp:
            backend = make_launch_mock()
            change(backend)
            orchestrator = start_project(backend=backend, directory=Path(tmp))
            
            manifest = orchestrator.run_pipeline()
            
            stage1 = manifest.stages[STAGE1_NAME]
            assert stage1.status == STAGE_GATE_FAILED, expected_message_part
            assert expected_message_part in stage1.error, stage1.error
            assert stage1.output_commit is None
            assert STAGE2_NAME not in manifest.stages
            assert launch_count(backend=backend) == 1
            assert backend.sleeps == [], "A refusal (HTTP 403) is not tried again"
    
    # The repository became readable again: the same output passes on a resume.
    with tempfile.TemporaryDirectory() as tmp:
        backend = make_launch_mock()
        backend.script("GET", "/contents/", rate_limited)
        orchestrator, result = run_stage1(backend=backend, directory=Path(tmp))
        assert result.status == STAGE_GATE_FAILED
        again = orchestrator._run_stage(
            stage=get_stage(orchestrator=orchestrator, name=STAGE1_NAME),
        )
        assert again.status == STAGE_COMPLETE
        assert launch_count(backend=backend) == 1
    print("✓ test_output_check_fails_without_a_branch_a_new_commit_or_a_readable_repository passed")


def test_failed_run_is_launched_again_only_by_an_explicit_rerun():
    """A plain resume never relaunches a failed stage; --rerun-stage does, and keeps the record."""
    with tempfile.TemporaryDirectory() as tmp:
        document_path = write_document(directory=Path(tmp), file_name="model_setup.qmd")
        manifest_path = Path(tmp) / "project_manifest.json"
        backend = make_launch_mock()
        backend.run_statuses[STAGE1_NAME] = ["RUNNING", "ERROR"]
        resume_arguments = ["--resume", str(manifest_path), "--config", str(REAL_CONFIG_PATH)]
        
        exit_code = run_cli(
            backend=backend,
            arguments=[
                "--execute", "--config", str(REAL_CONFIG_PATH), "--output", str(manifest_path),
            ],
            ask=answer_with(str(document_path)),
        )
        assert exit_code == 1
        failed = load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME]
        assert (failed.status, failed.run_status) == (STAGE_RUN_FAILED, "ERROR")
        assert launch_count(backend=backend) == 1
        
        assert run_cli(backend=backend, arguments=resume_arguments) == 1
        assert launch_count(backend=backend) == 1, "A plain resume launches nothing for it"
        
        refused = [
            [*resume_arguments, "--rerun-stage", STAGE2_NAME],
            [*resume_arguments, "--rerun-stage", STAGE1_NAME, "--approve", STAGE1_NAME],
            [*resume_arguments, "--rerun-stage", STAGE1_NAME,
             "--output", str(Path(tmp) / "other.json")],
            ["--execute", "--config", str(REAL_CONFIG_PATH),
             "--output", str(Path(tmp) / "new.json"), "--rerun-stage", STAGE1_NAME],
            [*resume_arguments, "--approve-commit", "a" * 40],
        ]
        for arguments in refused:
            assert run_cli(backend=backend, arguments=arguments) == 1, arguments
            assert launch_count(backend=backend) == 1, arguments
        assert load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME] == failed
        assert sorted(path.name for path in Path(tmp).iterdir()) == [
            "model_setup.qmd", "project_manifest.json",
        ]
        
        backend.run_statuses[STAGE1_NAME] = ["FINISHED"]
        exit_code = run_cli(
            backend=backend, arguments=[*resume_arguments, "--rerun-stage", STAGE1_NAME],
        )
        assert exit_code == 2, "The new attempt finished and now waits for review"
        manifest = load_manifest(manifest_path=manifest_path)
        rerun = manifest.stages[STAGE1_NAME]
        assert launch_count(backend=backend) == 2
        assert (rerun.attempt, rerun.status) == (2, STAGE_AWAITING_REVIEW)
        assert rerun.agent_id != failed.agent_id
        assert [
            (previous["attempt"], previous["status"], previous["agent_id"])
            for previous in manifest.previous_attempts
        ] == [(1, STAGE_RUN_FAILED, failed.agent_id)]
    print("✓ test_failed_run_is_launched_again_only_by_an_explicit_rerun passed")


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
    assert (len(backend.calls), backend.sleeps) == (2, [5])
    
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
        assert result.status == STAGE_COMPLETE
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
        assert result.status == STAGE_COMPLETE
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
        again = resumed._run_stage(stage=stage1_of(resumed))
        assert again.status == STAGE_COMPLETE
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
        assert result.status == STAGE_COMPLETE
        assert len(bodies) == 2 and bodies[0]["agentId"] == bodies[1]["agentId"]
        assert list(backend.agents) == [result.agent_id], "The same ID cannot create two agents"
    
    # The API refuses the request: no agent exists, and nothing is retried.
    with tempfile.TemporaryDirectory() as tmp:
        backend = make_launch_mock()
        backend.script(
            "POST", "/agents", (400, {"error": {"code": "invalid_request", "message": "bad field"}}),
        )
        orchestrator, result = run_stage1(backend=backend, directory=Path(tmp))
        assert result.status == STAGE_LAUNCH_REJECTED
        assert "bad field" in result.error
        assert (launch_count(backend=backend), backend.agents, backend.sleeps) == (1, {}, [])
        refused_agent_id = result.agent_id
        
        again = orchestrator._run_stage(stage=stage1_of(orchestrator))
        assert again.status == STAGE_COMPLETE
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
            orchestrator._run_stage(stage=stage1_of(orchestrator))
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
        
        result = orchestrator._run_stage(stage=stage1)
        assert result.status == STAGE_RUNNING
        assert "still going after 3 polls" in result.error
        assert backend.sleeps == [30, 30]
        
        backend.sleeps.clear()
        backend.script("GET", "/runs/", NO_RESPONSE, NO_RESPONSE, NO_RESPONSE)
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        result = resumed._run_stage(stage=stage1)
        assert result.status == STAGE_RUNNING
        assert "Could not read the run status" in result.error
        assert backend.sleeps == [5, 15]
        
        resumed = resume_from_file(backend=backend, manifest_path=manifest_path)
        result = resumed._run_stage(stage=stage1)
        assert result.status == STAGE_COMPLETE
        assert launch_count(backend=backend) == 1, "The same run was waited for; none was launched"
        assert len(backend.agents) == 1
    print("✓ test_run_that_is_still_going_is_waited_for_again_and_never_relaunched passed")


def test_manual_review_pauses_the_pipeline_until_the_output_is_approved():
    """Nothing is launched past a review point; approval fixes the commit that is handed on."""
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / "manifest.json"
        backend = make_launch_mock()
        orchestrator = start_project(backend=backend, directory=Path(tmp), manual_review=True)
        
        manifest = orchestrator.run_pipeline()
        stage1 = manifest.stages[STAGE1_NAME]
        assert stage1.status == STAGE_AWAITING_REVIEW
        assert stage1.review["requested_at"] and stage1.review["approved_at"] is None
        assert STAGE2_NAME not in manifest.stages
        assert launch_count(backend=backend) == 1
        
        resumed = resume_from_file(
            backend=backend, manifest_path=manifest_path, manual_review=True,
        )
        resumed.run_pipeline()
        assert launch_count(backend=backend) == 1, "A resume without an approval launches nothing"
        expect_state_error(
            action=lambda: resumed.approve_stage(stage_name=STAGE2_NAME),
            expected_message_part="not awaiting review",
        )
        
        # The reviewer pushes a correction to the stage's branch.
        stage1 = resumed.manifest.stages[STAGE1_NAME]
        checked_commit = stage1.output_commit
        corrected_commit = backend.add_commit(
            files=["outputs/stage1_derivation/reviewer_note.md"],
            parent=checked_commit,
            branch=stage1.output_branch,
        )
        expect_state_error(
            action=lambda: resumed.approve_stage(stage_name=STAGE1_NAME),
            expected_message_part=f"--approve-commit {corrected_commit}",
        )
        expect_state_error(
            action=lambda: resumed.approve_stage(stage_name=STAGE1_NAME, commit=checked_commit),
            expected_message_part="must be the current head",
        )
        assert stage1.status == STAGE_AWAITING_REVIEW
        assert stage1.output_commit == checked_commit
        
        resumed.approve_stage(stage_name=STAGE1_NAME, commit=corrected_commit)
        assert stage1.status == STAGE_COMPLETE
        assert stage1.output_commit == corrected_commit
        assert stage1.review["approved_commit"] == corrected_commit
        assert stage1.review["replaced_commit"] == checked_commit
        assert (stage1.gate["commit"], stage1.gate["passed"]) == (corrected_commit, True)
        assert load_manifest(manifest_path=manifest_path).stages[STAGE1_NAME] == stage1
        
        manifest = resumed.run_pipeline()
        stage2 = manifest.stages[STAGE2_NAME]
        assert stage2.status == STAGE_AWAITING_REVIEW
        assert stage2.input_commit == corrected_commit
        launched_from = get_launch_requests(mock_http=backend)[1]["repos"][0]["startingRef"]
        assert launched_from == corrected_commit
        
        # A reviewer's commit that removes a required file cannot be approved.
        stage2_commit = stage2.output_commit
        broken_commit = backend.add_commit(
            files=[],
            parent=stage2_commit,
            branch=stage2.output_branch,
            removed=("outputs/stage2_pseudocode/algorithm.md",),
        )
        expect_state_error(
            action=lambda: resumed.approve_stage(stage_name=STAGE2_NAME, commit=broken_commit),
            expected_message_part="outputs/stage2_pseudocode/algorithm.md",
        )
        assert stage2.status == STAGE_AWAITING_REVIEW
        
        backend.branches[stage2.output_branch] = stage2_commit
        resumed.approve_stage(stage_name=STAGE2_NAME)
        assert stage2.status == STAGE_COMPLETE
        assert stage2.review["approved_commit"] == stage2_commit
        assert "replaced_commit" not in stage2.review
        assert launch_count(backend=backend) == 2, "Approving launches nothing by itself"
    print("✓ test_manual_review_pauses_the_pipeline_until_the_output_is_approved passed")


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
        result = resumed._run_stage(stage=get_stage(orchestrator=resumed, name=STAGE1_NAME))
        assert result.status == STAGE_COMPLETE
        assert launch_count(backend=backend) == 1, "Collecting again launches no agent"
        
        saved_folder = f"collected_artifacts/manifest/{STAGE1_NAME}/attempt_1"
        files_on_disk = {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()
        }
        assert set(files_on_disk) == {
            "model_setup.qmd", "manifest.json",
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
        (lambda c: c["stages"][0].update(manual_review="yes"), ["manual_review"]),
        (lambda c: c["stages"][0].update(output_manifest={"required_files": ["../secret.txt"]}),
         ["required file '../secret.txt'"]),
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
    assert [stage["manual_review"] for stage in committed["stages"]] == [True, True, True, False]
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
        name=next(iter(backend.stage_by_agent_name)),
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
