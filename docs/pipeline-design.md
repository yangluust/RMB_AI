# Research Pipeline Design Document

**Project:** RMB_AI  
**Owner:** Yang Lu  
**Last Updated:** 2026-11-24  
**Status:** Draft - Ready for Review

---

## Overview

This document describes the design for a four-stage research pipeline that runs on the **Cursor Public Cloud Agents API**. A local Windows orchestrator script at `D:\RMB_AI` triggers separate cloud agents for each stage, with configurable model selection per stage and sequential artifact handoff through the Git repository.

### Design Goals

1. **Model Flexibility**: Each stage uses a specified model (validated against available models at runtime)
2. **Sequential Chaining**: Stages run in order, with validation gates between them
3. **Repository-Based Handoff**: Outputs committed to Git branches, inputs read from prior commits
4. **Resumable**: Failed pipelines can resume from the last successful stage
5. **Dry-Run Default**: No paid execution without explicit opt-in

### Historical Note

The original design specified Grok-only models with specific xAI reasoning tiers. This was superseded to allow wider model selection. The current design specifies requested model names and reasoning levels per stage, which must be mapped to actual API model IDs at runtime.

---

## Equilibrium Definition

> **⚠️ PENDING: Yang Lu to provide equilibrium definition**
>
> The equilibrium definition that Stage 1 should use for derivation has not yet been supplied. Yang Lu will specify the precise mathematical or economic definition of equilibrium that the agent should derive toward.
>
> This section will be updated once the definition is provided.

---

## Pipeline Stages

### Stage 1: Equilibrium Derivation

| Attribute | Value |
|-----------|-------|
| **Purpose** | Derive theoretical equilibrium conditions from economic model specification |
| **Input** | QMD (`.qmd`) or LaTeX (`.tex`) file specifying the economic model setup |
| **Input Location** | To be committed to repository (e.g., `inputs/model_setup.qmd` or `inputs/model_setup.tex`) |
| **Output** | LaTeX derivations in `outputs/stage1_derivation/` |
| **Equilibrium Definition** | ⚠️ **Pending** — Yang Lu to specify |
| **Requested Model** | **Claude Fable 5.2** |
| **Requested Reasoning** | **Second-highest** |

**Stage 1 Task:** The agent reads the QMD or LaTeX file containing the economic model setup, understands the model structure, and derives the equilibrium conditions according to the equilibrium definition (once provided by Yang Lu).

### Stage 2: Equilibrium to Pseudocode

| Attribute | Value |
|-----------|-------|
| **Purpose** | Translate equilibrium conditions into algorithmic pseudocode |
| **Input** | Stage 1 outputs (`outputs/stage1_derivation/`) |
| **Output** | Pseudocode in `outputs/stage2_pseudocode/` |
| **Requested Model** | **Claude Fable 5.2** |
| **Requested Reasoning** | **Second-highest** |

### Stage 3: Python Implementation and Plots

| Attribute | Value |
|-----------|-------|
| **Purpose** | Implement pseudocode in Python, generate visualizations |
| **Input** | Stage 2 outputs (`outputs/stage2_pseudocode/`), data (`data/`) |
| **Output** | Python code and plots in `outputs/stage3_implementation/` |
| **Requested Model** | **Grok** (version unspecified) |
| **Requested Reasoning** | Lower (implicit from task nature) |

#### Reproducibility Guardrails (Stage 3)

> **⚠️ PLACEHOLDER: Yang Lu to specify reproducibility guardrails**
>
> This section is reserved for reproducibility requirements that Yang Lu will define later. These may include:
> - Random seed handling
> - Version pinning for dependencies
> - Deterministic algorithm requirements
> - Output validation criteria
> - Numerical precision requirements
>
> **Content pending — do not invent specifications.**

### Stage 4: Academic Writing

| Attribute | Value |
|-----------|-------|
| **Purpose** | Polish research output for academic publication |
| **Input** | Stages 1 and 3 outputs |
| **Output** | Polished prose in `outputs/stage4_writing/` |
| **Requested Model** | **GPT-5.6** |
| **Requested Reasoning** | **High** |

---

## Model Selection

### Requested Models vs. Verified API Model IDs

**Critical Distinction:** The model names and reasoning levels in this document are **owner-requested specifications**, not verified API model identifiers. The actual `model.id` values that the Cursor API accepts must be discovered at runtime via `GET /v1/models`.

#### Summary of Requested Specifications

