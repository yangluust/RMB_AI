#!/usr/bin/env python3
"""
Checks of the verification evidence a stage must push with its output.

Each stage writes one JSON evidence file (its path and kind are set in
config.yaml under `verification`). The functions here check that file's
form, its internal consistency and its cross-references to the working
document and to the evidence of earlier stages.

What these checks are, and are not: they show that the stage reported what
it was required to report and that the report is consistent with the
files at the pushed commit. Everything in an evidence file is a statement
by the model that wrote it. A passed check is not a proof that a
derivation, a program or a number is right; that is for the owner's review.

A check yields:
- problems: the candidate fails a required check and cannot be accepted;
- blockers: open items that only the project owner can close (a choice or
  an assumption). They are never closed by a stage on its own;
- no_plausible_alternative: the stage's own statement that it sees no
  other route to try.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

KIND_EQUILIBRIUM_AUDIT = "equilibrium_audit"
KIND_TRACEABILITY = "traceability"
KIND_TEST_MAP = "test_map"
KIND_PARAMETER_BATCHES = "parameter_batches"
KINDS = (KIND_EQUILIBRIUM_AUDIT, KIND_TRACEABILITY, KIND_TEST_MAP, KIND_PARAMETER_BATCHES)

PHASE_DOMAIN_PROPOSAL = "domain_proposal"
PHASE_BATCH = "batch"

CLASSIFICATIONS = ("admissible", "inadmissible", "numerical_failure", "outside_domain")
TAG_PATTERN = re.compile(r"\\tag\{([^}]+)\}")


@dataclass
class EvidenceContext:
    """What a check of one stage's evidence can read."""
    stage_name: str
    round_number: int
    settings: dict
    # Bytes of a file at the pushed commit / at the input commit; None if absent.
    read: Callable[[str], Optional[bytes]]
    read_input: Callable[[str], Optional[bytes]]
    # This stage's section of the working document, and earlier sections by stage number.
    section: str = ""
    earlier_sections: dict = field(default_factory=dict)
    # Parsed evidence of earlier stages at the pushed commit, by kind (None: absent).
    upstream_evidence: dict = field(default_factory=dict)
    # IDs of the owner's decisions recorded for this project.
    decision_ids: frozenset = frozenset()
    # parameter_batches only: PHASE_DOMAIN_PROPOSAL or PHASE_BATCH.
    phase: Optional[str] = None


@dataclass
class EvidenceResult:
    present: bool
    problems: list
    blockers: list
    no_plausible_alternative: bool
    summary: dict

    def to_record(self) -> dict:
        return {
            "present": self.present,
            "problems": self.problems,
            "blockers": self.blockers,
            "no_plausible_alternative": self.no_plausible_alternative,
            "summary": self.summary,
        }


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _items(evidence: dict, key: str, problems: list, required: bool = True) -> list:
    """Return evidence[key] as a list of objects, noting a problem if it is not one."""
    value = evidence.get(key)
    if value is None and not required:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        problems.append(f'"{key}" must be a list of objects')
        return []
    if required and not value:
        problems.append(f'"{key}" must not be empty')
    return value


def _label(value: Any) -> str:
    """A condition label without its parentheses: "(E1)" and "E1" are the same label."""
    return str(value).strip().strip("()").strip()


def _labels_tagged_in(text: str) -> list:
    """The labels of the displayed equations of a section (its \\tag{...} entries), in order."""
    seen = []
    for tag in TAG_PATTERN.findall(text):
        if _label(tag) not in seen:
            seen.append(_label(tag))
    return seen


def _decided(item: dict, decision_ids: frozenset) -> bool:
    return _text(item.get("decision_ref")) and item["decision_ref"] in decision_ids


def _duplicates(values: list) -> list:
    return sorted({value for value in values if values.count(value) > 1})


def load_evidence(content: Optional[bytes], path: str) -> tuple[Optional[dict], list]:
    """Return (the evidence object, problems). No object means it could not be used."""
    if content is None:
        return None, [f"the evidence file {path} is not at the pushed commit"]
    try:
        evidence = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, [f"the evidence file {path} is not valid JSON ({error})"]
    if not isinstance(evidence, dict):
        return None, [f"the evidence file {path} must be a JSON object"]
    return evidence, []


