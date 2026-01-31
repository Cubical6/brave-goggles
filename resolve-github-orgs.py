#!/usr/bin/env python3
"""Scan project dependencies, resolve to GitHub orgs, update dev-docs.goggle."""

import argparse
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

CACHE_FILE = Path(__file__).parent / ".github-orgs-cache.json"
GOGGLE_FILE = Path(__file__).parent / "dev-docs.goggle"
SKIP_DIRS = {"vendor", "node_modules", ".git", "venv", "__pycache__", ".tox", "dist", "build"}
MARKER_START = "! === GitHub Dependencies (auto-generated) ==="
MARKER_END = "! === End GitHub Dependencies ==="
GITHUB_RE = re.compile(r"github\.com[/:]([^/]+)/([^/.]+)", re.IGNORECASE)
REQUEST_DELAY = 0.3
RETRY_DELAY = 2.0


# --- Phase 1: Scan ---

def scan_project(project_path: Path) -> dict[str, set[str]]:
    """Scan dependency manifests in a single project root (non-recursive)."""
    packages: dict[str, set[str]] = {"packagist": set(), "npm": set(), "pypi": set()}
    root = Path(project_path).expanduser()
    parsers = {
        "composer.json": ("packagist", _parse_composer),
        "package.json": ("npm", _parse_package_json),
        "requirements.txt": ("pypi", _parse_requirements),
        "pyproject.toml": ("pypi", _parse_pyproject),
    }
    for fname, (registry, parser) in parsers.items():
        fpath = root / fname
        if fpath.is_file():
            try:
                parser(fpath, packages[registry])
            except Exception as e:
                print(f"Warning: failed to parse {fpath}: {e}", file=sys.stderr)
    return packages


def scan_directories(roots: list[str]) -> dict[str, set[str]]:
    """Walk directories and collect package names grouped by registry."""
    packages: dict[str, set[str]] = {"packagist": set(), "npm": set(), "pypi": set()}
    for root in roots:
        root_path = Path(root).expanduser()
        if not root_path.is_dir():
            print(f"Warning: {root_path} is not a directory, skipping", file=sys.stderr)
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                try:
                    if fname == "composer.json":
                        _parse_composer(fpath, packages["packagist"])
                    elif fname == "package.json":
                        _parse_package_json(fpath, packages["npm"])
                    elif fname == "requirements.txt":
                        _parse_requirements(fpath, packages["pypi"])
                    elif fname == "pyproject.toml":
                        _parse_pyproject(fpath, packages["pypi"])
                except Exception as e:
                    print(f"Warning: failed to parse {fpath}: {e}", file=sys.stderr)
    return packages


def _parse_composer(path: Path, out: set[str]):
    data = json.loads(path.read_text())
    for section in ("require", "require-dev"):
        for name in data.get(section, {}):
            if "/" not in name or name.startswith("ext-") or name == "php":
                continue
            out.add(name)


def _parse_package_json(path: Path, out: set[str]):
    data = json.loads(path.read_text())
    for section in ("dependencies", "devDependencies"):
        for name, version in data.get(section, {}).items():
            if isinstance(version, str) and version.startswith("file:"):
                continue
            out.add(name)


def _parse_requirements(path: Path, out: set[str]):
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-") or line.startswith("--"):
            continue
        line = line.split("#")[0].strip()
        name = re.split(r"[>=<!;\[\]\s]", line)[0].strip()
        if name and name.lower() != "python":
            out.add(name)


def _parse_pyproject(path: Path, out: set[str]):
    data = tomllib.loads(path.read_text())
    # Poetry
    poetry = data.get("tool", {}).get("poetry", {})
    for name in poetry.get("dependencies", {}):
        if name.lower() != "python":
            out.add(name)
    for group in poetry.get("group", {}).values():
        for name in group.get("dependencies", {}):
            if name.lower() != "python":
                out.add(name)
    # PEP 621
    for dep_str in data.get("project", {}).get("dependencies", []):
        name = re.split(r"[>=<!;\[\]\s]", dep_str)[0].strip()
        if name and name.lower() != "python":
            out.add(name)
    for deps in data.get("project", {}).get("optional-dependencies", {}).values():
        for dep_str in deps:
            name = re.split(r"[>=<!;\[\]\s]", dep_str)[0].strip()
            if name and name.lower() != "python":
                out.add(name)


# --- Phase 2: Resolve ---

def _http_get_json(url: str) -> dict | None:
    """GET JSON with single retry on 429/5xx."""
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "resolve-github-orgs/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == 0 and (e.code == 429 or e.code >= 500):
                time.sleep(RETRY_DELAY)
                continue
            return None
        except Exception:
            return None
    return None


def _extract_github_org(url: str) -> str | None:
    m = GITHUB_RE.search(url)
    return m.group(1).lower() if m else None


def _resolve_packagist(name: str) -> str | None:
    data = _http_get_json(f"https://packagist.org/packages/{name}.json")
    if not data:
        return None
    for version in data.get("package", {}).get("versions", {}).values():
        source = version.get("source") or {}
        org = _extract_github_org(source.get("url", ""))
        if org:
            return org
    return None


