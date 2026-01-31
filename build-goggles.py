#!/usr/bin/env python3
"""Build per-project Brave Search goggles from modular rules and dependency analysis."""

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
RULES_DIR = SCRIPT_DIR / "rules"
RAW_BASE_URL = "https://raw.githubusercontent.com/Cubical6/brave-goggles/main"

# Stack detection: (manifest, key_package_pattern) -> (stack, implied_stacks)
STACK_SIGNALS = [
    ("composer.json", "laravel/framework", "laravel", ["php"]),
    ("composer.json", "cakephp/cakephp", "cakephp", ["php"]),
    ("composer.json", None, "php", []),
    ("package.json", r"^(@sveltejs/|svelte$)", "svelte", ["js-ts", "frontend"]),
    ("package.json", r"^(@vue/|vue$)", "vue", ["js-ts", "frontend"]),
    ("package.json", None, "js-ts", []),
    ("requirements.txt", None, "python", []),
    ("pyproject.toml", None, "python", []),
]

# Stack -> rules file mapping (only stacks that have their own rules file)
STACK_RULES = {
    "laravel": "laravel.rules",
    "cakephp": "cakephp.rules",
    "php": "php.rules",
    "js-ts": "js-ts.rules",
    "frontend": "frontend.rules",
    "python": "python.rules",
}


