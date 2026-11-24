# Research Pipeline Orchestrator

Local Windows orchestrator for running a multi-stage research pipeline on Cursor Cloud Agents.

## Overview

This orchestrator:
- Calls the **Cursor Public Cloud Agents API** to run each pipeline stage, one cloud agent per stage
- Sets a **model per stage** and checks it against `GET /v1/models` before any launch
- **Asks for the model setup / equilibrium definition document** (`.qmd`, `.tex` or `.md`) at every new project, before anything is launched
- Starts each stage from an **exact commit**: the project's source commit, then the commit that passed the previous stage's output check
- **Checks each stage's output** at the pushed commit: required files, the sections of the cumulative working document, and the last stage's run record
- **Downloads artifacts**, then **checks the output out in your local checkout**, at exactly the checked commit, so the working document can be read there
- **Stops for your review after every stage**, the last one included. Nothing advances until you approve that exact commit; `--revise` sends your feedback to a new agent and brings the revised output back for review
- Records every step in a **manifest file**, so a project can be resumed without launching any agent twice
- Launches agents only with `--execute` or `--resume`

The four stages: (1) derive the equilibrium as implementable mathematical conditions, (2) translate them to pseudocode, (3) translate the pseudocode to Python code, (4) determine a parameter configuration, run the computation and report the results. All four write into one working document, [`docs/equilibrium-working-document.md`](../docs/equilibrium-working-document.md).

