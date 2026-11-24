# Research Pipeline Design Document

**Project:** RMB_AI  
**Owner:** Yang Lu  
**Last Updated:** 2026-11-24  
**Status:** Draft - Ready for Review

---

## Overview

This document describes the design for a four-stage research pipeline that runs on the **Cursor Public Cloud Agents API**. A local Windows orchestrator script at `D:\RMB_AI` triggers separate cloud agents for each stage, with configurable model selection per stage and sequential artifact handoff through the Git repository.

### Design Goals

1. **Model Flexibility**: Each stage can use a different model from Cursor's available models
2. **Sequential Chaining**: Stages run in order, with validation gates between them
3. **Repository-Based Handoff**: Outputs committed to Git branches, inputs read from prior commits
4. **Resumable**: Failed pipelines can resume from the last successful stage
5. **Dry-Run Default**: No paid execution without explicit opt-in

### Historical Note

The original design specified Grok-only models with specific reasoning tiers (xhigh, low, high). **This constraint has been abandoned** in favor of wider model selection from whatever models are available through the Cursor API. The reasoning preferences below are desired characteristics, not API control parameters.

---

## Pipeline Stages

### Stage 1: Equilibrium Derivation

| Attribute | Value |
|-----------|-------|
| **Purpose** | Derive theoretical equilibrium conditions from economic models |
| **Input** | Reference papers (`reference/`), data context (`data/`) |
| **Output** | LaTeX derivations in `outputs/stage1_derivation/` |
| **Desired Reasoning** | **Highest** — mathematical proofs require maximum reasoning depth |
| **Model Choice** | Configurable; no specific model chosen yet |

### Stage 2: Equilibrium to Pseudocode

| Attribute | Value |
|-----------|-------|
| **Purpose** | Translate equilibrium conditions into algorithmic pseudocode |
| **Input** | Stage 1 outputs (`outputs/stage1_derivation/`) |
| **Output** | Pseudocode in `outputs/stage2_pseudocode/` |
| **Desired Reasoning** | **Highest** — accurate mathematical-to-algorithmic translation |
| **Model Choice** | Configurable; no specific model chosen yet |

### Stage 3: Python Implementation and Plots

| Attribute | Value |
|-----------|-------|
| **Purpose** | Implement pseudocode in Python, generate visualizations |
| **Input** | Stage 2 outputs (`outputs/stage2_pseudocode/`), data (`data/`) |
| **Output** | Python code and plots in `outputs/stage3_implementation/` |
| **Desired Reasoning** | **Lower** — implementation from clear pseudocode is more mechanical |
| **Model Choice** | Configurable; no specific model chosen yet |

### Stage 4: Academic Writing

| Attribute | Value |
|-----------|-------|
| **Purpose** | Polish research output for academic publication |
| **Input** | Stages 1 and 3 outputs |
| **Output** | Polished prose in `outputs/stage4_writing/` |
| **Desired Reasoning** | **Mid-to-High** — nuanced academic writing, not maximum depth |
| **Model Choice** | Configurable; no specific model chosen yet |

---

## Model Selection

### Desired Reasoning Preferences vs. Supported API Parameters

**Important:** The reasoning preferences above (highest, lower, mid-to-high) are *desired characteristics* for model selection, **not** specific API control parameters. The actual available parameters depend on the model chosen.

#### What the Cursor API Actually Supports

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

#### How to Discover Available Models and Parameters

```bash
curl https://api.cursor.com/v1/models -u YOUR_API_KEY:
```

Response includes:
- `id`: The model identifier to pass to `model.id`
- `parameters`: Per-model parameter definitions with allowed values
- `variants`: Pre-configured `id`+`params` combinations

**Example response structure:**
```json
{
  "items": [
    {
      "id": "composer-2",
      "displayName": "Composer 2",
      "parameters": [
        {
          "id": "fast",
          "values": [{"value": "false"}, {"value": "true"}]
        }
      ]
    },
    {
      "id": "claude-4.6-sonnet-thinking",
      "displayName": "Claude 4.6 Sonnet (Thinking)"
    }
  ]
}
```

#### Mapping Preferences to Model Choices

| Stage | Desired Reasoning | Suggested Approach |
|-------|-------------------|-------------------|
| Equilibrium Derivation | Highest | Choose a "thinking" model variant if available |
| Equilibrium to Pseudocode | Highest | Choose a "thinking" model variant if available |
| Python Implementation | Lower | Choose a faster variant (e.g., `fast: true`) or standard model |
| Academic Writing | Mid-to-High | Choose standard model without fast mode |

**No specific models are chosen yet.** The orchestrator validates selected models against `GET /v1/models` at runtime and refuses to launch if the model is unavailable.

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
   - Model IDs against GET /v1/models
   - API key present in CURSOR_API_KEY env var

3. For each stage (sequential):
   a. Check dependencies (prior stage completed)
   b. Validate input manifest (required files exist in repo)
   c. Launch cloud agent via POST /v1/agents
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

# 2. Commit any input changes
git add reference/ data/
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

1. **Model Validation**: Check `model_id` exists in `GET /v1/models` response
2. **Parameter Validation**: Check `model_params` are valid for the model
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
| Model not available | Stop before launch, suggest alternatives |
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

1. **Model Selection**: Which specific models should be used for each stage? The orchestrator supports any model from `GET /v1/models`, but no final choices have been made.

2. **Reasoning Parameters**: Some models may have parameters that control reasoning depth (e.g., Composer's `fast` parameter). Need to review available models and their parameters to map reasoning preferences.

3. **Cost Estimation**: Running 4 cloud agents per pipeline execution has cost implications. Need to establish budget controls.

4. **Parallelization**: Stages 1-3 are strictly sequential, but could Stage 4 (writing) run in parallel with Stage 3 (implementation)? Current design is fully sequential.

5. **Error Recovery**: If Stage 2 fails, should we allow manual fixes and resume, or require re-running from Stage 1?

6. **Output Review**: Should there be mandatory human review between stages, or is automated manifest validation sufficient?

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

---

## Next Steps

1. **Review this document** and provide feedback on the design
2. **Choose models** for each stage based on `GET /v1/models` output
3. **Test dry-run** to validate configuration
4. **Commit input data** to the repository
5. **Execute first pipeline run** with close monitoring
