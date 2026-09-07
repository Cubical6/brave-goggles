"""Behavioral checks for hook failure propagation, worktrees and preservation."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).with_name("run.py")
CAPACITY_SOURCE = Path(__file__).with_name("capacity.py")


class HooksTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="local-checks-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo with spaces"
        self.root.mkdir()
        self.xdg_config = Path(self.temp.name) / "xdg-config"
        self.xdg_runtime = Path(self.temp.name) / "xdg-runtime"
        self.xdg_config.mkdir(mode=0o700)
        self.xdg_runtime.mkdir(mode=0o700)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir(mode=0o700)
        self.global_config = self.home / "gitconfig"
        self.system_config = self.home / "system-gitconfig"
        self.env = dict(
            os.environ,
            HOME=str(self.home),
            XDG_CONFIG_HOME=str(self.xdg_config),
            XDG_RUNTIME_DIR=str(self.xdg_runtime),
            GIT_CONFIG_GLOBAL=str(self.global_config),
            GIT_CONFIG_SYSTEM=str(self.system_config),
            GIT_CONFIG_NOSYSTEM="1",
            PYTHONDONTWRITEBYTECODE="1",
        )
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "LOCAL_CHECKS_LEASE"):
            self.env.pop(key, None)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Local Test")
        self.git("config", "user.email", "local@example.invalid")
        folder = self.root / ".local-checks"
        folder.mkdir()
        shutil.copyfile(SOURCE, folder / "run.py")
        shutil.copyfile(CAPACITY_SOURCE, folder / "capacity.py")
        self.config = folder / "config.json"
        self.write_config([])

    def git(self, *args, cwd=None, **kwargs):
        return subprocess.run(
            ["git", "-C", str(cwd or self.root), *args],
            capture_output=True,
            text=True,
            env=self.env,
            **kwargs,
            check=False,
        )

    def write_config(self, steps, path=None):
        (path or self.config).write_text(
            json.dumps(
                {
                    "name": "fixture",
                    "quick": [],
                    "full": steps,
                    "integration": [],
                    "capacity": {"quick": "light", "full": "heavy", "integration": "heavy"},
                }
            )
        )

    def install(self):
        result = subprocess.run(
            [sys.executable, str(self.root / ".local-checks/run.py"), "install"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def commit(self):
        self.git("add", ".")
        result = self.git("commit", "-m", "fixture")
        self.assertEqual(result.returncode, 0, result.stderr)

    def push_hook(self, sha=None, cwd=None):
        directory = Path(self.git("config", "--get", "core.hooksPath").stdout.strip())
        sha = sha or self.git("rev-parse", "HEAD", cwd=cwd).stdout.strip()
        payload = f"refs/heads/main {sha} refs/heads/main {'0' * 40}\n"
        return subprocess.run(
            [str(directory / "pre-push"), "origin", "unused"],
            cwd=cwd or self.root,
            env=self.env,
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_worktree_alias_does_not_leak_git_context_to_checks(self):
        other = Path(self.temp.name) / "fixture repo"
        other.mkdir()
        self.assertEqual(self.git("init", "-b", "main", cwd=other).returncode, 0)
        command = (
            "import subprocess; "
            f"actual = subprocess.check_output(['git', '-C', {str(other)!r}, "
            "'rev-parse', '--show-toplevel'], text=True).strip(); "
            f"assert actual == {str(other)!r}, actual"
        )
        self.write_config([{"argv": [sys.executable, "-c", command]}])
        self.install()
        self.commit()
        linked = Path(self.temp.name) / "linked"
        self.assertEqual(self.git("worktree", "add", "-b", "other", str(linked)).returncode, 0)
        before = self.git("rev-parse", "HEAD", cwd=linked).stdout
        result = self.git("local-checks", "full", cwd=linked)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=linked).stdout, before)

    def test_failed_checks_block_push_and_do_not_call_legacy(self):
        legacy = self.root / ".git/hooks/pre-push"
        legacy.write_text("#!/bin/sh\ntouch legacy-ran\n")
        legacy.chmod(493)
        self.write_config([{"argv": [sys.executable, "-c", "raise SystemExit(19)"]}])
        self.install()
        self.commit()
        result = self.push_hook()
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertFalse((self.root / "legacy-ran").exists())

    def test_existing_hook_gets_arguments_and_stdin_after_success(self):
        legacy = self.root / ".git/hooks/pre-push"
        legacy.write_text(
            '#!/bin/sh\n[ "$1" = origin ] || exit 8\nread a b c d\n[ "$a" = refs/heads/main ] || exit 9\nexit 23\n'
        )
        legacy.chmod(493)
        self.install()
        self.install()
        self.commit()
        self.assertEqual(self.push_hook().returncode, 23)

    def test_dirty_tree_and_wrong_commit_rejected(self):
        self.install()
        self.commit()
        (self.root / "pending").write_text("pending")
        self.assertNotEqual(self.push_hook().returncode, 0)
        (self.root / "pending").unlink()
        self.assertNotEqual(self.push_hook("1" * 40).returncode, 0)

    def test_worktree_uses_its_own_configuration(self):
        self.install()
        self.commit()
        linked = Path(self.temp.name) / "linked"
        result = self.git("worktree", "add", "-b", "other", str(linked))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.write_config(
            [{"argv": [sys.executable, "-c", "raise SystemExit(17)"]}],
            linked / ".local-checks/config.json",
        )
        self.git("add", ".", cwd=linked)
        self.git("commit", "-m", "different checks", cwd=linked)
        self.assertEqual(self.push_hook(cwd=linked).returncode, 17)
        self.assertEqual(self.push_hook().returncode, 0)

    def test_real_git_push_runs_hook(self):
        self.write_config([{"argv": [sys.executable, "-c", "raise SystemExit(13)"]}])
        self.install()
        self.commit()
        remote = Path(self.temp.name) / "remote.git"
        self.git("init", "--bare", str(remote))
        result = self.git("push", str(remote), "HEAD:main")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("local-checks:", result.stdout + result.stderr)

    def test_staged_whitespace_blocks_commit(self):
        self.install()
        (self.root / "bad.txt").write_text("bad trailing space \n")
        self.git("add", ".")
        self.assertNotEqual(self.git("commit", "-m", "bad").returncode, 0)

    def test_check_that_modifies_source_blocks_push(self):
        self.write_config([{"argv": [sys.executable, "-c", 'from pathlib import Path; Path("changed").write_text("oops")']}])
        self.install()
        self.commit()
        result = self.push_hook()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Checks changed the worktree", result.stderr)

    def test_annotated_tag_at_head_is_checked(self):
        self.install()
        self.commit()
        self.git("tag", "-a", "v1", "-m", "release")
        tag = self.git("rev-parse", "v1").stdout.strip()
        self.assertEqual(self.push_hook(tag).returncode, 0)

    def test_uninstall_restores_original_hook_path(self):
        original = self.root / "previous hooks"
        original.mkdir()
        self.git("config", "core.hooksPath", str(original))
        self.install()
        result = subprocess.run(
            [sys.executable, str(self.root / ".local-checks/run.py"), "uninstall"],
            cwd=self.root,
            env=self.env,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.git("config", "--get", "core.hooksPath").stdout.strip(), str(original))


if __name__ == "__main__":
    unittest.main()
