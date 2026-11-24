# Research Pipeline Orchestrator

Local Windows orchestrator for running a multi-stage research pipeline on Cursor Cloud Agents.

## Overview

This orchestrator:
- Calls the **Cursor Public Cloud Agents API** to run each pipeline stage, one cloud agent per stage
- Sets a **model per stage** and checks it against `GET /v1/models` before any launch
- **Asks for the model setup / equilibrium definition document** (`.qmd` or `.tex`) at every new project, before anything is launched
- Starts each stage from an **exact commit**: the project's source commit, then the commit that passed the previous stage's output check
- **Checks each stage's output** (required files at the pushed commit), **downloads artifacts**, and can **pause for review** before the next stage
- Records every step in a **manifest file**, so a project can be resumed without launching any agent twice
- Launches agents only with `--execute` or `--resume`

**Status:** tested against stand-ins for the Cursor API and GitHub only. No agent has been launched and nothing has run against the live API. See [What Has and Has Not Been Tested](../docs/pipeline-design.md#what-has-and-has-not-been-tested).

## Quick Start (Windows)

```powershell
# 1. Set your API key
$env:CURSOR_API_KEY = "your-api-key-from-cursor-dashboard"

# 2. Navigate to repository
cd D:\RMB_AI

# 3. Offline check (asks for the model setup document; no network, launches nothing)
python orchestrator/orchestrator.py --dry-run

# 4. Online check (API key, models, repository access; launches nothing)
python orchestrator/orchestrator.py --check-online

# 5. Start a new project (asks for the model setup document, then launches paid cloud agents)
python orchestrator/orchestrator.py --execute

# 6. After reviewing a stage's output: approve it and go on to the next stage
python orchestrator/orchestrator.py --resume pipeline_manifest.json --approve equilibrium_derivation
```

Steps 3 and 5 each ask:

```
Location of the model setup / equilibrium definition document for this project (.qmd or .tex):
```

Type the path of your file, for example `D:\Research\my_model\setup.qmd`.

With the committed configuration, step 5 runs Stage 1 and then pauses for review (exit code `2`). Each `--approve` launches the next stage.

## Files

| File | Description |
|------|-------------|
| `config.yaml` | Pipeline and stage configuration |
| `orchestrator.py` | Main orchestrator script |
| `api_client.py` | Cursor Cloud Agents API client and read-only GitHub reader |
| `test_orchestrator.py` | Tests (stand-in services, no network) |
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
      required_files:           # Must exist at the commit the agent pushed
        - "outputs/my_stage/result.txt"
    manual_review: true         # Pause after this stage until it is approved
```

Other settings: `pipeline.repository` (a github.com URL and the starting ref), `api.polling`, `api.retries` (required), `artifacts`, `execution.auto_create_pr` and `manifest.output_file`. Each is explained in `config.yaml`.

### Guardrails

A stage may list `guardrails`. When the orchestrator launches that stage it appends the list, numbered, to the stage prompt, so the agent receives the guardrails in the launch request.

Stage 3 (`pseudocode_to_implementation`) carries the twelve reproducibility guardrails from [`docs/pipeline-design.md`](../docs/pipeline-design.md#reproducibility-guardrails-stage-3). The tests fail if the list in `config.yaml` differs from the list in the design document, so edit both together.

### Model Setup Document (Asked at Every New Project)

Every new project starts by asking for the location of the model setup / equilibrium definition document. This is mandatory: there is no setting, flag or environment variable that supplies the location, and an earlier project's document is never reused.

- The file must exist, be a `.qmd` or `.tex` file, hold non-empty UTF-8 text, and be self-contained (no `\input`, `\include` or Quarto `{{< include >}}` of other files).
- If the answer is blank or unusable, or no answer can be read (for example under a scheduler), the run stops with an error and nothing is launched.
- The file is only read. It is not changed, moved, copied or committed.
- The first stage must set `receives_model_setup_document: true`. At launch the orchestrator appends the document's full text to that stage's prompt. The cloud agent cannot open a path on your machine, so the text is sent and the path is not.
- The project manifest records the file's path, size and SHA-256 hash (not its text). `--resume` re-reads the same file without asking, if a stage that receives it may still be launched, and stops if it is missing or has changed.
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

These values come from tool smoke tests, not from the public REST API. They have **not** been checked against the public `GET /v1/models` listing. Before any launch (and in `--check-online`) the orchestrator calls `GET /v1/models` and stops if a model ID, parameter or value is not listed, showing what the API lists instead. It never substitutes another model or drops a parameter. See [Model Selection](../docs/pipeline-design.md#model-selection) in the design document.

To see the listing yourself:

```bash
curl https://api.cursor.com/v1/models -u YOUR_API_KEY:
```

Write `model_params` values as quoted strings. A stage with `model_params` must also set `model_id`. Setting `model_id: null` with no `model_params` uses your configured default model.

## Commands

| Command | Network | Can launch | Description |
|---------|---------|------------|-------------|
| `--dry-run` | No | No | Check the config, ask for the model setup document, print what each stage would do. Needs no API key. Saves nothing. |
| `--check-online` | Yes | No | Check the API key, the models (`GET /v1/models`) and that the starting ref can be read on GitHub. |
| `--execute` | Yes | Yes | Start a new project: ask for the document, run the online checks, then run the stages. |
| `--resume MANIFEST` | Yes | Yes | Continue an existing project. Does not ask. Never launches a second agent for a stage that already has one. |
| `--resume MANIFEST --approve STAGE` | Yes | Yes | Approve a stage that is paused for review, then go on to the next stage. |
| `... --approve STAGE --approve-commit SHA` | Yes | Yes | Approve a commit pushed to the stage's branch after the output check. |
| `--resume MANIFEST --rerun-stage STAGE` | Yes | Yes | Launch a **new paid agent** for a stage that failed or whose output is rejected at review. |
| `--config path/to/config.yaml` | | | Use another config file. |
| `--output path/to/manifest.json` | | | Manifest file for a new project (default `pipeline_manifest.json`). Not allowed with `--resume`. |

Exit codes: `0` every stage complete (or a check passed), `2` paused for review, `1` anything else.

## What Happens in a Stage

1. **Launch.** The orchestrator chooses the agent's ID, writes it to the manifest, then sends the launch request with `startingRef` set to an exact commit.
2. **Wait.** It polls the run until it ends.
3. **Output check.** It reads the head commit of the branch the agent pushed and requires a new commit that holds every file in `required_files`. That commit becomes the stage's output and the next stage's starting point.
4. **Artifacts.** If `artifacts.collect` is `true`, it downloads the agent's artifacts to `collected_artifacts/<manifest name>/<stage>/attempt_<n>/`. Unsafe paths are refused and recorded.
5. **Review.** If the stage has `manual_review: true`, the pipeline stops until the stage is approved.

The commits and required files are read through the GitHub REST API (read-only). `yangluust/RMB_AI` is public, so no token is needed; for a private repository set `GITHUB_TOKEN`.

In the committed `config.yaml`, Stages 1 to 3 pause for review. This was chosen when the feature was built; change `manual_review` per stage if you want other review points.

## After a Stop

The summary printed at the end names each stage's status and the command that continues it.

| Status | Meaning | To continue |
|--------|---------|-------------|
| `AWAITING_REVIEW` | Output checked; waiting for you | `--resume M --approve STAGE` (or `--rerun-stage STAGE` to reject) |
| `LAUNCHING` | Not known whether the agent was created | `--resume M` (looks the agent up first; never creates a second one) |
| `LAUNCH_REJECTED` | The API refused the launch; no agent exists | Fix the cause, then `--resume M` |
| `RUNNING` | The run had not ended when polling stopped | `--resume M` (waits for the same run) |
| `RUN_FAILED` | The run ended with an error | `--resume M --rerun-stage STAGE` (new paid agent) |
| `GATE_FAILED` | A required file is missing, or nothing was pushed | `--resume M` checks again; or `--rerun-stage STAGE` |
| `ARTIFACTS_FAILED` | Artifacts could not be downloaded | `--resume M` (downloads again) |

A plain `--resume` never launches a second agent for a stage. Only `--rerun-stage` does. Details: [Failure Handling and Resume](../docs/pipeline-design.md#failure-handling-and-resume).

## Important: Repository Snapshots

**Cloud agents execute against committed code on GitHub, not your local working directory.**

The model setup document is the exception: the orchestrator reads it from your machine and sends its text in the Stage 1 launch request, so it does not have to be committed.

Before running:
1. Commit your input data: `git add . && git commit -m "Add inputs"`
2. Push to GitHub: `git push origin main`
3. Then run the orchestrator

Local uncommitted changes are **invisible** to cloud agents.

When a project is started, the orchestrator records the commit that `starting_ref` points at. Stage 1 starts from that commit, even if the branch moves later.

## Tests

Run the tests. They use stand-ins for the Cursor API, GitHub and the download host, make no network call and launch nothing:

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
- GitHub REST API: [get a reference](https://docs.github.com/en/rest/git/refs#get-a-reference), [get a commit object](https://docs.github.com/en/rest/git/commits#get-a-commit-object), [get repository content](https://docs.github.com/en/rest/repos/contents#get-repository-content)

## Design Document

See [`docs/pipeline-design.md`](../docs/pipeline-design.md) for the full design specification.
