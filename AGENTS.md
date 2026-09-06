# Repository Guidelines

## Project Overview

This repository publishes curated [Brave Goggles](https://search.brave.com/help/goggles) filters for developer documentation. The tracked deliverables are generated `.goggle` files; the source tooling, build scripts, rules, and tests live in the external `melly-v2` plugin at `plugins/brave-goggles/scripts/`.

## Architecture & Data Flow

- Each `.goggle` file is a standalone Brave Search ruleset.
- Rules are declarative and evaluated top-to-bottom by Brave Search. Most files begin with `$discard` (whitelist mode), then add targeted `$boost=<number>,site=<domain>` or path-scoped boost rules.
- Category goggles provide reusable technology coverage, such as `dev-docs.goggle`, `js-ts.goggle`, `frontend.goggle`, `laravel.goggle`, `cakephp.goggle`, and `python.goggle`.
- Project goggles specialize the same pattern for named projects, for example `daisy-cms.goggle`, `creditable.goggle`, `queue_cms.goggle`, and `sip-standalone-cms.goggle`.
- Generated output is consumed directly through raw GitHub URLs; for example:
  `https://raw.githubusercontent.com/Cubical6/brave-goggles/main/dev-docs.goggle`.

Do not add application code or local generators here. Changes to generation logic belong in the linked `melly-v2` plugin.

## Key Directories

- Repository root: generated `.goggle` artifacts, `README.md`, and `.gitignore`.
- No local `src/`, `tests/`, scripts, package manifest, or build directory is present.

## Development Commands

There is no local build, run, lint, or test command in this repository. Use the external `melly-v2` plugin for generation and validation workflows documented there.

For a consumer smoke check, open a published raw URL in Brave Search or fetch the raw file from GitHub. Do not invent or add package-manager commands to this artifact-only repository.

## Code Conventions & Common Patterns

- Keep files as valid Brave Goggles text, not Markdown or JSON.
- Preserve the metadata header pattern:
  ```text
  ! name: <name>
  ! description: <description>
  ! public: false
  ! author: Cubical6
  ```
- Organize rules with `! === Section ===` headings. Keep general resources, security, infrastructure, language/framework, integrations, testing, and generated dependency sections distinct.
- Use `$discard` as the first filtering rule for whitelist goggles. Add explicit exceptions or boosts afterward.
- Site rules use `$boost=<integer>,site=<domain>`; path-specific rules put the path expression before the action, such as `/docs/$boost=3,site=redis.io`.
- Keep boost values and domain/path scope intentional. Avoid broadening a whitelist or adding duplicate rules without evidence that the ranking behavior requires it.
- Preserve generated dependency sections and their `! === ... (auto-generated) ===` markers. Do not hand-edit generated dependency lists when the plugin can regenerate them.
- Use lowercase kebab-case for category names and lowercase project identifiers matching the `! name` value and filename, except where an existing project name requires a different established spelling.

## Important Files

- `README.md`: repository purpose, ownership boundary, and raw URL usage.
- `dev-docs.goggle`: broad curated developer-documentation whitelist and the canonical example artifact.
- `js-ts.goggle`, `frontend.goggle`, `python.goggle`, `laravel.goggle`, `cakephp.goggle`: category-level rule sets.
- `brave-search-mcp.goggle`, `core-worktree.goggle`, `creditable.goggle`, `daisy-cms.goggle`, `daisy-sip-client.goggle`, `evalue8_portal_v3_worktree.goggle`, `melly.goggle`, `nw-core.goggle`, `opensignage_cms.goggle`, `queue_cms.goggle`, `queue_desk_client_v2.goggle`, `queue_kiosk_client.goggle`, `queue_socket_server.goggle`, `ralph-claude-code.goggle`, `sip-standalone-cms.goggle`: project-specific generated goggles.
- `.gitignore`: intentionally ignores everything except `.goggle`, `README.md`, and itself. New repository artifacts require an explicit allowlist update.

## Runtime/Tooling Preferences

This repository has no runtime or package manager. Treat the `.goggle` files as generated release artifacts and use the `melly-v2` plugin's documented tooling for regeneration. Avoid introducing Node, Python, PHP, or other runtime dependencies here merely to validate text.

## Testing & QA

There is no local test suite or coverage configuration. Before changing an artifact:

1. Confirm the target file's metadata, whitelist mode, section ordering, and generated markers match neighboring goggles.
2. Check that new domains and path expressions are narrowly scoped and use the intended boost value.
3. Use the external plugin's validation/build workflow when changing generated output or generation inputs.
4. Smoke-test the resulting raw URL in Brave Search when the change affects filtering behavior.

Do not treat a successful text edit as proof that Brave Search ranking behavior is correct; validate the published goggle through the actual consumer when practical.

<!-- repository-local-checks:start -->
## Local verification policy

- This repository uses its own `.local-checks/` scripts and `config.json`; do not
  introduce GitHub-hosted CI as a required development or merge step.
- At the start of work, run `git local-checks doctor`. If the alias is missing or
  another tool replaced the hook path, run `python3 .local-checks/run.py install`
  from a checkout containing these files, then repeat the doctor check. Recheck
  after dependency installation that installs hooks (Sail/Composer/Husky/Make).
- Commits trigger the quick gate; pushes trigger the full gate. Use
  `git local-checks plan` to inspect commands and `git local-checks full` before
  delivery. Missing tools, failing checks and unrun integration/manual checks
  must be reported explicitly; do not bypass hooks or claim success from setup.
- Keep this repository's configuration aligned with changed manifests, test
  commands and runtime requirements. Reuse its existing package managers and
  isolated fixtures; do not access live providers, private data or operational
  databases for automatic checks.
- The full push gate validates a clean current HEAD. Use the branch's own
  worktree for other refs. Local hooks do not enforce GitHub UI merges or a
  different clone; include actual local validation evidence in PR descriptions.
- Existing workflow YAML describes earlier CI; `.local-checks/config.json` and
  this policy define the local automation. See `.local-checks/README.md` for
  installation, project coverage, preserved hooks and rollback.
<!-- repository-local-checks:end -->
