# Instructions for AI assistants working in this repository

These are working rules set by the project owner, Yang Lu. They apply to every
assistant session in this repository, including sessions held by voice.

## Questions to the owner go in the main chat, in writing

Whenever you ask the owner a question about the project, also write the question
clearly in the main conversation chat (the written transcript of the session).

- This holds when the question is asked by voice during a call: say it, and write it
  in the chat as well.
- Write the full question, with the options if there are any, so that it can be
  understood and answered from the chat alone.
- A question put only in a tool call, a code comment, a commit message, a pull request
  or a file in the repository does not meet this rule.

## Project documents

- `docs/intervention-incomplete-information-source.md` is the owner's original source
  document. Never change it. `.gitattributes` keeps Git from converting its line endings.
- `docs/equilibrium-working-document.md` is the single cumulative working document. Each
  stage's section lies between its `BEGIN STAGE n` and `END STAGE n` marker lines. Do not
  edit a section the owner has approved, and do not add content for a stage that has not
  been carried out.
- Each stage stops for the owner's review. Do not begin the next stage before the owner
  has approved the one before it.
- The design, the commands and what has and has not been tested are in
  `docs/pipeline-design.md` and `orchestrator/README.md`.
