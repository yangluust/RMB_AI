# Research Pipeline Design Document

**Project:** RMB_AI  
**Owner:** Yang Lu  
**Last Updated:** 2026-11-24  
**Status:** Draft - implemented and tested with stand-ins; not yet run against the live API; the chat host is not built

---

## Overview

This document describes the design for a four-stage research pipeline that runs on the **Cursor Public Cloud Agents API**. A local Windows orchestrator script at `D:\RMB_AI` triggers separate cloud agents for each stage, with configurable model selection per stage and sequential artifact handoff through the Git repository.

The four stages are: (1) derive the equilibrium of the model as implementable mathematical conditions, (2) translate those conditions into pseudocode, (3) translate the pseudocode into Python code, (4) propose a parameter domain, then run batches of configurations the owner approved and report the results. All four write into one cumulative working document. A stage runs in counted rounds. Every round ends at a human gate: the output is shown in the local checkout, announced by a signed notice, and nothing goes on without the owner's signed decision.

**Implementation status.** What this document says the orchestrator does is implemented in `orchestrator/` and tested against stand-ins for the services it talks to. Nothing has been run against the live Cursor API: no agent has been launched and no pipeline has run end to end. See [What Has and Has Not Been Tested](#what-has-and-has-not-been-tested).

### Design Goals

1. **A model per stage**: Each stage names its model and reasoning parameter. Both are checked against the API's model listing before anything is launched.
2. **Sequential chaining on exact commits**: Stages run in order. Each starts from the exact commit that passed the previous stage's output check.
3. **Repository-based handoff**: Stage outputs are commits on Git branches that the agents push. Later stages read them from the repository.
4. **No duplicate paid runs**: Every step is recorded in a manifest file. A resume continues from that record and never repeats a recorded launch. A failed launch or run is never repeated automatically.
5. **A human gate after every round**: Rounds are counted against a hard limit per stage. A round's output is checked out in the owner's local checkout and announced by a signed notice; it waits for review only once the chat host acknowledged posting it. Decisions are signed and bound to the exact notice, round, commit and config. Accepting a stage and starting the next one are two decisions.
5a. **Checks of form, not of correctness**: Each stage must deliver evidence (audit, trace, test map, batch record) that is checked for form and cross-references. A passed check is never presented as proof that the mathematics, the code or a number is right.
6. **One cumulative working document**: Every stage adds its own section to one file in `docs/`. An accepted section is not changed by a later stage, and the document never holds content for a stage that has not been carried out.
7. **Nothing paid without an explicit command**: Only `--execute` and `--resume` can launch agents; after the first round, only together with a decision of the owner (or, where a stage sets `autonomous_fix: true`, a correction round inside the same stage and limit). `--dry-run` works offline. `--check-online` uses the network and launches nothing.

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

The orchestrator only reads the file. It does not change, move, copy or commit it.

**The committed copy.** Stages 2 to 4 do not get the document in their prompts; they read it from the repository. The document must therefore also be committed, unchanged, at the path `working_document.source_document` names (`docs/intervention-incomplete-information-source.md`), in the commit the project starts from. When a project is started, the orchestrator reads that file from GitHub at the source commit and stops, before anything is launched, if it is missing or if its SHA-256 differs from the document selected for the project. `--check-online` asks for no document and checks only that the committed copy is there. The simplest way to satisfy this is to answer the question with the path of the committed copy in the local checkout.

Limits of this handoff:

- **One file only.** Only the text of the selected file is sent. That is why a file that pulls in other files is refused; write that text into the file itself. Figures and bibliography files that the document refers to are not sent.
- **The text goes to Cursor.** It is part of the agent's prompt and is visible in that agent's conversation. Do not put secrets in the document.
- **Not committed by the orchestrator.** The orchestrator never commits or pushes anything. The committed copy is put in the repository by a normal commit. The project manifest records which file was used (see below).
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

| Stage | ID in `config.yaml` | Task | Writes |
|-------|---------------------|------|--------|
| 1 | `equilibrium_derivation` | Derive the equilibrium as implementable mathematical conditions | Section 1 of the working document |
| 2 | `equilibrium_to_pseudocode` | Translate the conditions into pseudocode | Section 2 of the working document |
| 3 | `pseudocode_to_implementation` | Translate the pseudocode into Python code, with tests | Section 3 of the working document; code in `outputs/stage3_implementation/` |
| 4 | `parameterization_and_results` | Propose a parameter domain; run approved batches of configurations; report the results | Section 4 of the working document; files in `outputs/stage4_results/` |

Stage 4 replaced the earlier "Academic Writing" stage (`academic_writing`), which no longer exists. The IDs of Stages 1 to 3 are unchanged; their tasks and outputs changed as described below. Each stage does its own task only: Stage 1 writes no pseudocode, Stage 2 no code, Stage 3 no parameter configuration and no results.

### Working Document

All four stages write into `docs/equilibrium-working-document.md` (`working_document.path` in `config.yaml`). Each stage's section is enclosed by two marker lines:

```
<!-- BEGIN STAGE 2: equilibrium_to_pseudocode -->
...
<!-- END STAGE 2: equilibrium_to_pseudocode -->
```

The orchestrator appends the exact marker lines and the rules below to each stage's prompt, and checks the same rules at the commit the stage pushes (see [Output Check After Each Launch](#output-check-after-each-launch)):

1. The stage's own section is there, each marker exactly once on a line of its own, and is not empty.
2. The sections of the earlier stages are byte-for-byte as they are in the commit the stage started from, which is the commit the owner accepted. A stage that finds an error in an earlier section reports it in its own section and does not edit the earlier one.
3. No marker of a later stage is in the document. The document therefore never holds content, or a placeholder, for a stage that has not been carried out.
4. The committed source document (`working_document.source_document`, `docs/intervention-incomplete-information-source.md`) is byte-for-byte unchanged.

The source document in `docs/` is the owner's original file, unchanged (79,082 bytes, SHA-256 `560b2ee1a7189ed8183dcc5ec8d6db9292f85ac645351bfe65a0a3292cad5960`, CRLF line endings). `.gitattributes` marks it `-text`, so Git converts its line endings on no platform. A test fails if its bytes change.

### Stage 1: Equilibrium Derivation

| Attribute | Value |
|-----------|-------|
| **Purpose** | Derive the equilibrium of the model and write it as implementable mathematical conditions |
| **Input** | QMD (`.qmd`), LaTeX (`.tex`) or Markdown (`.md`) file stating the economic model setup and the equilibrium definition |
| **Input Location** | Asked for at the start of every new project; the file's text is sent in the Stage 1 launch request (see [Project Input](#project-input-model-setup-and-equilibrium-definition-document)). The same file must be committed at `docs/intervention-incomplete-information-source.md` |
| **Scope** | Section 1 of the document, together with the lagged-price rule that Section 1.2 refers to, equation (12) in Section 4. The other sections are secondary reference only |
| **Output** | Section 1 of the working document: mathematics only, every displayed condition labelled with `\tag{...}`; the audit in `outputs/stage1_audit/` |
| **Equilibrium Definition** | Stated in the input document; this design document and the orchestrator supply none |
| **Requested Model** | **Claude Fable 5.2** |
| **Requested Reasoning** | **Second-highest** |
| **Configured** | `claude-fable-5-2` with `effort` = `xhigh` (see [Model Selection](#model-selection)) |

**Stage 1 Task:** The agent receives the text of the input document in its prompt and writes: the exogenous processes, parameters, state variables and unknowns; the timing and information; the equilibrium conditions as residual equations, each with its source equation; which terms are expectations and under which information; the boundary, positivity and admissibility conditions; for every condition whether it is exact or a first-order approximation in the source; and a numbered list of missing closures and ambiguities. It is told to keep as a symbol every functional form, value, initial condition and distribution the document does not give, and to stop and report rather than fill a gap. Stage 1 cannot launch until a usable document has been given for the project; see [Project Input](#project-input-model-setup-and-equilibrium-definition-document).

### Stage 2: Equilibrium to Pseudocode

| Attribute | Value |
|-----------|-------|
| **Purpose** | Translate the equilibrium conditions into algorithmic pseudocode |
| **Input** | Section 1 of the working document, as accepted |
| **Output** | Section 2 of the working document: pseudocode only (no program code, no parameter values), steps with IDs; the trace in `outputs/stage2_trace/trace.json` |
| **Requested Model** | **Claude Fable 5.2** |
| **Requested Reasoning** | **Second-highest** |
| **Configured** | `claude-fable-5-2` with `effort` = `xhigh` (see [Model Selection](#model-selection)) |

### Stage 3: Python Implementation

| Attribute | Value |
|-----------|-------|
| **Purpose** | Translate the pseudocode into Python code and test it |
| **Input** | Sections 1 and 2 of the working document, as accepted |
| **Output** | `outputs/stage3_implementation/solve_equilibrium.py`, `test_solve_equilibrium.py`, `requirements.txt`, `README.md`, the saved test output and `test_map.json`; Section 3 of the working document (the code, the map from pseudocode steps to functions, the test command and its printed output) |
| **Not in this stage** | The project's parameter configuration, results, plots. The code reads every parameter and functional-form choice from a configuration file and holds no built-in values |
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

### Stage 4: Parameters, Computation and Results

| Attribute | Value |
|-----------|-------|
| **Purpose** | Propose a parameter domain, run approved batches of configurations with the Stage 3 code, report the results |
| **Input** | Sections 1 to 3 of the working document, as accepted; the Stage 3 code; the source document |
| **Output** | Section 4 of the working document; `outputs/stage4_results/batch_record.json`, `next_batch_spec.json` and one folder per batch |
| **Requested Model** | **GPT-5.6 Sol** |
| **Requested Reasoning** | **High** |
| **Configured** | `gpt-5.6-sol` with `reasoning` = `high` (see [Model Selection](#model-selection)) |

**Two kinds of round.** The first round is a *domain proposal*: the parameter domain, seeds, solver tolerances and the size of the first batch, and nothing computed. It cannot be accepted as the stage's result. A *batch* round runs only after the owner's `iterate` decision with `approve_batch: true`, which fixes the proposed batch by its SHA-256: at most 25 configurations (`verification.max_configurations`), all inside the approved domain, every run counted. A batch round may propose the next batch; that one needs its own approval.

**What the source does not supply.** The source document gives no functional form for the reserve cost function, no numerical calibration, no initial stocks and no shock distributions. The prompt allows clearly labelled *illustrative* choices, each with its reason, and forbids presenting them as the source's calibration. A structural ambiguity of the model is reported, not filled.

**Classification of a run.** `admissible`, `inadmissible` (the solver converged and a named admissibility condition fails) `numerical_failure` (no convergence or an error, with the reason) or `outside_domain` (listed and counted, with no result). A numerical failure is not evidence of an economic boundary. A boundary claim must name admissible and inadmissible runs on its two sides and runs that reproduce it, and carries the status `evidence_not_proof`: it shows where solutions stopped being found, not that no equilibrium exists.

### Rounds, Evidence and Review

The settings, decisions and file formats are in [`orchestrator/README.md`](../orchestrator/README.md). In short:

| Per stage | Committed value | Meaning |
|-----------|-----------------|---------|
| `iteration.max_rounds` | 4, 3, 4, 6 | Hard limit of counted rounds. A round is counted before any work, failed and adopted rounds included. Never reset; raised only by the owner's `raise_limit` decision |
| `iteration.autonomous_fix` | `false` | `true`: after failed checks the stage may start a correction round by itself, until a round passes with no open item, the limit is reached, or no other route is seen. It never leaves the stage and never passes the gate |
| `verification.kind` | `equilibrium_audit`, `traceability`, `test_map`, `parameter_batches` | The evidence file the stage must deliver and how it is checked (`orchestrator/verification.py`) |
| `cross_model.enabled` | `false` | `true`: a round is three launches (propose; blind assessment by a second model from the same input; comparison with the exact candidate). The two models change roles every round. No second model is configured |

An item that depends on a choice of the owner (a missing closure, an ambiguity) is reported as an open item and blocks acceptance. A stage cannot close it: `resolved` needs the ID of a recorded decision of the owner. The items listed at the end of the Stage 1 section remain open in this sense.

**Trial log.** The orchestrator writes every event (round counted, launch, check, failure, round closed with KEEP / ABANDON / UNRESOLVED, rationale and next route, notice, decision) to a local journal beside the manifest, flushed to disk before the next step; if it cannot be written, nothing further is done. Each launched agent appends the journal's entries, word for word, and one entry of its own to [`docs/pipeline-trial-log.md`](pipeline-trial-log.md), which the output check verifies as append-only. The log holds evidence-based summaries, not private reasoning.

---

## Model Selection

### Configured Models

Each stage's model and reasoning level is set in `orchestrator/config.yaml` and sent in the `model` field of that stage's `POST /v1/agents` request.

| Stage | Requested by Yang Lu | `model` field sent in the launch request |
|-------|----------------------|------------------------------------------|
| 1. Equilibrium Derivation | Claude Fable 5.2, second-highest reasoning | `{"id": "claude-fable-5-2", "params": [{"id": "effort", "value": "xhigh"}]}` |
| 2. Equilibrium → Pseudocode | Claude Fable 5.2, second-highest reasoning | `{"id": "claude-fable-5-2", "params": [{"id": "effort", "value": "xhigh"}]}` |
| 3. Python Implementation | Grok, low reasoning | `{"id": "grok-4.7", "params": [{"id": "reasoning_effort", "value": "low"}]}` |
| 4. Parameters, Computation and Results | GPT-5.6 Sol, high reasoning | `{"id": "gpt-5.6-sol", "params": [{"id": "reasoning", "value": "high"}]}` |

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
│    - runs git fetch / checkout / merge --ff-only in the local checkout   │
│      to show a stage's output for review (never commits or pushes)       │
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
│  - read run status      │  │    commit; its bytes  │
│  - list artifacts       │  └───────────┬───────────┘
└────────────┬────────────┘              │ reads
             │ one agent per stage       ▼
             │               ┌─────────────────────────────┐
             └──────────────►│ github.com/yangluust/RMB_AI │
               clones a      │  main, cursor/* branches    │
               commit,       └─────────────────────────────┘
               pushes a branch
```

Models are reached only through Cursor. The orchestrator holds no Anthropic, OpenAI or xAI key and calls none of their APIs, and no chat or mail credentials: review notices are signed files that an external host must post (see [Notices and Decisions](#notices-and-decisions)). It writes nothing to GitHub: it never commits or pushes; only the cloud agents push. The only Git commands it runs are the ones that show a stage's output in the local checkout (see [Local Review](#local-review)); `git fetch` there uses the checkout's own Git credentials, with prompting switched off. `--dry-run` runs no Git command.

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
| Check that a file exists at a commit; read the working document, the source document, the trial log and the evidence files at a commit | GitHub `GET /repos/{owner}/{repo}/contents/{path}?ref={sha}` | No |
| Check that a commit builds on another, and list the files that differ | GitHub `GET /repos/{owner}/{repo}/compare/{base}...{head}` | No |
| Show a stage's output for review | Local `git fetch`, `git checkout`, `git merge --ff-only` in the local checkout | Only the local checkout (see [Local Review](#local-review)) |

The launch request is the only request that changes anything outside the local machine. It is handled differently from the others; see [Launch Safety](#launch-safety).

GitHub is needed because the run response of the Cursor API names the pushed branch but gives no commit SHA and no file listing.

### Commands

The commands, decisions and exit codes are listed in [`orchestrator/README.md`](../orchestrator/README.md#commands). `--dry-run` is offline; `--check-online`, `--adopt`, `--halt`, `--unhalt` and `--repin` launch nothing; `--execute` launches the first round of the first stage; `--resume` continues recorded work; `--resume ... --apply-decision FILE` applies one signed decision of the owner. The earlier flags `--approve`, `--revise` and `--rerun-stage` are removed.

### Execution Flow

```
python orchestrator.py --execute

1. Check config.yaml (offline). Ask for the model setup document.
2. Online checks: API key, models, starting ref, committed source
   document. Nothing has been launched yet.
3. Write the manifest. Count round 1 of Stage 1 (journal first).
4. The round:
   a. Launch   Local checkout and bridge must be ready. Record the
               agent ID, then POST /v1/agents from an exact commit.
               (With cross-model review: propose, blind assessment,
               comparison - three launches, one round.)
   b. Wait     Poll the run. A failed run ends the round.
   c. Check    Required files, working document rules, source
               document, changed paths, trial log, evidence file.
   d. Close    Record outcome, disposition, rationale, next route.
               With autonomous_fix: a failed round may be followed
               by a correction round of the same stage (back to a).
5. The gate:
   e. Show     Check the output commit out in the local checkout.
   f. Notice   Write the signed notice to the outbox: NOTICE_PENDING.
   g. Ack      The host posts it in the main chat and writes the
               signed ack: AWAITING_REVIEW.
6. Stop. Only a signed decision of the owner goes on:
   accept | iterate | redo | raise_limit | halt, then start_stage.
```

### Stage States

The manifest records one status per stage, for its current round: `BLOCKED`, `LAUNCHING`, `RUNNING`, `RUN_FINISHED`, `OUTPUT_CHECKED`, `ARTIFACTS_FAILED`, `LOCAL_SYNC_PENDING`, `NOTICE_PENDING`, `AWAITING_REVIEW`, `COMPLETE` (and `PENDING` for a counted round not yet launched; `DRY_RUN` only inside a dry run). What each means and what a plain `--resume` does is in the [README](../orchestrator/README.md#statuses).

A closed round also records its **outcome** (`passed`, `checks_failed`, `blockers_open`, `disagreement`, `review_invalid`, `review_failed`, `run_failed`, `launch_rejected`) and its **stop reason** (`passed`, `review_required`, `cap_exhausted`, `no_plausible_alternative`, `owner_decision_needed`, `disagreement`, `infrastructure_failure`, `adopted`). Every stop reason leads to the gate.

`COMPLETE` is reached only through the owner's signed `accept` decision for a round that passed. There is no setting that lets a stage complete without it.

### Project Manifest

The manifest is one JSON file per project (`pipeline_manifest.json` unless `--output` names another file). It records:

- `format_version` (now `4`). A manifest of another version is refused; none is converted.
- `project_id`, and `provenance`: how the project was created (`execute` or `adopt`), the SHA-256 of the config and of each orchestrator file, and every `--repin`.
- `model_setup_document`, `source_ref`, `source_commit`.
- For each stage, its current round: `round`, `kind` (`initial`, `fix`, `reconcile`, `revise`, `redo`, `adopted`), `origin` (`pipeline` or `adopted`), every launch (`launches`: phase, role, model, agent, run, prompt hash, start commit), the input, candidate, content and output commits, the checks (`gate`), the cross-model record, `outcome`, `stop_reason`, the local checkout record, the signed notice and ack, and the decision that accepted it.
- `previous_rounds`, `decisions` (every applied signed decision), `round_limit_raises`, `halted`.

On resume, every status is checked against the records that must have led to it: a launched round needs an agent, `AWAITING_REVIEW` needs the signed notice and its signed ack, `COMPLETE` needs the owner's signed decision for that output commit, a later stage needs the earlier one accepted and its own `start_stage` decision (or an adoption). A manifest written by hand therefore cannot stand in for a launch, a notice or a decision.

The file is written after every step, through a temporary file that replaces it in one step. It is local, is not committed, and holds local paths and no secrets.

---

## Source Snapshot and Branch Semantics

### Critical Understanding

**Cloud agents execute against the committed repository snapshot on GitHub, not your local working directory.**

This means:
- Local uncommitted changes are **NOT visible** to cloud agents
- You must `git commit` and `git push` before the agent can see your changes
- Each agent starts from one exact commit, which the orchestrator chooses and records

### Input Document for Stage 1

The orchestrator asks for the location of the model setup / equilibrium definition document when a project is started and sends its text to the Stage 1 agent inside the launch request. The same file must also be committed in `docs/`, because the later stages read it from the repository. See [Project Input](#project-input-model-setup-and-equilibrium-definition-document).

Everything else the agents read (the working document, the outputs of earlier stages) comes from the repository snapshot, so it must be committed and pushed.

### Branch Creation

By default (`workOnCurrentBranch: false`), Cursor:
1. Creates a new branch named `cursor/<descriptive-name>-<hash>` from the `startingRef` of the launch request
2. The agent commits its outputs to this new branch
3. The branch name appears in the run response under `git.branches[]`

### Sequential Chaining on Exact Commits

```
main ──● source commit            read once, when the project is started
       │
       └─ Stage 1 agent ─► cursor/... ──● output commit 1   checked, shown locally, then accepted
                                        │
                                        └─ Stage 2 agent ─► cursor/... ──● output commit 2
                                                                         │
                                                                         └─ Stage 3 agent ─► ...
```

1. When a project is started, the orchestrator reads from GitHub which commit `starting_ref` points at and records it as the project's **source commit**. If `starting_ref` is already a full commit SHA, it checks that the commit exists.
2. Stage 1 is launched with `startingRef` set to the source commit.
3. When a stage's run has finished, the orchestrator reads the head commit of the branch the agent pushed and checks it (see [Output Check](#output-check-after-each-launch)). The commit that passes is recorded as the stage's **output commit**.
4. After the owner accepted it and decided to start the next stage, that stage is launched with `startingRef` set to that output commit.
5. A further round of the same stage is launched from the reviewed output commit (`iterate`, a correction or a reconciliation round), from a commit the owner pushed to that branch and named (`iterate` with `base_commit`), or from the stage's input (`redo`).

Every launch request therefore carries a 40-character commit SHA, never a branch name. The reason: a branch can move between two stages. `main` gets new commits; someone pushes to a `cursor/*` branch. With commit SHAs, the next stage reads exactly the content that was checked and approved, and a resume days later uses the same commits as the first run.

What this does not do:

- It does not protect a commit from being deleted. If a `cursor/*` branch is deleted and GitHub drops its commits, a later launch from that commit fails. The launch error is reported; nothing else is launched.

The API documentation gives `startingRef` as "Branch name or full 40-character commit SHA". Launching from a commit SHA has not been tried against the live API.

### Safe Input Publishing Workflow

Before running the pipeline:

```bash
# 1. Ensure your inputs are committed
cd D:\RMB_AI
git status  # Check for uncommitted changes

# 2. Commit any changes to repository inputs (the source document is in docs/)
git add docs/
git commit -m "Add inputs for pipeline run"

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

1. **Configuration**: `config.yaml` is checked. The repository URL must be a github.com URL. Each stage needs a prompt, well-formed model settings and guardrails, `iteration.max_rounds`, well-formed `verification` and `cross_model` settings (an enabled cross-model review needs two different models), and `required_files` and `allowed_paths` that are safe relative paths; a `manual_review` or `completion_record` line is refused. `api.retries`, `review` (`local_checkout`, `remote`, `delivery`) and `trial_log.path` must be set and well-formed; `working_document`, if present, must name `path` and `source_document`. The first stage must receive the model setup document.
2. **Model setup document**: A usable `.qmd`, `.tex` or `.md` document must be given (see [Project Input](#project-input-model-setup-and-equilibrium-definition-document)).

Online (`--check-online`, `--execute`, `--resume`):

3. **API key**: `CURSOR_API_KEY` is set.
4. **Models**: Every stage's model, parameters and values are in `GET /v1/models` (see [Check Before Every Launch](#check-before-every-launch)).
5. **Source**: The starting ref can be read on GitHub, and the committed source document is at that commit. For a project, the document selected at the start must be the same file (same SHA-256): Stage 1 is sent the selected document and the later stages read the committed copy.
6. **Local checkout** (`--check-online`): the folder named by `review.local_checkout` is the top of a Git checkout whose remote points at the repository, no merge or rebase is under way, and no tracked file has uncommitted changes.

Before each single launch, the orchestrator also requires that the round was counted, that the project is not halted, and that the local checkout passes the check in item 6 and the bridge can write a signed notice (the secret is set). If not, the stage is `BLOCKED` and no paid run is started.

### Output Check After Each Launch

A candidate commit passes only if all of this holds:

1. The run ended `FINISHED` and reports exactly one pushed branch in this repository.
2. The branch's head commit, read from GitHub, builds on the commit the launch started from.
3. Every path in `output_manifest.required_files` exists at that commit.
4. The working document keeps its four rules (see [Working Document](#working-document)); the source document is unchanged.
5. Only the stage's own paths differ from its input commit: the working document, `required_files`, `allowed_paths`, the evidence file, the review folder and the trial log.
6. The trial log only grew: by the orchestrator's entries, word for word, and the agent's own entry with its required lines.
7. The stage's evidence file passes the check of its kind (see [Rounds, Evidence and Review](#rounds-evidence-and-review)).

A failed check is a result of the round, not an error to hide: the candidate is still shown to the owner, with the problems, and cannot be accepted. If GitHub cannot be read, nothing is recorded as checked and `--resume` checks again without launching anything.

Limits: the checks are about form and cross-references. They cannot tell whether a derivation, a program or a number is right. Evidence files are statements of the model that wrote them; the orchestrator runs neither the Stage 1 symbolic checks, nor the Stage 3 tests, nor the Stage 4 computation. A reviewer of a cross-model round is another model, not a proof, and two models are not statistically independent.

### Artifact Collection

Artifacts are files the agent saved to its `artifacts/` folder; the Cursor API lists them and gives a temporary download URL for each. They are separate from the stage's output files, which are in the repository at the output commit and are not downloaded.

After the output check passes, and if `artifacts.collect` is `true`, the orchestrator lists the agent's artifacts and saves each one to

```
<folder of the manifest>/<artifacts.local_output_dir>/<manifest file name without .json>/<stage>/round_<n>/<phase>/<artifact path>
```

The manifest records each saved file with its path, size and SHA-256 hash.

Artifact paths come from the API and are not trusted. A path is saved only if it is relative, uses forward slashes, has no backslash, colon or control character, and has no part that is empty, `.`, `..`, a Windows device name (such as `NUL` or `COM1`), or ends in a dot or a space. The final location must also lie inside the download folder. A path that fails is not downloaded and not written; it is recorded under `rejected` with the reason, and the stage goes on.

The download URL must be an `https` URL. It is fetched without the Cursor API key, because it points at another host.

If listing or downloading fails, the stage gets status `ARTIFACTS_FAILED` and the pipeline stops. `--resume` collects again and launches nothing.

### Local Review

The orchestrator runs in the owner's checkout (`D:\RMB_AI`). Each stage's agent pushes its own branch (`cursor/...`), so a `git pull` of `main` would not show a stage's output. After the output check and the artifact collection, the orchestrator therefore checks the stage's branch out in the local checkout named by `review.local_checkout`, at exactly the output commit, with these Git commands:

1. `git fetch --no-tags <remote> refs/heads/<branch>:refs/remotes/<remote>/<branch>`. The refspec has no `+`, so a remote branch that was rewritten is refused.
2. It requires the output commit to be on the fetched branch.
3. If there is no local branch of that name: `git checkout -b <branch> <output commit>`, then `git branch --set-upstream-to=<remote>/<branch>`. If there is one and the output commit is ahead of it: `git checkout <branch>` and `git merge --ff-only <output commit>`.
4. It reads back that `HEAD` is the output commit, that no tracked file is changed, and that the files to review (the working document and the stage's required files) exist on disk.

Only when step 4 holds is the signed notice written (see [Notices and Decisions](#notices-and-decisions)). The same is done for a round whose checks failed, so that the owner sees what was produced.

The checkout is on the stage's branch afterwards. `main` is not moved and nothing is merged into it, at any point, including after the last approval. `git checkout main` goes back. Because the commit is checked out, not the branch's head, a commit pushed to the branch after the output check is not shown until it is named (see below).

What it never does: `--force`, `reset`, `stash`, a merge other than a fast-forward, or any change while tracked files have uncommitted changes. Untracked files, such as the manifest and `collected_artifacts/`, are left where they are. In each of these cases nothing is changed, the stage stays `LOCAL_SYNC_PENDING` with the reason, it is **not** announced as ready, and `--resume` tries again without launching anything:

| Case | Message tells the owner to |
|------|----------------------------|
| Tracked files have uncommitted changes | Commit them (and push them, if they are review edits to hand on) or restore them |
| A merge, rebase, cherry-pick or revert is under way | Finish or abort it |
| The local branch of that name has the owner's own commits on top of the output | Push them and name the pushed commit as `base_commit` of an `iterate` decision |
| The local branch of that name has diverged from the output | Rename the local branch |
| The output commit holds another version of the running orchestrator (its five Python files or `config.yaml`) than the checkout | Bring the checkout to the orchestrator version the project was started from. The running orchestrator is never replaced by a checkout |
| The folder is not the top of a checkout, its remote points at another repository, the fetch fails, or the commit is not on the branch | Fix that |

The same readiness check runs before every launch, so a paid run does not start while its output could not be shown afterwards.

### Notices and Decisions

The orchestrator sends nothing itself and holds no credentials for a chat or mail service. At the gate it writes a signed notice file to an outbox folder beside the manifest. A **host outside this repository** must post the notice in the main chat and write a signed ack; only then is the round `AWAITING_REVIEW`. Without a valid ack it stays `NOTICE_PENDING`: console output is never counted as delivery. The owner's written reply comes back as a signed decision file, which `--resume MANIFEST --apply-decision FILE` applies.

- **Notice**: project, stage, round and limit, outcome and stop reason, the input, candidate and output commits, the checks' problems and open items, the local and GitHub paths of the output and of the trial log, and the decisions it allows, each with a token. Its ID is deterministic (`<project>-s<stage>-r<round>-notice-<n>`).
- **Decision**: applied only if its signature, the token of that notice and action, the notice's ID and content hash, the project, round, output commit and config hash all match the open notice. An applied decision applied again does nothing; a second decision for a decided notice, or a decision for another commit or config, is refused. `accept` is refused if the branch moved after the check.
- **Host duties**: verify the signature, deduplicate by ID *and* content hash (the same ID with other content is refused), post, ack. `HostReceiver`, `build_ack` and `build_decision` in `orchestrator/review_bridge.py` are the reference. **No host is implemented**; the tests use these functions as an offline stand-in.

The decisions (`accept`, `start_stage`, `iterate`, `redo`, `raise_limit`, `halt`) and when each is allowed are in the [README](../orchestrator/README.md#decisions). Accepting launches nothing: the next stage needs its own `start_stage` decision, and the last stage is accepted like any other. The rule in `AGENTS.md` that every question to the owner is also written in the main chat is unchanged.

### Adoption of Existing Work

`--adopt STAGE` registers work that is already on a branch (for example the directly written Stage 1 section) as round 1 of a stage, without redoing it and without launching anything. The command names the branch, the commit to adopt (the branch's current head), the input commit, and, when the branch moved on after the work, the content commit that holds the stage's files; those files must be identical at the content and adopted commits. The adopted commit goes through the whole output check; evidence that is not there is a failed check. The round is recorded with origin `adopted` and no agent, run or artifact, then goes to the gate and can only become `AWAITING_REVIEW`. Adoption never accepts anything and is never followed by an automatic round. Nothing has been adopted.

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
| Success | Records the run. The launch is `RUNNING`. |
| No response, HTTP 429, HTTP 5xx, or HTTP 409 | Waits (backoff), then asks `GET /v1/agents/{id}` for the recorded ID. If the agent exists, it is used and no launch request is sent. Only if the API answers that there is no such agent is the request sent again, with the same ID. |
| Any other HTTP 4xx | The API refused the request, so no agent exists. The round is closed (`launch_rejected`) and goes to the gate. Nothing is retried. |
| Still unclear after `max_attempts` tries | The stage stays `LAUNCHING`. A later `--resume` starts with the lookup. |

A new agent ID is generated only for a launch that has none recorded. A refused launch or a failed run ends its round; a new launch needs a new counted round.

Limits:

- This rests on the documented behaviour of `agentId`. It has been tested against a stand-in that behaves as documented, not against the live API.
- The manifest file is the only link between a project and its agents. If it is deleted, or a new project is started with `--execute` for the same work, the orchestrator cannot know about agents launched earlier.

### What Stops a Pipeline, and How to Continue

| Situation | Recorded | To continue |
|-----------|----------|-------------|
| Configuration, document, API key, model or source check fails | Nothing is launched; for `--execute`, no manifest is written | Fix the cause and run the command again |
| The local checkout or the bridge is not ready before a launch | `BLOCKED`; nothing is launched | Fix the cause, then `--resume` |
| Launch outcome unknown after all tries | `LAUNCHING` | `--resume` (looks the agent up first) |
| Run still going, or its status cannot be read | `RUNNING` | `--resume` (polls the same run) |
| GitHub cannot be read during a check | `RUN_FINISHED` | `--resume` (checks again) |
| Launch refused, or run ended `ERROR`, `CANCELLED` or `EXPIRED` | Round closed, `infrastructure_failure` | Gate; the owner decides `redo` (a new counted round) or `halt` |
| A check failed, an item is open, the reviewer disagreed, the limit is reached, or no other route is seen | Round closed with that stop reason | Gate; the owner decides |
| Artifact listing or download failed | `ARTIFACTS_FAILED` | `--resume` (collects again) |
| The output cannot be checked out in the local checkout | `LOCAL_SYNC_PENDING`; no notice | Fix the cause, then `--resume` |
| The host has not acknowledged the notice | `NOTICE_PENDING` | `--resume` looks for the ack |
| The trial journal cannot be written | The command stops | Fix the cause, then `--resume` |

There is no automatic retry of a failed run and no option to skip a failed stage.

### No Accidental Paid Reruns

- `--dry-run`, `--check-online`, `--adopt`, `--halt`, `--unhalt` and `--repin` cannot launch anything.
- `--execute` refuses to start if the manifest file already exists.
- A plain `--resume` launches only what a counted round still lacks: a launch never sent, or one whose outcome is unknown and whose agent the API says does not exist. It never counts a new round, starts a stage or passes a gate.
- A new round is counted only by the owner's `iterate`, `redo` or `start_stage` decision, or, where the stage sets `autonomous_fix: true`, by a correction or reconciliation round inside the stage's limit.
- The round limit is hard. Failed, interrupted and adopted rounds count. It is raised only by the owner's `raise_limit` decision.
- With cross-model review a round is one count and at most three launches, each recorded and resumed separately.
- `--resume` cannot be combined with `--output`, so the record of launches cannot be split over two files.

### Resume

```bash
python orchestrator/orchestrator.py --resume pipeline_manifest.json
```

The orchestrator:

1. Reads the manifest. It refuses a manifest of another format version, one for another repository than `config.yaml` names, one with stages the configuration does not define, one whose statuses are not backed by their signed records (see [Project Manifest](#project-manifest)), and one whose recorded configuration differs from the current one (until `--repin`).
2. Re-reads the recorded model setup document, without asking, only if a stage that receives it may still be launched. It stops if the file is missing or has changed.
3. Runs the online checks.
4. Continues the first stage that is not accepted, if a round was counted for it, from its recorded status (see [Stage States](#stage-states)). It uses the recorded source and output commits; it does not read `starting_ref` again.

---

## What Has and Has Not Been Tested

`orchestrator/test_orchestrator.py` holds the tests. They run without the network. The Cursor API, the GitHub API and the download host are replaced by one stand-in (`FakeServices` in the test file) that was written from the public documentation of the two APIs. It is not a recording of the live services. Its agents write stand-in outputs that have the required form and no content: no test derives, programs or computes anything of the model.

The local checkout is replaced by a stand-in that runs no Git command, except in `test_local_review_checkout_shows_the_exact_commit_and_never_forces`, which runs real Git commands on throw-away repositories in a temporary folder. The chat host is replaced by `FakeHost`, built on the reference functions of `review_bridge.py`: it posts nowhere.

Tested against the stand-ins (71 tests; the main ones for the round design):

| Behaviour | Test |
|-----------|------|
| Every launch starts from an exact commit; recorded commits are used after branches move | `test_each_stage_starts_from_an_exact_commit_and_hands_on_a_verified_commit` |
| A round with failed checks is shown to the owner and cannot be accepted | `test_round_with_failed_checks_goes_to_the_owner_and_cannot_be_accepted` |
| A failed run ends its round; only the owner's decision launches again | `test_failed_run_ends_its_round_and_is_launched_again_only_by_the_owner` |
| A launch with an unknown outcome never creates a second agent | `test_launch_with_an_unknown_outcome_never_creates_a_second_agent` |
| Nothing goes on without a signed decision for the exact notice, round, commit and config; forged, replayed and mismatched decisions are refused; accepting launches nothing | `test_nothing_goes_on_without_a_signed_decision_for_the_exact_notice_and_commit` |
| The round limit is hard; only `raise_limit` raises it; redo and resume do not reset it | `test_round_limit_is_a_hard_stop_that_only_the_owner_can_raise` |
| `autonomous_fix` off: every round goes to the gate. On: correction rounds stay inside the stage and stop on pass, limit, no alternative, an open owner item or a failed run | `test_autonomous_fix_is_off_by_default_and_when_on_stays_inside_the_stage` |
| Cross-model round: one count, three launches, blind assessment before comparison, verdict bound to the exact candidate, roles alternate, an interrupted round resumes without a second launch | `test_cross_model_round_counts_once_and_reviews_blind_before_comparing` |
| Pass policies, disagreement policies, limit and no-alternative stops, invalid and failed reviews | `test_cross_model_pass_policies_disagreement_and_reviews_that_cannot_be_used` |
| Output is `AWAITING_REVIEW` only after the local checkout and the host's signed ack; otherwise `LOCAL_SYNC_PENDING` or `NOTICE_PENDING` | `test_output_is_awaiting_review_only_after_the_local_checkout_and_the_hosts_ack` |
| The local checkout (real Git): exact commit; refusal on uncommitted changes, divergence, another orchestrator version, a rewritten branch, a wrong remote | `test_local_review_checkout_shows_the_exact_commit_and_never_forces` |
| Adoption launches nothing, records no agent, never accepts; missing evidence fails; refusals save nothing | `test_adoption_registers_existing_work_for_review_and_launches_nothing`, `test_adoption_of_a_later_stage_needs_the_stage_before_it_accepted` |
| A hand-written or old manifest is refused | `test_manifest_written_by_hand_cannot_stand_in_for_a_launch_a_notice_or_a_decision` |
| Halt, unhalt and a changed configuration (`--repin`) | `test_halt_and_changed_configuration_stop_everything_until_the_owner_acts` |
| Trial journal: append-only, idempotent, written before each step, blocks work when unwritable; repository log checked at each pushed commit; no entry in the committed log | `test_trial_journal_is_append_only_idempotent_and_blocks_work_when_it_cannot_be_written`, `test_trial_log_records_every_round_before_the_next_step_and_survives_interruptions` |
| Evidence checks of Stages 1 to 3 | `test_evidence_checks_of_stages_1_to_3_refuse_gaps_and_do_not_certify_content` |
| Stage 4: domain proposal first, then only approved batches inside the domain and the limit of 25 | `test_stage4_proposes_a_domain_first_and_computes_only_batches_the_owner_approved` |
| Working document rules and the unchanged source document | `test_working_document_rules_are_in_each_prompt_and_checked_at_the_pushed_commit`, `test_source_document_in_docs_is_the_unchanged_original_and_reaches_stage1_whole` |
| Config validation of rounds, evidence, cross-model, delivery and paths | `test_validate_config_checks_round_evidence_and_cross_model_settings`, `test_validate_config_checks_retry_review_path_and_repository_settings` |
| The committed configuration runs through all four stages with a decision at every gate | `test_cli_execute_asks_records_the_document_and_resume_does_not_ask` |

**Not tested: anything against the live services.** No request has been sent to the live Cursor API, no agent has been launched, and no pipeline has run end to end. In particular, these documented behaviours are assumed, not observed:

1. `POST /v1/agents` accepts a client-chosen `agentId` and answers a repeat with `409 agent_id_conflict`.
2. `POST /v1/agents` accepts a commit SHA as `startingRef`.
3. A finished run reports the pushed branch under `git.branches[]`.
4. The artifact listing and download responses have the documented shape.
5. `GET /v1/models` lists the configured model IDs, parameters and values (see [Model Selection](#model-selection)).
6. GitHub's compare endpoint reports `ahead` and lists the changed files as the orchestrator expects.

Also not tested:

- **The chat host.** None exists. No notice has been posted in any chat and no decision has come from one. In real use a round cannot get past `NOTICE_PENDING` until a host is built to the contract in `review_bridge.py`.
- Cross-model review and autonomous correction with real models: whether a reviewer keeps to the blind step, and whether a correction round corrects anything.
- Whether evidence files written by real models are truthful. The checks verify their form and cross-references only.
- Adoption of the real Stage 1 section. Nothing was adopted.
- The orchestrator's own HTTP transport (the `requests` library). The tests inject stand-in transports, and `requests` was not installed where this was built. Three GitHub endpoints (reference, commit, content) were called by hand on this repository with the GitHub command line tool (read-only), and answered in the shape the orchestrator expects; the compare endpoint was not.
- Running on Windows. The tests ran on Linux. The path checks are written for Windows file names but were not run there. The local checkout code was run with Git 2.43 on Linux only; Git for Windows, Windows paths, and a checkout with `core.autocrlf` set were not tried.
- The GitHub contents endpoint as used to read file content (the base64 `content` field). It was exercised against the stand-in only. GitHub sends no content this way for a file over 1 MB; the orchestrator then stops with an error.
- Whether cloud agents follow the working document rules in their prompts. The output check catches a broken rule; it cannot make an agent's mathematics, code or numbers right.

The first real use should therefore be `--check-online`, followed by a watched `--execute` that stops at the Stage 1 gate.

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

# Apply a signed decision of the owner (written by the chat host)
python orchestrator/orchestrator.py --resume pipeline_manifest.json --apply-decision review_bridge\pipeline_manifest\inbox\<id>.decision.json
```

Set `RMB_REVIEW_BRIDGE_SECRET` (the secret shared with the chat host, 16 or more characters) in the environment as well; it is never written in a file of the repository.

Answer the document question with `D:\RMB_AI\docs\intervention-incomplete-information-source.md`. Start from a checkout of the project's starting ref with no uncommitted changes to tracked files; after each stage the checkout is on that stage's branch (see [Local Review](#local-review)).

The manifest file and the `collected_artifacts` folder are created in the folder the command is run from, unless `--output` names another manifest file.

### Optional: Task Scheduler Design

**Note: This describes a possible design. Do NOT install or schedule anything.**

For automated/scheduled runs, Windows Task Scheduler could:
1. Trigger `orchestrator.py` on a schedule or event
2. Pass `--execute` flag only when explicitly configured
3. Log output to a file for review
4. Send notifications on completion/failure

A scheduled task cannot start a new project: `--execute` asks for the model setup document and fails when no one can answer. A scheduled task could only `--resume` an existing project, and a plain `--resume` does not pass a gate: a round that waits for the owner stays there until a signed decision is applied.

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

7. **Review Points**: Settled by Yang Lu: every round of every stage ends at a human gate, the last stage included, and this cannot be switched off in `config.yaml`.

8. **Live Behaviour of the API**: The launch safety, the commit-based handoff and the artifact download rest on documented API behaviour that has not been observed. See [What Has and Has Not Been Tested](#what-has-and-has-not-been-tested).

9. **Committing the Manifest**: The manifest is a local file and the orchestrator does not commit it. Whether a finished project's manifest should be kept in the repository is open.

10. **Stage Output Produced Outside the Pipeline**: `--adopt` registers such work as a round that awaits review (see [Adoption of Existing Work](#adoption-of-existing-work)). The directly written Stage 1 section has not been adopted and is not accepted. It has no audit evidence (`outputs/stage1_audit/`), so an adoption would report failed checks until that evidence is written in a further round.

11. **Where the Approved Final Output Goes**: After the last approval the output is a commit on the last stage's `cursor/...` branch. The orchestrator merges nothing into `main` and opens no pull request (`execution.auto_create_pr` is `false`). Bringing the result into `main` is a separate, manual step.

12. **Local Checkout on Windows**: The Git handling was run on Linux only. See [What Has and Has Not Been Tested](#what-has-and-has-not-been-tested).

13. **The Chat Host**: Who builds and runs the host that posts notices in the main chat and writes acks and decision files, and how it holds the shared secret, is open. The orchestrator side of the contract is implemented; the host side is a reference in `review_bridge.py` only.

14. **Second Model for Cross-Model Review**: None is configured. Enabling it needs a model ID and parameters from `GET /v1/models`, chosen by the owner.

15. **Starting Ref**: `pipeline.repository.starting_ref` is `main`, which does not hold the source document or this orchestrator version until the pull request is merged.

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
| 2026-11-24 | Cloud Agent | Four-stage redesign requested by Yang Lu: equilibrium conditions, pseudocode, Python code, then parameters, computation and results (`parameterization_and_results` replaces `academic_writing`). Models unchanged. One cumulative working document in `docs/` with a section per stage, checked at each pushed commit; the original source document committed unchanged in `docs/`. Every stage now stops for review, the last one included, after its output is checked out in the local checkout; `manual_review` is removed. New `--revise STAGE --feedback FILE`, status `LOCAL_SYNC_PENDING`, Stage 4 run record, manifest format 3. |
| 2026-11-24 | Cloud Agent | Rounds and gates redesign requested by Yang Lu: counted rounds with a hard limit per stage (4/3/4/6), optional `autonomous_fix` and `cross_model` (both off), evidence checks per stage, Stage 4 domain proposal and approved batches, trial journal and `docs/pipeline-trial-log.md`, signed notice / ack / decision files (`NOTICE_PENDING`; the chat host is external and not built), acceptance separate from `start_stage`, `--adopt`, `--halt`, `--repin`, manifest format 4. `--approve`, `--revise`, `--rerun-stage` and the Stage 4 run record are removed. |

---

## Next Steps

1. **Yang Lu: have the model setup / equilibrium definition document ready** as one self-contained `.qmd`, `.tex` or `.md` file; the orchestrator asks for its location
2. **Have the document committed** at `docs/intervention-incomplete-information-source.md` in the starting ref (`main`), together with this version of the orchestrator
3. **Run `--dry-run`** to check the configuration and the document offline
4. **Run `--check-online` with an API key** to check the configured models against the public `GET /v1/models` listing, repository access, the committed source document and the local checkout
5. **Try the local checkout on Windows**: the first stage output shown in `D:\RMB_AI` is also the first run of the Git handling on Windows
6. **Build the chat host** to the contract in `orchestrator/review_bridge.py`; without it no round gets past `NOTICE_PENDING`
7. **Run `--execute` and watch it** up to the Stage 1 gate. This is the first contact with the live API; compare what happens with [What Has and Has Not Been Tested](#what-has-and-has-not-been-tested)
