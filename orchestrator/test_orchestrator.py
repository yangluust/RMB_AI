#!/usr/bin/env python3
"""
Unit tests for the pipeline orchestrator.

All tests use mocked HTTP - no network calls are made.
Run with: python -m pytest test_orchestrator.py -v
Or simply: python test_orchestrator.py
"""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Optional
from unittest.mock import MagicMock

import yaml

from api_client import CursorAPIClient, CursorAPIError, Model, RunStatus
from orchestrator import (
    GUARDRAILS_HEADING,
    MODEL_SETUP_BEGIN_LINE,
    MODEL_SETUP_END_LINE,
    MODEL_SETUP_HEADING,
    MODEL_SETUP_QUESTION,
    ModelSetupDocumentError,
    Orchestrator,
    PipelineManifest,
    StageConfig,
    StageResult,
    build_stage_prompt,
    load_config,
    load_manifest,
    load_model_setup_document,
    main,
)

ORCHESTRATOR_DIR = Path(__file__).resolve().parent
REAL_CONFIG_PATH = ORCHESTRATOR_DIR / "config.yaml"
DESIGN_DOC_PATH = ORCHESTRATOR_DIR.parent / "docs" / "pipeline-design.md"
DESIGN_DOC_GUARDRAILS_HEADING = "#### Reproducibility Guardrails (Stage 3)"
STAGE1_NAME = "equilibrium_derivation"
STAGE2_NAME = "equilibrium_to_pseudocode"
STAGE3_NAME = "pseudocode_to_implementation"
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
        "dry_run": True,
        "fail_fast": True,
    },
}


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
        "execution": {"dry_run": True, "fail_fast": True},
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
    
    for call in mock_http.calls:
        assert "POST" not in call[0] or "/agents" not in call[1], \
            "Dry run should not POST to /agents"
    
    assert manifest.stages["stage1"].status == "DRY_RUN"
    print("✓ test_orchestrator_dry_run passed")


def test_pipeline_manifest_serialization():
    """Test manifest serialization and deserialization."""
    manifest = PipelineManifest(
        pipeline_name="test",
        repo_url="https://github.com/test/repo",
        started_at="2026-11-24T00:00:00Z",
    )
    manifest.stages["stage1"] = StageResult(
        name="stage1",
        status="FINISHED",
        agent_id="bc-123",
        output_branch="cursor/test-a1b2",
    )
    
    data = manifest.to_dict()
    assert data["pipeline_name"] == "test"
    assert "stage1" in data["stages"]
    assert data["stages"]["stage1"]["status"] == "FINISHED"
    
    restored = PipelineManifest.from_dict(data)
    assert restored.pipeline_name == "test"
    assert restored.stages["stage1"].status == "FINISHED"
    assert restored.stages["stage1"].output_branch == "cursor/test-a1b2"
    print("✓ test_pipeline_manifest_serialization passed")


def test_stage_input_ref_from_dependency():
    """Test that stage input ref comes from dependency output."""
    mock_http = MockHttpClient()
    mock_http.add_response("GET", "/models", 200, MOCK_MODELS_RESPONSE)
    
    client = CursorAPIClient(api_key="test-key", http_client=mock_http)
    orchestrator = Orchestrator(config=MOCK_CONFIG, api_client=client, dry_run=True)
    
    orchestrator.manifest = PipelineManifest(
        pipeline_name="test",
        repo_url="https://github.com/test/repo",
        started_at="2026-11-24T00:00:00Z",
    )
    orchestrator.manifest.stages["stage1"] = StageResult(
        name="stage1",
        status="FINISHED",
        output_branch="cursor/stage1-output",
    )
    
    stage2 = orchestrator.stages[1]
    input_ref = orchestrator._get_stage_input_ref(stage2)
    
    assert input_ref == "cursor/stage1-output"
    print("✓ test_stage_input_ref_from_dependency passed")


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