def check_evidence(kind: str, context: EvidenceContext) -> EvidenceResult:
    """Check a stage's evidence file at the pushed commit."""
    path = context.settings["evidence_file"]
    evidence, problems = load_evidence(content=context.read(path), path=path)
    if evidence is None:
        return EvidenceResult(
            present=False, problems=problems, blockers=[],
            no_plausible_alternative=False, summary={"evidence_file": path},
        )
    
    blockers: list = []
    summary: dict = {"evidence_file": path}
    if evidence.get("stage") != context.stage_name:
        problems.append(f'"stage" must be "{context.stage_name}"')
    if evidence.get("round") != context.round_number:
        problems.append(f'"round" must be {context.round_number}, the round that was launched')
    if not isinstance(evidence.get("no_plausible_alternative"), bool):
        problems.append('"no_plausible_alternative" must be true or false')
    attempt = evidence.get("attempt")
    if not (isinstance(attempt, dict) and _text(attempt.get("tried")) and _text(attempt.get("result"))):
        problems.append('"attempt" must be an object with "tried" and "result" texts')
    else:
        summary["attempt"] = {key: " ".join(attempt[key].split()) for key in ("tried", "result")}
    
    checks = {
        KIND_EQUILIBRIUM_AUDIT: _check_equilibrium_audit,
        KIND_TRACEABILITY: _check_traceability,
        KIND_TEST_MAP: _check_test_map,
        KIND_PARAMETER_BATCHES: _check_parameter_batches,
    }
    checks[kind](evidence=evidence, context=context, problems=problems, blockers=blockers, summary=summary)
    return EvidenceResult(
        present=True,
        problems=[f"{path}: {problem}" for problem in problems],
        blockers=blockers,
        no_plausible_alternative=evidence.get("no_plausible_alternative") is True,
        summary=summary,
    )


def _check_equilibrium_audit(
    evidence: dict, context: EvidenceContext, problems: list, blockers: list, summary: dict,
) -> None:
    """Stage 1: equation, unknown, closure and exact-vs-first-order audits."""
    equations = _items(evidence, "equations", problems)
    labels = [_label(equation.get("label")) for equation in equations]
    for duplicate in _duplicates(labels):
        problems.append(f"the equation ({duplicate}) is listed more than once")
    for equation in equations:
        label = _label(equation.get("label"))
        if not label or not _text(equation.get("source")):
            problems.append('every equation needs a "label" and the "source" equation it comes from')
            continue
        if f"({label})" not in context.section and f"\\tag{{{label}}}" not in context.section:
            problems.append(f"the equation ({label}) is not in this stage's section")
        if equation.get("status") not in ("exact", "first_order"):
            problems.append(f'({label}): "status" must be "exact" or "first_order"')
        check = equation.get("check") if isinstance(equation.get("check"), dict) else {}
        result = check.get("result")
        if result == "fail":
            problems.append(f"({label}): the stage reports that its check failed")
        elif result == "not_checked":
            if not _text(check.get("reason")):
                problems.append(f'({label}): a check that was not made needs a "reason"')
        elif result != "pass":
            problems.append(f'({label}): "check.result" must be "pass", "fail" or "not_checked"')
    for tagged in _labels_tagged_in(context.section):
        if tagged not in labels:
            problems.append(f"the displayed equation ({tagged}) of the section is not audited")
    
    unknowns = evidence.get("unknowns")
    residuals = evidence.get("residuals")
    if not (isinstance(unknowns, list) and unknowns and isinstance(residuals, list) and residuals):
        problems.append('"unknowns" and "residuals" must be non-empty lists')
    else:
        if len(unknowns) != len(residuals):
            problems.append(
                f"{len(unknowns)} unknowns but {len(residuals)} residual conditions: "
                f"the counts must be equal"
            )
        for residual in residuals:
            if _label(residual) not in labels:
                problems.append(f"the residual ({_label(residual)}) is not among the audited equations")
        summary["unknowns"] = len(unknowns)
        summary["residuals"] = len(residuals)
    
    closures = _items(evidence, "closures", problems, required=False)
    if "closures" not in evidence:
        problems.append('"closures" must be given (an empty list if there are none)')
    closure_state = {}
    for closure in closures:
        closure_id = str(closure.get("id", "")).strip()
        status = closure.get("status")
        if not closure_id or not _text(closure.get("title")):
            problems.append('every closure needs an "id" and a "title"')
            continue
        closure_state[closure_id] = status
        if status == "open":
            blockers.append(f"open item {closure_id}: {' '.join(closure['title'].split())}")
        elif status in ("resolved", "scoped"):
            if not _decided(closure, context.decision_ids):
                problems.append(
                    f"item {closure_id} is marked {status} without the ID of a recorded "
                    f"decision of the owner (decision_ref); a stage cannot close it itself"
                )
        else:
            problems.append(f'item {closure_id}: "status" must be "open", "resolved" or "scoped"')
    summary["closures_open"] = sum(status == "open" for status in closure_state.values())
    summary["closures_total"] = len(closure_state)
    
    for item in _items(evidence, "order_consistency", problems):
        status = item.get("status")
        name = " ".join(str(item.get("item", "")).split()) or "an unnamed item"
        if status == "consistent":
            continue
        if status not in ("inconsistent", "unresolved"):
            problems.append(f'{name}: "status" must be "consistent", "inconsistent" or "unresolved"')
        elif str(item.get("closure_ref", "")).strip() not in closure_state:
            problems.append(
                f"exact versus first-order: '{name}' is {status} and is not tied to a "
                f"listed open item (closure_ref)"
            )
    
    symbolic = evidence.get("symbolic_checks")
    if not isinstance(symbolic, dict):
        problems.append('"symbolic_checks" must be an object')
    else:
        for key in ("script", "log"):
            if not _text(symbolic.get(key)) or context.read(symbolic[key]) is None:
                problems.append(f'"symbolic_checks.{key}" must name a file at the pushed commit')
        if not (_whole(symbolic.get("passed")) and _whole(symbolic.get("failed"))):
            problems.append('"symbolic_checks.passed" and ".failed" must be whole numbers')
        elif symbolic["failed"] != 0:
            problems.append(f"{symbolic['failed']} symbolic check(s) are reported as failed")
        else:
            summary["symbolic_checks_passed"] = symbolic["passed"]


