# Local checks — brave-goggles

This repository owns its checker, commands and hook installation. No shared
workspace configuration, Actions runner, daemon or hook-framework dependency is
required. POSIX Python 3.10+ (Linux/macOS) and Git run the hooks; application dependencies and runtime
versions follow the root README and AGENTS.md.

```sh
python3 .local-checks/run.py install  # once per clone; also after another tool changes core.hooksPath
git local-checks doctor              # check hook registration and local prerequisites
git local-checks plan                # show full commands without executing them
git local-checks quick               # staged whitespace / quick checks
git local-checks full                # configured local verification
```

`pre-commit` runs the quick gate. `pre-push` runs the full gate before existing
hooks (including Git LFS); any failure aborts the push. Push checks require a
clean worktree and every non-deletion ref to point to its HEAD, so another branch
or uncommitted code cannot be mistaken for the pushed version. Push other branches
from their own worktrees. Ordinary quick/manual checks inspect the current
worktree and do not claim to test an isolated staged snapshot.

Git stores clone-specific registration and the original hook path/alias under
its common directory (`local-checks/record.json`). Existing executable hooks are
forwarded with their arguments, exit status and relevant stdin intact. Registered
worktrees share hook installation; each uses its own `.local-checks/config.json`
when present. Older worktrees use the installing checkout's configuration while
executing against their own source. Missing tools/commands fail explicitly.
Re-run installation from a durable checkout before removing the installing
worktree. Git intentionally does not activate hooks merely by cloning a repository.

The `config.json` command lists are the project contract. Keep commands non-watch
and non-autofixing. Dependency installation is explicit; uv runs do not sync
implicitly. Tests/builds can produce normal project outputs; operational services,
mail, credentials and production data must never be used as test fixtures.

## Full gate

- Git whitespace checks; no application test suite is configured.

Generated artifacts only: whitespace gate; use the owning melly-v2 plugin for semantic validation and generation.

Additional integration commands, when configured, run with
`git local-checks integration`; they are separate from the full gate and must be
run for the changes described in AGENTS.md. This does not reproduce an upstream
multi-OS/version CI matrix or automatic deployment/release jobs.

GitHub Actions is managed per remote repository in Settings → Actions → General.
Existing YAML is retained as historical command/matrix reference; do not enable
hosted workflows as a prerequisite for local development. Local hooks cannot
guard GitHub UI merges or another developer's unconfigured clone. Record local
validation in PR descriptions instead of claiming a remote check passed.

## Maintenance and rollback

```sh
python3 -m unittest discover -s .local-checks -p 'test_*.py'
python3 .local-checks/run.py uninstall
```

Uninstall restores the original clone-local hook path and alias. It preserves
source files and original hooks. Git's explicit `--no-verify` can bypass hooks;
agents must not use it to hide failures and must report any user-authorized bypass.
