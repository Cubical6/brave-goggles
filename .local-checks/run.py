#!/usr/bin/env python3
"""Repository-local Git quality gates; no third-party Python dependencies."""

import argparse
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
HOOKS = [
    "applypatch-msg",
    "pre-applypatch",
    "post-applypatch",
    "pre-commit",
    "pre-merge-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "pre-rebase",
    "post-checkout",
    "post-merge",
    "pre-push",
    "post-rewrite",
    "pre-auto-gc",
    "push-to-checkout",
    "reference-transaction",
    "sendemail-validate",
    "fsmonitor-watchman",
    "post-index-change",
]


def git(root, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=check, capture_output=True, text=True
    ).stdout.strip()


def identity(root):
    common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return hashlib.sha256(common.encode()).hexdigest()[:16]


def record(root):
    return (
        Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        / "local-checks"
        / "record.json"
    )


def profile(root):
    data = json.loads(record(root).read_text())
    source = root / ".local-checks" / "config.json"
    if not source.is_file():
        source = BASE / "config.json"
    return (data, json.loads(source.read_text()))


def run_steps(root, steps, dry=False):
    for step in steps:
        cwd = root / step.get("cwd", ".")
        helper = root / ".local-checks"
        if not (helper / "config.json").is_file():
            helper = BASE
        argv = [str(helper / a[1:]) if a.startswith("@") else a for a in step["argv"]]
        print(f"local-checks: [{step.get('cwd', '.')}] {shlex.join(argv)}", flush=True)
        if dry:
            continue
        env = dict(
            os.environ,
            CI="true",
            UV_NO_SYNC="true",
            PYTHONDONTWRITEBYTECODE="1",
            **step.get("env", {}),
        )
        for required in step.get("requires", []):
            if not (cwd / required).exists():
                print(
                    f"Missing prerequisite: {cwd / required}. Follow project setup, then retry.",
                    file=sys.stderr,
                )
                return 2
        result = subprocess.run(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, check=False
        )
        if result.returncode:
            return result.returncode
    return 0


def checks(root, phase, dry=False):
    _, config = profile(root)
    if config.get("note"):
        print("local-checks: " + config["note"], flush=True)
    whitespace = (
        ["diff", "--cached", "--check"] if phase == "quick" else ["diff", "--check"]
    )
    if not dry:
        result = subprocess.run(["git", "-C", str(root), *whitespace], check=False)
        if result.returncode:
            return result.returncode
    steps = config.get("quick", [])
    if phase == "full":
        steps = steps + config.get("full", [])
    if phase == "integration":
        steps = config.get("integration", [])
        if not steps:
            print("No additional integration command configured.", file=sys.stderr)
            return 2
    if dry:
        return run_steps(root, steps, True)
    lock = record(root).parent / (
        hashlib.sha256(str(root).encode()).hexdigest()[:16] + ".lock"
    )
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return run_steps(root, steps)


def doctor(root):
    data, config = profile(root)
    expected = str(record(root).parent / "hooks")
    problems = []
    if git(root, "config", "--get", "core.hooksPath") != expected:
        problems.append("Hook path changed; run the documented install command again.")
    for step in config.get("quick", []) + config.get("full", []):
        cwd = root / step.get("cwd", ".")
        for item in step.get("requires", []):
            if not (cwd / item).exists():
                problems.append(f"Missing {cwd / item}")
        executable = step["argv"][0]
        if (
            not executable.startswith("@")
            and (not (cwd / executable).is_file())
            and (not shutil.which(executable))
        ):
            problems.append(f"Missing executable: {executable}")
    print(
        json.dumps(
            {
                "repo": str(root),
                "profile": data["profile"],
                "hooks": not any("Hook path" in p for p in problems),
                "problems": sorted(set(problems)),
                "note": "Checks not executed; tool versions/services are project prerequisites.",
            },
            indent=2,
        )
    )
    return int(bool(problems))