def _check_traceability(
    evidence: dict, context: EvidenceContext, problems: list, blockers: list, summary: dict,
) -> None:
    """Stage 2: every condition of Stage 1 leads to a step, and every step comes from one."""
    conditions = _items(evidence, "conditions", problems)
    steps = _items(evidence, "steps", problems)
    condition_labels = [_label(condition.get("label")) for condition in conditions]
    step_ids = [str(step.get("id", "")).strip() for step in steps]
    for duplicate in _duplicates(condition_labels):
        problems.append(f"the condition ({duplicate}) is listed more than once")
    for duplicate in _duplicates(step_ids):
        problems.append(f"the step {duplicate} is listed more than once")
    
    for condition in conditions:
        label = _label(condition.get("label"))
        mapped = condition.get("steps")
        if not label or not _text(condition.get("kind")):
            problems.append('every condition needs a "label" and a "kind"')
        elif not (isinstance(mapped, list) and mapped):
            problems.append(f"the condition ({label}) is mapped to no step")
        else:
            for step_id in mapped:
                if str(step_id).strip() not in step_ids:
                    problems.append(f"the condition ({label}) names the step {step_id}, which is not listed")
    for step in steps:
        step_id = str(step.get("id", "")).strip()
        origins = step.get("origins")
        if not step_id:
            problems.append('every step needs an "id"')
        elif step_id not in context.section:
            problems.append(f"the step {step_id} is not in this stage's section")
        elif not (isinstance(origins, list) and origins):
            problems.append(f"the step {step_id} has no origin in the equilibrium conditions")
        else:
            for origin in origins:
                if _label(origin) not in condition_labels:
                    problems.append(f"the step {step_id} names the origin ({_label(origin)}), which is not listed")
    
    required = list(_labels_tagged_in(context.earlier_sections.get(1, "")))
    upstream = context.upstream_evidence.get(KIND_EQUILIBRIUM_AUDIT)
    if isinstance(upstream, dict):
        for equation in upstream.get("equations") or []:
            if isinstance(equation, dict) and _label(equation.get("label")) not in required:
                required.append(_label(equation.get("label")))
        for unknown in upstream.get("unknowns") or []:
            if _label(unknown) not in required:
                required.append(_label(unknown))
    for label in required:
        if label not in condition_labels:
            problems.append(f"({label}) of the equilibrium conditions is mapped to no step")
    summary["conditions_required"] = len(required)
    summary["conditions_mapped"] = len(condition_labels)
    summary["steps"] = len(step_ids)
    
    for block in _items(evidence, "solve_blocks", problems):
        unknowns, residuals = block.get("unknowns"), block.get("residuals")
        name = str(block.get("id", "a solve block")).strip()
        if not (isinstance(unknowns, list) and unknowns and isinstance(residuals, list)):
            problems.append(f'{name}: "unknowns" and "residuals" must be lists, with at least one unknown')
        elif len(unknowns) != len(residuals):
            problems.append(
                f"{name} solves for {len(unknowns)} unknowns with {len(residuals)} residuals: "
                f"the counts must be equal"
            )
    
    for choice in _items(evidence, "open_choices", problems, required=False):
        choice_id = str(choice.get("id", "")).strip() or "an unnamed choice"
        status = choice.get("status")
        if status == "open":
            blockers.append(f"open choice {choice_id}: {' '.join(str(choice.get('title', '')).split())}")
        elif status == "resolved":
            if not _decided(choice, context.decision_ids):
                problems.append(
                    f"choice {choice_id} is marked resolved without the ID of a recorded "
                    f"decision of the owner (decision_ref)"
                )
        elif status != "named_input":
            problems.append(f'choice {choice_id}: "status" must be "open", "named_input" or "resolved"')
    if "open_choices" not in evidence:
        problems.append('"open_choices" must be given (an empty list if there are none)')


