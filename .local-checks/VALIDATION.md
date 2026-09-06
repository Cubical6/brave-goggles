# Configuration validation — 2026-09-06

- Nine real-Git hook regression tests pass for this checkout: failed checks,
  legacy hook stdin/exit propagation, repeated installation, dirty-tree rejection,
  worktree-specific commands, actual push blocking, staged whitespace rejection,
  source mutation rejection, annotated tags and rollback are covered across them.
- New Python files pass syntax and Ruff checks; command JSON parses successfully.
- Hook registration is active in this repository's local Git configuration.
- GitHub Actions disabled and verified through the repository API.
- Application full gate was not executed during this configuration change.

## Application prerequisites

The doctor found the declared local files/executables. This does not verify
all runtime versions, browser binaries, image builds or service availability.

Run `git local-checks doctor` for current status and `git local-checks full`
for current application results. Missing dependencies and failing checks abort
the gate; they are never treated as successful validation.
