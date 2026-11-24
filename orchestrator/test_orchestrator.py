#!/usr/bin/env python3
"""
Unit tests for the pipeline orchestrator.

All tests use mocked HTTP - no network calls are made.
Run with: python -m pytest test_orchestrator.py -v
Or simply: python test_orchestrator.py
"""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import yaml

from api_client import CursorAPIClient, CursorAPIError, Model, RunStatus
from orchestrator import (
    DEFINITION_BEGIN_MARKER,
    DEFINITION_END_MARKER,
    DEFINITION_FILLED_STATUS_LINE,
    DEFINITION_HEADING,
    DEFINITION_PLACEHOLDER,
    GUARDRAILS_HEADING,
    REPO_ROOT,
    DefinitionNotSuppliedError,
    Orchestrator,
    PipelineManifest,
    StageConfig,
    StageResult,
    build_stage_prompt,
    load_config,
    read_equilibrium_definition,
)

ORCHESTRATOR_DIR = Path(__file__).resolve().parent
REAL_CONFIG_PATH = ORCHESTRATOR_DIR / "config.yaml"
DESIGN_DOC_RELATIVE_PATH = Path("docs") / "pipeline-design.md"
DESIGN_DOC_PATH = ORCHESTRATOR_DIR.parent / DESIGN_DOC_RELATIVE_PATH
DESIGN_DOC_GUARDRAILS_HEADING = "#### Reproducibility Guardrails (Stage 3)"
STAGE1_NAME = "equilibrium_derivation"
STAGE3_NAME = "pseudocode_to_implementation"
EXPECTED_GUARDRAIL_COUNT = 12

# Stand-in definition blocks for tests. The "filled" text is a sentinel, not
# a real equilibrium definition.
UNFILLED_DEFINITION_BLOCK = (
    f"**Status:** UNFILLED\n\n**1. Field.**\n\n> {DEFINITION_PLACEHOLDER}\n"
)
SENTINEL_DEFINITION_TEXT = "TEST-ONLY SENTINEL TEXT, not a real definition."
FILLED_DEFINITION_BLOCK = (
    f"{DEFINITION_FILLED_STATUS_LINE}\n\n**1. Field.**\n\n> {SENTINEL_DEFINITION_TEXT}\n"
)


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
                "prompt": "Do something",
            },
        ],
        "execution": {"dry_run": True, "fail_fast": True},
    }
    
    client = CursorAPIClient(api_key="test-key", http_client=mock_http)
    orchestrator = Orchestrator(config=config_no_deps, api_client=client, dry_run=True)
    
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


def load_real_orchestrator(
    mock_http: MockHttpClient,
    dry_run: bool,
    repo_root: Optional[Path] = None,
) -> Orchestrator:
    """Build an orchestrator from the committed config.yaml with mocked HTTP."""
    client = CursorAPIClient(api_key="test-key", http_client=mock_http)
    return Orchestrator(
        config=load_config(config_path=REAL_CONFIG_PATH),
        api_client=client,
        dry_run=dry_run,
        repo_root=repo_root,
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


def write_definition_doc(repo_root: Path, block: str) -> Path:
    """Write a stand-in design doc holding one definition block; return its path."""
    path = repo_root / DESIGN_DOC_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# Stand-in doc\n\n{DEFINITION_BEGIN_MARKER}\n{block}\n{DEFINITION_END_MARKER}\n",
        encoding="utf-8",
    )
    return path


def expect_definition_rejected(path: Path, expected_message_part: str) -> None:
    try:
        read_equilibrium_definition(path=path)
    except DefinitionNotSuppliedError as error:
        assert expected_message_part in str(error), str(error)
    else:
        raise AssertionError(f"Expected rejection mentioning '{expected_message_part}'")


def test_unfilled_definition_blocks_stage1_launch():
    """With an unfilled template, --execute sends no request at all for Stage 1."""
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        write_definition_doc(repo_root=repo_root, block=UNFILLED_DEFINITION_BLOCK)
        mock_http = make_launch_mock()
        orchestrator = load_real_orchestrator(
            mock_http=mock_http, dry_run=False, repo_root=repo_root,
        )
        stage1 = get_stage(orchestrator=orchestrator, name=STAGE1_NAME)
        
        result = orchestrator._run_stage(stage=stage1)
    
    assert result.status == "BLOCKED"
    assert "placeholder" in result.error
    assert result.agent_id is None
    assert mock_http.calls == [], "No HTTP request may be sent for a blocked stage"
    print("✓ test_unfilled_definition_blocks_stage1_launch passed")


def test_dry_run_reports_blocked_for_unfilled_definition():
    """A dry run does not report Stage 1 as ready while the template is unfilled."""
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        write_definition_doc(repo_root=repo_root, block=UNFILLED_DEFINITION_BLOCK)
        mock_http = make_launch_mock()
        orchestrator = load_real_orchestrator(
            mock_http=mock_http, dry_run=True, repo_root=repo_root,
        )
        
        manifest = orchestrator.run_pipeline()
    
    assert manifest.stages[STAGE1_NAME].status == "BLOCKED"
    assert list(manifest.stages) == [STAGE1_NAME], "Later stages must not run"
    assert mock_http.calls == []
    print("✓ test_dry_run_reports_blocked_for_unfilled_definition passed")