def _defined_names(source: Optional[bytes]) -> set:
    """The names defined with `def` in a Python source file."""
    if source is None:
        return set()
    return set(re.findall(r"^\s*def\s+([A-Za-z_]\w*)\s*\(", source.decode("utf-8", errors="replace"), re.MULTILINE))


def _check_test_map(
    evidence: dict, context: EvidenceContext, problems: list, blockers: list, summary: dict,
) -> None:
    """Stage 3: every pseudocode step has a function and tests; the tests were run."""
    settings = context.settings
    functions = _defined_names(context.read(settings["implementation_file"]))
    tests = _defined_names(context.read(settings["test_file"]))
    
    steps = _items(evidence, "steps", problems)
    step_ids = [str(step.get("id", "")).strip() for step in steps]
    mapped_tests = set()
    for step in steps:
        step_id = str(step.get("id", "")).strip() or "an unnamed step"
        for key, defined, where in (
            ("functions", functions, settings["implementation_file"]),
            ("tests", tests, settings["test_file"]),
        ):
            names = step.get(key)
            if not (isinstance(names, list) and names):
                problems.append(f'the step {step_id} has no entry in "{key}"')
                continue
            for name in names:
                if name not in defined:
                    problems.append(f"the step {step_id} names '{name}', which is not defined in {where}")
            if key == "tests":
                mapped_tests.update(names)
    
    upstream = context.upstream_evidence.get(KIND_TRACEABILITY)
    if not isinstance(upstream, dict):
        problems.append(
            "the pseudocode's evidence file is not at the pushed commit, so the "
            "coverage of its steps cannot be checked"
        )
    else:
        for step in upstream.get("steps") or []:
            if isinstance(step, dict) and str(step.get("id", "")).strip() not in step_ids:
                problems.append(f"the pseudocode step {step.get('id')} is mapped to no function and test")
    summary["steps_mapped"] = len(step_ids)
    
    tolerances = _items(evidence, "tolerances", problems)
    tolerance_names = [str(tolerance.get("name", "")).strip() for tolerance in tolerances]
    for tolerance in tolerances:
        if not (_text(tolerance.get("name")) and _text(tolerance.get("config_key"))):
            problems.append(
                'every tolerance needs a "name" and the "config_key" it is read from '
                "(a tolerance is a named configuration entry, not a built-in value)"
            )
    for key in ("residual_checks", "admissibility_checks"):
        for check in _items(evidence, key, problems):
            name = str(check.get("name", "")).strip() or "an unnamed check"
            if check.get("tolerance") not in tolerance_names:
                problems.append(f'{key}: {name} names no listed tolerance')
            if check.get("test") not in tests:
                problems.append(f"{key}: {name} names no test defined in {settings['test_file']}")
    
    run = evidence.get("test_run")
    if not isinstance(run, dict):
        problems.append('"test_run" must be an object')
    else:
        if not _text(run.get("command")):
            problems.append('"test_run.command" must be the exact command that was run')
        if not _text(run.get("log")) or context.read(run["log"]) is None:
            problems.append('"test_run.log" must name the printed test output, a file at the pushed commit')
        if not all(_whole(run.get(key)) for key in ("exit_code", "passed", "failed")):
            problems.append('"test_run.exit_code", ".passed" and ".failed" must be whole numbers')
        else:
            if run["exit_code"] != 0 or run["failed"] != 0:
                problems.append(
                    f"the stage reports a failing test run (exit code {run['exit_code']}, "
                    f"{run['failed']} failed)"
                )
            if run["passed"] < 1:
                problems.append("the stage reports no passing test")
            summary["tests_passed"] = run["passed"]
            summary["tests_failed"] = run["failed"]
        for skipped in _items(run, "skipped", problems, required=False):
            if not (_text(skipped.get("test")) and _text(skipped.get("reason"))):
                problems.append('every skipped test needs its "test" name and a "reason"')
    
    for deviation in _items(evidence, "deviations", problems, required=False):
        step = str(deviation.get("step", "")).strip() or "an unnamed step"
        if not _decided(deviation, context.decision_ids):
            blockers.append(
                f"deviation from the pseudocode at {step}: "
                f"{' '.join(str(deviation.get('description', '')).split())}"
            )
    if "deviations" not in evidence:
        problems.append('"deviations" must be given (an empty list if there are none)')


