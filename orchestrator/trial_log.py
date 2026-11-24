#!/usr/bin/env python3
"""
Trial log: the record of every round and every launch of a project.

Two files hold it.

1. The journal, a local file beside the project manifest
   (`<manifest name>.trial-journal.jsonl`). The orchestrator appends one
   JSON line per event and flushes it to disk before it takes the next
   step. It is written by the orchestrator only, so it does not depend on
   anything a model writes, and it also holds launches that failed or were
   interrupted. If an event cannot be written, the orchestrator stops.

2. The trial log in the repository (`docs/pipeline-trial-log.md`), which
   is read by people. A stage's agent appends to it, in the commit it
   pushes: first the orchestrator's entries that are not in the file yet,
   word for word as given in its prompt, then one entry of its own. The
   output check verifies that the file only grew, that the orchestrator's
   entries are there unchanged and that no other entry was added.

Events have stable IDs, so recording the same event twice (after an
interruption) adds nothing, and the same ID with other facts is an error.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

DISPOSITIONS = ("KEEP", "ABANDON", "UNRESOLVED")
# The lines an agent's own entry must have, each with text after the colon.
REPORT_FIELDS = ("Tried", "Evidence", "Result", "Disposition", "Rationale", "Next route")
ENTRY_HEADING = re.compile(r"^### (\S+)[ \t]*$", re.MULTILINE)

LOG_HEADER = """\
# Pipeline trial log

Append-only record of every round of every stage of the project whose working document
is [`equilibrium-working-document.md`](equilibrium-working-document.md): what was tried,
with which model in which role, from which commit, with what evidence and result, whether
the candidate was kept, abandoned or left unresolved, and why the next step was chosen.

- Entries "Recorded by: orchestrator" are copied word for word from the orchestrator's
  local journal. Entries "Recorded by: agent" are the report of the agent that made the
  attempt; they are that agent's statements, not checked facts.
- Checks named here are checks of form and of cross-references. A passed check is not a
  proof that the mathematics, the code or a number is right.
- Nothing is removed or rewritten. A stage that changes or deletes an earlier entry fails
  its output check.

