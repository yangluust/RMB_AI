# Research Pipeline Configuration

Configuration files for the Yang Lu research pipeline using xAI Grok models.

## Overview

This pipeline consists of four Grok-based bots for equilibrium analysis research:

| Bot | Purpose | Model | Reasoning Effort |
|-----|---------|-------|------------------|
| Equilibrium Derivation | Derive theoretical equilibrium conditions | grok-4.7 | xhigh |
| Equilibrium to Pseudocode | Translate conditions to algorithms | grok-4.7 | xhigh |
| Pseudocode to Implementation | Implement in Python with plots | grok-4.7 | low |
| Academic Writing | Polish for publication | grok-4.7 | high |

## xAI Grok API Reference

### Supported Models and Reasoning Efforts

Based on [xAI documentation](https://docs.x.ai/developers/model-capabilities/text/reasoning) (October 2026):

| Model | Supported Reasoning Efforts | Notes |
|-------|----------------------------|-------|
| `grok-4.7` | `low`, `medium`, `high` (default), `xhigh` | Current flagship |
| `grok-4.6` | `low`, `medium`, `high` (default), `xhigh` | Stable alternative |
| `grok-4.5` | `low`, `medium`, `high` (default) | No `xhigh` support |

### Reasoning Effort Descriptions

| Effort | Description | Best For |
|--------|-------------|----------|
| `low` | Fast, minimal reasoning tokens | Latency-sensitive tasks, simple tool calling |
| `medium` | Balanced thinking | Complex data analysis, long-context reasoning |
| `high` | Deep thinking (default) | Challenging problems, complex math, multi-step logic |
| `xhigh` | Maximum reasoning depth | Hardest problems where quality > latency |

### API Request Format

```json
{
  "model": "grok-4.7",
  "reasoning": {"effort": "xhigh"},
  "input": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ]
}
```

## Bot Assignments vs Requested Intent

| Requested Intent | Assigned Effort | API Support | Notes |
|-----------------|-----------------|-------------|-------|
| "highest reasoning" (derivation) | `xhigh` | ✅ Supported | grok-4.6+ only |
| "highest" (pseudocode) | `xhigh` | ✅ Supported | grok-4.6+ only |
| "lower" (implementation) | `low` | ✅ Supported | All models |
| "mid-to-high" (writing) | `high` | ✅ Supported | All models |

## Configuration Files

- `config.yaml` - Main pipeline configuration with bot definitions
- `validate_config.py` - Configuration validation script

## Prerequisites

### Environment Variables

```bash
export XAI_API_KEY="your-api-key-here"
```

**Security**: Never commit API keys to the repository.

### Python Dependencies (for validation)

```bash
pip install pyyaml
```

## Validation

Run the configuration validator:

```bash
python pipeline/validate_config.py
```

The validator checks:
- Required fields are present
- Reasoning efforts are valid for specified models
- Bot dependencies form a valid DAG
- API key environment variable is defined (optional check)

## Pipeline Execution Order

```
Stage 1: equilibrium_derivation
    ↓
Stage 2: equilibrium_to_pseudocode
    ↓
Stage 3: pseudocode_to_implementation
    ↓
Stage 4: academic_writing (depends on stages 1 and 3)
```

## Model Selection Notes

The default model is `grok-4.7`, the current xAI flagship. Consider:

- **grok-4.7**: Latest features, 500k context, best performance
- **grok-4.6**: Stable alternative if 4.7 has regressions
- **grok-4.5**: Use only if `xhigh` reasoning is not needed

If changing models, update the `model` field in each bot configuration and ensure the reasoning effort is supported.

## Unresolved Items

1. **Model Version Lock**: The config uses `grok-4.7` which may be updated by xAI. For production stability, consider pinning to a specific version when available (e.g., `grok-4.7-20260921`).

2. **Runtime Integration**: This configuration defines bot parameters but does not include execution framework. Integration with a runtime (e.g., LangChain, custom orchestrator) is required.

3. **Cost Estimation**: xhigh reasoning uses significantly more tokens. Monitor usage carefully.

## Limitations

- Configuration only - no execution framework included
- Credentials must be managed externally
- Dataset paths are relative references, not validated

## References

- [xAI Reasoning Documentation](https://docs.x.ai/developers/model-capabilities/text/reasoning)
- [Grok 4.7 Developer Guide](https://docs.x.ai/developers/grok-4-7)
- [xAI Models & Pricing](https://docs.x.ai/developers/models)