def test_filled_definition_is_sent_in_stage1_launch_request():
    """With a filled block, the Stage 1 launch request carries that block."""
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        write_definition_doc(repo_root=repo_root, block=FILLED_DEFINITION_BLOCK)
        mock_http = make_launch_mock()
        orchestrator = load_real_orchestrator(
            mock_http=mock_http, dry_run=False, repo_root=repo_root,
        )
        stage1 = get_stage(orchestrator=orchestrator, name=STAGE1_NAME)
        
        result = orchestrator._run_stage(stage=stage1)
    
    assert result.status == "FINISHED"
    launch_requests = [call for call in mock_http.calls if call[0] == "POST"]
    assert len(launch_requests) == 1
    prompt_text = launch_requests[0][3]["prompt"]["text"]
    
    task_position = prompt_text.index("TASK: Derive the equilibrium")
    heading_position = prompt_text.index(DEFINITION_HEADING)
    definition_position = prompt_text.index(SENTINEL_DEFINITION_TEXT)
    assert task_position < heading_position < definition_position
    assert DEFINITION_FILLED_STATUS_LINE in prompt_text
    assert DEFINITION_PLACEHOLDER not in prompt_text
    print("✓ test_filled_definition_is_sent_in_stage1_launch_request passed")


def test_definition_rejected_unless_status_and_content_are_both_filled():
    """Flipping the status, or removing placeholders, alone is not enough."""
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        
        status_flipped_only = f"{DEFINITION_FILLED_STATUS_LINE}\n\n> {DEFINITION_PLACEHOLDER}\n"
        path = write_definition_doc(repo_root=repo_root, block=status_flipped_only)
        expect_definition_rejected(path=path, expected_message_part="placeholder")
        
        placeholders_removed_only = f"**Status:** UNFILLED\n\n> {SENTINEL_DEFINITION_TEXT}\n"
        path = write_definition_doc(repo_root=repo_root, block=placeholders_removed_only)
        expect_definition_rejected(path=path, expected_message_part="status line")
        
        path = write_definition_doc(repo_root=repo_root, block=DEFINITION_FILLED_STATUS_LINE)
        expect_definition_rejected(path=path, expected_message_part="no content")
        
        path = write_definition_doc(repo_root=repo_root, block=FILLED_DEFINITION_BLOCK)
        assert SENTINEL_DEFINITION_TEXT in read_equilibrium_definition(path=path)
    print("✓ test_definition_rejected_unless_status_and_content_are_both_filled passed")


def test_definition_rejected_for_missing_file_or_bad_markers():
    """A missing file, or missing, duplicated or reversed markers, is rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        path = repo_root / DESIGN_DOC_RELATIVE_PATH
        expect_definition_rejected(path=path, expected_message_part="not found")
        
        path.parent.mkdir(parents=True)
        path.write_text(FILLED_DEFINITION_BLOCK, encoding="utf-8")
        expect_definition_rejected(path=path, expected_message_part="exactly once")
        
        path.write_text(
            f"{DEFINITION_BEGIN_MARKER}\n{DEFINITION_BEGIN_MARKER}\n"
            f"{FILLED_DEFINITION_BLOCK}\n{DEFINITION_END_MARKER}\n",
            encoding="utf-8",
        )
        expect_definition_rejected(path=path, expected_message_part="exactly once")
        
        path.write_text(
            f"{DEFINITION_END_MARKER}\n{FILLED_DEFINITION_BLOCK}\n{DEFINITION_BEGIN_MARKER}\n",
            encoding="utf-8",
        )
        expect_definition_rejected(path=path, expected_message_part="before the BEGIN")
    print("✓ test_definition_rejected_for_missing_file_or_bad_markers passed")


def test_build_stage_prompt_refuses_definition_stage_without_text():
    """A stage that needs a definition cannot get a prompt without definition text."""
    stage = StageConfig(
        name="needs_definition",
        display_name="Needs definition",
        description="",
        prompt="Do the task.\n",
        equilibrium_definition_file=str(DESIGN_DOC_RELATIVE_PATH),
    )
    for missing in (None, "", "   \n"):
        try:
            build_stage_prompt(stage=stage, equilibrium_definition=missing)
        except DefinitionNotSuppliedError:
            continue
        raise AssertionError(f"Expected refusal for equilibrium_definition={missing!r}")
    
    prompt_text = build_stage_prompt(
        stage=stage, equilibrium_definition=SENTINEL_DEFINITION_TEXT,
    )
    assert prompt_text == (
        f"Do the task.\n\n{DEFINITION_HEADING}\n{SENTINEL_DEFINITION_TEXT}\n"
    )
    print("✓ test_build_stage_prompt_refuses_definition_stage_without_text passed")


def test_run_validation_fails_only_while_definition_unfilled():
    """Validation (used by --dry-run, --execute and --resume) follows the block's state."""
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        
        write_definition_doc(repo_root=repo_root, block=UNFILLED_DEFINITION_BLOCK)
        unfilled = load_real_orchestrator(
            mock_http=make_launch_mock(), dry_run=True, repo_root=repo_root,
        )
        definitions_ok, errors = unfilled.validate_equilibrium_definitions()
        assert not definitions_ok
        assert len(errors) == 1 and STAGE1_NAME in errors[0]
        assert unfilled.run_validation() is False
        
        write_definition_doc(repo_root=repo_root, block=FILLED_DEFINITION_BLOCK)
        filled = load_real_orchestrator(
            mock_http=make_launch_mock(), dry_run=True, repo_root=repo_root,
        )
        assert filled.validate_equilibrium_definitions() == (True, [])
        assert filled.run_validation() is True
    print("✓ test_run_validation_fails_only_while_definition_unfilled passed")