Entries follow below, oldest first. Each begins with a line `### <event ID>`.
"""


class TrialLogError(Exception):
    """The trial journal could not be read or written, or contradicts itself."""


def event_id(project_id: str, stage_number: int, round_number: int, kind: str) -> str:
    """The stable ID of an event: the same event always gets the same ID."""
    return f"{project_id}-s{stage_number}-r{round_number:02d}-{kind}"


def _one_line(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        text = "; ".join(_one_line(item) for item in value) or "none"
    elif isinstance(value, dict):
        text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    elif value is None:
        text = "none"
    else:
        text = str(value)
    return " ".join(text.split())


def render_entry(event: dict) -> str:
    """Return an event as the Markdown entry of the repository trial log."""
    lines = [
        f"### {event['event_id']}",
        "",
        "- Recorded by: orchestrator",
        f"- Time: {event['at']}",
        f"- Stage: {event['stage_number']} ({event['stage']}), round {event['round']}",
        f"- Event: {event['kind']}",
    ]
    lines.extend(
        f"- {label}: {_one_line(value)}" for label, value in event["facts"].items()
    )
    return "\n".join(lines) + "\n"


class TrialJournal:
    """The orchestrator's own append-only record of a project's events."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def events(self) -> list[dict]:
        """Return the recorded events, oldest first.

        Raises:
            TrialLogError: if the journal cannot be read. A missing file is
                an empty journal.
        """
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_bytes().decode("utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as error:
            raise TrialLogError(f"The trial journal {self.path} could not be read: {error}") from error
        events = []
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                event["event_id"], event["facts"], event["at"]
            except (json.JSONDecodeError, KeyError, TypeError) as error:
                raise TrialLogError(
                    f"Line {number} of the trial journal {self.path} is not a "
                    f"recorded event ({error}). The journal is never repaired "
                    f"automatically."
                ) from error
            events.append(event)
        return events

    def find(self, event_id: str) -> Optional[dict]:
        """Return the recorded event with this ID, or None."""
        for event in self.events():
            if event["event_id"] == event_id:
                return event
        return None

    def record(self, event: dict) -> bool:
        """Append an event and flush it to disk. Recording it again adds nothing.

        Returns:
            True if the event was appended, False if it was already there.

        Raises:
            TrialLogError: if an event with this ID and other facts is
                already recorded, or the journal cannot be written.
        """
        for earlier in self.events():
            if earlier["event_id"] == event["event_id"]:
                same = all(
                    earlier.get(key) == event.get(key)
                    for key in ("project_id", "stage", "round", "kind", "facts")
                )
                if not same:
                    raise TrialLogError(
                        f"The trial journal {self.path} already holds event "
                        f"{event['event_id']} with other facts. An event is never "
                        f"rewritten."
                    )
                return False
        line = json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab") as handle:
                handle.write(line.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise TrialLogError(
                f"The trial journal {self.path} could not be written: {error}. "
                f"Nothing further is done until it can be."
            ) from error
        return True


def entry_ids(log_text: str) -> list[str]:
    """The event IDs of the entries in a trial log text, in order."""
    return ENTRY_HEADING.findall(log_text)


def entries_missing_from(log_text: str, events: list[dict]) -> list[dict]:
    """The journal events that have no entry in a trial log text, oldest first."""
    present = set(entry_ids(log_text))
    return [event for event in events if event["event_id"] not in present]


def _normalized(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))


def repository_log_problems(
    before: Optional[bytes],
    after: Optional[bytes],
    path: str,
    expected_entries: list[str],
    report_id: Optional[str],
) -> list[str]:
    """Check the repository trial log at a commit an agent pushed.

    Args:
        before: The file at the commit the agent started from (None: no file).
        after: The file at the commit the agent pushed (None: no file).
        expected_entries: The orchestrator's entries the agent was given to
            append, in order, as render_entry() wrote them.
        report_id: The ID of the entry the agent had to add itself, or None
            when the agent had to add none.

    Returns:
        The problems found; none means the file only grew, by exactly the
        given entries and the agent's own entry.
    """
    if after is None:
        return [f"the trial log {path} is not at the pushed commit"]
    before = before or b""
    if not after.startswith(before):
        return [
            f"the trial log {path} was changed or shortened: what it held at the "
            f"input commit must stay as it is, and new entries are added at the end"
        ]
    try:
        appended = _normalized(after[len(before):].decode("utf-8"))
        earlier_ids = set(entry_ids(before.decode("utf-8")))
    except UnicodeDecodeError as error:
        return [f"the trial log {path} is not UTF-8 text ({error})"]
    
    problems = []
    position = 0
    for entry in expected_entries:
        entry_id = entry_ids(entry)[0]
        found = appended.find(_normalized(entry).rstrip("\n"), position)
        if found < 0:
            problems.append(
                f"the trial log {path} does not hold the orchestrator's entry "
                f"{entry_id} word for word, in the order given"
            )
        else:
            position = found + len(_normalized(entry).rstrip("\n"))
    
    expected_ids = [entry_ids(entry)[0] for entry in expected_entries]
    allowed_ids = expected_ids + ([report_id] if report_id else [])
    added_ids = entry_ids(appended)
    for added_id in sorted(set(added_ids)):
        if added_id in earlier_ids or added_ids.count(added_id) > 1:
            problems.append(f"the trial log {path} holds the entry {added_id} more than once")
        elif added_id not in allowed_ids:
            problems.append(
                f"the trial log {path} holds an entry {added_id} that the "
                f"orchestrator did not give and that is not this attempt's own entry"
            )
    
    if report_id:
        problems.extend(_report_problems(appended=appended, path=path, report_id=report_id))
    return problems


def _report_problems(appended: str, path: str, report_id: str) -> list[str]:
    """Check the form of the agent's own entry."""
    match = re.search(rf"^### {re.escape(report_id)}[ \t]*$", appended, re.MULTILINE)
    if match is None:
        return [f"the trial log {path} has no entry '### {report_id}' for this attempt"]
    following = ENTRY_HEADING.search(appended, match.end())
    body = appended[match.end():following.start() if following else len(appended)]
    problems = []
    for label in REPORT_FIELDS:
        field = re.search(rf"^- {re.escape(label)}:[ \t]*(\S.*)$", body, re.MULTILINE)
        if field is None:
            problems.append(
                f"the entry {report_id} in {path} has no line '- {label}: ...' with text"
            )
        elif label == "Disposition" and field.group(1).strip() not in DISPOSITIONS:
            problems.append(
                f"the entry {report_id} in {path} has the disposition "
                f"{field.group(1).strip()!r}; it must be one of {', '.join(DISPOSITIONS)}"
            )
    return problems


def report_template(report_id: str, role: str, model: str) -> str:
    """The entry an agent is asked to add for its own attempt."""
    return "\n".join([
        f"### {report_id}",
        "",
        f"- Recorded by: agent ({role}, model {model})",
        "- Tried: <what you did in this attempt, in one or two sentences>",
        "- Evidence: <paths of the evidence files you wrote>",
        "- Result: <what the checks you ran showed; say which failed>",
        f"- Disposition: <one of {', '.join(DISPOSITIONS)}>",
        "- Rationale: <why, from the evidence; no private reasoning notes>",
        "- Next route: <what should be done next, and why>",
    ]) + "\n"