def batch_spec_problems(spec: Any, max_configurations: int) -> list:
    """Check the form of a proposed parameter batch."""
    if not isinstance(spec, dict):
        return ["it must be a JSON object"]
    problems = []
    domain = spec.get("domain")
    if not (isinstance(domain, dict) and domain):
        problems.append('"domain" must be a non-empty object, one entry per parameter')
    else:
        for name, entry in domain.items():
            if not isinstance(entry, dict):
                problems.append(f"domain of {name}: must be an object")
                continue
            is_range = (
                _number(entry.get("min")) and _number(entry.get("max")) and entry["min"] <= entry["max"]
            )
            is_set = isinstance(entry.get("values"), list) and bool(entry["values"])
            if is_range == is_set:
                problems.append(f'domain of {name}: give either "min" and "max", or "values"')
            if entry.get("origin") not in ("source", "illustrative"):
                problems.append(f'domain of {name}: "origin" must be "source" or "illustrative"')
            if not _text(entry.get("reason")):
                problems.append(f'domain of {name}: a "reason" is required')
    limit = spec.get("max_configurations")
    if not (_whole(limit) and 1 <= limit <= max_configurations):
        problems.append(f'"max_configurations" must be a whole number from 1 to {max_configurations}')
    seeds = spec.get("seeds")
    if not (isinstance(seeds, list) and seeds and all(_whole(seed) for seed in seeds)):
        problems.append('"seeds" must be a non-empty list of whole numbers')
    solver = spec.get("solver")
    if not (
        isinstance(solver, dict) and _number(solver.get("residual_tolerance"))
        and _whole(solver.get("max_iterations"))
    ):
        problems.append('"solver" must give "residual_tolerance" (a number) and "max_iterations" (a whole number)')
    return problems


def batch_file_problems(content: bytes, path: str, limit: int) -> list:
    """Problems of a batch file's content, each naming the file."""
    spec, problems = load_evidence(content=content, path=path)
    if spec is None:
        return [problem.replace("evidence file", "batch file") for problem in problems]
    return [f"{path}: {problem}" for problem in batch_spec_problems(spec, limit)]


def _outside_domain(parameters: Any, domain: dict) -> list:
    """Reasons why a configuration's parameters are not inside a domain."""
    if not isinstance(parameters, dict):
        return ['its parameters file has no "parameters" object']
    reasons = []
    for name in sorted(set(parameters) - set(domain)):
        reasons.append(f"{name} is not in the approved domain")
    for name, entry in domain.items():
        if name not in parameters:
            reasons.append(f"{name} is not set")
        elif "values" in entry:
            if parameters[name] not in entry["values"]:
                reasons.append(f"{name} = {parameters[name]!r} is not one of the approved values")
        elif not (_number(parameters[name]) and entry["min"] <= parameters[name] <= entry["max"]):
            reasons.append(f"{name} = {parameters[name]!r} is outside [{entry['min']}, {entry['max']}]")
    return reasons


