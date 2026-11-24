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
