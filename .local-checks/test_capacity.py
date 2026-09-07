"""Subprocess evidence for the shared local-checks capacity protocol."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

CAPACITY = Path(__file__).with_name("capacity.py")
RUNNER_SOURCES = tuple(
    Path(value)
    for value in os.environ.get("LOCAL_CHECKS_TEST_RUNNERS", "").split(os.pathsep)
    if value
)


class CapacityFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="capacity-test-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.config = base / "config"
        self.runtime = base / "runtime"
        self.config.mkdir(mode=0o700)
        self.runtime.mkdir(mode=0o700)
        self.env = dict(
            os.environ,
            XDG_CONFIG_HOME=str(self.config),
            XDG_RUNTIME_DIR=str(self.runtime),
            PYTHONDONTWRITEBYTECODE="1",
        )
        for key in ("LOCAL_CHECKS_LEASE", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.env.pop(key, None)
        self.repos: list[Path] = []
        self.runner_capacities = [
            source.with_name("capacity.py")
            for source in RUNNER_SOURCES
            if source.is_file() and source.with_name("capacity.py").is_file()
        ]

    def repo(self, name: str) -> Path:
        root = Path(self.temp.name) / name
        root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, env=self.env)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Capacity Test"], check=True, env=self.env)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "capacity@example.invalid"], check=True, env=self.env)
        (root / "fixture.txt").write_text(name)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True, env=self.env)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True, env=self.env)
        self.repos.append(root)
        return root

    def command(self, root: Path, resource_class: str, body: str, *options: str, source: Path = CAPACITY) -> list[str]:
        return [
            "python3", str(source), "exec", "--repo", str(root), "--class", resource_class,
            *options, "--", "python3", "-c", body,
        ]


    def wait_for_output(self, process: subprocess.Popen[str], timeout: float = 3.0) -> str:
        selector = selectors.DefaultSelector()
        streams = [stream for stream in (process.stdout, process.stderr) if stream is not None]
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ)
        events = selector.select(timeout)
        if not events:
            self.fail("capacity subprocess produced no wait/start evidence")
        return events[0][0].fileobj.readline()


class CapacityBehaviorTest(CapacityFixture):
    def test_heavy_is_global_across_independent_repositories(self):
        first = self.repo("first")
        second = self.repo("second")
        body = 'from pathlib import Path; import time; Path("started").write_text("yes"); time.sleep(.7)'
        p1 = subprocess.Popen(self.command(first, "heavy", body), cwd=first, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.1)
        p2 = subprocess.Popen(self.command(second, "heavy", body), cwd=second, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.wait_for_output(p2)
        self.assertFalse((second / "started").exists())
        self.assertEqual(p1.wait(timeout=4), 0)
        self.assertEqual(p2.wait(timeout=4), 0)
        p1.communicate(timeout=1)
        p2.communicate(timeout=1)
        self.assertTrue((second / "started").exists())

    def test_two_light_slots_can_run_and_third_waits(self):
        roots = [self.repo(f"light-{index}") for index in range(3)]
        body = 'from pathlib import Path; import time; Path("started").write_text("yes"); time.sleep(.6)'
        processes = [
            subprocess.Popen(
                self.command(root, "light", body),
                cwd=root,
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for root in roots
        ]
        try:
            deadline = time.monotonic() + 3
            while sum((root / "started").exists() for root in roots) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(sum((root / "started").exists() for root in roots), 2)
            waiting_index = next(index for index, root in enumerate(roots) if not (root / "started").exists())
            line = self.wait_for_output(processes[waiting_index])
            self.assertIn("capacity", line)
            self.assertTrue(all(process.wait(timeout=4) == 0 for process in processes))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=2)

    def test_same_worktree_is_serialized_before_capacity(self):
        root = self.repo("same")
        body = 'from pathlib import Path; import time; Path("started").write_text("yes"); time.sleep(.7)'
        p1 = subprocess.Popen(self.command(root, "light", body), cwd=root, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.1)
        p2 = subprocess.Popen(self.command(root, "light", body), cwd=root, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        line = self.wait_for_output(p2)
        self.assertIn("worktree", line)
        self.assertEqual(p1.wait(timeout=4), 0)
        self.assertEqual(p2.wait(timeout=4), 0)
        p1.communicate(timeout=1)
        p2.communicate(timeout=1)

    def test_signal_during_active_child_returns_signal_status(self):
        root = self.repo("signal")
        p = subprocess.Popen(self.command(root, "heavy", 'import time; time.sleep(30)'), cwd=root, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.3)
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=12), 143)
        p.communicate(timeout=1)
        probe = subprocess.run(self.command(root, "heavy", "print('reusable')"), cwd=root, env=self.env, text=True, capture_output=True, timeout=5)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn("reusable", probe.stdout)

    def test_timeout_during_capacity_wait_is_124(self):
        first = self.repo("timeout-first")
        second = self.repo("timeout-second")
        p1 = subprocess.Popen(self.command(first, "heavy", "import time; time.sleep(1)"), cwd=first, env=self.env, text=True)
        time.sleep(0.15)
        p2 = subprocess.run(self.command(second, "heavy", "print('must-not-run')", "--timeout", "0.25"), cwd=second, env=self.env, text=True, capture_output=True, timeout=5)
        self.assertEqual(p2.returncode, 124, p2.stderr)
        self.assertNotIn("must-not-run", p2.stdout)
        self.assertEqual(p1.wait(timeout=4), 0)
    def test_nested_exec_reuses_live_lease_without_deadlock(self):
        root = self.repo("nested")
        inner = (
            "import subprocess; "
            "raise SystemExit(subprocess.run([\"python3\", %r, \"exec\", \"--repo\", %r, "
            "\"--class\", \"heavy\", \"--\", \"python3\", \"-c\", \"print('nested-ok')\"], "
            "env=__import__('os').environ.copy()).returncode)"
            % (str(CAPACITY), str(root))
        )
        result = subprocess.run(
            self.command(root, "heavy", inner),
            cwd=root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nested-ok", result.stdout)

    def test_forged_lease_token_fails_before_tooling(self):
        root = self.repo("forged-token")
        token = {
            "version": 1,
            "run_id": "0" * 32,
            "owner_pid": os.getpid(),
            "owner_start": 1,
            "root": str(root),
            "class": "heavy",
            "slot": 0,
            "worktree_fd": -1,
            "capacity_fd": -1,
        }
        env = dict(self.env, LOCAL_CHECKS_LEASE=json.dumps(token))
        result = subprocess.run(
            self.command(root, "heavy", "print('must-not-run')"),
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("must-not-run", result.stdout)
    def test_uv_guard_rejects_forged_lease_before_docker(self):
        guard = CAPACITY.parent.parent / "bin/check-docker-context"
        if not guard.is_file():
            self.skipTest("UV guard wrapper is not part of this fixture root")
        fake_bin = Path(self.temp.name) / "forged-guard-bin"
        fake_bin.mkdir(mode=0o700)
        docker_log = Path(self.temp.name) / "forged-guard-docker.log"
        docker = fake_bin / "docker"
        docker.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{docker_log}"\nexit 0\n')
        docker.chmod(0o700)
        env = dict(
            self.env,
            PATH=str(fake_bin) + os.pathsep + self.env["PATH"],
            LOCAL_CHECKS_LEASE=json.dumps({"version": 1}),
        )
        result = subprocess.run(
            [str(guard)],
            cwd=guard.parent.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(docker_log.exists() and docker_log.read_text().strip())

    def test_uv_guard_cancels_wait_without_touching_docker(self):
        guard = CAPACITY.parent.parent / "bin/check-docker-context"
        if not guard.is_file():
            self.skipTest("UV guard wrapper is not part of this fixture root")
        holder_root = self.repo("guard-holder")
        marker = holder_root / "started"
        holder_body = (
            "from pathlib import Path; import time; "
            f"Path({str(marker)!r}).write_text('yes'); time.sleep(4)"
        )
        holder = subprocess.Popen(
            self.command(holder_root, "heavy", holder_body),
            cwd=holder_root,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        fake_bin = Path(self.temp.name) / "guard-fake-bin"
        fake_bin.mkdir(mode=0o700)
        docker_log = Path(self.temp.name) / "guard-docker.log"
        docker = fake_bin / "docker"
        docker.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{docker_log}"\nexit 0\n')
        docker.chmod(0o700)
        guard_env = dict(self.env, PATH=str(fake_bin) + os.pathsep + self.env["PATH"])
        waiting = None
        try:
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists())
            waiting = subprocess.Popen(
                [str(guard)],
                cwd=guard.parent.parent,
                env=guard_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            time.sleep(0.4)
            waiting.send_signal(signal.SIGTERM)
            self.assertEqual(waiting.wait(timeout=12), 143)
            waiting.communicate(timeout=1)
            self.assertEqual(holder.wait(timeout=6), 0)
            holder.communicate(timeout=1)
            self.assertFalse(docker_log.exists() and docker_log.read_text().strip())
            reusable = subprocess.run(
                self.command(holder_root, "heavy", "print('reusable-after-guard')"),
                cwd=holder_root,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(reusable.returncode, 0, reusable.stderr)
            self.assertIn("reusable-after-guard", reusable.stdout)
        finally:
            if waiting is not None and waiting.poll() is None:
                waiting.kill()
                waiting.communicate(timeout=2)
            if holder.poll() is None:
                holder.kill()
                holder.communicate(timeout=2)



    def test_invalid_machine_config_fails_before_tooling(self):
        root = self.repo("invalid-config")
        config = self.config / "local-checks"
        config.mkdir(mode=0o700)
        (config / "capacity.json").write_text(json.dumps({"version": 1, "limits": {"heavy": True, "light": 2}}))
        probe = subprocess.run(self.command(root, "heavy", "print('must-not-run')"), cwd=root, env=self.env, text=True, capture_output=True, timeout=5)
        self.assertEqual(probe.returncode, 2)
        self.assertNotIn("must-not-run", probe.stdout)

    def test_policy_change_requires_drain_then_applies(self):
        first = self.repo("policy-first")
        second = self.repo("policy-second")
        body = 'from pathlib import Path; import time; Path("started").write_text("yes"); time.sleep(.8)'
        p1 = subprocess.Popen(self.command(first, "heavy", body), cwd=first, env=self.env)
        deadline = time.monotonic() + 3
        while not (first / "started").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        policy = self.config / "local-checks/capacity.json"
        policy.write_text(json.dumps({"version": 1, "limits": {"heavy": 2, "light": 2}}))
        policy.chmod(0o600)
        blocked = subprocess.run(
            self.command(second, "heavy", "print('must-not-run')", "--timeout", "1"),
            cwd=second,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertNotIn("must-not-run", blocked.stdout)
        self.assertEqual(p1.wait(timeout=4), 0)
        applied = subprocess.run(
            self.command(second, "heavy", "print('updated-policy')"),
            cwd=second,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn("updated-policy", applied.stdout)

    def test_check_failure_survives_cleanup_failure(self):
        root = self.repo("resource-failure")
        fake_bin = Path(self.temp.name) / "cleanup-fail-bin"
        fake_bin.mkdir(mode=0o700)
        docker = fake_bin / "docker"
        docker.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = "rm" ]; then printf "%s\\n" "daemon unavailable" >&2; exit 42; fi\n'
            'exit 0\n'
        )
        docker.chmod(0o700)
        env = dict(self.env, PATH=str(fake_bin) + os.pathsep + self.env["PATH"])
        body = (
            'import json, os, subprocess; '
            'd=json.loads(os.environ["LOCAL_CHECKS_LEASE"]); '
            'name="localchecks-"+d["run_id"]+"-owned"; '
            'subprocess.run(["python3", %r, "resource-add", "--repo", %r, "--class", "heavy", "--kind", "container", "--name", name], env=os.environ.copy(), check=True); '
            'raise SystemExit(17)'
            % (str(CAPACITY), str(root))
        )
        result = subprocess.run(
            self.command(root, "heavy", body),
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        status = json.loads((self.runtime / "local-checks/heavy.0.json").read_text())
        self.assertEqual(status["state"], "recovery_required")

    def test_recovery_marker_blocks_slot_until_explicit_recovery(self):
        root = self.repo("recovery")
        fake_bin = Path(self.temp.name) / "fake-bin"
        fake_bin.mkdir(mode=0o700)
        docker = fake_bin / "docker"
        docker.write_text("#!/bin/sh\nexit 1\n")
        docker.chmod(0o700)
        env = dict(self.env, PATH=str(fake_bin) + os.pathsep + self.env["PATH"])
        body = (
            'import json, os, subprocess; '
            'd=json.loads(os.environ["LOCAL_CHECKS_LEASE"]); '
            'name="localchecks-"+d["run_id"]+"-owned"; '
            'raise SystemExit(subprocess.run(["python3", %r, "resource-add", "--repo", %r, "--class", "heavy", "--kind", "container", "--name", name], env=os.environ.copy()).returncode)'
            % (str(CAPACITY), str(root))
        )
        failed = subprocess.run(
            self.command(root, "heavy", body),
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(failed.returncode, 2, failed.stderr)
        status_path = self.runtime / "local-checks/heavy.0.json"
        status = json.loads(status_path.read_text())
        self.assertEqual(status["state"], "recovery_required")
        blocked = subprocess.run(
            self.command(root, "heavy", "print('must-not-run')"),
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(blocked.returncode, 2)
        recovered = subprocess.run(
            ["python3", str(CAPACITY), "recover", "--class", "heavy", "--slot", "0", "--run-id", status["run_id"]],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        reusable = subprocess.run(
            self.command(root, "heavy", "print('recovered')"),
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(reusable.returncode, 0, reusable.stderr)
        self.assertIn("recovered", reusable.stdout)

    def test_registered_resource_cleanup_is_successful_when_docker_succeeds(self):
        root = self.repo("resource-success")
        fake_bin = Path(self.temp.name) / "resource-fake-bin"
        fake_bin.mkdir(mode=0o700)
        log = Path(self.temp.name) / "resource-docker.log"
        docker = fake_bin / "docker"
        docker.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexit 0\n')
        docker.chmod(0o700)
        env = dict(self.env, PATH=str(fake_bin) + os.pathsep + self.env["PATH"])
        body = (
            'import json, os, subprocess; '
            'd=json.loads(os.environ["LOCAL_CHECKS_LEASE"]); '
            'name="localchecks-"+d["run_id"]+"-owned"; '
            'raise SystemExit(subprocess.run(["python3", %r, "resource-add", "--repo", %r, "--class", "heavy", "--kind", "container", "--name", name], env=os.environ.copy()).returncode)'
            % (str(CAPACITY), str(root))
        )
        result = subprocess.run(
            self.command(root, "heavy", body),
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads((self.runtime / "local-checks/heavy.0.json").read_text())
        self.assertEqual(status["state"], "complete")
        self.assertIn("rm --force localchecks-", log.read_text())

    def test_registered_resources_cleanup_containers_before_network(self):
        root = self.repo("resource-order")
        fake_bin = Path(self.temp.name) / "resource-order-bin"
        fake_bin.mkdir(mode=0o700)
        log = Path(self.temp.name) / "resource-order-docker.log"
        docker = fake_bin / "docker"
        docker.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexit 0\n')
        docker.chmod(0o700)
        env = dict(self.env, PATH=str(fake_bin) + os.pathsep + self.env["PATH"])
        body = (
            'import json, os, subprocess; '
            'd=json.loads(os.environ["LOCAL_CHECKS_LEASE"]); '
            'prefix="localchecks-"+d["run_id"]; '
            'items=[("network","network"),("container","db")]; '
            '[subprocess.run(["python3", %r, "resource-add", "--repo", %r, "--class", "heavy", "--kind", kind, "--name", prefix+"-"+suffix], env=os.environ.copy(), check=True) for kind,suffix in items]; '
            'print("registered")'
            % (str(CAPACITY), str(root))
        )
        result = subprocess.run(
            self.command(root, "heavy", body),
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = log.read_text().splitlines()
        container_index = next(index for index, line in enumerate(lines) if line.startswith("rm --force localchecks-"))
        network_index = next(index for index, line in enumerate(lines) if line.startswith("network rm localchecks-"))
        self.assertLess(container_index, network_index)
    def test_killed_supervisor_leaves_running_marker_until_recovery(self):
        root = self.repo("killed-supervisor")
        child_pid_path = root / "child.pid"
        process = subprocess.Popen(
            self.command(
                root,
                "heavy",
                'from pathlib import Path; import os, time; Path("child.pid").write_text(str(os.getpid())); time.sleep(5)',
            ),
            cwd=root,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.wait_for_output(process)
        deadline = time.monotonic() + 3
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(child_pid_path.exists())
        child_pid = int(child_pid_path.read_text())
        child_pgid = os.getpgid(child_pid)
        process.kill()
        self.assertEqual(process.wait(timeout=4), -signal.SIGKILL)
        try:
            os.killpg(child_pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=1)
        deadline = time.monotonic() + 3
        while Path(f"/proc/{child_pid}/stat").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if Path(f"/proc/{child_pid}/stat").exists():
            state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
            self.assertEqual(state, "Z")
        status_path = self.runtime / "local-checks/heavy.0.json"
        status = json.loads(status_path.read_text())
        self.assertEqual(status["state"], "running")
        blocked = subprocess.run(
            self.command(root, "heavy", "print('must-not-run')"),
            cwd=root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertNotIn("must-not-run", blocked.stdout)
        recovered = subprocess.run(
            ["python3", str(CAPACITY), "recover", "--class", "heavy", "--slot", "0", "--run-id", status["run_id"]],
            cwd=root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        reusable = subprocess.run(
            self.command(root, "heavy", "print('reusable-after-kill')"),
            cwd=root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(reusable.returncode, 0, reusable.stderr)
        self.assertIn("reusable-after-kill", reusable.stdout)

    def test_distinct_runner_copies_share_the_same_pool(self):
        if len(self.runner_capacities) < 2:
            self.skipTest("set LOCAL_CHECKS_TEST_RUNNERS to two runner sources")
        first = self.repo("runner-copy-first")
        second = self.repo("runner-copy-second")
        body = 'from pathlib import Path; import time; Path("started").write_text("yes"); time.sleep(.5)'
        p1 = subprocess.Popen(
            self.command(first, "heavy", body, source=self.runner_capacities[0]),
            cwd=first,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.1)
        p2 = subprocess.Popen(
            self.command(second, "heavy", body, source=self.runner_capacities[1]),
            cwd=second,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.wait_for_output(p2)
        self.assertFalse((second / "started").exists())
        self.assertEqual(p1.wait(timeout=4), 0)
        self.assertEqual(p2.wait(timeout=4), 0)
        p1.communicate(timeout=1)
        p2.communicate(timeout=1)
        self.assertTrue((second / "started").exists())

if __name__ == "__main__":
    unittest.main()