def _check_parameter_batches(
    evidence: dict, context: EvidenceContext, problems: list, blockers: list, summary: dict,
) -> None:
    """Stage 4: first a domain proposal that computes nothing, then one approved batch per round."""
    settings = context.settings
    limit = settings["max_configurations"]
    next_spec_path = settings["next_batch_spec"]
    summary["phase"] = context.phase
    if evidence.get("phase") != context.phase:
        problems.append(f'"phase" must be "{context.phase}" in this round')
        return
    
    next_spec_content = context.read(next_spec_path)
    proposes_next = next_spec_content is not None and (
        context.phase == PHASE_DOMAIN_PROPOSAL or next_spec_content != context.read_input(next_spec_path)
    )
    spec_problems: list = []
    if proposes_next:
        spec_problems = batch_file_problems(content=next_spec_content, path=next_spec_path, limit=limit)
        problems.extend(spec_problems)
    summary["proposes_next_batch"] = proposes_next and not spec_problems
    
    if context.phase == PHASE_DOMAIN_PROPOSAL:
        if next_spec_content is None:
            problems.append(f"the proposed first batch {next_spec_path} is not at the pushed commit")
        if evidence.get("configurations"):
            problems.append("a domain proposal computes nothing: \"configurations\" must be empty")
        return
    
    approved = context.read_input(next_spec_path)
    if approved is None:
        problems.append(f"no approved batch {next_spec_path} is at the input commit")
        return
    approved_problems = batch_file_problems(content=approved, path=next_spec_path, limit=limit)
    if approved_problems:
        problems.extend(f"at the input commit, {problem}" for problem in approved_problems)
        return
    approved_sha256 = hashlib.sha256(approved).hexdigest()
    spec = json.loads(approved.decode("utf-8"))
    batch_spec_file = evidence.get("batch_spec_file")
    if evidence.get("approved_spec_sha256") != approved_sha256:
        problems.append('"approved_spec_sha256" is not the SHA-256 of the batch the owner approved')
    if not _text(batch_spec_file) or context.read(batch_spec_file) != approved:
        problems.append(
            '"batch_spec_file" must name a copy of the approved batch, unchanged, at the pushed commit'
        )
    
    configurations = _items(evidence, "configurations", problems)
    if len(configurations) > min(limit, spec["max_configurations"]):
        problems.append(
            f"{len(configurations)} configurations were run; the approved batch allows "
            f"{min(limit, spec['max_configurations'])}"
        )
    classes: dict = {}
    for configuration in configurations:
        name = str(configuration.get("id", "")).strip()
        classification = configuration.get("classification")
        if not name:
            problems.append('every configuration needs an "id"')
            continue
        if name in classes:
            problems.append(f"the configuration {name} is listed more than once")
        classes[name] = classification
        if classification not in CLASSIFICATIONS:
            problems.append(f'{name}: "classification" must be one of {", ".join(CLASSIFICATIONS)}')
        if classification == "inadmissible" and not _text(configuration.get("violated_condition")):
            problems.append(f'{name}: an inadmissible solution must name the "violated_condition"')
        if classification == "numerical_failure" and not _text(configuration.get("failure_reason")):
            problems.append(f'{name}: a numerical failure needs a "failure_reason"')
        parameters_content = (
            context.read(configuration["parameters_file"])
            if _text(configuration.get("parameters_file")) else None
        )
        if parameters_content is None:
            problems.append(f'{name}: "parameters_file" must name a file at the pushed commit')
        else:
            try:
                parameters = json.loads(parameters_content.decode("utf-8")).get("parameters")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                parameters = None
            outside = _outside_domain(parameters=parameters, domain=spec["domain"])
            if outside and classification != "outside_domain":
                problems.append(f"{name} is outside the approved domain: {'; '.join(outside)}")
        if classification != "outside_domain" and (
            not _text(configuration.get("results_file"))
            or context.read(configuration["results_file"]) is None
        ):
            problems.append(f'{name}: "results_file" must name a file at the pushed commit')
    summary["configurations"] = len(classes)
    summary["by_classification"] = {
        classification: sum(value == classification for value in classes.values())
        for classification in CLASSIFICATIONS
    }
    
    for claim in _items(evidence, "boundary_claims", problems, required=False):
        name = " ".join(str(claim.get("claim", "")).split()) or "an unnamed claim"
        if claim.get("status") != "evidence_not_proof":
            problems.append(
                f'boundary claim "{name}": "status" must be "evidence_not_proof"; a '
                f"computed boundary is evidence, not a proof that no equilibrium exists"
            )
        inside = [classes.get(item) for item in claim.get("admissible") or []]
        beyond = [classes.get(item) for item in claim.get("inadmissible") or []]
        if not inside or not beyond or any(value != "admissible" for value in inside) or any(
            value != "inadmissible" for value in beyond
        ):
            problems.append(
                f'boundary claim "{name}" must name at least one configuration classified '
                f"admissible and one classified inadmissible; a numerical failure is not "
                f"evidence of a boundary"
            )
        reproduced = claim.get("reproduced_with")
        if not (isinstance(reproduced, list) and reproduced and all(item in classes for item in reproduced)):
            problems.append(
                f'boundary claim "{name}" needs "reproduced_with": the configurations that '
                f"repeat it from another starting value or with a tighter tolerance"
            )
    if "boundary_claims" not in evidence:
        problems.append('"boundary_claims" must be given (an empty list if there are none)')