def load_real_orchestrator(
    mock_http: MockHttpClient,
    dry_run: bool,
    ask: Callable[[str], str] = must_not_ask,
) -> Orchestrator:
    """Build an orchestrator from the committed config.yaml with mocked HTTP."""
    client = CursorAPIClient(api_key="test-key", http_client=mock_http)
    return Orchestrator(
        config=load_config(config_path=REAL_CONFIG_PATH),
        api_client=client,
        dry_run=dry_run,
        ask=ask,
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
    mock_http = MockHttpClient()
    mock_http.add_response("POST", "/agents", 200, MOCK_CREATE_RESPONSE)
    mock_http.add_response("GET", "/runs/", 200, MOCK_RUN_FINISHED)
    
    orchestrator = load_real_orchestrator(mock_http=mock_http, dry_run=False)
    stage3 = get_stage(orchestrator=orchestrator, name=STAGE3_NAME)
    
    result = orchestrator._run_stage(stage=stage3)
    assert result.status == "FINISHED"
    
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


def make_launch_mock() -> MockHttpClient:
    """Mock HTTP that accepts an agent launch and reports the run finished."""
    mock_http = MockHttpClient()
    mock_http.add_response("GET", "/models", 200, MOCK_MODELS_RESPONSE)
    mock_http.add_response("POST", "/agents", 200, MOCK_CREATE_RESPONSE)
    mock_http.add_response("GET", "/runs/", 200, MOCK_RUN_FINISHED)
    return mock_http


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
            
            assert result.status == "FINISHED", file_name
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
        stage1 = get_stage(orchestrator=orchestrator, name=STAGE1_NAME)
        
        result = orchestrator._run_stage(stage=stage1)
        
        assert result.status == "BLOCKED", f"dry_run={dry_run}"
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
        
        assert result.status == "FINISHED"
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
            pipeline_name="test", repo_url="https://github.com/test/repo",
            started_at="2026-11-24T00:00:00Z",
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
    
    manifest.stages[STAGE1_NAME] = StageResult(
        name=STAGE1_NAME, status="FINISHED", output_branch="cursor/stage1-output",
    )
    mock_http = make_launch_mock()
    orchestrator = load_real_orchestrator(mock_http=mock_http, dry_run=False)
    orchestrator.resume_project(manifest=manifest)
    
    result = orchestrator.run_pipeline()
    
    launch_requests = get_launch_requests(mock_http=mock_http)
    assert len(launch_requests) == 3
    assert launch_requests[0]["repos"][0]["startingRef"] == "cursor/stage1-output"
    assert all(MODEL_SETUP_HEADING not in body["prompt"]["text"] for body in launch_requests)
    assert result.stages[STAGE2_NAME].status == "FINISHED"
    print("✓ test_resume_after_stage1_finished_does_not_need_the_document passed")


def test_cli_execute_asks_records_the_document_and_resume_does_not_ask():
    """--execute asks before anything is sent; --resume continues without asking."""
    with tempfile.TemporaryDirectory() as tmp:
        document_path = write_document(directory=Path(tmp), file_name="model_setup.qmd")
        manifest_path = Path(tmp) / "project_manifest.json"
        mock_http = make_launch_mock()
        client = CursorAPIClient(api_key="test-key", http_client=mock_http)
        execute_arguments = [
            "--execute", "--config", str(REAL_CONFIG_PATH), "--output", str(manifest_path),
        ]
        
        exit_code = main(argv=execute_arguments, api_client=client, ask=end_of_input)
        assert exit_code == 1
        assert mock_http.calls == [], "No request may be sent when no document is given"
        assert not manifest_path.exists()
        
        ask = answer_with(str(document_path))
        exit_code = main(argv=execute_arguments, api_client=client, ask=ask)
        assert exit_code == 0
        assert ask.questions == [MODEL_SETUP_QUESTION]
        launch_requests = get_launch_requests(mock_http=mock_http)
        assert len(launch_requests) == 4
        assert SENTINEL_DOCUMENT_TEXT.rstrip() in launch_requests[0]["prompt"]["text"]
        assert all(
            MODEL_SETUP_HEADING not in body["prompt"]["text"] for body in launch_requests[1:]
        )
        
        saved_text = manifest_path.read_text(encoding="utf-8")
        assert SENTINEL_DOCUMENT_TEXT.rstrip() not in saved_text, "The text is not stored"
        saved = load_manifest(manifest_path=manifest_path)
        assert saved.model_setup_document["path"] == str(document_path.resolve())
        assert saved.model_setup_document["sha256"] == hashlib.sha256(
            document_path.read_bytes()
        ).hexdigest()
        
        exit_code = main(argv=execute_arguments, api_client=client, ask=must_not_ask)
        assert exit_code == 1, "A new project must not overwrite an existing project's manifest"
        assert manifest_path.read_text(encoding="utf-8") == saved_text
        
        exit_code = main(
            argv=["--resume", str(manifest_path), "--config", str(REAL_CONFIG_PATH)],
            api_client=client,
            ask=must_not_ask,
        )
        assert exit_code == 0
        assert len(get_launch_requests(mock_http=mock_http)) == 4, "Finished stages are not rerun"
    print("✓ test_cli_execute_asks_records_the_document_and_resume_does_not_ask passed")


def test_cli_dry_run_asks_and_launches_and_saves_nothing():
    """--dry-run asks too, reports failure without a document, and never launches or saves."""
    with tempfile.TemporaryDirectory() as tmp:
        document_path = write_document(directory=Path(tmp), file_name="model_setup.tex")
        manifest_path = Path(tmp) / "project_manifest.json"
        dry_run_arguments = [
            "--dry-run", "--config", str(REAL_CONFIG_PATH), "--output", str(manifest_path),
        ]
        
        mock_http = make_launch_mock()
        client = CursorAPIClient(api_key="test-key", http_client=mock_http)
        exit_code = main(argv=dry_run_arguments, api_client=client, ask=answer_with(""))
        assert exit_code == 1
        assert mock_http.calls == []
        
        ask = answer_with(str(document_path))
        exit_code = main(argv=dry_run_arguments, api_client=client, ask=ask)
        assert exit_code == 0
        assert ask.questions == [MODEL_SETUP_QUESTION]
        assert get_launch_requests(mock_http=mock_http) == []
        assert not manifest_path.exists()
    print("✓ test_cli_dry_run_asks_and_launches_and_saves_nothing passed")


def test_validate_config_requires_the_first_stage_to_receive_the_document():
    """The config cannot switch off sending the document to the first stage."""
    def validate(first_stage_extra: dict) -> tuple[bool, list[str]]:
        config = {
            "pipeline": {"repository": {"url": "https://github.com/test/repo"}},
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


def run_all_tests():
    """Run every test_* function in this module."""
    print("Running orchestrator tests (mocked HTTP, no network)...")
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