def detect_stacks(project_path: Path) -> set[str]:
    """Detect tech stacks from manifest files in project root."""
    stacks: set[str] = set()

    for manifest, pattern, stack, implied in STACK_SIGNALS:
        fpath = project_path / manifest
        if not fpath.is_file():
            continue

        if pattern is None:
            # Manifest presence is enough
            stacks.add(stack)
            stacks.update(implied)
            continue

        try:
            data = json.loads(fpath.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        # Determine which keys to search
        if manifest == "composer.json":
            keys = list(data.get("require", {})) + list(data.get("require-dev", {}))
        else:
            keys = list(data.get("dependencies", {})) + list(data.get("devDependencies", {}))

        if any(re.match(pattern, k) for k in keys):
            stacks.add(stack)
            stacks.update(implied)

    return stacks


def resolve_project_name(project_path: Path) -> str:
    """Derive goggle name, stripping .feature* suffix for worktrees."""
    name = project_path.resolve().name
    # Strip .feature* suffix (worktree convention)
    name = re.sub(r"\.feature.*$", "", name)
    return name


def load_rules(filename: str) -> str:
    """Load a rules file, stripping trailing whitespace."""
    path = RULES_DIR / filename
    if not path.is_file():
        print(f"Warning: rules file not found: {path}", file=sys.stderr)
        return ""
    return path.read_text().rstrip("\n")


def generate_header(project_name: str, stacks: set[str]) -> str:
    """Generate goggle header with metadata."""
    stack_list = ", ".join(sorted(stacks)) if stacks else "generic"
    return (
        f"! name: {project_name}\n"
        f"! description: Per-project goggle for {project_name} ({stack_list})\n"
        f"! public: false\n"
        f"! author: Cubical6"
    )


def build_github_org_rules(project_path: Path) -> list[str]:
    """Resolve project dependencies to GitHub org boost rules."""
    # Import from sibling module
    sys.path.insert(0, str(SCRIPT_DIR))
    from resolve_github_orgs import scan_project, resolve_all, build_boost_rules, load_cache, save_cache

    packages = scan_project(project_path)
    total = sum(len(v) for v in packages.values())
    if total == 0:
        return []

    cache = load_cache()
    orgs = resolve_all(packages, cache)
    save_cache(cache)

    return build_boost_rules(orgs)


def assemble_goggle(project_path: Path, extra_rules: list[str] | None = None,
                     dry_run: bool = False) -> str:
    """Assemble a complete goggle for a project."""
    project_name = resolve_project_name(project_path)
    stacks = detect_stacks(project_path)

    parts = []

    # Header
    parts.append(generate_header(project_name, stacks))

    # Base rules (always)
    parts.append(load_rules("base.rules"))

    # Stack-specific rules
    loaded = set()
    for stack in sorted(stacks):
        rules_file = STACK_RULES.get(stack)
        if rules_file and rules_file not in loaded:
            loaded.add(rules_file)
            parts.append(load_rules(rules_file))

    # Extra rules (e.g., integrations)
    for extra in (extra_rules or []):
        fname = f"{extra}.rules"
        if fname not in loaded:
            loaded.add(fname)
            parts.append(load_rules(fname))

    # GitHub org boost rules from dependencies
    if not dry_run:
        org_rules = build_github_org_rules(project_path)
        if org_rules:
            block = "\n".join(["! === GitHub Dependencies (auto-generated) ===", *org_rules,
                               "! === End GitHub Dependencies ==="])
            parts.append(block)

    return "\n\n".join(p for p in parts if p) + "\n"


def find_all_projects() -> list[Path]:
    """Find all project directories under ~/bitbucket and ~/github."""
    projects: list[Path] = []
    for base in [Path.home() / "bitbucket", Path.home() / "github"]:
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            # Must have at least one manifest
            manifests = ["composer.json", "package.json", "requirements.txt", "pyproject.toml"]
            if any((child / m).is_file() for m in manifests):
                projects.append(child)
    return projects


def group_by_project_name(paths: list[Path]) -> dict[str, list[Path]]:
    """Group paths by resolved project name (worktrees collapse)."""
    groups: dict[str, list[Path]] = {}
    for p in paths:
        name = resolve_project_name(p)
        groups.setdefault(name, []).append(p)
    return groups


def update_claude_md(project_path: Path, project_name: str):
    """Add/update goggles reference in project's .claude/CLAUDE.md."""
    claude_dir = project_path / ".claude"
    claude_md = claude_dir / "CLAUDE.md"
    goggle_url = f"{RAW_BASE_URL}/{project_name}.goggle"

    new_directive = (
        f'- When using `brave_web_search`, ALWAYS pass the goggles parameter:\n'
        f'  `goggles: ["{goggle_url}"]`'
    )

    marker = "brave_web_search"

    if not claude_md.is_file():
        print(f"  Skipped: {claude_md} does not exist", file=sys.stderr)
        return

    content = claude_md.read_text()
    lines = content.splitlines()
    new_lines = []
    replaced = False

    i = 0
    while i < len(lines):
        if marker in lines[i] and "goggles" in lines[i]:
            # Replace this line and next indented line(s)
            new_lines.extend(new_directive.splitlines())
            i += 1
            # Skip continuation lines (indented)
            while i < len(lines) and lines[i].startswith("  ") and "goggles" in lines[i]:
                i += 1
            replaced = True
        else:
            new_lines.append(lines[i])
            i += 1

    if not replaced:
        # Append at end
        new_lines.append("")
        new_lines.extend(new_directive.splitlines())

    claude_md.write_text("\n".join(new_lines) + "\n")
    print(f"  Updated {claude_md}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Build per-project Brave Search goggles.")
    parser.add_argument("project", nargs="?", help="Project directory path")
    parser.add_argument("--all", action="store_true", help="Build goggles for all detected projects")
    parser.add_argument("--dry-run", action="store_true", help="Print goggle to stdout without writing")
    parser.add_argument("--update-claude-md", action="store_true",
                        help="Update project .claude/CLAUDE.md with goggle URL")
    parser.add_argument("--extra", action="append", default=[],
                        help="Extra rules files to include (e.g., integrations)")
    args = parser.parse_args()

    if not args.project and not args.all:
        parser.error("Provide a project path or use --all")

    if args.all:
        projects = find_all_projects()
        groups = group_by_project_name(projects)
        for project_name, paths in sorted(groups.items()):
            # Use first path (main worktree) for building
            project_path = paths[0]
            print(f"\n=== {project_name} ({len(paths)} path(s)) ===", file=sys.stderr)
            for p in paths:
                print(f"  {p}", file=sys.stderr)

            goggle_content = assemble_goggle(project_path, args.extra, dry_run=args.dry_run)

            if args.dry_run:
                print(f"\n--- {project_name}.goggle ---")
                print(goggle_content)
            else:
                out_path = SCRIPT_DIR / f"{project_name}.goggle"
                existed = out_path.is_file()
                out_path.write_text(goggle_content)
                label = "UPDATED" if existed else "NEW"
                print(f"  {label}: {out_path}", file=sys.stderr)
                if not existed:
                    print(f"  → Push, then register: https://search.brave.com/goggles/create",
                          file=sys.stderr)
                    print(f"  → URL: {RAW_BASE_URL}/{project_name}.goggle", file=sys.stderr)

            if args.update_claude_md:
                for p in paths:
                    # Only update main worktrees (those with .claude/CLAUDE.md)
                    if (p / ".claude" / "CLAUDE.md").is_file():
                        update_claude_md(p, project_name)
        return

    # Single project
    project_path = Path(args.project).expanduser().resolve()
    if not project_path.is_dir():
        parser.error(f"Not a directory: {project_path}")

    project_name = resolve_project_name(project_path)
    stacks = detect_stacks(project_path)
    print(f"Project: {project_name}", file=sys.stderr)
    print(f"Stacks: {', '.join(sorted(stacks)) or 'none detected'}", file=sys.stderr)

    goggle_content = assemble_goggle(project_path, args.extra, dry_run=args.dry_run)

    if args.dry_run:
        print(goggle_content)
    else:
        out_path = SCRIPT_DIR / f"{project_name}.goggle"
        existed = out_path.is_file()
        out_path.write_text(goggle_content)
        label = "UPDATED" if existed else "NEW"
        print(f"{label}: {out_path}", file=sys.stderr)
        if not existed:
            print(f"  → Push, then register: https://search.brave.com/goggles/create",
                  file=sys.stderr)
            print(f"  → URL: {RAW_BASE_URL}/{project_name}.goggle", file=sys.stderr)

    if args.update_claude_md and (project_path / ".claude" / "CLAUDE.md").is_file():
        update_claude_md(project_path, project_name)


if __name__ == "__main__":
    main()