def test_validate_config_rejects_malformed_equilibrium_definition_file():
    """equilibrium_definition_file must be a non-empty path string when present."""
    for malformed in ("", "   ", 5, ["docs/pipeline-design.md"]):
        config = {
            "pipeline": {"repository": {"url": "https://github.com/test/repo"}},
            "stages": [
                {"name": "s1", "prompt": "test", "equilibrium_definition_file": malformed},
            ],
        }
        orchestrator = Orchestrator(config=config, dry_run=True)
        is_valid, errors = orchestrator.validate_config()
        assert not is_valid, f"Expected rejection for {malformed!r}"
        assert any("equilibrium_definition_file" in error for error in errors)
    print("✓ test_validate_config_rejects_malformed_equilibrium_definition_file passed")


def test_committed_design_doc_gates_real_stage1_by_its_own_state():
    """The committed config and design doc together gate the real Stage 1.

    While the committed template is unfilled, Stage 1 must be blocked with no
    request sent. Once Yang Lu fills it in, the launch request must carry the
    committed block. Both branches are asserted, so this test stays valid
    after the definition is supplied.
    """
    doc_text = DESIGN_DOC_PATH.read_text(encoding="utf-8")
    assert doc_text.count(DEFINITION_BEGIN_MARKER) == 1
    assert doc_text.count(DEFINITION_END_MARKER) == 1
    block = doc_text.split(DEFINITION_BEGIN_MARKER)[1].split(DEFINITION_END_MARKER)[0].strip()
    committed_block_is_unfilled = (
        DEFINITION_PLACEHOLDER in block or DEFINITION_FILLED_STATUS_LINE not in block
    )
    
    mock_http = make_launch_mock()
    orchestrator = load_real_orchestrator(mock_http=mock_http, dry_run=False)
    assert orchestrator.repo_root == REPO_ROOT
    stage1 = get_stage(orchestrator=orchestrator, name=STAGE1_NAME)
    assert stage1.equilibrium_definition_file == DESIGN_DOC_RELATIVE_PATH.as_posix()
    
    result = orchestrator._run_stage(stage=stage1)
    
    if committed_block_is_unfilled:
        assert result.status == "BLOCKED"
        assert mock_http.calls == []
    else:
        assert result.status == "FINISHED"
        launch_requests = [call for call in mock_http.calls if call[0] == "POST"]
        assert len(launch_requests) == 1
        assert block in launch_requests[0][3]["prompt"]["text"]
    print("✓ test_committed_design_doc_gates_real_stage1_by_its_own_state passed")


def test_documented_fill_steps_open_the_gate_for_the_committed_template():
    """Following the design doc's fill steps on a copy of the committed doc opens the gate.

    The copy gets sentinel text in place of every placeholder and its status
    set to FILLED. If the committed doc is already filled, both edits are
    no-ops and the gate must still open.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        copy_path = repo_root / DESIGN_DOC_RELATIVE_PATH
        copy_path.parent.mkdir(parents=True)
        filled_text = (
            DESIGN_DOC_PATH.read_text(encoding="utf-8")
            .replace(DEFINITION_PLACEHOLDER, SENTINEL_DEFINITION_TEXT)
            .replace("**Status:** UNFILLED", DEFINITION_FILLED_STATUS_LINE)
        )
        copy_path.write_text(filled_text, encoding="utf-8")
        
        block = read_equilibrium_definition(path=copy_path)
        assert DEFINITION_PLACEHOLDER not in block
        
        mock_http = make_launch_mock()
        orchestrator = load_real_orchestrator(
            mock_http=mock_http, dry_run=False, repo_root=repo_root,
        )
        stage1 = get_stage(orchestrator=orchestrator, name=STAGE1_NAME)
        result = orchestrator._run_stage(stage=stage1)
    
    assert result.status == "FINISHED"
    launch_requests = [call for call in mock_http.calls if call[0] == "POST"]
    assert len(launch_requests) == 1
    assert block in launch_requests[0][3]["prompt"]["text"]
    print("✓ test_documented_fill_steps_open_the_gate_for_the_committed_template passed")


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