def install(root, name):
    json.loads((BASE / "config.json").read_text())
    target = record(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        data = json.loads(target.read_text())
        if data["profile"] != name:
            raise ValueError("Existing installation uses another profile")
    else:
        local = subprocess.run(
            ["git", "-C", str(root), "config", "--local", "--get", "core.hooksPath"],
            capture_output=True,
            text=True,
            check=False,
        )
        effective = git(root, "config", "--get", "core.hooksPath", check=False)
        common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
        data = {
            "profile": name,
            "root": str(root),
            "previous_local_hooks": local.stdout.strip()
            if local.returncode == 0
            else None,
            "legacy_hooks": effective or str(Path(common) / "hooks"),
            "previous_alias": git(
                root, "config", "--local", "--get", "alias.local-checks", check=False
            )
            or None,
        }
        target.write_text(json.dumps(data, indent=2) + "\n")
    hooks = target.parent / "hooks"
    hooks.mkdir(exist_ok=True)
    legacy = Path(data["legacy_hooks"])
    if not legacy.is_absolute():
        legacy = root / legacy
    names = {"pre-commit", "pre-push"} | {
        p.name for p in legacy.glob("*") if p.name in HOOKS and os.access(p, os.X_OK)
    }
    for hook in names:
        path = hooks / hook
        path.write_text(
            "#!/bin/sh\nexec python3 "
            + shlex.quote(str(BASE / "run.py"))
            + " hook "
            + shlex.quote(hook)
            + ' "$@"\n'
        )
        path.chmod(493)
    git(root, "config", "--local", "core.hooksPath", str(hooks))
    git(
        root,
        "config",
        "--local",
        "alias.local-checks",
        "!python3 " + shlex.quote(str(BASE / "run.py")) + " run",
    )
    print(f"Installed {name}: {root}")


def uninstall(root):
    data = json.loads(record(root).read_text())
    for key, value in [
        ("core.hooksPath", data["previous_local_hooks"]),
        ("alias.local-checks", data["previous_alias"]),
    ]:
        if value is None:
            git(root, "config", "--local", "--unset", key, check=False)
        else:
            git(root, "config", "--local", key, value)


def hook(name, args):
    top = git(Path.cwd(), "rev-parse", "--show-toplevel", check=False)
    root = Path(top) if top else Path.cwd()
    data, _ = profile(root)
    payload = (
        sys.stdin.buffer.read()
        if name in ("pre-push", "post-rewrite", "reference-transaction")
        else None
    )
    if top and name in ("pre-commit", "pre-push"):
        if name == "pre-push":
            updates = [line.split() for line in payload.decode().splitlines()]
            if any(len(row) != 4 for row in updates):
                raise ValueError("Malformed pre-push input")
            live = [row for row in updates if set(row[1]) != {"0"}]
            if live:
                head = git(root, "rev-parse", "HEAD")
                commits = [
                    git(
                        root, "rev-parse", "--verify", row[1] + "^{commit}", check=False
                    )
                    for row in live
                ]
                if any(commit != head for commit in commits):
                    print(
                        "Push each branch from its own checked-out worktree; untested refs rejected.",
                        file=sys.stderr,
                    )
                    return 1
                if git(root, "status", "--porcelain", "--untracked-files=normal"):
                    print(
                        "Pre-push requires a clean worktree so checks validate the pushed commit. Commit intended changes or use an isolated worktree.",
                        file=sys.stderr,
                    )
                    return 1
                result = checks(root, "full")
                if not result and (
                    git(root, "rev-parse", "HEAD") != head
                    or git(root, "status", "--porcelain", "--untracked-files=normal")
                ):
                    print(
                        "Checks changed the worktree or HEAD; review the changes and retry.",
                        file=sys.stderr,
                    )
                    return 1
            else:
                result = 0
        else:
            result = checks(root, "quick")
        if result:
            return result
    legacy = Path(data["legacy_hooks"])
    if not legacy.is_absolute():
        legacy = root / legacy
    path = legacy / name
    if path.is_file() and os.access(path, os.X_OK):
        return subprocess.run([str(path), *args], input=payload, check=False).returncode
    return 0


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "hook":
        return hook(sys.argv[2], sys.argv[3:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "uninstall", "run"])
    parser.add_argument(
        "phase",
        nargs="?",
        default="full",
        choices=["quick", "full", "integration", "plan", "doctor"],
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = Path(git(args.repo, "rev-parse", "--show-toplevel")).resolve()
    if args.action == "install":
        install(root, json.loads((BASE / "config.json").read_text())["name"])
        return 0
    if args.action == "uninstall":
        uninstall(root)
        return 0
    if args.phase == "doctor":
        return doctor(root)
    return checks(
        root, "full" if args.phase == "plan" else args.phase, args.phase == "plan"
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"local-checks: {exc}", file=sys.stderr)
        sys.exit(2)
