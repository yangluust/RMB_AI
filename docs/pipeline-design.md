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

**Not supplied yet.** Yang Lu has not provided the equilibrium definition. The template below is unfilled: every field holds a placeholder for Yang to replace, and nothing in it describes the model or states a definition.

Stage 1 derives the equilibrium according to this definition. The orchestrator reads the template block from this file in the local checkout and appends it to the Stage 1 prompt at launch. While the block is unfilled the orchestrator refuses to launch Stage 1: `--dry-run`, `--execute` and `--resume` all stop with an error that names this file.

### How to Fill It In

1. Replace every `TODO(Yang)` placeholder in the template with your own text. Formulas can be written as `$$ ... $$`.
2. Delete any field that does not apply and add fields you need. The field list is only a suggested layout, not a statement about the model.
3. Change the status line from `UNFILLED` to `FILLED`.
4. Commit and push this file, so the repository records the definition that Stage 1 used.

Keep the two hidden `EQUILIBRIUM_DEFINITION` comment lines around the template; the orchestrator uses them to find the block.

The orchestrator treats the definition as supplied only when the status line reads `**Status:** FILLED`, no `TODO(Yang)` placeholder remains, and the block has content besides the status line. It cannot judge whether that content is a correct or complete definition; that stays with Yang.

**Limit:** the orchestrator checks the copy of this file in the local checkout. It does not yet check that the same text is in the repository snapshot the Stage 1 agent runs on, which is why step 4 matters.

### Definition Template

<!-- EQUILIBRIUM_DEFINITION:BEGIN -->
**Status:** UNFILLED

**1. Equilibrium concept.** The name of the concept and, if there is one, the source it follows.

> TODO(Yang)

**2. Objects.** The objects that together make up an equilibrium.

> TODO(Yang)

**3. Conditions.** The conditions those objects must satisfy, one per line.

> TODO(Yang)

**4. Taken as given.** What the definition takes as given from the model setup file.

> TODO(Yang)

**5. Notation.** How the symbols used above map to the symbols in the model setup file.

> TODO(Yang)

**6. Restrictions.** Any restriction on which equilibria Stage 1 should consider.

> TODO(Yang)

**7. Required result.** What the Stage 1 derivation must deliver for this definition.

> TODO(Yang)

<!-- EQUILIBRIUM_DEFINITION:END -->

---

## Pipeline Stages

### Stage 1: Equilibrium Derivation

| Attribute | Value |
|-----------|-------|
| **Purpose** | Derive theoretical equilibrium conditions from economic model specification |
| **Input** | QMD (`.qmd`) or LaTeX (`.tex`) file specifying the economic model setup |
| **Input Location** | To be committed to repository (e.g., `inputs/model_setup.qmd` or `inputs/model_setup.tex`) |
| **Output** | LaTeX derivations in `outputs/stage1_derivation/` |
| **Equilibrium Definition** | Not supplied yet — unfilled template in [Equilibrium Definition](#equilibrium-definition) |
| **Requested Model** | **Claude Fable 5.2** |
| **Requested Reasoning** | **Second-highest** |

**Stage 1 Task:** The agent reads the QMD or LaTeX file containing the economic model setup, understands the model structure, and derives the equilibrium conditions according to the definition in [Equilibrium Definition](#equilibrium-definition). That definition is not supplied yet, and Stage 1 cannot launch until Yang Lu fills in the template there.

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

Principles supplied by Yang Lu for the Stage 3 Python implementation:

1. Reusable code without duplication.
2. Configuration-driven parameters, saved with the results.
3. Stable semantic paths, without timestamps or version suffixes.
4. Named function arguments.
5. Version-controlled code, configs, and documentation.
6. Declared dependency versions.
7. Explicit random seeds.
8. External specification kept in sync with clear code.
9. Property-based, unit, and integration tests.
10. Programmatically generated outputs, never manually edited.
11. Raw inputs left unchanged.
12. Secrets and large generated files kept outside Git.

**Runtime:** The same twelve principles are listed under `guardrails` for this stage in `orchestrator/config.yaml`. When the orchestrator launches Stage 3 it appends them, numbered, to the stage prompt, so the agent receives them in the launch request. `orchestrator/test_orchestrator.py` fails if the list in the config differs from the list above, so edit both together. This has been checked with mocked HTTP only; no Stage 3 agent has been launched.

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
5. **Equilibrium Definition**: Stage 1 does not launch while the template in [Equilibrium Definition](#equilibrium-definition) is unfilled

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

1. **Equilibrium Definition**: Not supplied yet. Yang Lu fills in the template in [Equilibrium Definition](#equilibrium-definition); Stage 1 cannot launch until then.

2. **Model Setup File Path**: Stage 1 reads a QMD or LaTeX model setup file, but no path has been supplied and the Stage 1 prompt in `orchestrator/config.yaml` does not name one.

3. **Model ID Resolution**: The requested model names (Claude Fable 5.2, Grok, GPT-5.6) must be mapped to actual `model.id` values from `GET /v1/models`. This mapping is unresolved until runtime validation.

4. **Reasoning Level Parameters**: The API may not support ordinal reasoning levels like "second-highest." Need to discover what parameters each model actually exposes and how to map requested levels.

5. **Grok Version**: Stage 3 requests "Grok" without a specific version. Need to determine which Grok version is available and appropriate.

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
| 2026-11-24 | Cloud Agent | Replaced the Stage 3 reproducibility guardrails placeholder with the twelve principles supplied by Yang Lu. Equilibrium definition remains pending. |
| 2026-11-24 | Cloud Agent | The twelve Stage 3 guardrails are now in `orchestrator/config.yaml` and appended to the Stage 3 prompt at launch. |
| 2026-11-24 | Cloud Agent | Replaced the pending equilibrium-definition notes with an unfilled template for Yang Lu. The orchestrator refuses to launch Stage 1 while the template is unfilled. |

---

## Next Steps

1. **Yang Lu: Fill in the equilibrium definition template** in [Equilibrium Definition](#equilibrium-definition)
2. **Resolve model IDs** by querying `GET /v1/models` and mapping requested names
3. **Commit input files** (economic model specification in QMD or LaTeX)
4. **Test dry-run** to validate configuration and model availability
5. **Execute first pipeline run** with close monitoring