def check_comparison(
    comparison: Any,
    candidate_commit: str,
    candidate_files: dict,
    blind_commit: Optional[str],
    blind_sha256: Optional[str],
) -> tuple[list, dict]:
    """Check a cross-model reviewer's comparison file.

    Args:
        candidate_files: path -> SHA-256 of the candidate's files, as the
            orchestrator read them at the candidate commit.

    Returns:
        (problems, verdict). The verdict holds "accept" (the reviewer
        judges that the candidate meets the requirements), "agree" (the
        reviewer's blind assessment and the candidate reach the same
        results), "blocking_differences" and "plausible_alternative".
    """
    verdict = {"accept": False, "agree": False, "blocking_differences": [], "plausible_alternative": True}
    if not isinstance(comparison, dict):
        return ["the comparison must be a JSON object"], verdict
    problems = []
    if comparison.get("candidate_commit") != candidate_commit:
        problems.append(
            f'"candidate_commit" must be {candidate_commit}, the candidate that was reviewed'
        )
    if comparison.get("candidate_files") != candidate_files:
        problems.append(
            '"candidate_files" must give the SHA-256 of each reviewed file as it is at the '
            "candidate commit; the verdict holds for exactly those files"
        )
    if comparison.get("blind_commit") != blind_commit or comparison.get("blind_assessment_sha256") != blind_sha256:
        problems.append(
            '"blind_commit" and "blind_assessment_sha256" must identify the blind assessment '
            "as it was committed before the comparison"
        )
    if comparison.get("verdict") not in ("accept", "reject"):
        problems.append('"verdict" must be "accept" or "reject"')
    if comparison.get("agreement") not in ("agree", "disagree"):
        problems.append('"agreement" must be "agree" or "disagree"')
    if not isinstance(comparison.get("plausible_alternative"), bool):
        problems.append('"plausible_alternative" must be true or false')
    differences = comparison.get("differences")
    if not isinstance(differences, list) or not all(isinstance(item, dict) for item in differences):
        problems.append('"differences" must be a list of objects (empty if there are none)')
        differences = []
    blocking = []
    for difference in differences:
        if difference.get("severity") not in ("blocking", "minor") or not _text(difference.get("item")):
            problems.append('every difference needs an "item" and a "severity" ("blocking" or "minor")')
        elif difference["severity"] == "blocking":
            blocking.append(" ".join(difference["item"].split()))
    if not _text(comparison.get("rationale")):
        problems.append('"rationale" must say, from the evidence, why the verdict was reached')
    if comparison.get("verdict") == "accept" and blocking:
        problems.append("the verdict is accept although blocking differences are listed")
    if comparison.get("agreement") == "agree" and blocking:
        problems.append("the agreement is agree although blocking differences are listed")
    if comparison.get("agreement") == "disagree" and not differences:
        problems.append("the agreement is disagree but no difference is listed")
    verdict = {
        "accept": comparison.get("verdict") == "accept" and not problems,
        "agree": comparison.get("agreement") == "agree" and not problems,
        "blocking_differences": blocking,
        "plausible_alternative": comparison.get("plausible_alternative") is not False,
        "rationale": " ".join(str(comparison.get("rationale", "")).split()),
    }
    return problems, verdict


