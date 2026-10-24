[Yordamchi] New project {{name}}: write its plan from the owner's description below. This turn plans only; the build starts after the owner approves the plan.

This folder is new and empty apart from git. Follow the owner's workflow for a new project from his global CLAUDE.md, which this session loads:
1. `docs/REQUIREMENTS.md`: the goal, the users, the use cases with acceptance criteria, what is out of scope, open questions.
2. `docs/STACK.md`: two or three real stack options with their trade-offs and a marked recommendation. The stack is the owner's choice: ask it with AskUserQuestion (one question, the options as choices, the recommendation first) and wait for his answer before the SPEC.
3. `docs/SPEC.md` for the chosen stack: architecture, data model, modules, phases, and how each phase is checked done.
4. `README.md` (what this is, in a few lines) and a `.gitignore` for the stack.

Rules for this turn:
- Write only under `docs/`, `README.md` and `.gitignore`. No source code, no scaffolding, no installs, no tests yet: anything else is refused in this session.
- Write the documents in English.
- Commit the documents and push them. `origin` is the project's private GitHub repository; if it is missing, create it private under the logged-in `gh` account.
- Your `Natija:` line is the plan summary the owner approves from his phone: what will be built, on which stack, in which phases.

The owner's description (his own words, possibly several messages):

{{description}}
