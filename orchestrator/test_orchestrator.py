#!/usr/bin/env python3
"""
Unit tests for the pipeline orchestrator.

All tests use mocked HTTP - no network calls are made.
Run with: python -m pytest test_orchestrator.py -v
Or simply: python test_orchestrator.py
"""

import json
import os
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import yaml

from api_client import CursorAPIClient, CursorAPIError, Model, RunStatus
from orchestrator import (
    Orchestrator,
    PipelineManifest,
    StageConfig,
    StageResult,
    load_config,
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


def run_all_tests():
    """Run all tests."""
    print("Running orchestrator tests (mocked HTTP, no network)...")
    print("-" * 50)
    
    test_run_status_helpers()
    test_api_client_list_models()
    test_api_client_validate_model_valid()
    test_api_client_validate_model_invalid()
    test_api_client_validate_params_valid()
    test_api_client_validate_params_invalid()
    test_api_client_create_agent()
    test_api_client_get_run()
    test_api_client_auth_error()
    test_orchestrator_validate_config_valid()
    test_orchestrator_validate_config_missing_repo()
    test_orchestrator_validate_config_bad_dependency()
    test_orchestrator_validate_models()
    test_orchestrator_validate_models_invalid()
    test_orchestrator_dry_run()
    test_pipeline_manifest_serialization()
    test_stage_input_ref_from_dependency()
    
    print("-" * 50)
    print("All 17 tests passed!")


if __name__ == "__main__":
    run_all_tests()
