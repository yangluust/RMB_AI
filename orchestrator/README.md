# Research Pipeline Orchestrator

Local Windows orchestrator that runs a four-stage research pipeline on Cursor Cloud Agents: (1) derive the equilibrium as implementable mathematical conditions, (2) translate them to pseudocode, (3) translate the pseudocode to Python code, (4) propose a parameter domain and run approved batches of configurations. All four stages write into one working document, [`docs/equilibrium-working-document.md`](../docs/equilibrium-working-document.md).

**Status.** Tested offline only: against stand-ins for the Cursor API, GitHub and the chat host, and (for the local checkout) against throw-away Git repositories on Linux. Nothing has run against the live API or on Windows, and no agent has been launched by it. **The chat host that posts notices and returns the owner's decisions is outside this repository and does not exist yet** (see [Notices and Decisions](#notices-and-decisions)); until it does, every round stops at `NOTICE_PENDING`.

## How a Stage Runs

A stage runs in **rounds**. Each round is counted, before any work, against the stage's hard limit (`iteration.max_rounds`: 4, 3, 4, 6 for Stages 1 to 4). The limit is not reset by a redo or a resume; only the owner's `raise_limit` decision raises it.

1. **Launch** one agent from an exact commit (three launches with [cross-model review](#cross-model-review)). The agent's ID is recorded before the request, so a resume never launches it twice. A failed launch or run ends the round; nothing is relaunched automatically.
2. **Check** the pushed commit: required files, working-document rules, unchanged source document, changed paths, the trial log, and the stage's [evidence file](#evidence).
3. **Close the round**, then either start one more round of the same stage (only with `autonomous_fix: true`, see below) or go to the gate.
4. **Gate**: check the output commit out in the local checkout, write a signed notice, wait for the host's signed ack. Only then is the round `AWAITING_REVIEW`.
5. **Decision** of the owner, signed and bound to that notice, round, commit and config. Nothing else moves the project on.

Accepting a round launches nothing. The next stage starts only on a separate `start_stage` decision. No stage is skipped and no gate can be switched off.

**The checks are checks of form and cross-references.** Evidence files are statements of the model that wrote them. A passed check is not a proof that the mathematics, the code or a number is right; that is what the owner's review is for.

## Quick Start (Windows)

```powershell
$env:CURSOR_API_KEY = "your-api-key"
$env:RMB_REVIEW_BRIDGE_SECRET = "the secret shared with the chat host"   # 16+ characters, never in a file
cd D:\RMB_AI

python orchestrator/orchestrator.py --dry-run        # offline; asks for the document; launches nothing
python orchestrator/orchestrator.py --check-online   # API key, models, GitHub, local checkout, secret; launches nothing
python orchestrator/orchestrator.py --execute        # new project: asks for the document, runs round 1 of Stage 1
python orchestrator/orchestrator.py --resume pipeline_manifest.json                                # go on from the record
python orchestrator/orchestrator.py --resume pipeline_manifest.json --apply-decision <file.decision.json>
```

`--dry-run` and `--execute` ask for the model setup document; answer with `D:\RMB_AI\docs\intervention-incomplete-information-source.md`. Run from a checkout with no uncommitted changes to tracked files, whose orchestrator files are those of the commit the project works on.

## Commands

| Command | Can launch | What it does |
|---------|------------|--------------|
| `--dry-run` | No | Offline. Checks the config, asks for the document, builds every prompt. Saves nothing. |
| `--check-online` | No | Checks the API key, the models (`GET /v1/models`, the second model of an enabled cross-model review included), the starting ref, the committed source document, the local checkout and the bridge secret. |
| `--execute` | Yes | Starts a new project and runs the first stage's first round to its gate. |
| `--resume M` | Yes | Continues from the manifest. Never repeats a recorded launch, never starts a stage, never passes a gate. |
| `--resume M --apply-decision F` | Yes | Applies one signed decision of the owner (table below). |
| `--adopt STAGE --adopt-branch B --adopt-commit SHA --adopt-input-commit SHA [--adopt-content-commit SHA]` | No | [Adoption](#adoption): registers existing work as round 1 of a stage. Alone: new project, first stage. With `--resume M`: the next stage. |
| `--resume M --halt REASON` / `--unhalt` | No | Halts the project (no launch, no decision applied) / ends the halt. |
| `--resume M --repin` | No | Records a changed config for the project. Open notices are written again and must be answered again. Refused while a round is under way. |
| `--config F`, `--output F` | | Another config file; the manifest file of a new project. |

Exit codes: `0` all stages accepted (or a check passed), `2` waiting for the owner, `3` a notice waits for the host's ack, `1` anything else.

Removed: `--approve`, `--approve-commit`, `--revise`, `--feedback`, `--revise-commit`, `--rerun-stage`. A console command is no longer a decision.

## Decisions

| Decision | Allowed when | Effect |
|----------|--------------|--------|
| `accept` | The round passed every check, has no open item, the reviewer (if any) accepted, and the branch has not moved | Stage accepted. Launches nothing. |
| `start_stage` | Answers the acceptance notice of the stage before | Counts and launches round 1 of the next stage. |
| `iterate` (`feedback`, optional `base_commit`, `approve_batch`) | The round has an output commit and the limit is not reached | New round from the reviewed commit (or a commit the owner pushed to its branch), with the feedback. `approve_batch: true` approves the Stage 4 batch that the round proposed. |
| `redo` | The limit is not reached | New round from the stage's input. |
| `raise_limit` (`by`) | The limit is reached | Raises the stage's limit for this project. Launches nothing. |
| `halt` (`reason`) | At every round's gate | Halts the project until `--unhalt`; the round is then announced again. |

A round that failed a check, has an open owner item, or ended in disagreement cannot be accepted.

## Notices and Decisions

The orchestrator holds no chat or mail credentials and sends nothing. It writes files (`review_bridge.py`), next to the manifest:

```
review_bridge/<manifest name>/outbox/<event_id>.json             signed notice (orchestrator)
review_bridge/<manifest name>/inbox/<event_id>.ack.json          signed ack (host, after posting in the main chat)
review_bridge/<manifest name>/inbox/<decision_id>.decision.json  signed decision (host, from the owner's written message)
```

- Records are signed with HMAC-SHA256 using the secret in the environment variable named by `review.delivery.secret_env`. The secret is never written in the config.
- A notice names the project, stage, round, stop reason, the commits, the checks' result, the local and GitHub paths of the output and the trial log, and the decisions it allows, each with a token.
- No valid ack: the round stays `NOTICE_PENDING`. Console output is never delivery.
- A decision is applied only if its signature, token, notice ID and notice content hash, project, round, commit and config hash all match the open notice. The same decision again does nothing; anything else is refused.
- Before a notice is written, the output commit must be verified in the local checkout. A dirty, diverged or wrong-remote checkout, or another orchestrator version at that commit, leaves the round `LOCAL_SYNC_PENDING` with no notice.

**The host is not part of this repository.** It must: verify each notice, deduplicate by ID *and* content hash, post it in the main chat, write the ack, and turn the owner's written reply into a decision file. `build_ack`, `build_decision` and `HostReceiver` in `review_bridge.py` are its reference; the tests use them as an offline stand-in (`FakeHost`). Anyone who holds the secret can sign a decision, so the secret belongs to the host and the owner only.

## Configuration

Per stage, in `config.yaml` (stage models are unchanged):

```yaml
iteration:
  max_rounds: 4            # hard limit; required
  autonomous_fix: false    # default
verification:
  kind: "equilibrium_audit"                      # or traceability, test_map, parameter_batches
  evidence_file: "outputs/stage1_audit/audit.json"
cross_model:
  enabled: false           # default
  secondary_model_id: null
  secondary_model_params: []
  pass_policy: "both_agree"          # or reviewer_accept
  on_disagreement: "human_review"    # or revise_next_round
  review_dir: "outputs/stage1_review"
output_manifest:
  required_files: [...]
  allowed_paths: [...]     # with the working document, the evidence, the review folder and the trial log: all a round may change
```

Top level: `trial_log.path` (`docs/pipeline-trial-log.md`) and `review.delivery` (`directory`, `secret_env`). The rest (`pipeline.repository`, `working_document`, `review.local_checkout`, `api`, `artifacts`) is explained in `config.yaml`. The model setup document, the working-document rules, the Stage 3 guardrails and the model check are described in [`docs/pipeline-design.md`](../docs/pipeline-design.md).

### Autonomous fix

`autonomous_fix: false` (default): every round goes to the gate. `true`: after a round whose checks failed, the orchestrator starts a correction round of the same stage by itself, until a round passes with no open item, the limit is reached, or the stage reports `no_plausible_alternative`. Then the gate, always. It never starts another stage, never repeats a failed launch, and stops at once for an item only the owner can decide. The correction prompt forbids inventing assumptions, changing the model and weakening checks. Not allowed for Stage 4, where every batch needs the owner's approval.

### Cross-model review

Off in every stage; no second model is configured. When enabled (two different models required), a round is one counted iteration with three launches:

1. **Propose**: the proposer writes the candidate.
2. **Blind assessment**: the reviewer starts from the round's input commit with the same task and criteria, is not shown the candidate, and commits only `review_dir/round_NN/blind_assessment.md`.
3. **Comparison**: launched from the blind assessment's commit, merges the exact candidate commit unchanged and writes `comparison.json` (accept or reject, agree or disagree, blocking differences).

`both_agree` needs acceptance and agreement; `reviewer_accept` needs acceptance. Both need every stage check. The verdict is bound to the candidate's file hashes; a later commit on the branch cannot be accepted. On disagreement: `human_review` goes to the gate; `revise_next_round` (needs `autonomous_fix: true`) starts the next counted round with a reconciliation prompt. The two models change roles every round. Two models are not statistically independent.

Illustration only, not active configuration: `secondary_model_id: "<a model ID from GET /v1/models>"`, `secondary_model_params: [{id: "<parameter>", value: "<value>"}]`.

### Evidence

| Stage | `kind` | The evidence file must show |
|-------|--------|-----------------------------|
| 1 | `equilibrium_audit` | Every `\tag{}` equation of the section audited with its source, exact or first-order status and check result; as many residuals as unknowns; closures (open, or closed by a recorded decision of the owner); exact-versus-first-order consistency; the symbolic check script and log |
| 2 | `traceability` | Every Stage 1 condition and unknown mapped to a step, every step to a condition; solve blocks with equal counts; open choices |
| 3 | `test_map` | Every pseudocode step mapped to functions and tests that exist in the code; named tolerances read from configuration; residual and admissibility checks; the saved test output with no failure |
| 4 | `parameter_batches` | Round 1: a domain proposal only, nothing computed, cannot be accepted. Later rounds: one batch the owner approved (`iterate` with `approve_batch`), at most 25 configurations inside the approved domain, every run counted and classified |

Stage 4 classifies every run: `admissible`, `inadmissible` (names the violated condition), `numerical_failure` (names the reason) or `outside_domain` (counted, no result). A numerical failure is not evidence of a boundary. A boundary claim must name admissible and inadmissible runs and runs that reproduce it, and carries the status `evidence_not_proof`: it is not a proof that no equilibrium exists. An open item is reported to the owner and blocks acceptance; a stage cannot close it itself.

## Trial Log

- **Journal** (`<manifest name>.trial-journal.jsonl`, local): written and flushed by the orchestrator before each next step: round counted, launch, check, failure, round closed (result, KEEP / ABANDON / UNRESOLVED, rationale, next route), notice acknowledged, decision, adoption, halt, repin. It does not depend on model output. Events have stable IDs; a repeated step adds nothing. If it cannot be written, nothing further is done.
- **Repository log** ([`docs/pipeline-trial-log.md`](../docs/pipeline-trial-log.md)): each launched agent appends the journal entries not yet in it, word for word, and one entry of its own. The output check fails if the file was changed, entries are missing or other entries were added. It holds evidence-based summaries, not private reasoning. It has no entries yet: no stage has run.

## Adoption

For work that is already on a branch, such as the directly written Stage 1:

```powershell
python orchestrator/orchestrator.py --adopt equilibrium_derivation --adopt-branch <branch> `
    --adopt-commit <branch head> --adopt-input-commit <commit the work started from> `
    --adopt-content-commit <commit that holds the stage's files>
```

The adopted commit must be the branch head and build on the input and content commits; the stage's own files must be identical at the content and adopted commits. It is checked like an agent's candidate, counted as round 1 with origin `adopted`, and recorded with no agent, run or artifact. Missing evidence is a failed check. It then goes to the gate and can only become `AWAITING_REVIEW`; adoption accepts nothing and is never followed by an automatic round. A refused adoption saves nothing. **Nothing has been adopted.**

## Manifest

Format version 4; older manifests are refused, not converted. It records the project ID, the config and code hashes (`provenance`), every round (`stages`, `previous_rounds`) with its launches, prompts' hashes, commits, checks, notice, ack and decision. On resume each status is checked against the signed records that must have led to it, so a hand-written manifest cannot stand in for a launch, a notice or a decision. A changed config is refused until `--repin`. The hashes of the orchestrator's files are recorded with the project; the local checkout refuses an output commit that holds another orchestrator version than the one running.

## Statuses

| Status | Meaning | A plain `--resume M` |
|--------|---------|----------------------|
| `BLOCKED` | Nothing launched (local checkout or bridge not ready, no document) | Tries again |
| `LAUNCHING` / `RUNNING` | Agent may exist / run not ended | Looks the agent up / waits; never a second agent |
| `RUN_FINISHED` | Output could not be checked yet (GitHub unreadable) | Checks again |
| `ARTIFACTS_FAILED` | Artifact download failed | Downloads again |
| `LOCAL_SYNC_PENDING` | Output not in the local checkout; no notice written | Tries the checkout again |
| `NOTICE_PENDING` | Notice written, no ack from the host | Looks for the ack |
| `AWAITING_REVIEW` | Notice acknowledged; waiting for a decision | Nothing |
| `COMPLETE` | Accepted by the owner | Nothing; the next stage needs `start_stage` |

## Tests

```powershell
python orchestrator/test_orchestrator.py
```

Offline: stand-ins for the Cursor API, GitHub, the download host and the chat host; real Git only on throw-away repositories. The stand-in agents write files of the required form with no content: no test derives, programs or computes anything of the model. What is and is not tested: [design document](../docs/pipeline-design.md#what-has-and-has-not-been-tested).

## Official Documentation

- [Cloud Agents API](https://cursor.com/docs/cloud-agent/api/endpoints), [authentication](https://cursor.com/docs/api), [OpenAPI spec](https://cursor.com/docs-static/cloud-agents-openapi.yaml)
- GitHub REST API: [reference](https://docs.github.com/en/rest/git/refs#get-a-reference), [commit](https://docs.github.com/en/rest/git/commits#get-a-commit-object), [content](https://docs.github.com/en/rest/repos/contents#get-repository-content), [compare two commits](https://docs.github.com/en/rest/commits/commits#compare-two-commits)