| Stage | Requested Model Name | Requested Reasoning Level | API Model ID | API Params |
|-------|---------------------|--------------------------|--------------|------------|
| 1. Equilibrium Derivation | Claude Fable 5.2 | Second-highest | **Unresolved** | **Unresolved** |
| 2. Equilibrium → Pseudocode | Claude Fable 5.2 | Second-highest | **Unresolved** | **Unresolved** |
| 3. Python Implementation | Grok (version unspecified) | Lower | **Unresolved** | **Unresolved** |
| 4. Academic Writing | GPT-5.6 | High | **Unresolved** | **Unresolved** |

#### What "Unresolved" Means

- **API Model ID**: The exact string to pass as `model.id` (e.g., `"claude-fable-5.2-high"` or similar) has not been verified against `GET /v1/models`. The orchestrator must query available models and find the matching ID.

- **API Params**: The mapping from "second-highest reasoning" to actual API parameters (if any) is unknown. Not all models expose reasoning-level parameters. The API may not support ordinal reasoning tiers like "second-highest."

- **No invented mappings**: This document does not fabricate model IDs or parameter values. Runtime resolution is required.

### What the Cursor API Actually Supports

According to [Cursor Cloud Agents API documentation](https://cursor.com/docs/cloud-agent/api/endpoints):

```
POST /v1/agents
{
  "model": {
    "id": "<model-id-from-GET-/v1/models>",
    "params": [
      { "id": "<param-id>", "value": "<param-value>" }
    ]
  }
}
```

- **`model.id`**: Must be a value returned by `GET /v1/models`
- **`model.params`**: Optional array of `{id, value}` pairs; valid params come from `GET /v1/models` for the selected model

### How to Discover Available Models and Parameters

```bash
curl https://api.cursor.com/v1/models -u YOUR_API_KEY:
```

Response includes:
- `id`: The model identifier to pass to `model.id`
- `parameters`: Per-model parameter definitions with allowed values
- `variants`: Pre-configured `id`+`params` combinations

**Before first execution:** Run the above command to discover actual model IDs and map them to the requested model names.

### Runtime Model Resolution

The orchestrator should:
1. Query `GET /v1/models` to get available models
2. Search for models matching the requested names (e.g., containing "fable", "grok", "gpt-5.6")
3. If multiple matches exist, prefer variants with reasoning-related parameters matching the requested level
4. If no match found, fail with a clear error listing available alternatives
5. Log the resolved `model.id` and `params` for each stage

---

## Architecture

### Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                     Local Windows Machine                        │
│                        D:\RMB_AI                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                   orchestrator.py                        │    │
│  │  - Reads config.yaml                                     │    │
│  │  - Validates models against GET /v1/models               │    │
│  │  - Launches cloud agents via POST /v1/agents             │    │
│  │  - Polls run status via GET /v1/agents/{id}/runs/{runId} │    │
│  │  - Validates output manifests                            │    │
│  │  - Collects artifacts via GET /v1/agents/{id}/artifacts  │    │
│  │  - Writes pipeline_manifest.json                         │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ HTTPS (api.cursor.com)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Cursor Cloud Agents API                        │
│                   https://api.cursor.com/v1                      │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │   Agent 1   │  │   Agent 2   │  │   Agent 3   │  ...         │
│  │  (Stage 1)  │  │  (Stage 2)  │  │  (Stage 3)  │              │
│  │ Claude Fable│  │ Claude Fable│  │    Grok     │              │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘              │
│         │                │                │                      │
│         ▼                ▼                ▼                      │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              GitHub Repository Snapshot                  │    │
│  │              github.com/yangluust/RMB_AI                 │    │
│  │  - Each agent clones committed code                      │    │
│  │  - Pushes outputs to cursor/* branches                   │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

### Execution Flow

```
1. User runs: python orchestrator.py --config config.yaml

2. Orchestrator validates:
   - Config file syntax
   - Model IDs against GET /v1/models (resolve requested names to actual IDs)
   - API key present in CURSOR_API_KEY env var

3. For each stage (sequential):
   a. Check dependencies (prior stage completed)
   b. Validate input manifest (required files exist in repo)
   c. Launch cloud agent via POST /v1/agents with resolved model ID
   d. Poll GET /v1/agents/{id}/runs/{runId} until terminal status
   e. If FINISHED:
      - Fetch pushed branch info from response
      - Validate output manifest (required files in branch)
      - Collect artifacts if configured
      - Record commit SHA in manifest
   f. If ERROR/CANCELLED/EXPIRED:
      - Log failure details
      - If fail_fast: stop pipeline
      - Otherwise: continue to next independent stage

4. Write pipeline_manifest.json with all commit SHAs and branch refs

5. Report summary to user
```

---

## Source Snapshot and Branch Semantics

### Critical Understanding

**Cloud agents execute against the committed repository snapshot on GitHub, not your local working directory.**

This means:
- Local uncommitted changes are **NOT visible** to cloud agents
- You must `git commit` and `git push` before the agent can see your changes
- Each agent starts from the `starting_ref` (branch or commit) you specify

### Input File Requirements for Stage 1

Stage 1 requires a QMD or LaTeX file specifying the economic model setup. This file must be:
1. Committed to the repository before pipeline execution
2. Located at a known path (configure in `config.yaml`)
3. Contain the full model specification that the agent will use for derivation

**Example workflow:**
```bash
# Create your model specification
# (file: inputs/model_setup.qmd or inputs/model_setup.tex)

# Commit the input file
git add inputs/model_setup.qmd
git commit -m "Add economic model specification for equilibrium derivation"
git push origin main

# Now run the pipeline
python orchestrator/orchestrator.py --dry-run
```

### Branch Creation

By default (`workOnCurrentBranch: false`), Cursor:
1. Creates a new branch named `cursor/<descriptive-name>-<hash>` from your `starting_ref`
2. The agent commits its outputs to this new branch
3. The branch name appears in the run response under `git.branches[]`

### Sequential Chaining via Git

```
main (starting_ref)
  │
  ├── cursor/stage1-derivation-a1b2
  │     └── outputs/stage1_derivation/*
  │
  ├── cursor/stage2-pseudocode-c3d4  (starts from stage1 branch)
  │     └── outputs/stage2_pseudocode/*
  │
  ├── cursor/stage3-impl-e5f6  (starts from stage2 branch)
  │     └── outputs/stage3_implementation/*
  │
  └── cursor/stage4-writing-g7h8  (starts from stage3 branch)
        └── outputs/stage4_writing/*
```

The orchestrator:
1. Launches Stage 1 from `main`
2. Waits for Stage 1 to complete, records its output branch
3. Launches Stage 2 with `starting_ref` set to Stage 1's output branch
4. Repeats for subsequent stages

### Safe Input Publishing Workflow

Before running the pipeline:

```bash
# 1. Ensure your inputs are committed
cd D:\RMB_AI
git status  # Check for uncommitted changes

# 2. Commit any input changes (including model specification)
git add inputs/ reference/ data/
git commit -m "Add input data for pipeline run"

# 3. Push to GitHub
git push origin main

# 4. Now run the orchestrator (dry-run first)
python orchestrator.py --config config.yaml --dry-run

# 5. If validation passes, run for real
python orchestrator.py --config config.yaml --execute
```

---

## Validation and Review Gates

### Pre-Launch Validation

Before launching each stage:

1. **Model Validation**: Check resolved `model.id` exists in `GET /v1/models` response
2. **Parameter Validation**: Check `model.params` are valid for the model
3. **Dependency Check**: Prior stage completed successfully
4. **Input Manifest**: Required input files exist in the starting ref

### Post-Run Validation

After each stage completes:

1. **Run Status**: Must be `FINISHED` (not `ERROR`, `CANCELLED`, `EXPIRED`)
2. **Output Manifest**: Required output files exist in the pushed branch
3. **Artifact Collection**: Download and verify any expected artifacts

### Manual Review Points

The orchestrator can pause for manual review:
- After each stage (configurable)
- Before launching expensive/high-reasoning stages
- When output manifest validation fails

---

## Failure Handling and Resume

### Failure Scenarios

| Scenario | Handling |
|----------|----------|
| API authentication failure | Stop immediately, report error |
| Requested model not found in API | Stop before launch, list available models |
| Run status ERROR | Log details, optionally continue to next stage |
| Run status CANCELLED | User cancelled; stop pipeline |
| Run status EXPIRED | Timeout; retry with same config |
| Output manifest missing files | Log warning, optionally proceed |
| Network timeout during polling | Retry with exponential backoff |

### Resume Capability

The pipeline manifest (`pipeline_manifest.json`) records:
- Each stage's input commit SHA
- Each stage's output branch and commit SHA
- Stage completion status

To resume a failed pipeline:

```bash
python orchestrator.py --config config.yaml --resume pipeline_manifest.json
```

The orchestrator:
1. Reads the manifest to find the last successful stage
2. Skips completed stages
3. Resumes from the first incomplete stage

---

## Setup Instructions (Windows)

### Prerequisites

1. **Python 3.10+** installed and in PATH
2. **Git** installed and configured
3. **Cursor API Key** from [Cursor Dashboard → API Keys](https://cursor.com/dashboard)

### Installation

```powershell
# Clone the repository (if not already)
cd D:\
git clone https://github.com/yangluust/RMB_AI.git
cd RMB_AI

# Install Python dependencies
pip install pyyaml requests

# Set API key (do NOT commit this)
$env:CURSOR_API_KEY = "your-api-key-here"

# Or add to your PowerShell profile for persistence
# Add-Content $PROFILE "`n`$env:CURSOR_API_KEY = 'your-api-key-here'"
```

### Running the Orchestrator

```powershell
cd D:\RMB_AI

# Validate config and models (no network calls to launch agents)
python orchestrator/orchestrator.py --dry-run

# Execute pipeline (will launch paid cloud agents)
python orchestrator/orchestrator.py --execute
```

### Optional: Task Scheduler Design

**Note: This describes a possible design. Do NOT install or schedule anything.**

For automated/scheduled runs, Windows Task Scheduler could:
1. Trigger `orchestrator.py` on a schedule or event
2. Pass `--execute` flag only when explicitly configured
3. Log output to a file for review
4. Send notifications on completion/failure

This would require:
- Secure storage for `CURSOR_API_KEY`
- Error handling for network failures
- Budget controls to prevent runaway costs

**This is NOT implemented and should NOT be set up without explicit approval.**

---

## API Limitations and Open Questions

### Known Limitations

| Limitation | Impact | Source |
|------------|--------|--------|
| No file upload endpoint | Inputs must be committed to repo | [API Docs](https://cursor.com/docs/cloud-agent/api/endpoints) |
| No webhooks in v1 (coming soon) | Must poll for status | [API Docs](https://cursor.com/docs/cloud-agent/api/endpoints) |
| Agent executes repo snapshot | Local changes invisible | [API Docs](https://cursor.com/docs/cloud-agent/api/endpoints) |
| One active run per agent | Cannot parallelize within agent | [API Docs](https://cursor.com/docs/cloud-agent/api/endpoints) |
| API in public beta | Endpoints may change | [API Docs](https://cursor.com/docs/cloud-agent/api/endpoints) |

### Open Questions

1. **Equilibrium Definition**: ⚠️ Yang Lu to provide the mathematical/economic equilibrium definition for Stage 1 derivation.

2. **Model ID Resolution**: The requested model names (Claude Fable 5.2, Grok, GPT-5.6) must be mapped to actual `model.id` values from `GET /v1/models`. This mapping is unresolved until runtime validation.

3. **Reasoning Level Parameters**: The API may not support ordinal reasoning levels like "second-highest." Need to discover what parameters each model actually exposes and how to map requested levels.

4. **Grok Version**: Stage 3 requests "Grok" without a specific version. Need to determine which Grok version is available and appropriate.

5. **Reproducibility Guardrails**: ⚠️ Yang Lu to specify Stage 3 reproducibility requirements.

6. **Cost Estimation**: Running 4 cloud agents per pipeline execution has cost implications. Need to establish budget controls.

7. **Error Recovery**: If Stage 2 fails, should we allow manual fixes and resume, or require re-running from Stage 1?

---

## Official Documentation References

| Resource | URL |
|----------|-----|
| Cloud Agents API (v1) | https://cursor.com/docs/cloud-agent/api/endpoints |
| API Overview & Authentication | https://cursor.com/docs/api |
| OpenAPI Specification | https://cursor.com/docs-static/cloud-agents-openapi.yaml |
| Legacy v0 API (webhooks) | https://cursor.com/docs/cloud-agent/api/v0 |

---

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-11-24 | Cloud Agent | Initial draft |
| 2026-11-24 | Cloud Agent | Updated with specific model requests: Claude Fable 5.2 (Stages 1-2), Grok (Stage 3), GPT-5.6 (Stage 4). Added Stage 1 input format (QMD/LaTeX). Added pending sections for equilibrium definition and reproducibility guardrails. |

---

## Next Steps

1. **Yang Lu: Provide equilibrium definition** for Stage 1 derivation task
2. **Yang Lu: Specify reproducibility guardrails** for Stage 3 Python implementation
3. **Resolve model IDs** by querying `GET /v1/models` and mapping requested names
4. **Commit input files** (economic model specification in QMD or LaTeX)
5. **Test dry-run** to validate configuration and model availability
6. **Execute first pipeline run** with close monitoring
