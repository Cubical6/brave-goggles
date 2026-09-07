#!/usr/bin/env python3
"""Repository-local Git quality gates with shared Linux capacity leases."""

from __future__ import annotations

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
from typing import Any, Mapping, Sequence

import capacity

BASE = Path(__file__).resolve().parent
HOOKS = [
    "applypatch-msg", "pre-applypatch", "pre-commit", "pre-merge-commit",
    "prepare-commit-msg", "commit-msg", "post-commit", "pre-rebase",
    "post-checkout", "post-merge", "pre-push", "post-rewrite",
    "pre-auto-gc", "push-to-checkout", "reference-transaction",
    "sendemail-validate", "fsmonitor-watchman", "post-index-change",
]
PHASES = ("quick", "full", "integration")


def git(root: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=check, capture_output=True, text=True
    ).stdout.strip()


def identity(root: Path) -> str:
    common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return hashlib.sha256(common.encode()).hexdigest()[:16]


def record(root: Path) -> Path:
    return (
        Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        / "local-checks"
        / "record.json"
    )


def _config_source(root: Path) -> Path:
    source = root / ".local-checks" / "config.json"
    return source if source.is_file() else BASE / "config.json"


def _load_config(root: Path) -> dict[str, Any]:
    try:
        value = json.loads(_config_source(root).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise capacity.CapacityError(f"cannot read local-checks config: {exc}") from exc
    if not isinstance(value, dict):
        raise capacity.CapacityError("local-checks config must be an object")
    return value


def _validate_capacity_basis(data: Mapping[str, Any]) -> None:
    recorded_config = data.get("capacity_config")
    recorded_runtime = data.get("capacity_runtime")
    if recorded_config is None and recorded_runtime is None:
        return
    if not isinstance(recorded_config, str) or not isinstance(recorded_runtime, str):
        raise capacity.CapacityError("installation record has incomplete capacity basis")
    current_config = str(capacity.config_path())
    current_runtime = str(capacity._runtime_root())
    if recorded_config != current_config or recorded_runtime != current_runtime:
        raise capacity.CapacityError(
            "capacity basis changed; use the installed XDG config/runtime locations"
        )


def profile(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        data = json.loads(record(root).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise capacity.CapacityError(f"cannot read local-checks installation record: {exc}") from exc
    if not isinstance(data, dict):
        raise capacity.CapacityError("local-checks installation record must be an object")
    _validate_capacity_basis(data)
    return data, _load_config(root)


def _capacity_settings(config: Mapping[str, Any]) -> tuple[dict[str, str], bool]:
    value = config.get("capacity")
    if value is None:
        print("local-checks: old worktree config has no capacity metadata; using heavy defaults and drain cancellation", file=sys.stderr)
        return {phase: "heavy" for phase in PHASES}, True
    if not isinstance(value, dict) or set(value) != set(PHASES):
        raise capacity.CapacityError("capacity config must contain exactly quick, full and integration")
    result: dict[str, str] = {}
    for phase in PHASES:
        resource_class = value[phase]
        if resource_class not in capacity.CLASSES:
            raise capacity.CapacityError(f"invalid capacity class for {phase}: {resource_class}")
        result[phase] = resource_class
    return result, False


def _step_cancel_mode(step: Mapping[str, Any], *, old_config: bool) -> str:
    mode = step.get("cancel_mode", "drain" if old_config else "signal")
    if mode not in {"signal", "drain"}:
        raise capacity.CapacityError(f"invalid cancel_mode: {mode}")
    return mode


def _steps(config: Mapping[str, Any], phase: str) -> list[dict[str, Any]]:
    if phase not in PHASES:
        raise capacity.CapacityError(f"unknown check phase: {phase}")
    value = config.get(phase, [])
    if not isinstance(value, list):
        raise capacity.CapacityError(f"{phase} must be a command list")
    return value


def _phase_steps(config: Mapping[str, Any], phase: str) -> list[dict[str, Any]]:
    quick = _steps(config, "quick")
    if phase == "quick":
        return quick
    if phase == "full":
        return quick + _steps(config, "full")
    return _steps(config, "integration")


def _phase_class(config: Mapping[str, Any], phase: str) -> tuple[str, bool]:
    settings, old = _capacity_settings(config)
    return settings[phase], old


def _helper(root: Path) -> Path:
    local = root / ".local-checks"
    return local if (local / "capacity.py").is_file() else BASE


def _command_for(root: Path, helper: Path, step: Mapping[str, Any]) -> list[str]:
    argv = step.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise capacity.CapacityError("each local-checks step needs a non-empty argv array")
    return [str(helper / item[1:]) if item.startswith("@") else item for item in argv]


def run_steps(root: Path, steps: Sequence[Mapping[str, Any]], dry: bool = False, *, lease: capacity.Lease | None = None) -> int:
    helper = _helper(root)
    for step in steps:
        if not isinstance(step, Mapping):
            raise capacity.CapacityError("local-checks steps must be objects")
        cwd = root / str(step.get("cwd", "."))
        argv = _command_for(root, helper, step)
        mode = step.get("cancel_mode", "signal")
        if mode not in {"signal", "drain"}:
            raise capacity.CapacityError(f"invalid cancel_mode: {mode}")
        print(f"local-checks: [{step.get('cwd', '.')}] {shlex.join(argv)}", flush=True)
        if dry:
            continue
        if lease is not None:
            lease.raise_if_cancelled()
        env = dict(
            os.environ,
            CI="true",
            UV_NO_SYNC="true",
            PYTHONDONTWRITEBYTECODE="1",
            **(step.get("env", {}) if isinstance(step.get("env", {}), dict) else {}),
        )
        for name in git(root, "rev-parse", "--local-env-vars").splitlines():
            env.pop(name, None)
        for required in step.get("requires", []):
            if not isinstance(required, str) or not (cwd / required).exists():
                print(f"Missing prerequisite: {cwd / required}. Follow project setup, then retry.", file=sys.stderr)
                return 2
        if lease is None:
            try:
                result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, check=False).returncode
            except OSError as exc:
                print(f"local-checks: cannot start {shlex.join(argv)}: {exc}", file=sys.stderr)
                return 2
        else:
            try:
                result = capacity.run_command(argv, cwd=cwd, env=env, lease=lease, stdin=subprocess.DEVNULL)
            except capacity.CapacityError as exc:
                print(f"local-checks: {exc}", file=sys.stderr)
                return 2
        if result:
            return result
    return 0


def _worktree_state(root: Path, expected_head: str) -> bool:
    return (
        git(root, "rev-parse", "HEAD", check=False) == expected_head
        and not git(root, "status", "--porcelain", "--untracked-files=normal", check=False)
    )


def _checks_body(root: Path, phase: str, config: Mapping[str, Any], dry: bool, *, expected_head: str | None, lease: capacity.Lease | None) -> int:
    if expected_head is not None and not _worktree_state(root, expected_head):
        print("local-checks: HEAD or worktree changed before checks started", file=sys.stderr)
        return 1
    whitespace = ["diff", "--cached", "--check"] if phase == "quick" else ["diff", "--check"]
    if not dry and subprocess.run(["git", "-C", str(root), *whitespace], check=False).returncode:
        return 1
    steps = _phase_steps(config, phase)
    if phase == "integration" and not steps:
        print("No additional integration command configured.", file=sys.stderr)
        return 2
    result = run_steps(root, steps, dry, lease=lease)
    if expected_head is not None and result == 0 and not _worktree_state(root, expected_head):
        print("Checks changed the worktree or HEAD; review the changes and retry.", file=sys.stderr)
        return 1
    return result


def checks(root: Path, phase: str, dry: bool = False, *, expected_head: str | None = None, lease: capacity.Lease | None = None) -> int:
    _, config = profile(root)
    if config.get("note"):
        print("local-checks: " + str(config["note"]), flush=True)
    resource_class, old_config = _phase_class(config, phase)
    steps = _phase_steps(config, phase)
    if phase == "integration" and not steps:
        return _checks_body(root, phase, config, dry, expected_head=expected_head, lease=lease)
    if dry:
        return run_steps(root, steps, True)
    if lease is not None:
        if lease.resource_class != resource_class:
            raise capacity.CapacityError("inherited lease class does not match phase capacity")
        return _checks_body(root, phase, config, False, expected_head=expected_head, lease=lease)
    cancel_mode = "drain" if old_config or any(_step_cancel_mode(step, old_config=old_config) == "drain" for step in steps) else "signal"
    try:
        acquired = capacity.reserve(root, resource_class, cancel_mode=cancel_mode)
    except capacity.Cancelled as exc:
        return 128 + exc.signum
    except capacity.CapacityTimeout:
        return 124
    except capacity.CapacityError as exc:
        print(f"local-checks: {exc}", file=sys.stderr)
        return 2
    result = 2
    cleanup_error: Exception | None = None
    try:
        result = _checks_body(root, phase, config, False, expected_head=expected_head, lease=acquired)
    except capacity.Cancelled as exc:
        result = 128 + exc.signum
    except capacity.CapacityError as exc:
        print(f"local-checks: {exc}", file=sys.stderr)
        result = 2
    finally:
        try:
            acquired.finish()
        except Exception as exc:
            cleanup_error = exc
    if result == 0 and cleanup_error is not None:
        print(f"local-checks: cleanup failed: {cleanup_error}", file=sys.stderr)
        return 2
    return result


def _prepush_updates(payload: bytes) -> tuple[list[list[bytes]], list[list[bytes]]]:
    updates: list[list[bytes]] = []
    for line in payload.splitlines():
        row = line.split()
        if len(row) != 4:
            raise ValueError("Malformed pre-push input")
        updates.append(row)
    return updates, [row for row in updates if set(row[1]) != {ord("0")}]


def _legacy(root: Path, data: Mapping[str, Any], name: str, args: Sequence[str], payload: bytes | None) -> int:
    legacy = Path(str(data["legacy_hooks"]))
    if not legacy.is_absolute():
        legacy = root / legacy
    path = legacy / name
    if path.is_file() and os.access(path, os.X_OK):
        try:
            return subprocess.run([str(path), *args], input=payload, check=False).returncode
        except OSError as exc:
            print(f"local-checks: legacy hook failed to start: {exc}", file=sys.stderr)
            return 2
    return 0


def hook(name: str, args: Sequence[str]) -> int:
    top = git(Path.cwd(), "rev-parse", "--show-toplevel", check=False)
    root = Path(top).resolve() if top else Path.cwd()
    data, config = profile(root)
    payload = sys.stdin.buffer.read() if name in {"pre-push", "post-rewrite", "reference-transaction"} else None
    if top and name in {"pre-commit", "pre-push"}:
        if name == "pre-push":
            updates, live = _prepush_updates(payload or b"")
            if live:
                head = git(root, "rev-parse", "HEAD")
                commits = [git(root, "rev-parse", "--verify", row[1].decode() + "^{commit}", check=False) for row in live]
                if any(commit != head for commit in commits):
                    print("Push each branch from its own checked-out worktree; untested refs rejected.", file=sys.stderr)
                    return 1
                if git(root, "status", "--porcelain", "--untracked-files=normal"):
                    print("Pre-push requires a clean worktree so checks validate the pushed commit. Commit intended changes or use an isolated worktree.", file=sys.stderr)
                    return 1
                resource_class, old_config = _phase_class(config, "full")
                full_steps = _phase_steps(config, "full")
                cancel_mode = "drain" if old_config or any(_step_cancel_mode(step, old_config=old_config) == "drain" for step in full_steps) else "signal"
                try:
                    acquired = capacity.reserve(root, resource_class, cancel_mode=cancel_mode)
                except capacity.Cancelled as exc:
                    return 128 + exc.signum
                except capacity.CapacityTimeout:
                    return 124
                except capacity.CapacityError as exc:
                    print(f"local-checks: {exc}", file=sys.stderr)
                    return 2
                result = 2
                cleanup_error: Exception | None = None
                try:
                    result = checks(root, "full", expected_head=head, lease=acquired)
                    if result == 0:
                        result = _legacy(root, data, name, args, payload)
                    if result == 0 and not _worktree_state(root, head):
                        print("Checks changed the worktree or HEAD; review the changes and retry.", file=sys.stderr)
                        result = 1
                except capacity.Cancelled as exc:
                    result = 128 + exc.signum
                except capacity.CapacityError as exc:
                    print(f"local-checks: {exc}", file=sys.stderr)
                    result = 2
                finally:
                    try:
                        acquired.finish()
                    except Exception as exc:
                        cleanup_error = exc
                if result == 0 and cleanup_error is not None:
                    print(f"local-checks: cleanup failed: {cleanup_error}", file=sys.stderr)
                    return 2
                return result
            else:
                result = 0
        else:
            result = checks(root, "quick")
        if result:
            return result
    return _legacy(root, data, name, args, payload)


def _capacity_report(config: Mapping[str, Any]) -> dict[str, Any]:
    settings, old = _capacity_settings(config)
    try:
        config_file = str(capacity.config_path())
        runtime = str(capacity._runtime_root())
        config_path, limits = capacity._load_policy_config()
    except capacity.CapacityError as exc:
        config_file, runtime, limits = f"error: {exc}", "error", {}
    return {"config": config_file, "runtime": runtime, "version": capacity.PROTOCOL_VERSION, "limits": limits, "phase_classes": settings, "old_config_defaults": old}


def doctor(root: Path) -> int:
    data, config = profile(root)
    expected = str(record(root).parent / "hooks")
    problems: list[str] = []
    if git(root, "config", "--get", "core.hooksPath") != expected:
        problems.append("Hook path changed; run the documented install command again.")
    for phase in PHASES:
        for step in _phase_steps(config, phase):
            cwd = root / str(step.get("cwd", "."))
            for item in step.get("requires", []):
                if not (cwd / item).exists():
                    problems.append(f"Missing {cwd / item}")
            executable = step["argv"][0]
            if not executable.startswith("@") and not (cwd / executable).is_file() and not shutil.which(executable):
                problems.append(f"Missing executable: {executable}")
    print(json.dumps({"repo": str(root), "profile": data["profile"], "hooks": not any("Hook path" in p for p in problems), "capacity": _capacity_report(config), "problems": sorted(set(problems)), "note": "Checks not executed; tool versions/services are project prerequisites."}, indent=2))
    return int(bool(problems))


def install(root: Path, name: str) -> None:
    config = _load_config(root)
    _capacity_settings(config)
    target = record(root)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    for lock_path in target.parent.glob("*.lock"):
        if lock_path.is_file() and not lock_path.is_symlink():
            lock_path.chmod(0o600)
    if target.exists():
        data = json.loads(target.read_text())
        if data["profile"] != name:
            raise ValueError("Existing installation uses another profile")
    else:
        local = subprocess.run(["git", "-C", str(root), "config", "--local", "--get", "core.hooksPath"], capture_output=True, text=True, check=False)
        effective = git(root, "config", "--get", "core.hooksPath", check=False)
        common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
        data = {"profile": name, "root": str(root), "previous_local_hooks": local.stdout.strip() if local.returncode == 0 else None, "legacy_hooks": effective or str(Path(common) / "hooks"), "previous_alias": git(root, "config", "--local", "--get", "alias.local-checks", check=False) or None}
    config_path, _ = capacity._load_policy_config()
    data.update({"runner": str((root / ".local-checks/run.py").resolve()), "capacity_protocol": capacity.PROTOCOL_VERSION, "capacity_config": str(config_path), "capacity_runtime": str(capacity._runtime_root())})
    target.write_text(json.dumps(data, indent=2) + "\n")
    hooks = target.parent / "hooks"
    hooks.mkdir(exist_ok=True, mode=0o700)
    hooks.chmod(0o700)
    legacy = Path(data["legacy_hooks"])
    if not legacy.is_absolute():
        legacy = root / legacy
    names = {"pre-commit", "pre-push"} | {p.name for p in legacy.glob("*") if p.name in HOOKS and os.access(p, os.X_OK)}
    for hook_name in names:
        path = hooks / hook_name
        path.write_text("#!/bin/sh\nexec python3 " + shlex.quote(str((root / ".local-checks/run.py").resolve())) + " hook " + shlex.quote(hook_name) + ' "$@"\n')
        path.chmod(0o755)
    git(root, "config", "--local", "core.hooksPath", str(hooks))
    git(root, "config", "--local", "alias.local-checks", "!python3 " + shlex.quote(str((root / ".local-checks/run.py").resolve())) + " run")
    print(f"Installed {name}: {root}")


def uninstall(root: Path) -> None:
    data = json.loads(record(root).read_text())
    for key, value in [("core.hooksPath", data["previous_local_hooks"]), ("alias.local-checks", data["previous_alias"])]:
        if value is None:
            git(root, "config", "--local", "--unset", key, check=False)
        else:
            git(root, "config", "--local", key, value)


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "hook":
        return hook(sys.argv[2], sys.argv[3:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "uninstall", "run"])
    parser.add_argument("phase", nargs="?", default="full", choices=["quick", "full", "integration", "plan", "doctor"])
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = Path(git(args.repo, "rev-parse", "--show-toplevel")).resolve()
    if args.action == "install":
        install(root, _load_config(root)["name"])
        return 0
    if args.action == "uninstall":
        uninstall(root)
        return 0
    if args.phase == "doctor":
        return doctor(root)
    return checks(root, "full" if args.phase == "plan" else args.phase, args.phase == "plan")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, capacity.CapacityError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
