# Research Pipeline Design Document

**Project:** RMB_AI  
**Owner:** Yang Lu  
**Last Updated:** 2026-11-24  
**Status:** Draft - implemented and tested with stand-ins; not yet run against the live API

---

## Overview

This document describes the design for a four-stage research pipeline that runs on the **Cursor Public Cloud Agents API**. A local Windows orchestrator script at `D:\RMB_AI` triggers separate cloud agents for each stage, with configurable model selection per stage and sequential artifact handoff through the Git repository.

**Implementation status.** What this document says the orchestrator does is implemented in `orchestrator/` and tested against stand-ins for the services it talks to. Nothing has been run against the live Cursor API: no agent has been launched and no pipeline has run end to end. See [What Has and Has Not Been Tested](#what-has-and-has-not-been-tested).

### Design Goals

1. **A model per stage**: Each stage names its model and reasoning parameter. Both are checked against the API's model listing before anything is launched.
2. **Sequential chaining on exact commits**: Stages run in order. Each starts from the exact commit that passed the previous stage's output check.
3. **Repository-based handoff**: Stage outputs are commits on Git branches that the agents push. Later stages read them from the repository.
4. **No duplicate paid runs**: Every step is recorded in a manifest file. A resume continues from that record and never launches a second agent for a stage that already has one, unless it is told to.
5. **Review between stages**: The pipeline can pause after a stage until its output is approved.
6. **Nothing paid without an explicit command**: Only `--execute` and `--resume` can launch agents. `--dry-run` works offline. `--check-online` uses the network and launches nothing.

### Historical Note

The original design specified Grok-only models with specific xAI reasoning tiers. This was superseded to allow wider model selection. The current design sets a model and a reasoning level for each stage; see [Model Selection](#model-selection).

---

## Project Input: Model Setup and Equilibrium Definition Document

Each project starts from one document written by Yang Lu: a Quarto Markdown (`.qmd`), LaTeX (`.tex`) or Markdown (`.md`) file that states the economic model setup and the equilibrium definition. Stage 1 derives the equilibrium from what that document states. This design document states no equilibrium definition, and the orchestrator never supplies one.

### The Orchestrator Asks for the Document

Starting a new project with `--execute` always asks this question first:

```
Location of the model setup / equilibrium definition document for this project (.qmd, .tex or .md):
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
| The extension is not `.qmd`, `.tex` or `.md` | the document must be a `.qmd`, `.tex` or `.md` file |
| The file is empty or holds only whitespace | the document is empty |
| The file is not UTF-8 text | the document is not UTF-8 text |
| The file pulls in other files (see the table below) | the document pulls in other files |

There is one attempt per run. After an error, run the command again.

The format is taken from the file extension, without regard to case:

| Extension | Format named in the Stage 1 prompt | Recorded in the manifest as | Refused when a line contains |
|-----------|------------------------------------|-----------------------------|------------------------------|
| `.qmd` | Quarto Markdown (.qmd) | `qmd` | Quarto `{{< include ... >}}` |
| `.tex` | LaTeX (.tex) | `tex` | `\input`, `\include`, `\subfile`, `\import` or `\subimport`, outside a `%` comment |
| `.md` | Markdown (.md) | `md` | Quarto `{{< include ... >}}`, or a MyST `{include}` or `{literalinclude}` directive at the start of a fence |

No other extension is accepted; `.markdown` and `.Rmd` are refused. The same checks apply to all three formats: the file must exist, be UTF-8 text, be non-empty and be self-contained.

Markdown has no include syntax of its own. The check on a `.md` file covers the two forms in the table. The include forms of other Markdown tools (for example MkDocs snippets or Obsidian embeds) are not detected: such a file passes the check and reaches the agent without the included text.

### How the Document Reaches the Stage 1 Agent

The cloud agent cannot open a path on the local machine, and `POST /v1/agents` has no field for uploading an input file. The orchestrator therefore reads the file and puts its full text into `prompt.text` of the Stage 1 launch request. The text comes after the task, between a `----- BEGIN DOCUMENT -----` line and an `----- END DOCUMENT -----` line, together with the file name, format and SHA-256 hash. The local path is not sent.

The orchestrator only reads the file. It does not change, move, copy or commit it, and it runs no Git command.

Limits of this handoff:

- **One file only.** Only the text of the selected file is sent. That is why a file that pulls in other files is refused; write that text into the file itself. Figures and bibliography files that the document refers to are not sent.
- **The text goes to Cursor.** It is part of the agent's prompt and is visible in that agent's conversation. Do not put secrets in the document.
- **Not recorded in Git by the orchestrator.** The repository holds the document only if Yang commits it himself. The project manifest records which file was used (see below).
- **Size.** The API documentation states no size limit for `prompt.text`, and the orchestrator sets none. A very large document may be rejected by the API; this has not been tested.
- **Content is not judged.** The orchestrator checks that the file is a readable, non-empty, self-contained `.qmd`, `.tex` or `.md` file. It cannot tell whether the file states an equilibrium definition, or a correct one. The Stage 1 prompt tells the agent to stop and report what is missing if the document does not state enough, instead of filling the gap itself.
- **Stand-in tests only.** This has been checked against a stand-in for the API. No Stage 1 agent has been launched.

### What the Project Records

A project is one `--execute` run and its manifest file (`pipeline_manifest.json` unless `--output` names another file). The manifest is written before the first agent is launched and again after every step (see [Project Manifest](#project-manifest)). Under `model_setup_document` it records the file's full local path, format, size in bytes, SHA-256 hash and the time it was selected. The document's text is not stored in the manifest.

`--execute` refuses to start when the manifest file already exists, because a new project would overwrite the earlier project's record. Use `--resume` to continue that project, or `--output` to give the new project its own manifest file.

`--resume` reads the document again from the recorded path, without asking, and only if a stage that receives the document may still be launched. It stops before launching anything if the manifest records no document, if the file is gone, or if its content no longer matches the recorded hash. To use a changed document, start a new project.

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
| **Input** | QMD (`.qmd`), LaTeX (`.tex`) or Markdown (`.md`) file stating the economic model setup and the equilibrium definition |
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

**Runtime:** The same twelve principles are listed under `guardrails` for this stage in `orchestrator/config.yaml`. When the orchestrator launches Stage 3 it appends them, numbered, to the stage prompt, so the agent receives them in the launch request. `orchestrator/test_orchestrator.py` fails if the list in the config differs from the list above, so edit both together. This has been checked against a stand-in for the API only; no Stage 3 agent has been launched.

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
| The orchestrator sends exactly that field for each stage | Tested against a stand-in for the API | `test_launch_requests_carry_each_stage_model_in_the_documented_rest_shape` |
| The public `GET /v1/models` lists these IDs, parameter names and values | **Not verified** | No public REST call has been made; no API key was available where this was built. |
| A launch through the public REST API with these values succeeds | **Not verified** | The orchestrator has launched no agent. |

The tool and the public REST API may name models or parameters differently. For example, the REST listing could carry the reasoning level in the model ID instead of in a parameter. The check below is what catches such a difference.

### Check Before Every Launch

`--check-online`, `--execute` and `--resume` call `GET /v1/models` before any stage is launched and check, for each stage:

1. The configured `model_id` is listed, as a model `id` or as one of its `aliases`.
2. Each configured parameter `id` is among that model's `parameters`.
3. Each configured `value` is among the permitted `values` of that parameter.

If a check fails, validation fails and nothing is launched. The error names the stage and shows what the API lists instead: the listed model IDs, the model's parameter names, or the parameter's permitted values. The orchestrator does not substitute another model, drop a parameter or fall back to the default model. To fix it, change `model_id` and `model_params` for that stage in `config.yaml`, and the table above.

`--dry-run` does not make this check: it is offline. Use `--check-online` to check the models without starting a project.

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

### Components

```
┌────────────────────────────────────────────────────────────────────────┐
│ Local Windows machine, D:\RMB_AI                                         │
│                                                                          │
│   orchestrator.py                                                        │
│    - reads config.yaml and the model setup document                      │
│    - writes the project manifest after every step                        │
│    - saves downloaded artifacts under the project folder                 │
└────────┬───────────────────────────┬──────────────────────┬─────────────┘
         │ HTTPS                     │ HTTPS, read-only     │ HTTPS
         │ with CURSOR_API_KEY       │                      │ no credentials
         ▼                           ▼                      ▼
┌─────────────────────────┐  ┌───────────────────────┐  ┌──────────────────┐
│ Cursor Cloud Agents API │  │ GitHub REST API       │  │ Presigned        │
│ api.cursor.com/v1       │  │ api.github.com        │  │ download URLs    │
│  - list models          │  │  - branch -> commit   │  │ (artifact bytes) │
│  - launch / look up     │  │  - commit exists      │  └──────────────────┘
│    an agent             │  │  - file exists at a   │
│  - read run status      │  │    commit             │
│  - list artifacts       │  └───────────┬───────────┘
└────────────┬────────────┘              │ reads
             │ one agent per stage       ▼
             │               ┌─────────────────────────────┐
             └──────────────►│ github.com/yangluust/RMB_AI │
               clones a      │  main, cursor/* branches    │
               commit,       └─────────────────────────────┘
               pushes a branch
```

Models are reached only through Cursor. The orchestrator holds no Anthropic, OpenAI or xAI key and calls none of their APIs. It runs no Git command and writes nothing to GitHub; only the cloud agents push.

### Requests the Orchestrator Sends

| Purpose | Request | Changes anything? |
|---------|---------|-------------------|
| List models | Cursor `GET /v1/models` | No |
| Launch a stage's agent | Cursor `POST /v1/agents` | **Yes: starts a paid agent** |
| Look an agent up by ID | Cursor `GET /v1/agents/{id}` | No |
| Read run status and pushed branch | Cursor `GET /v1/agents/{id}/runs/{runId}` | No |
| List artifacts | Cursor `GET /v1/agents/{id}/artifacts` | No |
| Get a download URL for an artifact | Cursor `GET /v1/agents/{id}/artifacts/download?path=...` | No |
| Read the head commit of a branch | GitHub `GET /repos/{owner}/{repo}/git/ref/heads/{branch}` | No |
| Check that a commit exists | GitHub `GET /repos/{owner}/{repo}/git/commits/{sha}` | No |
| Check that a file exists at a commit | GitHub `GET /repos/{owner}/{repo}/contents/{path}?ref={sha}` | No |

The launch request is the only request that changes anything. It is handled differently from the others; see [Launch Safety](#launch-safety).

GitHub is needed because the run response of the Cursor API names the pushed branch but gives no commit SHA and no file listing.

### Commands

| Command | Uses the network | Can launch agents | What it does |
|---------|------------------|-------------------|--------------|
| `--dry-run` | No | No | Checks `config.yaml`, asks for the model setup document, prints what each stage would do. Needs no API key. Saves nothing. |
| `--check-online` | Yes | No | Checks the API key, the configured models against `GET /v1/models`, and that the starting ref can be read on GitHub. Does not ask for the document. |
| `--execute` | Yes | Yes | Starts a new project: asks for the document, runs the online checks, writes the manifest, then runs the stages until one pauses, stops or the last one completes. |
| `--resume MANIFEST` | Yes | Yes | Continues a project from its manifest. Never launches a second agent for a stage that already has one. |
| `--resume MANIFEST --approve STAGE` | Yes | Yes | Approves a stage that is paused for review, then goes on: the next stage is launched by the same command. |
| `--resume MANIFEST --approve STAGE --approve-commit SHA` | Yes | Yes | Approves a commit that was pushed to the stage's branch after the output check. |
| `--resume MANIFEST --rerun-stage STAGE` | Yes | Yes | Launches a **new paid agent** for a stage whose run failed, whose output check failed, or whose output is rejected at review. |

`--output FILE` names the manifest file of a new project. It is refused together with `--resume`: a resumed project keeps writing to the manifest it was resumed from.

Exit codes: `0` when every stage is complete (or a check passed), `2` when the pipeline is paused for review, `1` otherwise.

### Execution Flow

```
python orchestrator.py --execute

1. Check config.yaml (offline). Stop on any error.

2. Ask for the model setup / equilibrium definition document and read it.
   Stop if no usable document is given.

3. Online checks. Stop on any failure. Nothing has been launched yet.
   - CURSOR_API_KEY is set
   - every stage's model, parameters and values are in GET /v1/models
   - the starting ref can be read on GitHub; the commit it points at
     becomes the project's source commit

4. Write the manifest file (document record, source ref, source commit).

5. For each stage, in order:
   a. Launch   Choose an agent ID and write it to the manifest. Then
               POST /v1/agents with startingRef = the input commit.
   b. Wait     Poll the run until it ends.
   c. Check    Read the head commit of the branch the agent pushed.
               Require a new commit that holds every required file.
               Record it as the stage's output commit.
   d. Collect  Download the agent's artifacts into the project folder.
   e. Review   If the stage has manual_review: stop until it is approved.
   The manifest is written after each of these steps.
   The first stage that cannot go on stops the pipeline. Later stages
   are not started.

6. Print each stage's status and, for a stage that stopped, the command
   that continues it.
```

### Stage States

The manifest records one status per stage. It decides what a later `--resume` does.

| Status | Meaning | A plain `--resume` |
|--------|---------|--------------------|
| `LAUNCHING` | A launch request was sent, or was about to be sent. Whether the agent exists is not known. | Looks the agent up by its recorded ID. Sends the launch request only if the API says there is no such agent, and then with the same ID. |
| `LAUNCH_REJECTED` | The API refused the launch request. No agent exists. | Launches the stage. |
| `RUNNING` | The agent exists. Its run had not ended when the orchestrator last looked. | Polls the same run again. |
| `RUN_FAILED` | The run ended with `ERROR`, `CANCELLED` or `EXPIRED`. | Launches nothing. Needs `--rerun-stage`. |
| `RUN_FINISHED` | The run ended with `FINISHED`. The output check is not done yet. | Does the output check. |
| `GATE_FAILED` | The output check failed. | Does the output check again. Launches nothing. |
| `OUTPUT_VERIFIED` | The output check passed. Artifacts are not collected yet. | Collects the artifacts. |
| `ARTIFACTS_FAILED` | Listing or downloading the artifacts failed. | Collects the artifacts again. |
| `AWAITING_REVIEW` | The stage waits for approval. | Stays paused. Needs `--approve` (or `--rerun-stage` to reject). |
| `COMPLETE` | Done. Its output commit is handed to the next stage. | Skips the stage. |

A stage with no record has not been started. `PENDING` is a stage reset by `--rerun-stage` that has not been launched again yet. `BLOCKED` means the stage needs the model setup document and none was loaded; the command line always loads it first. `DRY_RUN` appears only inside a dry run and is never saved.

### Project Manifest

The manifest is one JSON file per project (`pipeline_manifest.json` unless `--output` names another file). It is the only record that links a project to the agents launched for it.

It records:

- `format_version` (now `2`). A manifest with another version, including one written before this field existed, is refused.
- `model_setup_document`: path, format, size, SHA-256 and selection time of the input document. Not its text.
- `source_ref` and `source_commit`: the configured starting ref and the commit it pointed at when the project was started.
- For each stage: `status`, `attempt`, `agent_id`, `run_id`, `agent_url`, `run_status`, `input_source`, `input_commit`, `output_branch`, `output_commit`, the output check (`gate`), the collected `artifacts`, the `review` record, `duration_ms`, `error`, `completed_at`.
- `previous_attempts`: the full record of every attempt replaced by `--rerun-stage`.

The file is written after every step. Each write goes to a temporary file that then replaces the manifest in one step, so an interrupted write does not leave a half-written manifest.

The manifest is a local file. The orchestrator does not commit it. It holds local paths and no secrets.

---

## Source Snapshot and Branch Semantics

### Critical Understanding

**Cloud agents execute against the committed repository snapshot on GitHub, not your local working directory.**

This means:
- Local uncommitted changes are **NOT visible** to cloud agents
- You must `git commit` and `git push` before the agent can see your changes
- Each agent starts from one exact commit, which the orchestrator chooses and records

### Input Document for Stage 1

The model setup / equilibrium definition document is the one input that does not have to be committed. The orchestrator asks for its location when a project is started and sends its text to the Stage 1 agent inside the launch request. See [Project Input](#project-input-model-setup-and-equilibrium-definition-document).

Everything else the agents read (`reference/`, `data/`, the outputs of earlier stages) comes from the repository snapshot, so it must be committed and pushed.

### Branch Creation

By default (`workOnCurrentBranch: false`), Cursor:
1. Creates a new branch named `cursor/<descriptive-name>-<hash>` from the `startingRef` of the launch request
2. The agent commits its outputs to this new branch
3. The branch name appears in the run response under `git.branches[]`

### Sequential Chaining on Exact Commits

```
main ──● source commit            read once, when the project is started
       │
       └─ Stage 1 agent ─► cursor/... ──● output commit 1   checked, then approved
                                        │
                                        └─ Stage 2 agent ─► cursor/... ──● output commit 2
                                                                         │
                                                                         └─ Stage 3 agent ─► ...
```

1. When a project is started, the orchestrator reads from GitHub which commit `starting_ref` points at and records it as the project's **source commit**. If `starting_ref` is already a full commit SHA, it checks that the commit exists.
2. Stage 1 is launched with `startingRef` set to the source commit.
3. When a stage's run has finished, the orchestrator reads the head commit of the branch the agent pushed and checks it (see [Output Check](#output-check-after-each-stage)). The commit that passes is recorded as the stage's **output commit**.
4. The next stage is launched with `startingRef` set to that output commit.

Every launch request therefore carries a 40-character commit SHA, never a branch name. The reason: a branch can move between two stages. `main` gets new commits; someone pushes to a `cursor/*` branch. With commit SHAs, the next stage reads exactly the content that was checked and approved, and a resume days later uses the same commits as the first run.

What this does not do:

- It does not protect a commit from being deleted. If a `cursor/*` branch is deleted and GitHub drops its commits, a later launch from that commit fails. The launch error is reported; nothing else is launched.
- It does not check that the output commit descends from the input commit. It checks that the two differ.

The API documentation gives `startingRef` as "Branch name or full 40-character commit SHA". Launching from a commit SHA has not been tried against the live API.

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

# 4. Check the configuration offline, then the models and repository access online
python orchestrator/orchestrator.py --dry-run
python orchestrator/orchestrator.py --check-online

# 5. If both pass, start the project
python orchestrator/orchestrator.py --execute
```

---

## Validation and Review Gates

### Before Any Launch

Offline (`--dry-run` stops here):

1. **Configuration**: `config.yaml` is checked. The repository URL must be a github.com URL. Each stage needs a prompt, well-formed model settings and guardrails, a true/false `manual_review`, and `required_files` that are safe relative paths. `api.retries` must be set and well-formed. The first stage must receive the model setup document.
2. **Model setup document**: A usable `.qmd`, `.tex` or `.md` document must be given (see [Project Input](#project-input-model-setup-and-equilibrium-definition-document)).

Online (`--check-online`, `--execute`, `--resume`):

3. **API key**: `CURSOR_API_KEY` is set.
4. **Models**: Every stage's model, parameters and values are in `GET /v1/models` (see [Check Before Every Launch](#check-before-every-launch)).
5. **Source**: The starting ref can be read on GitHub.

Before each single launch, the orchestrator also requires that the previous stage is `COMPLETE` and that the project has a manifest file to record the launch in.

### Output Check After Each Stage

A stage's output is handed on only if all of this holds:

1. The run ended with status `FINISHED`.
2. The run reports exactly one pushed branch in this repository (`git.branches[]`). No branch, or more than one, fails the check.
3. The head commit of that branch, read from GitHub, differs from the stage's input commit. Otherwise the agent pushed nothing.
4. Every path under the stage's `output_manifest.required_files` exists as a file at that commit.

If the check passes, that commit is recorded as the stage's output commit.

If the check fails, the stage gets status `GATE_FAILED`, the reason is recorded, and the pipeline stops. No output commit is recorded and no later stage is launched. The same happens when GitHub cannot be read (for example, a rate limit).

A failed check can be repeated at no cost: `--resume` reads the branch head again and repeats the check without launching anything. So if a required file is missing, it can be pushed to the stage's branch by hand; the next `--resume` then passes the check at the new head commit and records that commit.

Limits:

- The check tests that the files exist. It does not read them and cannot tell whether their content is right. That is what the manual review is for.

### Artifact Collection

Artifacts are files the agent saved to its `artifacts/` folder; the Cursor API lists them and gives a temporary download URL for each. They are separate from the stage's output files, which are in the repository at the output commit and are not downloaded.

After the output check passes, and if `artifacts.collect` is `true`, the orchestrator lists the agent's artifacts and saves each one to

```
<folder of the manifest>/<artifacts.local_output_dir>/<manifest file name without .json>/<stage>/attempt_<n>/<artifact path>
```

The manifest records each saved file with its path, size and SHA-256 hash.

Artifact paths come from the API and are not trusted. A path is saved only if it is relative, uses forward slashes, has no backslash, colon or control character, and has no part that is empty, `.`, `..`, a Windows device name (such as `NUL` or `COM1`), or ends in a dot or a space. The final location must also lie inside the download folder. A path that fails is not downloaded and not written; it is recorded under `rejected` with the reason, and the stage goes on.

The download URL must be an `https` URL. It is fetched without the Cursor API key, because it points at another host.

If listing or downloading fails, the stage gets status `ARTIFACTS_FAILED` and the pipeline stops. `--resume` collects again and launches nothing.

### Manual Review

A stage with `manual_review: true` pauses the pipeline after its output check and artifact collection. The stage gets status `AWAITING_REVIEW`, the orchestrator prints a link to the output commit on GitHub and exits with code `2`. No later stage is launched.

In the committed `config.yaml`, Stages 1, 2 and 3 pause for review; Stage 4 is the last stage and does not. **These review points were chosen when the feature was built, not specified by Yang Lu.** Change `manual_review` per stage in `config.yaml`.

To approve:

```bash
python orchestrator/orchestrator.py --resume pipeline_manifest.json --approve equilibrium_derivation
```

The approval is recorded in the manifest, the stage becomes `COMPLETE`, and the same command goes on to launch the next stage.

Approval names an exact commit:

- Without `--approve-commit`, the commit that passed the output check is approved. This is refused if the stage's branch has moved since the check; the message shows the branch's new head commit.
- To hand on a commit pushed after the check (for example, a correction made by the reviewer), add `--approve-commit SHA`. The SHA must be the current head of the stage's branch, and the required files are checked again at that commit. It then replaces the stage's output commit; the replaced commit stays in the review record.

To reject the output and have a new agent redo the stage, use `--rerun-stage` (a new paid run from the same input commit).

A plain `--resume` without `--approve` leaves the stage paused.

---

## Failure Handling and Resume

### Retries

`api.retries` in `config.yaml` sets `max_attempts` (every try counts, the first included) and `backoff_seconds` (the waits before the second, third, ... try; the last value repeats). The committed values are 3 tries with waits of 5 and 15 seconds.

Only **read-only** requests are simply tried again, and only when the failure may pass: no response, HTTP 429, or HTTP 5xx. Any other HTTP 4xx answer is a refusal and is not retried. When the tries run out, the last error is reported.

The launch request is never simply tried again.

### Launch Safety

A launch request starts a paid agent. If the request gets no clear answer (a timeout, a dropped connection, HTTP 5xx), the agent may or may not have been created. Sending the request again blindly could create a second paid agent.

The orchestrator avoids this with the `agentId` field of `POST /v1/agents`. The API documentation says a client may choose the agent's ID, and that "Re-POSTing the same `agentId` returns `409 agent_id_conflict` instead of creating a duplicate".

1. Before launching a stage, the orchestrator generates an agent ID (`bc-` followed by a random UUID) and writes it, with the input commit and status `LAUNCHING`, to the manifest file.
2. Only then does it send the launch request, carrying that ID.
3. What happens next depends on the answer:

| Answer to the launch request | What the orchestrator does |
|------------------------------|----------------------------|
| Success | Records the run. The stage is `RUNNING`. |
| No response, HTTP 429, HTTP 5xx, or HTTP 409 | Waits (backoff), then asks `GET /v1/agents/{id}` for the recorded ID. If the agent exists, it is used and no launch request is sent. Only if the API answers that there is no such agent is the request sent again, with the same ID. |
| Any other HTTP 4xx | The API refused the request, so no agent exists. The stage is `LAUNCH_REJECTED`. Nothing is retried. |
| Still unclear after `max_attempts` tries | The stage stays `LAUNCHING`. A later `--resume` starts with the lookup. |

A new agent ID is generated only for a stage that was never launched, after a refusal (no agent exists under the refused ID), or for a new attempt made with `--rerun-stage`.

Limits:

- This rests on the documented behaviour of `agentId`. It has been tested against a stand-in that behaves as documented, not against the live API.
- The manifest file is the only link between a project and its agents. If it is deleted, or a new project is started with `--execute` for the same work, the orchestrator cannot know about agents launched earlier.

### What Stops a Pipeline, and How to Continue

| Situation | Recorded status | To continue |
|-----------|-----------------|-------------|
| Configuration, document, API key, model or source check fails | Nothing is launched; for `--execute`, no manifest is written | Fix the cause and run the command again |
| Launch request refused by the API | `LAUNCH_REJECTED` | Fix the cause, then `--resume` |
| Launch outcome unknown after all tries | `LAUNCHING` | `--resume` (looks the agent up first) |
| Run still going after `api.polling.max_attempts` polls, or its status cannot be read | `RUNNING` | `--resume` (polls the same run) |
| Run ended `ERROR`, `CANCELLED` or `EXPIRED` | `RUN_FAILED` | `--resume --rerun-stage STAGE` (new paid agent) |
| No pushed branch, no new commit, a required file missing, or GitHub unreadable | `GATE_FAILED` | `--resume` checks again; or `--rerun-stage STAGE` |
| Artifact listing or download failed | `ARTIFACTS_FAILED` | `--resume` (collects again) |
| Stage waits for review | `AWAITING_REVIEW` | `--resume --approve STAGE`, or `--rerun-stage STAGE` to reject |

There is no automatic retry of a failed run and no option to skip a failed stage: every later stage needs the earlier stage's output.

### No Accidental Paid Reruns

- `--dry-run` and `--check-online` cannot launch anything.
- `--execute` refuses to start if the manifest file already exists.
- `--resume` launches an agent only for a stage that has none: one never started, one whose launch the API refused, or one whose launch outcome is still unknown and whose agent the API says does not exist.
- A stage that is `RUNNING`, `RUN_FAILED`, `GATE_FAILED`, `ARTIFACTS_FAILED`, `AWAITING_REVIEW` or `COMPLETE` is never launched again by a plain `--resume`.
- A second agent for a stage is launched only by `--rerun-stage STAGE`. The replaced attempt is kept under `previous_attempts`, and the stage's `attempt` number goes up.
- `--resume` cannot be combined with `--output`, so the record of launches cannot be split over two files.

### Resume

```bash
python orchestrator/orchestrator.py --resume pipeline_manifest.json
```

The orchestrator:

1. Reads the manifest. It refuses a manifest of another format version, one for another repository than `config.yaml` names, or one with stages the configuration does not define.
2. Re-reads the recorded model setup document, without asking, only if a stage that receives it may still be launched. It stops if the file is missing or has changed.
3. Runs the online checks.
4. Goes through the stages in order and continues each from its recorded status (see [Stage States](#stage-states)). It uses the recorded source and output commits; it does not read `starting_ref` again.

---

## What Has and Has Not Been Tested

`orchestrator/test_orchestrator.py` holds the tests. They run without the network. The Cursor API, the GitHub API and the download host are replaced by one stand-in (`FakeServices` in the test file) that was written from the public documentation of the two APIs. It is not a recording of the live services.

Tested against the stand-in:

| Behaviour | Test |
|-----------|------|
| Every launch starts from an exact commit; the recorded commits are used even after the branches move | `test_each_stage_starts_from_an_exact_commit_and_hands_on_a_verified_commit` |
| A missing required file stops the pipeline; re-checking launches nothing; the pipeline goes on once the file is there | `test_missing_output_file_stops_the_pipeline_until_the_file_is_there` |
| No pushed branch, no new commit, or an unreadable repository fails the output check | `test_output_check_fails_without_a_branch_a_new_commit_or_a_readable_repository` |
| Read-only requests are retried up to the limit with the configured waits; refusals are not retried | `test_read_requests_are_retried_a_bounded_number_of_times_with_backoff` |
| A launch with an unknown outcome never creates a second agent; the agent ID is on disk before the request | `test_launch_with_an_unknown_outcome_never_creates_a_second_agent` |
| A run that outlasts the polls is waited for again, not relaunched | `test_run_that_is_still_going_is_waited_for_again_and_never_relaunched` |
| A failed stage is relaunched only with `--rerun-stage`; the earlier attempt is kept | `test_failed_run_is_launched_again_only_by_an_explicit_rerun` |
| Review pauses the pipeline; approval fixes the commit handed on; a moved branch needs `--approve-commit` | `test_manual_review_pauses_the_pipeline_until_the_output_is_approved` |
| An output rejected at review is replaced only with `--rerun-stage` | `test_output_rejected_at_review_is_replaced_only_by_an_explicit_rerun` |
| Artifacts are saved only to safe paths inside the project folder; a non-https download URL is refused | `test_artifacts_are_downloaded_only_to_safe_paths_inside_the_project_folder` |
| A `.qmd`, `.tex` or `.md` document is accepted and its text reaches Stage 1; another extension, an empty or non-UTF-8 file, or a file that pulls in other files is refused | `test_valid_qmd_tex_and_md_documents_reach_stage1_in_the_launch_request`, `test_unusable_locations_are_rejected_before_any_launch`, `test_document_that_pulls_in_other_files_is_rejected` |
| `--dry-run` sends no request, needs no API key and saves nothing, with a `.tex`, `.qmd` or `.md` document | `test_cli_dry_run_is_offline_and_saves_nothing` |
| `--check-online` reads the model listing and the starting ref and launches nothing | `test_cli_check_online_validates_over_the_network_and_launches_nothing` |
| The committed configuration runs through all four stages with three approvals | `test_cli_execute_asks_records_the_document_and_resume_does_not_ask` |

**Not tested: anything against the live services.** No request has been sent to the live Cursor API, no agent has been launched, and no pipeline has run end to end. In particular, these documented behaviours are assumed, not observed:

1. `POST /v1/agents` accepts a client-chosen `agentId` and answers a repeat with `409 agent_id_conflict`.
2. `POST /v1/agents` accepts a commit SHA as `startingRef`.
3. A finished run reports the pushed branch under `git.branches[]`.
4. The artifact listing and download responses have the documented shape.
5. `GET /v1/models` lists the configured model IDs, parameters and values (see [Model Selection](#model-selection)).

Also not tested:

- The orchestrator's own HTTP transport (the `requests` library). The tests inject stand-in transports, and `requests` was not installed where this was built. The three GitHub endpoints were called by hand on this repository with the GitHub command line tool (read-only), and answered in the shape the orchestrator expects.
- Running on Windows. The tests ran on Linux. The path checks are written for Windows file names but were not run there.

The first real use should therefore be `--check-online`, followed by a watched `--execute` that stops at the Stage 1 review.

---

## Setup Instructions (Windows)

### Prerequisites

1. **Python 3.10+** installed and in PATH
2. **Git** installed and configured
3. **Cursor API Key** from [Cursor Dashboard → API Keys](https://cursor.com/dashboard)
4. **Read access to the repository on GitHub.** `yangluust/RMB_AI` is public, so no GitHub token is needed. For a private repository, set `GITHUB_TOKEN` to a token that can read it. Without a token GitHub allows a limited number of requests per hour from one address; a pipeline run needs only a few per stage.

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

# Only for a private repository (do NOT commit this)
# $env:GITHUB_TOKEN = "your-github-token-here"
```

### Running the Orchestrator

```powershell
cd D:\RMB_AI

# Offline check (asks for the model setup document; no network, no API key, launches nothing)
python orchestrator/orchestrator.py --dry-run

# Online check (API key, models, repository access; launches nothing)
python orchestrator/orchestrator.py --check-online

# Start a new project (asks for the model setup document, then launches paid cloud agents)
python orchestrator/orchestrator.py --execute

# Continue an existing project (does not ask; uses the document recorded in the manifest)
python orchestrator/orchestrator.py --resume pipeline_manifest.json

# Approve a stage that is paused for review, and go on to the next stage
python orchestrator/orchestrator.py --resume pipeline_manifest.json --approve equilibrium_derivation

# Launch a NEW paid agent for a stage that failed or whose output is rejected
python orchestrator/orchestrator.py --resume pipeline_manifest.json --rerun-stage equilibrium_derivation
```

The manifest file and the `collected_artifacts` folder are created in the folder the command is run from, unless `--output` names another manifest file.

### Optional: Task Scheduler Design

**Note: This describes a possible design. Do NOT install or schedule anything.**

For automated/scheduled runs, Windows Task Scheduler could:
1. Trigger `orchestrator.py` on a schedule or event
2. Pass `--execute` flag only when explicitly configured
3. Log output to a file for review
4. Send notifications on completion/failure

A scheduled task cannot start a new project: `--execute` asks for the model setup document and fails when no one can answer. A scheduled task could only `--resume` an existing project, and a plain `--resume` does not pass a review point: a stage that is paused for review stays paused until someone approves it.

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
| The run response names the pushed branch but gives no commit SHA and no file listing | The orchestrator reads the commit and checks the files through the GitHub REST API, so only github.com repositories are supported | [API Docs](https://cursor.com/docs/cloud-agent/api/endpoints) |
| The API documents no way to cancel a launch whose outcome is unknown | The orchestrator relies on the client-chosen `agentId` to avoid duplicates (see [Launch Safety](#launch-safety)) | [API Docs](https://cursor.com/docs/cloud-agent/api/endpoints) |
| Agent executes repo snapshot | Local changes invisible | [API Docs](https://cursor.com/docs/cloud-agent/api/endpoints) |
| One active run per agent | Cannot parallelize within agent | [API Docs](https://cursor.com/docs/cloud-agent/api/endpoints) |
| API in public beta | Endpoints may change | [API Docs](https://cursor.com/docs/cloud-agent/api/endpoints) |

### Open Questions

1. **Equilibrium Definition**: Comes from the input document Yang Lu gives when a project is started. The orchestrator cannot check that the document states one; see the limits in [Project Input](#project-input-model-setup-and-equilibrium-definition-document).

2. **Size of the Input Document**: The document's text travels in `prompt.text`, for which the API documentation states no size limit. Whether a long document is accepted has not been tested.

3. **Model IDs on the Public API**: The configured model IDs come from tool smoke tests. Whether the public `GET /v1/models` lists the same IDs has not been checked; the first `--check-online` with an API key checks it. See [Model Selection](#model-selection).

4. **Reasoning Parameters on the Public API**: The same holds for the parameter names and values (`effort` = `xhigh`, `reasoning_effort` = `low`, `reasoning` = `high`).

5. **Grok Version**: Stage 3 uses `grok-4.7`, the version named by the owner and used in the smoke test.

6. **Cost Estimation**: Running 4 cloud agents per pipeline execution has cost implications. The orchestrator has no budget control; it only avoids launching an agent twice for the same stage.

7. **Review Points**: The committed configuration pauses for review after Stages 1, 2 and 3. That choice was made when the feature was built. Yang Lu to confirm or change it in `config.yaml`.

8. **Live Behaviour of the API**: The launch safety, the commit-based handoff and the artifact download rest on documented API behaviour that has not been observed. See [What Has and Has Not Been Tested](#what-has-and-has-not-been-tested).

9. **Committing the Manifest**: The manifest is a local file and the orchestrator does not commit it. Whether a finished project's manifest should be kept in the repository is open.

---

## Official Documentation References

| Resource | URL |
|----------|-----|
| Cloud Agents API (v1) | https://cursor.com/docs/cloud-agent/api/endpoints |
| API Overview & Authentication | https://cursor.com/docs/api |
| OpenAPI Specification | https://cursor.com/docs-static/cloud-agents-openapi.yaml |
| Legacy v0 API (webhooks) | https://cursor.com/docs/cloud-agent/api/v0 |
| GitHub REST: get a reference (branch head) | https://docs.github.com/en/rest/git/refs#get-a-reference |
| GitHub REST: get a commit object | https://docs.github.com/en/rest/git/commits#get-a-commit-object |
| GitHub REST: get repository content | https://docs.github.com/en/rest/repos/contents#get-repository-content |

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
| 2026-11-24 | Cloud Agent | Implemented the output check, launch safety, bounded retries, artifact download, manual review and commit-based handoff. Rewrote Architecture, Source Snapshot, Validation and Review Gates, and Failure Handling to describe what is implemented. `--dry-run` is now offline; `--check-online` is new. Added What Has and Has Not Been Tested. |
| 2026-11-24 | Cloud Agent | The model setup document may also be a Markdown (`.md`) file. A `.md` file is checked for the Quarto include shortcode and the MyST include directives. The checks on `.qmd` and `.tex` files are unchanged. |

---

## Next Steps

1. **Yang Lu: have the model setup / equilibrium definition document ready** as one self-contained `.qmd`, `.tex` or `.md` file; the orchestrator asks for its location
2. **Commit and push repository inputs** the stages need (`reference/`, `data/`)
3. **Run `--dry-run`** to check the configuration and the document offline
4. **Run `--check-online` with an API key** to check the configured models against the public `GET /v1/models` listing, and repository access
5. **Confirm the review points** in `config.yaml` (`manual_review` per stage)
6. **Run `--execute` and watch it** up to the Stage 1 review. This is the first contact with the live API; compare what happens with [What Has and Has Not Been Tested](#what-has-and-has-not-been-tested)