def _resolve_npm(name: str) -> str | None:
    data = _http_get_json(f"https://registry.npmjs.org/{name}")
    if not data:
        return None
    repo = data.get("repository")
    if isinstance(repo, dict):
        repo_url = repo.get("url", "")
    elif isinstance(repo, str):
        repo_url = repo
    else:
        return None
    return _extract_github_org(repo_url)


def _resolve_pypi(name: str) -> str | None:
    data = _http_get_json(f"https://pypi.org/pypi/{name}/json")
    if not data:
        return None
    urls = data.get("info", {}).get("project_urls") or {}
    for key in ("Repository", "Source", "Source Code", "Homepage", "GitHub", "Code"):
        org = _extract_github_org(urls.get(key, ""))
        if org:
            return org
    for url in urls.values():
        org = _extract_github_org(url)
        if org:
            return org
    return None


_RESOLVERS = {
    "packagist": _resolve_packagist,
    "npm": _resolve_npm,
    "pypi": _resolve_pypi,
}


def resolve_all(packages: dict[str, set[str]], cache: dict) -> dict[str, set[str]]:
    """Resolve packages to GitHub orgs. Returns {org: set_of_package_names}."""
    orgs: dict[str, set[str]] = {}
    for registry, names in packages.items():
        resolver = _RESOLVERS[registry]
        for name in sorted(names):
            cache_key = f"{registry}:{name}"
            if cache_key in cache:
                org = cache[cache_key]
            else:
                print(f"  Resolving {registry}/{name}...", file=sys.stderr)
                org = resolver(name)
                cache[cache_key] = org
                time.sleep(REQUEST_DELAY)
            if org:
                orgs.setdefault(org, set()).add(name)
    return orgs


# --- Phase 3: Aggregate ---

def build_boost_rules(orgs: dict[str, set[str]]) -> list[str]:
    """Generate goggle rules sorted by boost tier desc, then org name."""
    rules = []
    for org, pkgs in sorted(orgs.items(), key=lambda x: (-len(x[1]), x[0])):
        count = len(pkgs)
        boost = 5 if count >= 5 else 4 if count >= 3 else 3
        rules.append(f"/{org}/$boost={boost},site=github.com")
    return rules


# --- Phase 4: Update goggle ---

def generate_block(rules: list[str]) -> str:
    return "\n".join([MARKER_START, *rules, MARKER_END])


def update_goggle(block: str):
    content = GOGGLE_FILE.read_text()

    # Replace existing block or insert before generic github.com line
    marker_pat = re.compile(
        rf"^{re.escape(MARKER_START)}$.*?^{re.escape(MARKER_END)}$\n?",
        re.MULTILINE | re.DOTALL,
    )
    if marker_pat.search(content):
        content = marker_pat.sub(block + "\n", content)
    else:
        gh_pat = re.compile(r"^\$boost=[0-9]+,site=github\.com$", re.MULTILINE)
        m = gh_pat.search(content)
        if m:
            content = content[: m.start()] + block + "\n" + content[m.start() :]
        else:
            content = content.rstrip("\n") + "\n\n" + block + "\n"

    # Lower generic github.com boost (only bare line, not /org/ prefixed rules)
    content = re.sub(
        r"^\$boost=[0-9]+,site=github\.com$",
        "$boost=1,site=github.com",
        content,
        flags=re.MULTILINE,
    )

    GOGGLE_FILE.write_text(content)


# --- CLI ---

def main():
    parser = argparse.ArgumentParser(
        description="Resolve project dependencies to GitHub org boost rules for Brave Goggles.",
    )
    parser.add_argument(
        "dirs", nargs="*", default=["~/bitbucket", "~/github"],
        help="Directories to scan (default: ~/bitbucket ~/github)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print generated block without writing")
    parser.add_argument("--clear-cache", action="store_true", help="Delete cache and re-resolve")
    args = parser.parse_args()

    if args.clear_cache and CACHE_FILE.exists():
        CACHE_FILE.unlink()
        print("Cache cleared.", file=sys.stderr)

    print("Scanning dependencies...", file=sys.stderr)
    packages = scan_directories(args.dirs)
    total = sum(len(v) for v in packages.values())
    print(
        f"Found {total} packages ({', '.join(f'{k}: {len(v)}' for k, v in packages.items())})",
        file=sys.stderr,
    )

    cache = load_cache() if not args.clear_cache else {}
    cached = sum(1 for reg, names in packages.items() for n in names if f"{reg}:{n}" in cache)
    print(f"Resolving ({cached}/{total} cached)...", file=sys.stderr)

    orgs = resolve_all(packages, cache)
    save_cache(cache)

    rules = build_boost_rules(orgs)
    print(f"Generated {len(rules)} org rules from {len(orgs)} organizations.", file=sys.stderr)

    block = generate_block(rules)
    if args.dry_run:
        print(block)
    else:
        update_goggle(block)
        print(f"Updated {GOGGLE_FILE}", file=sys.stderr)


def load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}


def save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