**Status:** tested against stand-ins for the Cursor API and GitHub only, and (for the local checkout) against throw-away Git repositories on Linux. No agent has been launched, nothing has run against the live API, and nothing has run on Windows. See [What Has and Has Not Been Tested](../docs/pipeline-design.md#what-has-and-has-not-been-tested).

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

# 6. After reviewing a stage's output in D:\RMB_AI: approve it and go on to the next stage
python orchestrator/orchestrator.py --resume pipeline_manifest.json --approve equilibrium_derivation

# 6b. Or have it revised: write your feedback in a text file, then
python orchestrator/orchestrator.py --resume pipeline_manifest.json --revise equilibrium_derivation --feedback D:\notes\stage1_feedback.txt
```

Steps 3 and 5 each ask:

```
Location of the model setup / equilibrium definition document for this project (.qmd, .tex or .md):
```

Type the path of the committed copy of the document: `D:\RMB_AI\docs\intervention-incomplete-information-source.md`. The document you select must be the same file (same SHA-256) as the copy committed at the starting ref; `--execute` stops before any launch if it is not.

Step 5 runs Stage 1, checks its output out in `D:\RMB_AI` (on the stage's own branch), prints `READY FOR REVIEW` and stops (exit code `2`). Each `--approve` launches the next stage, which stops for review in the same way. The project is finished only when the fourth stage is approved (exit code `0`).

Run the commands from a checkout of `main` with no uncommitted changes to tracked files. The orchestrator's own files must be the same in your checkout as in the commit the project starts from; otherwise a stage output is not checked out (see [Local Review](#local-review)).

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
      completion_record: "outputs/my_stage/run_record.json"  # Optional, see below
```

Other settings: `pipeline.repository` (a github.com URL and the starting ref), `working_document` (the cumulative working document and the committed source document), `review` (the local checkout and its remote; required), `api.polling`, `api.retries` (required), `artifacts`, `execution.auto_create_pr` and `manifest.output_file`. Each is explained in `config.yaml`.

There is no setting that switches the review of a stage off. A config that still has a `manual_review` line is refused.

### Working Document

All four stages write into one file, `docs/equilibrium-working-document.md`. Each stage writes its section between two marker lines, `<!-- BEGIN STAGE n: name -->` and `<!-- END STAGE n: name -->`. The orchestrator appends the exact marker lines and the rules to each stage's prompt, and checks the same rules at the commit the stage pushes:

- the stage's own section is there, once, and is not empty;
- the sections of earlier stages are byte-for-byte as they were in the commit the stage started from (the approved version);
- no marker of a later stage is there, so the document never holds content for a stage that has not been carried out;
- the committed source document, `docs/intervention-incomplete-information-source.md`, is byte-for-byte unchanged.

Stage 3 must also push the Python code and its tests (`outputs/stage3_implementation/solve_equilibrium.py`, `test_solve_equilibrium.py`, `requirements.txt`, `README.md`). Stage 4 must push `outputs/stage4_results/run_record.json`, its own record of the run: `"status": "completed"` with the command, exit code `0`, the parameters and results files (which must exist), `converged` and `max_abs_residual`; or `"status": "blocked"` with the reason. The record is printed in the review notice as *reported by the stage*.

These checks are about form. A stage that passes them has delivered the files and sections asked for. Whether a derivation, a program or a number is right is not checked by the orchestrator; that is what the review after each stage is for.

### Guardrails

A stage may list `guardrails`. When the orchestrator launches that stage it appends the list, numbered, to the stage prompt, so the agent receives the guardrails in the launch request.

Stage 3 (`pseudocode_to_implementation`) carries the twelve reproducibility guardrails from [`docs/pipeline-design.md`](../docs/pipeline-design.md#reproducibility-guardrails-stage-3). The tests fail if the list in `config.yaml` differs from the list in the design document, so edit both together.

### Model Setup Document (Asked at Every New Project)

Every new project starts by asking for the location of the model setup / equilibrium definition document. This is mandatory: there is no setting, flag or environment variable that supplies the location, and an earlier project's document is never reused.

- The file must exist, be a `.qmd` (Quarto Markdown), `.tex` (LaTeX) or `.md` (Markdown) file, hold non-empty UTF-8 text, and be self-contained (no `\input`, `\include` or Quarto `{{< include >}}` of other files). The extension is matched without regard to case; `.markdown` and `.Rmd` are not accepted.
- A `.md` file is checked for the Quarto `{{< include >}}` shortcode and for the MyST `{include}` and `{literalinclude}` directives. Markdown has no include syntax of its own, and the include forms of other Markdown tools are not detected.
- If the answer is blank or unusable, or no answer can be read (for example under a scheduler), the run stops with an error and nothing is launched.
- The file is only read. It is not changed, moved, copied or committed by the orchestrator.
- A copy of the document must be committed in the repository at `working_document.source_document` (`docs/intervention-incomplete-information-source.md`), at the starting ref. The first stage is sent the document you select; the later stages read the committed copy. `--check-online` and `--execute` stop if the copy is missing; `--execute` also stops if the document you select is not the same file (same SHA-256). `.gitattributes` keeps Git from converting that file's line endings.
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
| 4. Parameters, Computation and Results | `gpt-5.6-sol` | `reasoning` = `high` |

These values come from tool smoke tests, not from the public REST API. They have **not** been checked against the public `GET /v1/models` listing. Before any launch (and in `--check-online`) the orchestrator calls `GET /v1/models` and stops if a model ID, parameter or value is not listed, showing what the API lists instead. It never substitutes another model or drops a parameter. See [Model Selection](../docs/pipeline-design.md#model-selection) in the design document.

To see the listing yourself:

```bash
curl https://api.cursor.com/v1/models -u YOUR_API_KEY:
```

Write `model_params` values as quoted strings. A stage with `model_params` must also set `model_id`. Setting `model_id: null` with no `model_params` uses your configured default model.

## Commands

| Command | Network | Can launch | Description |
|---------|---------|------------|-------------|
| `--dry-run` | No | No | Check the config, ask for the model setup document, print what each stage would do. Needs no API key. Runs no Git command. Saves nothing. Carries out no stage: nothing is derived, written or computed. |
| `--check-online` | Yes | No | Check the API key, the models (`GET /v1/models`), the starting ref and the committed source document on GitHub, and that the local checkout can receive stage outputs. |
| `--execute` | Yes | Yes | Start a new project: ask for the document, run the online checks, then run the first stage up to its review. |
| `--resume MANIFEST` | Yes | Yes | Continue an existing project. Does not ask. Never launches a second agent for a stage that already has one, and never goes past a review. |
| `--resume MANIFEST --approve STAGE` | Yes | Yes | Approve the reviewed output of a stage, then run the next stage up to its review. The local checkout must be at the approved commit, with no uncommitted changes. |
| `... --approve STAGE --approve-commit SHA` | Yes | Yes | Approve a commit pushed to the stage's branch after the output check (for example your own review edits). It must pass the output check too. |
| `--resume MANIFEST --revise STAGE --feedback FILE` | Yes | Yes | Launch a **new paid agent** that starts from the reviewed output and revises it as the text in `FILE` asks. The revised output comes back for review. |
| `... --revise STAGE --feedback FILE --revise-commit SHA` | Yes | Yes | The same, starting from a commit you pushed to the stage's branch. |
| `--resume MANIFEST --rerun-stage STAGE` | Yes | Yes | Launch a **new paid agent**, from the stage's original input, for a stage that failed or whose output is rejected at review. |
| `--config path/to/config.yaml` | | | Use another config file. |
| `--output path/to/manifest.json` | | | Manifest file for a new project (default `pipeline_manifest.json`). Not allowed with `--resume`. |

Exit codes: `0` every stage approved (or a check passed), `2` paused for review, `1` anything else.

## What Happens in a Stage

1. **Launch.** The orchestrator chooses the agent's ID, writes it to the manifest, then sends the launch request with `startingRef` set to an exact commit.
2. **Wait.** It polls the run until it ends.
3. **Output check.** It reads the head commit of the branch the agent pushed and requires a new commit that holds every file in `required_files` and keeps the [working document](#working-document) rules. That commit becomes the stage's output.
4. **Artifacts.** If `artifacts.collect` is `true`, it downloads the agent's artifacts to `collected_artifacts/<manifest name>/<stage>/attempt_<n>/`. Unsafe paths are refused and recorded.
5. **Local checkout.** It checks the stage's branch out in your local checkout, at exactly the output commit, and verifies that the checkout is at that commit and the files to review are there.
6. **Review.** Only then it prints `READY FOR REVIEW` and stops. The next stage starts from the commit you approve.

The commits and files are read through the GitHub REST API (read-only). `yangluust/RMB_AI` is public, so no token is needed; for a private repository set `GITHUB_TOKEN`.

## Local Review

Each stage's agent pushes its own branch (`cursor/...`). A `git pull` of `main` would not show it. After the output check, the orchestrator runs these Git commands in the checkout named by `review.local_checkout` (`D:\RMB_AI`):

1. `git fetch origin refs/heads/<branch>:refs/remotes/origin/<branch>` (no `+`: a remote branch that was rewritten is refused);
2. `git checkout -b <branch> <output commit>` if you have no local branch of that name, otherwise `git checkout <branch>` and `git merge --ff-only <output commit>`;
3. it then reads back that `HEAD` is the output commit, that no tracked file is changed and that the files to review exist.

So after Stage 1 your checkout is on the Stage 1 branch, at the checked commit, and `D:\RMB_AI\docs\equilibrium-working-document.md` holds Section 1. `main` is not touched; `git checkout main` takes you back. Nothing is merged into `main` at any point, including after the last approval.

It never uses `--force`, `reset`, `stash` or a merge other than a fast-forward, and it does nothing in these cases. The stage then stays `LOCAL_SYNC_PENDING`, is **not** announced as ready, and `--resume` tries again without launching anything:

| Case | What to do |
|------|------------|
| Tracked files have uncommitted changes | Commit them (and push them, if they are review edits to hand on) or restore them yourself |
| Your local branch of that name has commits of your own on top of the output | Push them, and name the pushed commit with `--approve-commit` or `--revise-commit` |
| Your local branch of that name has diverged from the output | Rename your branch (`git branch -m`) |
| The output commit holds another version of `orchestrator.py`, `api_client.py` or `config.yaml` than your checkout | Bring your checkout to the orchestrator version the project was started from. The running orchestrator is never replaced by a checkout |
| A merge or rebase is under way, the remote is another repository, or the fetch fails | Fix that first |

The same check of the checkout is made before every launch, so a paid run is not started while its output could not be shown afterwards. Untracked files (the manifest, `collected_artifacts/`) are left where they are.

**Your review edits.** If you edit the working document while reviewing, the edits are yours and are never overwritten:

- To hand them on to the next stage: commit them on the stage's branch, `git push origin <branch>`, then `--approve STAGE --approve-commit <SHA>`. The pushed commit must pass the output check (you cannot change an earlier, approved section this way).
- To have the agent continue from them: commit, push, then `--revise STAGE --feedback FILE --revise-commit <SHA>`.
- To give feedback only: put it in a text file outside the tracked files and use `--revise STAGE --feedback FILE`.

`--approve` and `--revise` stop with instructions while tracked files have uncommitted changes.

The notice is printed in the console only. The orchestrator sends no message through any other service.

## After a Stop

The summary printed at the end names each stage's status and the command that continues it.

| Status | Meaning | To continue |
|--------|---------|-------------|
| `AWAITING_REVIEW` | Output checked and in your local checkout; waiting for you | `--resume M --approve STAGE`, or `--revise STAGE --feedback FILE`, or `--rerun-stage STAGE` to discard it |
| `LOCAL_SYNC_PENDING` | Output checked on GitHub but not in your local checkout; not ready for review | Fix the cause printed, then `--resume M` (no agent is launched) |
| `BLOCKED` | Nothing was launched (no document, or the local checkout cannot receive the output) | Fix the cause printed, then `--resume M` |
| `LAUNCHING` | Not known whether the agent was created | `--resume M` (looks the agent up first; never creates a second one) |
| `LAUNCH_REJECTED` | The API refused the launch; no agent exists | Fix the cause, then `--resume M` |
| `RUNNING` | The run had not ended when polling stopped | `--resume M` (waits for the same run) |
| `RUN_FAILED` | The run ended with an error | `--resume M --rerun-stage STAGE` (new paid agent) |
| `GATE_FAILED` | A required file is missing, a working document rule is broken, the run record is malformed, or nothing was pushed | `--resume M` checks again; or `--rerun-stage STAGE` |
| `ARTIFACTS_FAILED` | Artifacts could not be downloaded | `--resume M` (downloads again) |

A plain `--resume` never launches a second agent for a stage and never goes past a review. Only `--revise` and `--rerun-stage` launch a stage again; both keep the replaced attempt's record under `previous_attempts` in the manifest. Details: [Failure Handling and Resume](../docs/pipeline-design.md#failure-handling-and-resume).

## Important: Repository Snapshots

**Cloud agents execute against committed code on GitHub, not your local working directory.**

The model setup document reaches Stage 1 in the launch request: the orchestrator reads it from your machine and sends its text. Its committed copy in `docs/` is what the later stages read.

Before running:
1. Commit your input data: `git add . && git commit -m "Add inputs"`
2. Push to GitHub: `git push origin main`
3. Then run the orchestrator

Local uncommitted changes are **invisible** to cloud agents.

When a project is started, the orchestrator records the commit that `starting_ref` points at. Stage 1 starts from that commit, even if the branch moves later.

## Tests

Run the tests. They use stand-ins for the Cursor API, GitHub and the download host, make no network call and launch nothing. The tests of the local checkout run real Git commands on throw-away repositories in a temporary folder (Git must be installed); the other tests use a stand-in for the local checkout:

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
