#!/usr/bin/env python3
"""
Configuration validator for the research pipeline.

Validates that config.yaml conforms to xAI Grok API requirements
and pipeline structure constraints.
"""

import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Install with: pip install pyyaml")
    sys.exit(1)


SUPPORTED_REASONING_EFFORTS = {
    "grok-4.7": ["low", "medium", "high", "xhigh"],
    "grok-4.6": ["low", "medium", "high", "xhigh"],
    "grok-4.5": ["low", "medium", "high"],
}

REQUIRED_BOT_FIELDS = ["name", "model", "reasoning", "system_prompt"]


class ValidationError(Exception):
    pass


def load_config(config_path: Path) -> dict:
    """Load and parse the YAML configuration file."""
    if not config_path.exists():
        raise ValidationError(f"Config file not found: {config_path}")
    
    with open(config_path, "r") as f:
        try:
            return yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValidationError(f"Invalid YAML syntax: {e}")


def validate_bot(bot_id: str, bot_config: dict, errors: list) -> None:
    """Validate a single bot configuration."""
    for field in REQUIRED_BOT_FIELDS:
        if field not in bot_config:
            errors.append(f"Bot '{bot_id}': missing required field '{field}'")
    
    if "model" not in bot_config or "reasoning" not in bot_config:
        return
    
    model = bot_config["model"]
    reasoning = bot_config.get("reasoning", {})
    effort = reasoning.get("effort")
    
    if model not in SUPPORTED_REASONING_EFFORTS:
        errors.append(
            f"Bot '{bot_id}': model '{model}' not in known models. "
            f"Known: {list(SUPPORTED_REASONING_EFFORTS.keys())}"
        )
        return
    
    allowed_efforts = SUPPORTED_REASONING_EFFORTS[model]
    if effort not in allowed_efforts:
        errors.append(
            f"Bot '{bot_id}': reasoning effort '{effort}' not supported by {model}. "
            f"Allowed: {allowed_efforts}"
        )


def validate_dependencies(config: dict, errors: list) -> None:
    """Validate that bot dependencies form a valid DAG."""
    bots = config.get("bots", {})
    bot_ids = set(bots.keys())
    
    execution = config.get("execution", {})
    stages = execution.get("stages", [])
    
    seen_bots = set()
    for stage in stages:
        stage_num = stage.get("stage")
        stage_bots = stage.get("bots", [])
        depends_on = stage.get("depends_on", [])
        
        for bot_id in stage_bots:
            if bot_id not in bot_ids:
                errors.append(
                    f"Stage {stage_num}: references unknown bot '{bot_id}'"
                )
        
        for dep in depends_on:
            if dep not in seen_bots:
                errors.append(
                    f"Stage {stage_num}: dependency '{dep}' not defined in earlier stage"
                )
        
        seen_bots.update(stage_bots)


def validate_api_config(config: dict, errors: list, warnings: list) -> None:
    """Validate API configuration."""
    validation = config.get("validation", {})
    api_key_env = validation.get("api_key_env_var", "XAI_API_KEY")
    
    if validation.get("require_api_key_env", False):
        if api_key_env not in os.environ:
            warnings.append(
                f"Environment variable '{api_key_env}' is not set. "
                f"Required for runtime execution."
            )


def validate_config(config_path: Path) -> tuple[list, list]:
    """
    Validate the configuration file.
    
    Returns:
        Tuple of (errors, warnings)
    """
    errors = []
    warnings = []
    
    config = load_config(config_path)
    
    if "pipeline" not in config:
        errors.append("Missing required 'pipeline' section")
    
    if "bots" not in config:
        errors.append("Missing required 'bots' section")
    else:
        for bot_id, bot_config in config["bots"].items():
            validate_bot(bot_id, bot_config, errors)
    
    if "execution" in config:
        validate_dependencies(config, errors)
    
    validate_api_config(config, errors, warnings)
    
    return errors, warnings


def main():
    """Run validation and report results."""
    script_dir = Path(__file__).parent
    config_path = script_dir / "config.yaml"
    
    print(f"Validating: {config_path}")
    print("-" * 50)
    
    try:
        errors, warnings = validate_config(config_path)
    except ValidationError as e:
        print(f"FATAL: {e}")
        sys.exit(1)
    
    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(f"  ⚠ {w}")
    
    if errors:
        print("\nErrors:")
        for e in errors:
            print(f"  ✗ {e}")
        print(f"\nValidation FAILED with {len(errors)} error(s)")
        sys.exit(1)
    
    print("\n✓ Configuration is valid")
    
    config = load_config(config_path)
    bots = config.get("bots", {})
    print(f"\nBot Summary ({len(bots)} bots configured):")
    print("-" * 50)
    for bot_id, bot_config in bots.items():
        model = bot_config.get("model", "unknown")
        effort = bot_config.get("reasoning", {}).get("effort", "default")
        name = bot_config.get("name", bot_id)
        print(f"  {name}")
        print(f"    Model: {model}, Reasoning: {effort}")
    
    sys.exit(0)


if __name__ == "__main__":
    main()
