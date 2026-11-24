# Research Pipeline Orchestrator

Local Windows orchestrator for running a multi-stage research pipeline on Cursor Cloud Agents.

## Overview

This orchestrator:
- Calls the **Cursor Public Cloud Agents API** to run each pipeline stage
- Supports **configurable model selection** per stage from available models
- Validates models against `GET /v1/models` before launch
- Chains stages via **Git branch artifacts**
- **Asks for the model setup / equilibrium definition document** (`.qmd` or `.tex`) at every new project, before anything is launched
- Launches agents only with `--execute` or `--resume`

## Quick Start (Windows)

```powershell
# 1. Set your API key
$env:CURSOR_API_KEY = "your-api-key-from-cursor-dashboard"

# 2. Navigate to repository
cd D:\RMB_AI

# 3. Validate (asks for the model setup document; launches nothing)
python orchestrator/orchestrator.py --dry-run

# 4. Start a new project (asks for the model setup document, then launches paid cloud agents)
python orchestrator/orchestrator.py --execute
```

Steps 3 and 4 each ask:

```
Location of the model setup / equilibrium definition document for this project (.qmd or .tex):
```

Type the path of your file, for example `D:\Research\my_model\setup.qmd`.

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
    receives_model_setup_document: true  # Required on the first stage, see below
    output_manifest:
      required_files:
        - "outputs/my_stage/result.txt"
```

### Guardrails

A stage may list `guardrails`. When the orchestrator launches that stage it appends the list, numbered, to the stage prompt, so the agent receives the guardrails in the launch request.

Stage 3 (`pseudocode_to_implementation`) carries the twelve reproducibility guardrails from [`docs/pipeline-design.md`](../docs/pipeline-design.md#reproducibility-guardrails-stage-3). The tests fail if the list in `config.yaml` differs from the list in the design document, so edit both together.

### Model Setup Document (Asked at Every New Project)

Every new project starts by asking for the location of the model setup / equilibrium definition document. This is mandatory: there is no setting, flag or environment variable that supplies the location, and an earlier project's document is never reused.

- The file must exist, be a `.qmd` or `.tex` file, hold non-empty UTF-8 text, and be self-contained (no `\input`, `\include` or Quarto `{{< include >}}` of other files).
- If the answer is blank or unusable, or no answer can be read (for example under a scheduler), the run stops with an error and nothing is launched.
- The file is only read. It is not changed, moved, copied or committed.
- The first stage must set `receives_model_setup_document: true`. At launch the orchestrator appends the document's full text to that stage's prompt. The cloud agent cannot open a path on your machine, so the text is sent and the path is not.
- The project manifest records the file's path, size and SHA-256 hash (not its text). `--resume` re-reads the same file without asking and stops if it is missing or has changed.
- `--execute` refuses to start if the manifest file already exists. Use `--resume` to continue that project, or `--output other_manifest.json` to start another one.

The orchestrator cannot judge whether the document states a correct equilibrium definition. See [Project Input](../docs/pipeline-design.md#project-input-model-setup-and-equilibrium-definition-document) in the design document for the details and limits.

### Model Selection

Each stage sets `model_id` and `model_params`. The orchestrator sends them as the `model` field of the launch request, `{"id": ..., "params": [{"id": ..., "value": ...}]}`.

| Stage | `model_id` | `model_params` |
|-------|------------|----------------|
| 1. Equilibrium Derivation | `claude-fable-5-2` | `effort` = `xhigh` |
| 2. Equilibrium to Pseudocode | `claude-fable-5-2` | `effort` = `xhigh` |
| 3. Python Implementation | `grok-4.7` | `reasoning_effort` = `low` |
| 4. Academic Writing | `gpt-5.6-sol` | `reasoning` = `high` |

These values come from tool smoke tests, not from the public REST API. They have **not** been checked against the public `GET /v1/models` listing. Before any launch the orchestrator calls `GET /v1/models` and stops if a model ID, parameter or value is not listed, showing what the API lists instead. It never substitutes another model or drops a parameter. See [Model Selection](../docs/pipeline-design.md#model-selection) in the design document.

To see the listing yourself:

```bash
curl https://api.cursor.com/v1/models -u YOUR_API_KEY:
```

Write `model_params` values as quoted strings. A stage with `model_params` must also set `model_id`. Setting `model_id: null` with no `model_params` uses your configured default model.

## Commands

| Command | Description |
|---------|-------------|
| `--dry-run` | Ask for the model setup document, validate config and models; no agent launch, nothing saved |
| `--execute` | Start a new project: ask for the model setup document, then run the pipeline (launches paid agents) |
| `--resume manifest.json` | Continue an existing project; does not ask, uses the document recorded in the manifest |
| `--config path/to/config.yaml` | Use custom config file |
| `--output path/to/manifest.json` | Manifest file for the project (default `pipeline_manifest.json`; for `--resume`, the manifest being resumed) |

## Important: Repository Snapshots

**Cloud agents execute against committed code on GitHub, not your local working directory.**

The model setup document is the exception: the orchestrator reads it from your machine and sends its text in the Stage 1 launch request, so it does not have to be committed.

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