COMMON_INSTRUCTIONS = """\
Write the file as one JSON object. These entries are required for every stage:
- "stage": this stage's name; "round": the round number given above.
- "attempt": {"tried": what this attempt did, "result": what its checks showed}.
- "no_plausible_alternative": true only if the required checks cannot be met and you
  see no other route to try without a decision of the project owner; otherwise false.
Report what you found. A check that fails is reported as failed; a failed check that is
reported honestly is useful, a check reported as passed that was not made is not."""

EVIDENCE_INSTRUCTIONS = {
    KIND_EQUILIBRIUM_AUDIT: """\
- "equations": one object for every displayed, labelled equation of your section:
  {"label", "source" (the equation of the source document it comes from),
   "status" ("exact" or "first_order"),
   "check": {"method", "result" ("pass", "fail" or "not_checked"), "reason" (required
   when not_checked)}}.
- "unknowns" and "residuals": the unknowns of a period and the labels of the residual
  conditions that determine them. The two counts must be equal.
- "closures": one object for every item the document leaves open:
  {"id", "title", "kind" ("owner_choice", "missing_input" or "note"),
   "status" ("open", "resolved" or "scoped"), "decision_ref"}.
  An item is "resolved" or "scoped" only by a decision of the project owner; give that
  decision's ID as "decision_ref". Without one the status is "open". Never close an item
  by choosing an assumption yourself.
- "order_consistency": for each place where exact and first-order statements meet:
  {"item", "status" ("consistent", "inconsistent" or "unresolved"), "closure_ref" (the
  closure ID it belongs to, required unless consistent)}.
- "symbolic_checks": {"script", "log", "passed", "failed"}: a script that checks the
  algebra symbolically (no parameter values, no solver) and its printed output, both
  committed in this stage's evidence folder.""",
    KIND_TRACEABILITY: """\
- "conditions": one object for every equilibrium condition, unknown, timing step,
  expectation, admissibility, boundary and terminal condition of Stage 1:
  {"label", "kind", "steps": [IDs of the pseudocode steps that use it]}.
  Every labelled equation of the Stage 1 section must be listed.
- "steps": one object for every pseudocode step: {"id" (as written in your section),
  "origins": [labels of the conditions it comes from]}.
- "solve_blocks": [{"id", "unknowns": [...], "residuals": [...]}], with equal counts.
- "open_choices": [{"id", "title", "status" ("open", "named_input" or "resolved"),
  "decision_ref"}]. "resolved" needs the ID of a decision of the project owner.""",
    KIND_TEST_MAP: """\
- "steps": one object for every pseudocode step of Stage 2:
  {"id", "functions": [names defined in the implementation], "tests": [test names]}.
- "tolerances": [{"name", "config_key"}]: every tolerance is read from the configuration
  file; none is built in.
- "residual_checks" and "admissibility_checks": [{"name", "tolerance", "test"}].
- "test_run": {"command", "exit_code", "passed", "failed",
  "skipped": [{"test", "reason"}], "log" (the printed output, committed)}.
- "deviations": [{"step", "description", "decision_ref"}]: every place where the code
  departs from the pseudocode (an empty list if none).""",
    KIND_PARAMETER_BATCHES: """\
- "phase": "domain_proposal" or "batch", as this round is.
- A domain proposal computes nothing. It writes the proposed first batch (see the path
  below) and gives "configurations": [].
- The batch file: {"domain": {parameter: {"min", "max"} or {"values": [...]}, each with
  "origin" ("source" or "illustrative") and "reason"}, "max_configurations", "seeds",
  "solver": {"residual_tolerance", "max_iterations"}}.
- A batch round runs the batch the owner approved, unchanged:
  "approved_spec_sha256", "batch_spec_file" (your copy of the approved batch),
  "configurations": one object for EVERY run, failed runs included:
  {"id", "parameters_file", "results_file", "classification", "violated_condition"
  (if inadmissible), "failure_reason" (if numerical_failure)}.
  Classifications: "admissible"; "inadmissible" (the solver converged and an
  admissibility condition fails); "numerical_failure" (no convergence or an error: this
  says nothing about whether an equilibrium exists); "outside_domain".
- "boundary_claims": [{"claim", "status": "evidence_not_proof", "admissible": [IDs],
  "inadmissible": [IDs], "reproduced_with": [IDs of runs that repeat it from another
  starting value or with a tighter tolerance]}].""",
}
