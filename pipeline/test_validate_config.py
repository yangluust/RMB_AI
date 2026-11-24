#!/usr/bin/env python3
"""
Tests for the configuration validator.

Run with: python3 -m pytest pipeline/test_validate_config.py -v
Or simply: python3 pipeline/test_validate_config.py
"""

import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Install with: pip install pyyaml")
    exit(1)

from validate_config import (
    validate_bot,
    validate_dependencies,
    SUPPORTED_REASONING_EFFORTS,
)


def test_valid_bot_config():
    """Test that a valid bot configuration passes validation."""
    errors = []
    bot_config = {
        "name": "Test Bot",
        "model": "grok-4.7",
        "reasoning": {"effort": "xhigh"},
        "system_prompt": "You are a helpful assistant.",
    }
    validate_bot("test_bot", bot_config, errors)
    assert len(errors) == 0, f"Unexpected errors: {errors}"
    print("✓ test_valid_bot_config passed")


def test_invalid_reasoning_effort_for_model():
    """Test that xhigh on grok-4.5 is rejected."""
    errors = []
    bot_config = {
        "name": "Test Bot",
        "model": "grok-4.5",
        "reasoning": {"effort": "xhigh"},
        "system_prompt": "You are a helpful assistant.",
    }
    validate_bot("test_bot", bot_config, errors)
    assert len(errors) == 1, f"Expected 1 error, got {len(errors)}: {errors}"
    assert "xhigh" in errors[0] and "grok-4.5" in errors[0]
    print("✓ test_invalid_reasoning_effort_for_model passed")


def test_missing_required_field():
    """Test that missing required fields are caught."""
    errors = []
    bot_config = {
        "name": "Test Bot",
        "model": "grok-4.7",
    }
    validate_bot("test_bot", bot_config, errors)
    assert len(errors) >= 1, f"Expected errors for missing fields, got: {errors}"
    print("✓ test_missing_required_field passed")


def test_valid_reasoning_efforts_per_model():
    """Test that all documented reasoning efforts are allowed for each model."""
    for model, efforts in SUPPORTED_REASONING_EFFORTS.items():
        for effort in efforts:
            errors = []
            bot_config = {
                "name": "Test Bot",
                "model": model,
                "reasoning": {"effort": effort},
                "system_prompt": "Test prompt",
            }
            validate_bot("test_bot", bot_config, errors)
            assert len(errors) == 0, (
                f"Effort '{effort}' should be valid for {model}: {errors}"
            )
    print("✓ test_valid_reasoning_efforts_per_model passed")


def test_unknown_model_warning():
    """Test that unknown models are flagged."""
    errors = []
    bot_config = {
        "name": "Test Bot",
        "model": "grok-999",
        "reasoning": {"effort": "high"},
        "system_prompt": "Test prompt",
    }
    validate_bot("test_bot", bot_config, errors)
    assert len(errors) == 1
    assert "grok-999" in errors[0]
    print("✓ test_unknown_model_warning passed")


def test_dependency_validation():
    """Test that stage dependencies are validated correctly."""
    errors = []
    config = {
        "bots": {
            "bot_a": {},
            "bot_b": {},
        },
        "execution": {
            "stages": [
                {"stage": 1, "bots": ["bot_a"]},
                {"stage": 2, "bots": ["bot_b"], "depends_on": ["bot_a"]},
            ]
        }
    }
    validate_dependencies(config, errors)
    assert len(errors) == 0, f"Valid dependencies should pass: {errors}"
    print("✓ test_dependency_validation passed")


def test_invalid_dependency():
    """Test that invalid dependencies are caught."""
    errors = []
    config = {
        "bots": {
            "bot_a": {},
            "bot_b": {},
        },
        "execution": {
            "stages": [
                {"stage": 1, "bots": ["bot_a"]},
                {"stage": 2, "bots": ["bot_b"], "depends_on": ["nonexistent_bot"]},
            ]
        }
    }
    validate_dependencies(config, errors)
    assert len(errors) == 1
    assert "nonexistent_bot" in errors[0]
    print("✓ test_invalid_dependency passed")


def run_all_tests():
    """Run all tests."""
    print("Running validation tests...")
    print("-" * 50)
    
    test_valid_bot_config()
    test_invalid_reasoning_effort_for_model()
    test_missing_required_field()
    test_valid_reasoning_efforts_per_model()
    test_unknown_model_warning()
    test_dependency_validation()
    test_invalid_dependency()
    
    print("-" * 50)
    print("All tests passed!")


if __name__ == "__main__":
    run_all_tests()
