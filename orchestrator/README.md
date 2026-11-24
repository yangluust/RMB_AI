# Research Pipeline Orchestrator

Local Windows orchestrator for running a multi-stage research pipeline on Cursor Cloud Agents.

## Overview

This orchestrator:
- Calls the **Cursor Public Cloud Agents API** to run each pipeline stage
- Supports **configurable model selection** per stage from available models
- Validates models against `GET /v1/models` before launch
- Chains stages via **Git branch artifacts**
- Defaults to **dry-run mode** (no paid execution without explicit opt-in)

## Quick Start (Windows)

```powershell
# 1. Set your API key
$env:CURSOR_API_KEY = "your-api-key-from-cursor-dashboard"

# 2. Navigate to repository
cd D:\RMB_AI

# 3. Validate configuration (dry run, no network calls to launch agents)
python orchestrator/orchestrator.py --dry-run

# 4. Execute pipeline (launches paid cloud agents)
python orchestrator/orchestrator.py --execute
```

## Files

| File | Description |
|------|-------------|
| `config.yaml` | Pipeline and stage configuration |
| `orchestrator.py` | Main orchestrator script |
| `api_client.py` | Cursor Cloud Agents API client |
| `test_orchestrator.py` | Unit tests (mocked HTTP, no network) |
| `README.md` | This file |

## Configuration

Edit `config.yaml` to customize:

```yaml
stages:
  - name: "my_stage"
    display_name: "My Stage"
    model_id: "composer-2"      # From GET /v1/models, or null for default
    model_params:               # Optional, model-specific params
      - id: "fast"
        value: "true"
    prompt: |
      Your task prompt here...
    guardrails:                 # Optional, appended to the prompt at launch
      - "A rule the agent must follow."
    equilibrium_definition_file: "docs/pipeline-design.md"  # Optional, see below
    output_manifest:
      required_files:
        - "outputs/my_stage/result.txt"
```

### Guardrails

A stage may list `guardrails`. When the orchestrator launches that stage it appends the list, numbered, to the stage prompt, so the agent receives the guardrails in the launch request.

Stage 3 (`pseudocode_to_implementation`) carries the twelve reproducibility guardrails from [`docs/pipeline-design.md`](../docs/pipeline-design.md#reproducibility-guardrails-stage-3). The tests fail if the list in `config.yaml` differs from the list in the design document, so edit both together.

### Equilibrium Definition (Stage 1)

Stage 1 (`equilibrium_derivation`) sets `equilibrium_definition_file: "docs/pipeline-design.md"`. At launch the orchestrator reads the definition block from that file in the local checkout and appends it to the Stage 1 prompt.

**The definition has not been supplied yet, so the pipeline cannot run.** The block is an unfilled template, and until Yang Lu fills it in, `--dry-run`, `--execute` and `--resume` all stop with an error like:

```
ERROR: Stage 'equilibrium_derivation' cannot launch: Equilibrium definition in ...\docs\pipeline-design.md is not supplied: 7 'TODO(Yang)' placeholder(s) remain; the status line is not '**Status:** FILLED'
```

To supply it, follow [How to Fill It In](../docs/pipeline-design.md#how-to-fill-it-in) in the design document. The orchestrator only checks that the template is filled in; it cannot judge whether the definition is correct.

### Model Selection

Models are validated at runtime against `GET /v1/models`. To see available models:

```bash
curl https://api.cursor.com/v1/models -u YOUR_API_KEY:
```

Set `model_id: null` to use your configured default model.

## Commands

| Command | Description |
|---------|-------------|
| `--dry-run` | Validate config and models, no agent launch |
| `--execute` | Run the pipeline (launches paid agents) |
| `--resume manifest.json` | Resume from a previous checkpoint |
| `--config path/to/config.yaml` | Use custom config file |
| `--output path/to/manifest.json` | Custom output manifest path |

## Important: Repository Snapshots

**Cloud agents execute against committed code on GitHub, not your local working directory.**

Before running:
1. Commit your input data: `git add . && git commit -m "Add inputs"`
2. Push to GitHub: `git push origin main`
3. Then run the orchestrator

Local uncommitted changes are **invisible** to cloud agents.

## Tests

Run unit tests (mocked HTTP, no network calls):

```powershell
python orchestrator/test_orchestrator.py
```

Or with pytest:

```powershell
python -m pytest orchestrator/test_orchestrator.py -v
```

## Official Documentation

- [Cloud Agents API](https://cursor.com/docs/cloud-agent/api/endpoints)
- [API Authentication](https://cursor.com/docs/api)
- [OpenAPI Spec](https://cursor.com/docs-static/cloud-agents-openapi.yaml)

## Design Document

See [`docs/pipeline-design.md`](../docs/pipeline-design.md) for the full design specification.
