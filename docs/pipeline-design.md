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

The original design specified Grok-only models with specific xAI reasoning tiers. This was superseded to allow wider model selection. The current design sets a model and a reasoning level for each stage; see [Model Selection](#model-selection).

---

## Project Input: Model Setup and Equilibrium Definition Document

Each project starts from one document written by Yang Lu: a Quarto Markdown (`.qmd`) or LaTeX (`.tex`) file that states the economic model setup and the equilibrium definition. Stage 1 derives the equilibrium from what that document states. This design document states no equilibrium definition, and the orchestrator never supplies one.

### The Orchestrator Asks for the Document

Starting a new project with `--execute` always asks this question first:

```
Location of the model setup / equilibrium definition document for this project (.qmd or .tex):
```

- The question comes before any request is sent to Cursor. No stage is launched until a usable document has been given.
- It is asked for every new project. The location is never taken from `config.yaml`, an environment variable or an earlier project.
- `--dry-run` asks the same question and checks the document, then saves nothing. The `--execute` that follows asks again.
- `--resume` does not ask. It continues an existing project with the document recorded for that project.

The answer is the path of the file on the local machine, for example `D:\Research\my_model\setup.qmd`. Surrounding quotes are accepted. The start stops with an error, and nothing is launched, in each of these cases:

| Case | The error says |
|------|----------------|
| No answer can be read (input is closed, as under a scheduler or a pipe) | input ended before an answer was given |
| The answer is blank | no location was given |
| The path does not exist | the document does not exist |
| The path is a folder | the location is not a file |
| The extension is not `.qmd` or `.tex` | the document must be a `.qmd` or `.tex` file |
| The file is empty or holds only whitespace | the document is empty |
| The file is not UTF-8 text | the document is not UTF-8 text |
| The file pulls in other files (`\input`, `\include`, `\subfile`, `\import`, `\subimport`, or Quarto `{{< include ... >}}`) | the document pulls in other files |

There is one attempt per run. After an error, run the command again.

### How the Document Reaches the Stage 1 Agent

The cloud agent cannot open a path on the local machine, and `POST /v1/agents` has no field for uploading an input file. The orchestrator therefore reads the file and puts its full text into `prompt.text` of the Stage 1 launch request. The text comes after the task, between a `----- BEGIN DOCUMENT -----` line and an `----- END DOCUMENT -----` line, together with the file name, format and SHA-256 hash. The local path is not sent.

The orchestrator only reads the file. It does not change, move, copy or commit it, and it runs no Git command.

Limits of this handoff:

- **One file only.** Only the text of the selected file is sent. That is why a file that pulls in other files is refused; write that text into the file itself. Figures and bibliography files that the document refers to are not sent.
- **The text goes to Cursor.** It is part of the agent's prompt and is visible in that agent's conversation. Do not put secrets in the document.
- **Not recorded in Git by the orchestrator.** The repository holds the document only if Yang commits it himself. The project manifest records which file was used (see below).
- **Size.** The API documentation states no size limit for `prompt.text`, and the orchestrator sets none. A very large document may be rejected by the API; this has not been tested.
- **Content is not judged.** The orchestrator checks that the file is a readable, non-empty, self-contained `.qmd` or `.tex` file. It cannot tell whether the file states an equilibrium definition, or a correct one. The Stage 1 prompt tells the agent to stop and report what is missing if the document does not state enough, instead of filling the gap itself.
- **Mocked tests only.** This has been checked against mocked HTTP. No Stage 1 agent has been launched.

### What the Project Records

A project is one `--execute` run and its manifest file (`pipeline_manifest.json` unless `--output` names another file). The manifest is written before the first agent is launched. Under `model_setup_document` it records the file's full local path, format, size in bytes, SHA-256 hash and the time it was selected. The document's text is not stored in the manifest.

`--execute` refuses to start when the manifest file already exists, because a new project would overwrite the earlier project's record. Use `--resume` to continue that project, or `--output` to give the new project its own manifest file.

`--resume` reads the document again from the recorded path, without asking, and only if a stage that receives the document still has to run. It stops before launching anything if the manifest records no document, if the file is gone, or if its content no longer matches the recorded hash. To use a changed document, start a new project.

### Optional Outline for the Document

Yang decides what the document contains and how it is laid out. The list below is only a reminder of points a derivation usually needs. The orchestrator does not read this list or check the document against it, and nothing in it describes the model.

1. Equilibrium concept: its name and, if there is one, the source it follows.
2. Objects: the objects that together make up an equilibrium.
3. Conditions: the conditions those objects must satisfy.
4. Taken as given: what the definition takes as given from the model setup.
5. Notation: what each symbol means.
6. Restrictions: any restriction on which equilibria Stage 1 should consider.
7. Required result: what the Stage 1 derivation must deliver.

---

## Pipeline Stages

### Stage 1: Equilibrium Derivation

| Attribute | Value |
|-----------|-------|
| **Purpose** | Derive theoretical equilibrium conditions from economic model specification |
| **Input** | QMD (`.qmd`) or LaTeX (`.tex`) file stating the economic model setup and the equilibrium definition |
| **Input Location** | Asked for at the start of every new project; the file's text is sent in the Stage 1 launch request (see [Project Input](#project-input-model-setup-and-equilibrium-definition-document)) |
| **Output** | LaTeX derivations in `outputs/stage1_derivation/` |
| **Equilibrium Definition** | Stated in the input document; this design document and the orchestrator supply none |
| **Requested Model** | **Claude Fable 5.2** |
| **Requested Reasoning** | **Second-highest** |
| **Configured** | `claude-fable-5-2` with `effort` = `xhigh` (see [Model Selection](#model-selection)) |

**Stage 1 Task:** The agent receives the text of the input document in its prompt, understands the model structure, and derives the equilibrium using only the setup and the equilibrium definition stated in that document. If the document does not state enough, the agent is told to stop and report what is missing. Stage 1 cannot launch until a usable document has been given for the project; see [Project Input](#project-input-model-setup-and-equilibrium-definition-document).

### Stage 2: Equilibrium to Pseudocode

| Attribute | Value |
|-----------|-------|
| **Purpose** | Translate equilibrium conditions into algorithmic pseudocode |
| **Input** | Stage 1 outputs (`outputs/stage1_derivation/`) |
| **Output** | Pseudocode in `outputs/stage2_pseudocode/` |
| **Requested Model** | **Claude Fable 5.2** |
| **Requested Reasoning** | **Second-highest** |
| **Configured** | `claude-fable-5-2` with `effort` = `xhigh` (see [Model Selection](#model-selection)) |

### Stage 3: Python Implementation and Plots

| Attribute | Value |
|-----------|-------|
| **Purpose** | Implement pseudocode in Python, generate visualizations |
| **Input** | Stage 2 outputs (`outputs/stage2_pseudocode/`), data (`data/`) |
| **Output** | Python code and plots in `outputs/stage3_implementation/` |
| **Requested Model** | **Grok** |
| **Requested Reasoning** | **Low** |
| **Configured** | `grok-4.7` with `reasoning_effort` = `low` (see [Model Selection](#model-selection)) |

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
| **Requested Model** | **GPT-5.6 Sol** |
| **Requested Reasoning** | **High** |
| **Configured** | `gpt-5.6-sol` with `reasoning` = `high` (see [Model Selection](#model-selection)) |

---

## Model Selection

### Configured Models

Each stage's model and reasoning level is set in `orchestrator/config.yaml` and sent in the `model` field of that stage's `POST /v1/agents` request.

| Stage | Requested by Yang Lu | `model` field sent in the launch request |
|-------|----------------------|------------------------------------------|
| 1. Equilibrium Derivation | Claude Fable 5.2, second-highest reasoning | `{"id": "claude-fable-5-2", "params": [{"id": "effort", "value": "xhigh"}]}` |
| 2. Equilibrium → Pseudocode | Claude Fable 5.2, second-highest reasoning | `{"id": "claude-fable-5-2", "params": [{"id": "effort", "value": "xhigh"}]}` |
| 3. Python Implementation | Grok, low reasoning | `{"id": "grok-4.7", "params": [{"id": "reasoning_effort", "value": "low"}]}` |
| 4. Academic Writing | GPT-5.6 Sol, high reasoning | `{"id": "gpt-5.6-sol", "params": [{"id": "reasoning", "value": "high"}]}` |

`orchestrator/test_orchestrator.py` fails if this table and `config.yaml` differ, so edit both together.

**"Second-highest" is `xhigh`.** The reasoning options reported for Claude Fable 5.2 are `low`, `medium`, `high`, `xhigh` and `max`, which makes `xhigh` the second-highest. The orchestrator checks that `xhigh` is a permitted value and prints the values the API lists, but it cannot tell which value is "second-highest". If the list of options changes, look at this mapping again.

### What Is Verified and What Is Not

| Claim | Status | Basis |
|-------|--------|-------|
| These model IDs, parameter names and values work in Cursor cloud agents | Reported by the owner | One minimal cloud-agent launch per model, each answering `OK`. They were made through Cursor's agent tooling, which takes parameters as a map such as `{effort: xhigh}`. They were not made through the public REST API. |
| The `model` field has the shape shown above | Taken from the public API documentation | [Create An Agent](https://cursor.com/docs/cloud-agent/api/endpoints): `model.id` is a string and `model.params` is an array of `{id, value}` items. The tool's map `{effort: xhigh}` is therefore written `[{"id": "effort", "value": "xhigh"}]`. |
| The orchestrator sends exactly that field for each stage | Tested with mocked HTTP | `test_launch_requests_carry_each_stage_model_in_the_documented_rest_shape` |
| The public `GET /v1/models` lists these IDs, parameter names and values | **Not verified** | No public REST call has been made; no API key was available where this was built. |
| A launch through the public REST API with these values succeeds | **Not verified** | The orchestrator has launched no agent. |

The tool and the public REST API may name models or parameters differently. For example, the REST listing could carry the reasoning level in the model ID instead of in a parameter. The check below is what catches such a difference.

### Check Before Every Launch

`--dry-run`, `--execute` and `--resume` call `GET /v1/models` before any stage is launched and check, for each stage:

1. The configured `model_id` is listed, as a model `id` or as one of its `aliases`.
2. Each configured parameter `id` is among that model's `parameters`.
3. Each configured `value` is among the permitted `values` of that parameter.

If a check fails, validation fails and nothing is launched. The error names the stage and shows what the API lists instead: the listed model IDs, the model's parameter names, or the parameter's permitted values. The orchestrator does not substitute another model, drop a parameter or fall back to the default model. To fix it, change `model_id` and `model_params` for that stage in `config.yaml`, and the table above.

`config.yaml` itself is checked first, without the network: `model_params` must be a list of `{id, value}` items with string values, and a stage with `model_params` must have a `model_id`, because the API takes parameters only together with `model.id`.

Limits of this check:

- It does not compare the configured parameters with the model's `variants` (the combinations the API lists as accepted). The API has the final say when the stage is launched; a rejected launch is reported as an error and is not retried with other settings.
- It has run only against stand-in listings in the tests, not against the live listing.

### Looking at the Live Listing

```bash
curl https://api.cursor.com/v1/models -u YOUR_API_KEY:
```

Each item has an `id` (the value for `model.id`), optional `aliases`, `parameters` (each with an `id` and its permitted `values`) and `variants`. Leaving `model` out of a launch request makes Cursor use the account's default model; this pipeline sets a model for every stage.

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
1. User runs: python orchestrator.py --execute

2. Orchestrator, before any stage:
   - Checks config file syntax
   - Asks for the model setup / equilibrium definition document (.qmd or
     .tex) and reads it; stops here if no usable document is given
   - Checks the API key is present in CURSOR_API_KEY env var
   - Validates model IDs against GET /v1/models

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

### Input Document for Stage 1

The model setup / equilibrium definition document is the one input that does not have to be committed. The orchestrator asks for its location when a project is started and sends its text to the Stage 1 agent inside the launch request. See [Project Input](#project-input-model-setup-and-equilibrium-definition-document).

Everything else the agents read (`reference/`, `data/`, the outputs of earlier stages) comes from the repository snapshot, so it must be committed and pushed.

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

# 2. Commit any changes to repository inputs
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

1. **Model Validation**: Check resolved `model.id` exists in `GET /v1/models` response
2. **Parameter Validation**: Check `model.params` are valid for the model
3. **Dependency Check**: Prior stage completed successfully
4. **Input Manifest**: Required input files exist in the starting ref
5. **Model Setup Document**: Stage 1 does not launch unless a usable model setup / equilibrium definition document has been given for the project (see [Project Input](#project-input-model-setup-and-equilibrium-definition-document))

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
- The model setup document selected for the project (path and SHA-256 hash, not its text)
- Each stage's input commit SHA
- Each stage's output branch and commit SHA
- Stage completion status

To resume a failed pipeline:

```bash
python orchestrator.py --config config.yaml --resume pipeline_manifest.json
```

The orchestrator:
1. Reads the manifest to find the last successful stage
2. If Stage 1 still has to run, re-reads the recorded model setup document without asking, and stops if it is missing or has changed
3. Skips completed stages
4. Resumes from the first incomplete stage

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

# Validate (asks for the model setup document; launches nothing)
python orchestrator/orchestrator.py --dry-run

# Start a new project (asks for the model setup document, then launches paid cloud agents)
python orchestrator/orchestrator.py --execute

# Continue an existing project (does not ask; uses the document recorded in the manifest)
python orchestrator/orchestrator.py --resume pipeline_manifest.json
```

### Optional: Task Scheduler Design

**Note: This describes a possible design. Do NOT install or schedule anything.**

For automated/scheduled runs, Windows Task Scheduler could:
1. Trigger `orchestrator.py` on a schedule or event
2. Pass `--execute` flag only when explicitly configured
3. Log output to a file for review
4. Send notifications on completion/failure

A scheduled task cannot start a new project: `--execute` asks for the model setup document and fails when no one can answer. A scheduled task could only `--resume` an existing project.

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
| `POST /v1/agents` has no field for input files | Repository inputs must be committed; the model setup document is sent as prompt text | [API Docs](https://cursor.com/docs/cloud-agent/api/endpoints) |
| `POST /v1/projects` has a `files` field (beta, being rolled out) | Not used: it belongs to Projects, a different API surface from the per-stage agents here | [API Docs](https://cursor.com/docs/cloud-agent/api/endpoints) |
| No webhooks in v1 (coming soon) | Must poll for status | [API Docs](https://cursor.com/docs/cloud-agent/api/endpoints) |
| Agent executes repo snapshot | Local changes invisible | [API Docs](https://cursor.com/docs/cloud-agent/api/endpoints) |
| One active run per agent | Cannot parallelize within agent | [API Docs](https://cursor.com/docs/cloud-agent/api/endpoints) |
| API in public beta | Endpoints may change | [API Docs](https://cursor.com/docs/cloud-agent/api/endpoints) |

### Open Questions

1. **Equilibrium Definition**: Comes from the input document Yang Lu gives when a project is started. The orchestrator cannot check that the document states one; see the limits in [Project Input](#project-input-model-setup-and-equilibrium-definition-document).

2. **Size of the Input Document**: The document's text travels in `prompt.text`, for which the API documentation states no size limit. Whether a long document is accepted has not been tested.

3. **Model IDs on the Public API**: The configured model IDs come from tool smoke tests. Whether the public `GET /v1/models` lists the same IDs has not been checked; the first `--dry-run` with an API key checks it. See [Model Selection](#model-selection).

4. **Reasoning Parameters on the Public API**: The same holds for the parameter names and values (`effort` = `xhigh`, `reasoning_effort` = `low`, `reasoning` = `high`).

5. **Grok Version**: Stage 3 uses `grok-4.7`, the version named by the owner and used in the smoke test.

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
| 2026-11-24 | Cloud Agent | The orchestrator now asks for the model setup / equilibrium definition document (`.qmd` or `.tex`) at every new project and sends its text to Stage 1. The fill-in template and its gate are removed; an optional outline remains. |
| 2026-11-24 | Cloud Agent | Set each stage's model and reasoning parameter in `orchestrator/config.yaml` and recorded the mapping, with what is and is not verified, under Model Selection. |

---

## Next Steps

1. **Yang Lu: have the model setup / equilibrium definition document ready** as one self-contained `.qmd` or `.tex` file; the orchestrator asks for its location
2. **Run `--dry-run` with an API key** to check the configured models against the public `GET /v1/models` listing
3. **Commit and push repository inputs** the stages need (`reference/`, `data/`)
4. **Test dry-run** to validate configuration and model availability
5. **Execute first pipeline run** with close monitoring
