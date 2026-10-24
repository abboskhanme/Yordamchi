{{request}}

[Yordamchi] A self-update: the owner asked for the change above in Yordamchi itself. You are working in Yordamchi's own
working copy, on the branch {{branch}}, which was cut from main for this change.

- Stay on {{branch}}. Never switch to, merge into or push main: the supervisor tests your commit, switches to it and
  merges it only if the new version comes back healthy.
- Follow CLAUDE.md and docs/CONVENTIONS.md. Behaviour goes into settings, bot text into core/strings.py, prompts into
  prompts/**. Never delete, skip or weaken a test, an eval case or tests/eval/thresholds.toml: a release with fewer
  collected tests is refused.
- Change either supervisor/ or the bots, never both in one update. If the request needs both, do the bot part and
  say in the result that the supervisor part is a separate request.
- Run `make check` until it is green, then commit your work on {{branch}} (push the branch only if a remote exists).
- End the turn with the usual Natija and Testlar lines. Natija is what the owner will read before the update is
  applied: say in one or two plain sentences what changes for them.
