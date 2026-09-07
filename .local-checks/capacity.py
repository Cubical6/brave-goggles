#!/usr/bin/env python3
"""Cross-repository local-checks capacity leases for Linux.

The implementation intentionally uses only the Python standard library.  Locks
are ordinary flocked files; policy and status are metadata, never lock
identity.  A lease is valid only while the owning process (or an attested
ancestor supervisor) still holds the corresponding lock descriptions.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

PROTOCOL_VERSION = 1
DEFAULT_LIMITS = {"heavy": 1, "light": 2}
CLASSES = frozenset(DEFAULT_LIMITS)
TOKEN_ENV = "LOCAL_CHECKS_LEASE"
POLL_SECONDS = 0.2
REPORT_SECONDS = 5.0
CANCEL_GRACE_SECONDS = 10.0


class CapacityError(Exception):
    """A fail-closed capacity/protocol error."""


class Cancelled(CapacityError):
    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(f"local-checks: cancelled by signal {signum}")


class CapacityTimeout(CapacityError):
    pass


def _die(message: str, code: int = 2) -> int:
    print(f"local-checks: {message}", file=sys.stderr)
    return code


def _uid() -> int:
    return os.getuid()


def _canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CapacityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text, object_pairs_hook=_duplicate_keys)
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, CapacityError) as exc:
        raise CapacityError(f"cannot read valid JSON from {path}: {exc}") from exc


def _check_owner_mode(path: Path, *, directory: bool) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CapacityError(f"cannot inspect {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise CapacityError(f"unsafe symlink: {path}")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise CapacityError(f"not a directory: {path}")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise CapacityError(f"not a regular file: {path}")
    if info.st_uid != _uid():
        raise CapacityError(f"wrong owner for {path}")
    if not directory and info.st_nlink != 1:
        raise CapacityError(f"unexpected hard links for {path}")
    if stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
        raise CapacityError(f"unsafe mode for {path}; expected {('0700' if directory else '0600')}")
    return info


def _mkdir_private(path: Path) -> None:
    path = _canonical(path)
    if path.exists() or path.is_symlink():
        _check_owner_mode(path, directory=True)
        return
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _check_owner_mode(path, directory=True)


def _config_root() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    if raw is None or raw == "":
        return _canonical(Path.home() / ".config")
    path = Path(raw)
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise CapacityError("XDG_CONFIG_HOME must be an absolute non-traversing path")
    if path.exists() or path.is_symlink():
        _check_owner_mode(path, directory=True)
    else:
        _mkdir_private(path)
    return _canonical(path)


def config_path() -> Path:
    root = _config_root() / "local-checks"
    _mkdir_private(root)
    return root / "capacity.json"


def _runtime_root() -> Path:
    raw = os.environ.get("XDG_RUNTIME_DIR")
    if raw:
        path = Path(raw)
        if not path.is_absolute() or any(part == ".." for part in path.parts):
            raise CapacityError("XDG_RUNTIME_DIR must be an absolute non-traversing path")
        _check_owner_mode(path, directory=True)
        base = _canonical(path)
    else:
        user_run = Path(f"/run/user/{_uid()}")
        if user_run.exists():
            _check_owner_mode(user_run, directory=True)
            base = _canonical(user_run)
        else:
            base = _canonical(Path(f"/tmp/local-checks-{_uid()}"))
            _mkdir_private(base)
    runtime = base / "local-checks"
    _mkdir_private(runtime)
    _mkdir_private(runtime / "runs")
    return runtime


def _open_relative(directory: Path, name: str, *, create: bool = True) -> int:
    _check_owner_mode(directory, directory=True)
    flags = os.O_RDWR | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise CapacityError(f"cannot open {directory / name}: {exc}") from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != _uid() or info.st_nlink != 1:
        os.close(fd)
        raise CapacityError(f"unsafe lock or metadata file: {directory / name}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        os.close(fd)
        raise CapacityError(f"unsafe mode for {directory / name}; expected 0600")
    return fd


def _atomic_json(path: Path, data: Mapping[str, Any]) -> None:
    _check_owner_mode(path.parent, directory=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(temporary, flags, 0o600)
        try:
            payload = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        _check_owner_mode(path, directory=False)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise CapacityError(f"cannot write {path}: {exc}") from exc


def _load_policy_config() -> tuple[Path, dict[str, int]]:
    path = config_path()
    if not path.exists():
        return path, dict(DEFAULT_LIMITS)
    _check_owner_mode(path, directory=False)
    data = _read_json(path)
    if not isinstance(data, dict) or set(data) != {"version", "limits"}:
        raise CapacityError("capacity config must contain exactly version and limits")
    version = data["version"]
    if _is_bool(version) or not isinstance(version, int) or version != PROTOCOL_VERSION:
        raise CapacityError("unsupported capacity config version")
    limits = data["limits"]
    if not isinstance(limits, dict) or set(limits) != CLASSES:
        raise CapacityError("capacity config limits must contain exactly heavy and light")
    result: dict[str, int] = {}
    for name in sorted(CLASSES):
        value = limits[name]
        if _is_bool(value) or not isinstance(value, int) or value <= 0:
            raise CapacityError(f"capacity limit {name} must be a positive integer")
        result[name] = value
    return path, result


def _validate_policy(data: Any) -> tuple[Path, dict[str, int]]:
    if not isinstance(data, dict) or set(data) != {"version", "config_path", "limits"}:
        raise CapacityError("invalid capacity policy")
    if data["version"] != PROTOCOL_VERSION or _is_bool(data["version"]):
        raise CapacityError("invalid capacity policy version")
    raw_path = data["config_path"]
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise CapacityError("invalid capacity policy config path")
    limits = data["limits"]
    if not isinstance(limits, dict) or set(limits) != CLASSES:
        raise CapacityError("invalid capacity policy limits")
    normalized: dict[str, int] = {}
    for name in sorted(CLASSES):
        value = limits[name]
        if _is_bool(value) or not isinstance(value, int) or value <= 0:
            raise CapacityError("invalid capacity policy limit")
        normalized[name] = value
    return _canonical(Path(raw_path)), normalized


def _policy_for(path: Path, limits: Mapping[str, int]) -> dict[str, Any]:
    return {"version": PROTOCOL_VERSION, "config_path": str(_canonical(path)), "limits": dict(sorted(limits.items()))}


def _proc_start(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, UnicodeError):
        return None
    closing = text.rfind(")")
    if closing < 0:
        return None
    fields = text[closing + 2 :].split()
    if len(fields) <= 19:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _pid_alive(pid: int, start: int) -> bool:
    return pid > 0 and _proc_start(pid) == start


def _is_ancestor(owner_pid: int) -> bool:
    current = os.getpid()
    seen: set[int] = set()
    while current > 1 and current not in seen:
        if current == owner_pid:
            return True
        seen.add(current)
        try:
            status = Path(f"/proc/{current}/status").read_text()
        except OSError:
            return False
        match = re.search(r"^PPid:\s+(\d+)$", status, re.MULTILINE)
        if not match:
            return False
        current = int(match.group(1))
    return owner_pid == 1


def _fd_info_has_write_lock(pid: int, fd: int) -> bool:
    try:
        text = Path(f"/proc/{pid}/fdinfo/{fd}").read_text()
    except OSError:
        return False
    return bool(re.search(r"^lock:.*\bWRITE\b", text, re.MULTILINE))


def _owner_holds_inode(pid: int, expected: os.stat_result) -> bool:
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        names = list(fd_dir.iterdir())
    except OSError:
        return False
    for entry in names:
        try:
            info = entry.stat()
        except OSError:
            continue
        if info.st_dev == expected.st_dev and info.st_ino == expected.st_ino:
            try:
                fd = int(entry.name)
            except ValueError:
                continue
            if _fd_info_has_write_lock(pid, fd):
                return True
    return False


def _fd_matches(fd: int, expected: os.stat_result) -> bool:
    try:
        info = os.fstat(fd)
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == _uid()
        and info.st_nlink == 1
        and info.st_dev == expected.st_dev
        and info.st_ino == expected.st_ino
        and _fd_info_has_write_lock(os.getpid(), fd)
    )


def _slot_lock_path(runtime: Path, resource_class: str, slot: int) -> Path:
    return runtime / f"{resource_class}.{slot}.lock"


def _slot_status_path(runtime: Path, resource_class: str, slot: int) -> Path:
    return runtime / f"{resource_class}.{slot}.json"


def _run_dir(runtime: Path, run_id: str) -> Path:
    return runtime / "runs" / run_id


@dataclass
class Lease:
    root: Path
    resource_class: str
    slot: int
    run_id: str
    owner_pid: int
    owner_start: int
    worktree_fd: int | None
    capacity_fd: int | None
    runtime: Path
    run_path: Path
    cancel_fd: int
    cancel_mode: str = "signal"
    owner: bool = True
    worktree_wait: float = 0.0
    capacity_wait: float = 0.0
    started_at: float = field(default_factory=time.monotonic)
    _cancel_signal: int | None = None
    _handler_installed: bool = False
    _old_handlers: dict[int, Any] = field(default_factory=dict)
    _closed: bool = False

    def token(self) -> dict[str, Any]:
        return {
            "version": PROTOCOL_VERSION,
            "run_id": self.run_id,
            "owner_pid": self.owner_pid,
            "owner_start": self.owner_start,
            "root": str(self.root),
            "class": self.resource_class,
            "slot": self.slot,
            "worktree_fd": self.worktree_fd if self.worktree_fd is not None else -1,
            "capacity_fd": self.capacity_fd if self.capacity_fd is not None else -1,
        }

    def environment(self, env: Mapping[str, str] | None = None) -> dict[str, str]:
        result = dict(os.environ if env is None else env)
        result[TOKEN_ENV] = json.dumps(self.token(), sort_keys=True, separators=(",", ":"))
        return result

    def _signal_handler(self, signum: int, _frame: Any) -> None:
        if self._cancel_signal is None:
            self._cancel_signal = signum
        try:
            os.pwrite(self.cancel_fd, bytes((signum & 0xFF,)), 0)
        except OSError:
            pass

    def install_handlers(self) -> None:
        if self._handler_installed:
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            self._old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, self._signal_handler)
        self._handler_installed = True

    def restore_handlers(self) -> None:
        if not self._handler_installed:
            return
        for signum, handler in self._old_handlers.items():
            signal.signal(signum, handler)
        self._old_handlers.clear()
        self._handler_installed = False

    def cancelled_signal(self) -> int | None:
        if self._cancel_signal is not None:
            return self._cancel_signal
        try:
            value = os.pread(self.cancel_fd, 1, 0)
        except OSError:
            return None
        if value and value[0] in (signal.SIGINT, signal.SIGTERM):
            self._cancel_signal = value[0]
        return self._cancel_signal

    def raise_if_cancelled(self) -> None:
        signum = self.cancelled_signal()
        if signum:
            raise Cancelled(signum)

    def status(self) -> dict[str, Any]:
        return {
            "version": PROTOCOL_VERSION,
            "run_id": self.run_id,
            "owner_pid": self.owner_pid,
            "owner_start": self.owner_start,
            "state": "running",
            "run_dir": str(self.run_path),
            "cancel_signal": self.cancelled_signal(),
            "cancel_mode": self.cancel_mode,
        }

    def _write_status(self, state: str) -> None:
        data = self.status()
        data["state"] = state
        _atomic_json(_slot_status_path(self.runtime, self.resource_class, self.slot), data)

    def register_resource(
        self,
        kind: str,
        name: str,
        *,
        compose_file: Path | None = None,
        env_file: Path | None = None,
    ) -> None:
        if self._closed:
            raise CapacityError("closed lease cannot register resources")
        if kind not in {"compose", "container", "network"} or not name:
            raise CapacityError("invalid owned resource")
        if kind == "compose" and (compose_file is None or env_file is None):
            raise CapacityError("compose resources require compose_file and env_file")
        if kind != "compose" and (compose_file is not None or env_file is not None):
            raise CapacityError("only compose resources accept compose_file and env_file")
        if not name.startswith(f"localchecks-{self.run_id}-"):
            raise CapacityError("resource name is not owned by this run")
        resource = {
            "version": PROTOCOL_VERSION,
            "run_id": self.run_id,
            "kind": kind,
            "name": name,
            "cwd": str(self.root),
            "compose_file": str(_canonical(compose_file)) if compose_file else None,
            "env_file": str(_canonical(env_file)) if env_file else None,
        }
        _atomic_json(self.run_path / f"resource-{uuid.uuid4().hex}.json", resource)

    def _resources(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for path in sorted(self.run_path.glob("resource-*.json")):
            try:
                data = _read_json(path)
            except (OSError, CapacityError, json.JSONDecodeError):
                raise CapacityError(f"invalid resource metadata: {path}")
            if not isinstance(data, dict) or data.get("run_id") != self.run_id:
                raise CapacityError(f"resource metadata does not belong to {self.run_id}")
            result.append(data)
        return result

    def cleanup_resources(self) -> None:
        """Clean exactly the resources registered by this run."""
        resources = self._resources()
        if not resources:
            return
        docker = shutil_which("docker")
        if not docker:
            raise CapacityError("Docker is unavailable for registered resource cleanup")
        cleanup_error: Exception | None = None
        for resource in resources:
            try:
                kind = resource.get("kind")
                name = resource.get("name")
                if not isinstance(name, str):
                    raise CapacityError("invalid resource name")
                if kind == "compose":
                    compose_file = resource.get("compose_file")
                    env_file = resource.get("env_file")
                    if not isinstance(compose_file, str) or not isinstance(env_file, str):
                        raise CapacityError("invalid compose resource metadata")
                    command = [
                        docker, "compose", "-f", compose_file, "--env-file", env_file,
                        "-p", name, "down", "--volumes", "--remove-orphans",
                    ]
                    cwd = resource.get("cwd") or str(self.root)
                elif kind == "container":
                    command = [docker, "rm", "--force", name]
                    cwd = None
                elif kind == "network":
                    command = [docker, "network", "rm", name]
                    cwd = None
                else:
                    raise CapacityError("invalid resource kind")
                result = subprocess.run(
                    command,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                if result.returncode and not _missing_docker_resource(result.stderr):
                    raise CapacityError(f"cleanup command failed for {name}: exit {result.returncode}: {result.stderr.strip()}")
            except (OSError, CapacityError) as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error:
            raise CapacityError(f"resource cleanup failed: {cleanup_error}")

    def finish(self, result: int | None = None) -> None:
        if self._closed:
            return
        cleanup_error: Exception | None = None
        try:
            if self.owner:
                try:
                    self.cleanup_resources()
                except Exception as exc:  # marker must remain recoverable
                    cleanup_error = exc
                if cleanup_error is None:
                    self._write_status("complete")
                else:
                    self._write_status("recovery_required")
        finally:
            self.restore_handlers()
            try:
                os.close(self.cancel_fd)
            except OSError:
                pass
            if self.owner:
                for fd in (self.capacity_fd, self.worktree_fd):
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
            else:
                for fd in (self.capacity_fd, self.worktree_fd):
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
            self._closed = True
        if cleanup_error:
            raise cleanup_error

    def __enter__(self) -> "Lease":
        self.install_handlers()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            self.finish()
        except CapacityError:
            if exc is None:
                raise
        return False


def _missing_docker_resource(stderr: str) -> bool:
    message = stderr.lower()
    return any(
        phrase in message
        for phrase in ("no such container", "no such network", "not found", "does not exist")
    )
def shutil_which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / name
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            pass
    return None


def _worktree_lock(root: Path) -> tuple[Path, int]:
    root = _canonical(root)
    try:
        common = _canonical(Path(subprocess.check_output(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"], text=True).strip()))
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CapacityError(f"cannot resolve Git common directory: {exc}") from exc
    directory = common / "local-checks"
    if directory.exists():
        _check_owner_mode(directory, directory=True)
    else:
        _mkdir_private(directory)
    name = hashlib.sha256(str(root).encode()).hexdigest()[:16] + ".lock"
    return directory / name, _open_relative(directory, name)


@contextmanager
def _flock_wait(fd: int, *, root: Path, label: str, timeout: float | None, lease: Lease | None = None) -> Iterator[float]:
    started = time.monotonic()
    next_report = started
    deadline = started + timeout if timeout is not None else None
    reported = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield time.monotonic() - started
            return
        except BlockingIOError:
            elapsed = time.monotonic() - started
            if not reported:
                print(f"local-checks: waiting for {root} {label} (0s elapsed)", file=sys.stderr, flush=True)
                reported = True
            elif time.monotonic() >= next_report:
                print(f"local-checks: waiting for {root} {label} ({elapsed:.1f}s elapsed)", file=sys.stderr, flush=True)
                next_report = time.monotonic() + REPORT_SECONDS
            if lease:
                lease.raise_if_cancelled()
            if deadline is not None and time.monotonic() >= deadline:
                raise CapacityTimeout(f"timeout waiting for {label}")
            time.sleep(POLL_SECONDS)
        except InterruptedError:
            if lease:
                lease.raise_if_cancelled()
            continue


def _locked_nonblocking(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock_close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _ensure_policy(runtime: Path, admission_fd: int) -> tuple[Path, dict[str, int]]:
    config, limits = _load_policy_config()
    policy_path = runtime / "policy.json"
    desired = _policy_for(config, limits)
    if policy_path.exists():
        _check_owner_mode(policy_path, directory=False)
        current = _read_json(policy_path)
        current_path, current_limits = _validate_policy(current)
        if str(current_path) != desired["config_path"] or current_limits != limits:
            # Admission excludes new reservations.  All existing slots must be
            # idle before a changed machine policy can become authoritative.
            held: list[int] = []
            try:
                for klass, count in current_limits.items():
                    for slot in range(max(count, limits[klass])):
                        fd = _open_relative(runtime, f"{klass}.{slot}.lock")
                        if not _locked_nonblocking(fd):
                            _unlock_close(fd)
                            raise CapacityError("capacity policy changed while checks are active; drain first")
                        held.append(fd)
                        status_path = _slot_status_path(runtime, klass, slot)
                        if status_path.exists():
                            data = _read_json(status_path)
                            if isinstance(data, dict) and data.get("state") in {"running", "recovery_required"}:
                                raise CapacityError(f"slot {klass}.{slot} requires recovery")
            finally:
                for fd in held:
                    _unlock_close(fd)
            _atomic_json(policy_path, desired)
    else:
        _atomic_json(policy_path, desired)
    for klass, count in limits.items():
        for slot in range(count):
            fd = _open_relative(runtime, f"{klass}.{slot}.lock")
            os.close(fd)
    return config, limits


def _validate_lease_token(data: Any, root: Path, resource_class: str) -> Lease:
    if not isinstance(data, dict):
        raise CapacityError("invalid LOCAL_CHECKS_LEASE JSON")
    expected_keys = {"version", "run_id", "owner_pid", "owner_start", "root", "class", "slot", "worktree_fd", "capacity_fd"}
    if set(data) != expected_keys:
        raise CapacityError("invalid LOCAL_CHECKS_LEASE keys")
    if data["version"] != PROTOCOL_VERSION or _is_bool(data["version"]):
        raise CapacityError("invalid LOCAL_CHECKS_LEASE version")
    if not isinstance(data["run_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", data["run_id"]):
        raise CapacityError("invalid LOCAL_CHECKS_LEASE run_id")
    for key in ("owner_pid", "owner_start", "slot", "worktree_fd", "capacity_fd"):
        if _is_bool(data[key]) or not isinstance(data[key], int):
            raise CapacityError(f"invalid LOCAL_CHECKS_LEASE {key}")
    if data["owner_pid"] <= 0 or data["owner_start"] <= 0 or data["slot"] < 0:
        raise CapacityError("invalid LOCAL_CHECKS_LEASE process identity")
    token_root = _canonical(Path(data["root"])) if isinstance(data["root"], str) else None
    root = _canonical(root)
    if token_root != root or data["class"] != resource_class or resource_class not in CLASSES:
        raise CapacityError("LOCAL_CHECKS_LEASE root or class mismatch")
    if not _pid_alive(data["owner_pid"], data["owner_start"]):
        raise CapacityError("LOCAL_CHECKS_LEASE owner is not alive")
    if not _is_ancestor(data["owner_pid"]):
        raise CapacityError("LOCAL_CHECKS_LEASE owner is not an ancestor")
    runtime = _runtime_root()
    worktree_path, expected_work_fd = _worktree_lock(root)
    os.close(expected_work_fd)
    slot_path = _slot_lock_path(runtime, resource_class, data["slot"])
    if not slot_path.exists():
        raise CapacityError("LOCAL_CHECKS_LEASE slot does not exist")
    expected_work = worktree_path.stat()
    expected_slot = slot_path.stat()
    work_fd = data["worktree_fd"]
    cap_fd = data["capacity_fd"]
    direct = work_fd >= 0 and cap_fd >= 0 and _fd_matches(work_fd, expected_work) and _fd_matches(cap_fd, expected_slot)
    attested = _owner_holds_inode(data["owner_pid"], expected_work) and _owner_holds_inode(data["owner_pid"], expected_slot)
    if not direct and not attested:
        raise CapacityError("LOCAL_CHECKS_LEASE does not prove live held locks")
    status_path = _slot_status_path(runtime, resource_class, data["slot"])
    if not status_path.exists():
        raise CapacityError("LOCAL_CHECKS_LEASE slot status is missing")
    status = _read_json(status_path)
    if not isinstance(status, dict) or status.get("run_id") != data["run_id"] or status.get("state") != "running":
        raise CapacityError("LOCAL_CHECKS_LEASE slot status mismatch")
    if not _pid_alive(data["owner_pid"], data["owner_start"]):
        raise CapacityError("LOCAL_CHECKS_LEASE owner changed during inspection")
    run_path = _run_dir(runtime, data["run_id"])
    if not run_path.is_dir():
        raise CapacityError("LOCAL_CHECKS_LEASE run directory is missing")
    _check_owner_mode(run_path, directory=True)
    cancel_path = run_path / "cancel.signal"
    _check_owner_mode(cancel_path, directory=False)
    cancel_fd = os.open(cancel_path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        lease = Lease(root, resource_class, data["slot"], data["run_id"], data["owner_pid"], data["owner_start"], work_fd if direct else None, cap_fd if direct else None, runtime, run_path, cancel_fd, str(status.get("cancel_mode", "signal")), owner=False)
    except BaseException:
        os.close(cancel_fd)
        raise
    return lease


def inherited_lease(root: Path, resource_class: str) -> Lease | None:
    raw = os.environ.get(TOKEN_ENV)
    if raw is None:
        return None
    try:
        data = json.loads(raw, object_pairs_hook=_duplicate_keys)
        return _validate_lease_token(data, root, resource_class)
    except (json.JSONDecodeError, CapacityError, OSError, ValueError) as exc:
        raise CapacityError(str(exc)) from exc


def _write_cancel_signal(lease: Lease, signum: int) -> None:
    lease._cancel_signal = signum
    try:
        os.pwrite(lease.cancel_fd, bytes((signum & 0xFF,)), 0)
    except OSError:
        pass


def _terminate_group(process: subprocess.Popen[bytes], signum: int, *, drain: bool, lease: Lease) -> None:
    if drain:
        print("local-checks: cancellation waits for external tooling to return (drain mode)", file=sys.stderr, flush=True)
        return
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + CANCEL_GRACE_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _enable_subreaper() -> None:
    if sys.platform != "linux":
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None)
        libc.prctl(36, 1, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    lease: Lease,
    stdin: Any = subprocess.DEVNULL,
    input: bytes | None = None,
) -> int:
    if not argv:
        raise CapacityError("cannot execute an empty command")
    lease.raise_if_cancelled()
    lease.install_handlers()
    _enable_subreaper()
    started = time.monotonic()
    child_env = lease.environment(env)
    pass_fds = tuple(fd for fd in (lease.worktree_fd, lease.capacity_fd) if fd is not None and fd >= 0)
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=child_env,
            stdin=subprocess.PIPE if input is not None else stdin,
            stdout=None,
            stderr=None,
            start_new_session=lease.owner,
            pass_fds=pass_fds,
            close_fds=True,
        )
    except OSError as exc:
        raise CapacityError(f"cannot start {' '.join(map(str, argv))}: {exc}") from exc
    cancellation: int | None = None
    try:
        if input is not None:
            process.stdin.write(input)
            process.stdin.close()
        while True:
            try:
                code = process.wait(timeout=POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                signum = lease.cancelled_signal()
                if signum and cancellation is None:
                    cancellation = signum
                    _terminate_group(process, signum, drain=lease.cancel_mode == "drain", lease=lease)
                elif cancellation and lease.cancel_mode == "drain":
                    if time.monotonic() >= started + REPORT_SECONDS:
                        print("local-checks: cancellation still waiting for external tooling", file=sys.stderr, flush=True)
                        started = time.monotonic()
        if cancellation:
            return 128 + cancellation
        if code < 0:
            return 128 + (-code)
        return code
    finally:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _terminate_group(process, signal.SIGKILL, drain=False, lease=lease)
            process.wait()


def reserve(root: Path, resource_class: str, *, timeout: float | None = None, cancel_mode: str = "signal") -> Any:
    if resource_class not in CLASSES:
        raise CapacityError(f"unknown capacity class: {resource_class}")
    if cancel_mode not in {"signal", "drain"}:
        raise CapacityError(f"invalid cancel mode: {cancel_mode}")
    root = _canonical(root)
    worktree_path, worktree_fd = _worktree_lock(root)
    wait_handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    def wait_handler(signum: int, _frame: Any) -> None:
        raise Cancelled(signum)
    for signum in wait_handlers:
        signal.signal(signum, wait_handler)
    started = time.monotonic()
    lease: Lease | None = None
    admission_runtime: Path | None = None
    try:
        # Worktree identity is always acquired before admission/capacity.
        with _flock_wait(worktree_fd, root=root, label="worktree", timeout=timeout) as worktree_wait:
            admission_runtime = _runtime_root()
            admission_fd = _open_relative(admission_runtime, "admission.lock")
            try:
                with _flock_wait(admission_fd, root=root, label="admission", timeout=timeout):
                    _, limits = _ensure_policy(admission_runtime, admission_fd)
            finally:
                _unlock_close(admission_fd)
            deadline = started + timeout if timeout is not None else None
            cap_started = time.monotonic()
            next_report = cap_started
            first_wait = True
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    raise CapacityTimeout(f"timeout waiting for {resource_class} capacity")
                admission_fd = _open_relative(admission_runtime, "admission.lock")
                capacity_fd: int | None = None
                try:
                    remaining = max(0.0, deadline - time.monotonic()) if deadline is not None else None
                    with _flock_wait(admission_fd, root=root, label="admission", timeout=remaining):
                        _, limits = _ensure_policy(admission_runtime, admission_fd)
                        slot: int | None = None
                        for candidate in range(limits[resource_class]):
                            fd = _open_relative(admission_runtime, f"{resource_class}.{candidate}.lock")
                            if _locked_nonblocking(fd):
                                slot = candidate
                                capacity_fd = fd
                                break
                            _unlock_close(fd)
                        if capacity_fd is not None and slot is not None:
                            status_path = _slot_status_path(admission_runtime, resource_class, slot)
                            if status_path.exists():
                                status = _read_json(status_path)
                                if isinstance(status, dict) and status.get("state") in {"running", "recovery_required"}:
                                    raise CapacityError(f"slot {resource_class}.{slot} requires recovery")
                            run_id = uuid.uuid4().hex
                            run_path = _run_dir(admission_runtime, run_id)
                            _mkdir_private(run_path)
                            cancel_path = run_path / "cancel.signal"
                            fd = os.open(cancel_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                            os.write(fd, b"\0")
                            os.fsync(fd)
                            os.close(fd)
                            cancel_fd = os.open(cancel_path, os.O_RDWR | os.O_NOFOLLOW)
                            _check_owner_mode(cancel_path, directory=False)
                            owner_pid = os.getpid()
                            owner_start = _proc_start(owner_pid)
                            if owner_start is None:
                                raise CapacityError("cannot determine owner process start time")
                            lease = Lease(
                                root, resource_class, slot, run_id, owner_pid, owner_start,
                                worktree_fd, capacity_fd, admission_runtime, run_path,
                                cancel_fd, cancel_mode,
                            )
                            lease.worktree_wait = worktree_wait
                            lease.capacity_wait = time.monotonic() - cap_started
                            lease._write_status("running")
                            lease.install_handlers()
                            print(
                                f"local-checks: started {root} class={resource_class} "
                                f"slot={slot} wait={lease.capacity_wait:.3f}s",
                                flush=True,
                            )
                            return lease
                finally:
                    if lease is None and capacity_fd is not None:
                        _unlock_close(capacity_fd)
                    _unlock_close(admission_fd)
                if first_wait:
                    print(
                        f"local-checks: waiting for {root} {resource_class} capacity "
                        "(0s elapsed)",
                        file=sys.stderr,
                        flush=True,
                    )
                    first_wait = False
                elif time.monotonic() >= next_report:
                    print(
                        f"local-checks: waiting for {root} {resource_class} capacity "
                        f"({time.monotonic() - cap_started:.1f}s elapsed)",
                        file=sys.stderr,
                        flush=True,
                    )
                    next_report = time.monotonic() + REPORT_SECONDS
                time.sleep(POLL_SECONDS)
    except Exception:
        if lease is None:
            _unlock_close(worktree_fd)
        raise
    finally:
        if lease is None:
            for signum, handler in wait_handlers.items():
                signal.signal(signum, handler)


def ensure_capacity(root: Path, resource_class: str, argv: list[str], *, cancel_mode: str = "signal") -> None:
    lease = inherited_lease(root, resource_class)
    if lease is not None:
        try:
            result = run_command(argv, cwd=_canonical(root), env=os.environ, lease=lease)
        except BaseException:
            try:
                lease.finish()
            except CapacityError:
                pass
            raise
        try:
            lease.finish()
        except CapacityError:
            if result == 0:
                raise
        if result:
            raise SystemExit(result)
        return
    lease = reserve(root, resource_class, cancel_mode=cancel_mode)
    try:
        result = run_command(argv, cwd=_canonical(root), env=os.environ, lease=lease)
    except BaseException:
        try:
            lease.finish()
        except CapacityError:
            pass
        raise
    try:
        lease.finish()
    except CapacityError:
        if result == 0:
            raise
    if result:
        raise SystemExit(result)


def _finish_preserving_result(lease: Lease, result: int) -> int:
    try:
        lease.finish()
    except CapacityError:
        if result == 0:
            raise
    return result
def _cli_exec(args: argparse.Namespace) -> int:
    root = _canonical(args.repo)
    command = list(args.command)
    if not command:
        return _die("exec requires a command")
    inherited = inherited_lease(root, args.resource_class)
    if inherited is not None:
        try:
            result = run_command(command, cwd=root, env=os.environ, lease=inherited)
        except BaseException:
            try:
                inherited.finish()
            except CapacityError:
                pass
            raise
        return _finish_preserving_result(inherited, result)
    try:
        lease = reserve(root, args.resource_class, timeout=args.timeout, cancel_mode=args.cancel_mode)
        try:
            result = run_command(command, cwd=root, env=os.environ, lease=lease)
        except BaseException:
            try:
                lease.finish()
            except CapacityError:
                pass
            raise
        return _finish_preserving_result(lease, result)
    except CapacityTimeout as exc:
        print(f"local-checks: {exc}", file=sys.stderr)
        return 124
    except Cancelled as exc:
        return 128 + exc.signum




def _cli_inherited(args: argparse.Namespace) -> int:
    try:
        lease = inherited_lease(_canonical(args.repo), args.resource_class)
    except CapacityError as exc:
        return _die(str(exc), 2)
    if lease is None:
        return 3
    lease.finish()
    return 0


def _cli_cancelled(args: argparse.Namespace) -> int:
    try:
        lease = inherited_lease(_canonical(args.repo), "heavy")
    except CapacityError:
        try:
            lease = inherited_lease(_canonical(args.repo), "light")
        except CapacityError as exc:
            return _die(str(exc), 2)
    if lease is None:
        return 0
    signal_number = lease.cancelled_signal() or 0
    lease.finish()
    return signal_number + 128 if signal_number else 0


def _cli_resource_add(args: argparse.Namespace) -> int:
    try:
        lease = _validate_lease_token(
            json.loads(os.environ[TOKEN_ENV], object_pairs_hook=_duplicate_keys),
            _canonical(args.repo),
            args.resource_class,
        )
        lease.register_resource(args.kind, args.name, compose_file=args.compose_file, env_file=args.env_file)
        lease.finish()
        return 0
    except (KeyError, json.JSONDecodeError, CapacityError) as exc:
        return _die(str(exc), 2)


def _cli_recover(args: argparse.Namespace) -> int:
    if args.resource_class not in CLASSES or args.slot < 0:
        return _die("invalid recovery slot")
    try:
        runtime = _runtime_root()
        admission = _open_relative(runtime, "admission.lock")
        try:
            fcntl.flock(admission, fcntl.LOCK_EX)
            _, limits = _ensure_policy(runtime, admission)
            if args.slot >= limits[args.resource_class]:
                return _die("slot is outside active policy")
            slot_fd = _open_relative(runtime, f"{args.resource_class}.{args.slot}.lock")
            try:
                try:
                    fcntl.flock(slot_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return _die("slot still has an active lease")
                status_path = _slot_status_path(runtime, args.resource_class, args.slot)
                status = _read_json(status_path) if status_path.exists() else None
                if not isinstance(status, dict) or status.get("run_id") != args.run_id or status.get("state") not in {"running", "recovery_required"}:
                    return _die("recovery run-id or state mismatch")
                status["state"] = "complete"
                _atomic_json(status_path, status)
                return 0
            finally:
                _unlock_close(slot_fd)
        finally:
            _unlock_close(admission)
    except CapacityError as exc:
        return _die(str(exc), 2)


def _cli_cancelled(args: argparse.Namespace) -> int:
    raw = os.environ.get(TOKEN_ENV)
    if raw is None:
        return 0
    try:
        data = json.loads(raw, object_pairs_hook=_duplicate_keys)
        resource_class = data.get("class") if isinstance(data, dict) else None
        if resource_class not in CLASSES:
            raise CapacityError("invalid LOCAL_CHECKS_LEASE class")
        lease = inherited_lease(_canonical(args.repo), resource_class)
    except (CapacityError, json.JSONDecodeError) as exc:
        return _die(str(exc), 2)
    if lease is None:
        return 0
    signal_number = lease.cancelled_signal() or 0
    lease.finish()
    return signal_number + 128 if signal_number else 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="action", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--repo", type=Path, required=True)
        command.add_argument("--class", dest="resource_class", choices=sorted(CLASSES), required=True)

    command = sub.add_parser("exec")
    common(command)
    command.add_argument("--timeout", type=float)
    command.add_argument("--cancel-mode", choices=["signal", "drain"], default="signal")
    command.add_argument("command", nargs=argparse.REMAINDER)
    command = sub.add_parser("inherited")
    common(command)
    command = sub.add_parser("cancelled")
    command.add_argument("--repo", type=Path, required=True)
    command = sub.add_parser("resource-add")
    common(command)
    command.add_argument("--kind", choices=["compose", "container", "network"], required=True)
    command.add_argument("--name", required=True)
    command.add_argument("--compose-file", type=Path)
    command.add_argument("--env-file", type=Path)
    command = sub.add_parser("recover")
    command.add_argument("--class", dest="resource_class", choices=sorted(CLASSES), required=True)
    command.add_argument("--slot", type=int, required=True)
    command.add_argument("--run-id", required=True)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.action == "exec":
            if args.command and args.command[0] == "--":
                args.command = args.command[1:]
            return _cli_exec(args)
        if args.action == "inherited":
            return _cli_inherited(args)
        if args.action == "cancelled":
            return _cli_cancelled(args)
        if args.action == "resource-add":
            return _cli_resource_add(args)
        if args.action == "recover":
            return _cli_recover(args)
        return _die("unknown capacity action")
    except Cancelled as exc:
        return 128 + exc.signum
    except CapacityTimeout:
        return 124
    except (CapacityError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        return _die(str(exc), 2)


if __name__ == "__main__":
    raise SystemExit(main())
