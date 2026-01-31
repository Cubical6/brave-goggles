"""Tests for build-goggles.py — stack detection, goggle assembly, and CLI."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Import the module under test
sys.path.insert(0, str(Path(__file__).parent))
import importlib

spec = importlib.util.spec_from_file_location(
    "build_goggles",
    Path(__file__).parent / "build-goggles.py",
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


# ── detect_stacks ─────────────────────────────────────────────────────


class TestDetectStacks:
    def test_laravel_implies_php(self, tmp_path):
        (tmp_path / "composer.json").write_text(json.dumps({
            "require": {"laravel/framework": "^11.0"},
        }))
        stacks = mod.detect_stacks(tmp_path)
        assert "laravel" in stacks
        assert "php" in stacks

    def test_cakephp_implies_php(self, tmp_path):
        (tmp_path / "composer.json").write_text(json.dumps({
            "require": {"cakephp/cakephp": "^5.0"},
        }))
        stacks = mod.detect_stacks(tmp_path)
        assert "cakephp" in stacks
        assert "php" in stacks

    def test_bare_composer_is_php_only(self, tmp_path):
        (tmp_path / "composer.json").write_text(json.dumps({
            "require": {"vendor/generic": "^1.0"},
        }))
        stacks = mod.detect_stacks(tmp_path)
        assert "php" in stacks
        assert "laravel" not in stacks
        assert "cakephp" not in stacks

    def test_svelte_implies_js_ts_and_frontend(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({
            "devDependencies": {"@sveltejs/kit": "^2.0"},
        }))
        stacks = mod.detect_stacks(tmp_path)
        assert "svelte" in stacks
        assert "js-ts" in stacks
        assert "frontend" in stacks

    def test_vue_implies_js_ts_and_frontend(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({
            "dependencies": {"vue": "^3.0"},
        }))
        stacks = mod.detect_stacks(tmp_path)
        assert "vue" in stacks
        assert "js-ts" in stacks
        assert "frontend" in stacks

    def test_bare_package_json_is_js_ts_only(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({
            "dependencies": {"express": "^4.0"},
        }))
        stacks = mod.detect_stacks(tmp_path)
        assert "js-ts" in stacks
        assert "frontend" not in stacks
        assert "svelte" not in stacks

    def test_requirements_txt_is_python(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask>=2.3\n")
        assert "python" in mod.detect_stacks(tmp_path)

    def test_pyproject_toml_is_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
        assert "python" in mod.detect_stacks(tmp_path)

    def test_no_manifests_returns_empty(self, tmp_path):
        assert mod.detect_stacks(tmp_path) == set()

    def test_multi_stack_laravel_svelte(self, tmp_path):
        """Laravel + Svelte project yields laravel, php, svelte, js-ts, frontend."""
        (tmp_path / "composer.json").write_text(json.dumps({
            "require": {"laravel/framework": "^11.0"},
        }))
        (tmp_path / "package.json").write_text(json.dumps({
            "devDependencies": {"@sveltejs/kit": "^2.0"},
        }))
        stacks = mod.detect_stacks(tmp_path)
        assert stacks == {"laravel", "php", "svelte", "js-ts", "frontend"}

    def test_invalid_json_still_detects_manifest_presence(self, tmp_path):
        """Bad JSON prevents pattern match but presence signal still fires."""
        (tmp_path / "composer.json").write_text("NOT JSON")
        stacks = mod.detect_stacks(tmp_path)
        assert "php" in stacks  # presence signal fires
        assert "laravel" not in stacks  # pattern match failed

    def test_laravel_in_require_dev(self, tmp_path):
        (tmp_path / "composer.json").write_text(json.dumps({
            "require-dev": {"laravel/framework": "^11.0"},
        }))
        stacks = mod.detect_stacks(tmp_path)
        assert "laravel" in stacks


# ── resolve_project_name ──────────────────────────────────────────────


class TestResolveProjectName:
    def test_plain_directory(self, tmp_path):
        proj = tmp_path / "my-project"
        proj.mkdir()
        assert mod.resolve_project_name(proj) == "my-project"

    def test_strips_feature_suffix(self, tmp_path):
        proj = tmp_path / "core-worktree.feature-page-tests"
        proj.mkdir()
        assert mod.resolve_project_name(proj) == "core-worktree"

    def test_strips_feature_no_extra(self, tmp_path):
        proj = tmp_path / "app.feature"
        proj.mkdir()
        assert mod.resolve_project_name(proj) == "app"

    def test_no_feature_suffix_unchanged(self, tmp_path):
        proj = tmp_path / "app-v2"
        proj.mkdir()
        assert mod.resolve_project_name(proj) == "app-v2"


# ── load_rules ────────────────────────────────────────────────────────


class TestLoadRules:
    def test_loads_existing_file(self):
        content = mod.load_rules("base.rules")
        assert "$discard" in content
        assert content == content.rstrip("\n")  # trailing newlines stripped

    def test_missing_file_returns_empty(self, capsys):
        result = mod.load_rules("nonexistent.rules")
        assert result == ""
        assert "Warning" in capsys.readouterr().err


# ── generate_header ───────────────────────────────────────────────────


class TestGenerateHeader:
    def test_with_stacks(self):
        header = mod.generate_header("myapp", {"laravel", "php"})
        assert "! name: myapp" in header
        assert "laravel, php" in header
        assert "! public: false" in header

    def test_empty_stacks_shows_generic(self):
        header = mod.generate_header("myapp", set())
        assert "(generic)" in header


# ── assemble_goggle ──────────────────────────────────────────────────


class TestAssembleGoggle:
    def test_dry_run_skips_github_org_rules(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask>=2.3\n")
        goggle = mod.assemble_goggle(tmp_path, dry_run=True)
        assert "! name:" in goggle
        assert "$discard" in goggle  # base rules included
        assert "GitHub Dependencies" not in goggle

    def test_includes_stack_rules(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask>=2.3\n")
        goggle = mod.assemble_goggle(tmp_path, dry_run=True)
        assert "python" in goggle.lower()  # python rules present

    def test_extra_rules_included(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask>=2.3\n")
        goggle = mod.assemble_goggle(tmp_path, extra_rules=["integrations"], dry_run=True)
        # integrations.rules content should appear
        assert "exactonline" in goggle.lower() or "openai" in goggle.lower() or "integrations" in goggle.lower()

    def test_no_duplicate_rules_files(self, tmp_path):
        """Even if stack implies frontend twice, rules only loaded once."""
        (tmp_path / "package.json").write_text(json.dumps({
            "devDependencies": {"@sveltejs/kit": "^2.0"},
        }))
        goggle = mod.assemble_goggle(tmp_path, dry_run=True)
        # base.rules $discard should appear exactly once
        assert goggle.count("$discard") == 1

    def test_ends_with_newline(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask\n")
        goggle = mod.assemble_goggle(tmp_path, dry_run=True)
        assert goggle.endswith("\n")

    @patch("time.sleep")
    def test_with_github_org_rules(self, _sleep, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text(json.dumps({
            "dependencies": {"react": "^18"},
        }))
        # Mock resolve_github_orgs functions to avoid network
        monkeypatch.setattr(mod, "build_github_org_rules",
                            lambda p: ["/facebook/$boost=3,site=github.com"])
        goggle = mod.assemble_goggle(tmp_path, dry_run=False)
        assert "GitHub Dependencies" in goggle
        assert "/facebook/" in goggle


# ── find_all_projects ─────────────────────────────────────────────────


class TestFindAllProjects:
    def test_finds_projects_with_manifests(self, tmp_path, monkeypatch):
        github = tmp_path / "github"
        proj = github / "myapp"
        proj.mkdir(parents=True)
        (proj / "package.json").write_text("{}")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        projects = mod.find_all_projects()
        assert proj in projects

    def test_skips_hidden_dirs(self, tmp_path, monkeypatch):
        github = tmp_path / "github"
        hidden = github / ".hidden"
        hidden.mkdir(parents=True)
        (hidden / "package.json").write_text("{}")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        projects = mod.find_all_projects()
        assert hidden not in projects

    def test_skips_dirs_without_manifests(self, tmp_path, monkeypatch):
        github = tmp_path / "github"
        proj = github / "docs-only"
        proj.mkdir(parents=True)
        (proj / "README.md").write_text("# Docs")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        projects = mod.find_all_projects()
        assert proj not in projects


# ── group_by_project_name ─────────────────────────────────────────────


class TestGroupByProjectName:
    def test_collapses_worktrees(self, tmp_path):
        main = tmp_path / "myapp"
        wt = tmp_path / "myapp.feature-login"
        main.mkdir()
        wt.mkdir()
        groups = mod.group_by_project_name([main, wt])
        assert "myapp" in groups
        assert len(groups["myapp"]) == 2

    def test_distinct_projects_separate(self, tmp_path):
        a = tmp_path / "app-a"
        b = tmp_path / "app-b"
        a.mkdir()
        b.mkdir()
        groups = mod.group_by_project_name([a, b])
        assert "app-a" in groups
        assert "app-b" in groups


# ── update_claude_md ──────────────────────────────────────────────────


class TestUpdateClaudeMd:
    def _setup(self, tmp_path, content):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        md = claude_dir / "CLAUDE.md"
        md.write_text(content)
        return md

    def test_replaces_existing_goggles_directive(self, tmp_path):
        md = self._setup(tmp_path, (
            "# Config\n"
            "- When using `brave_web_search`, ALWAYS pass the goggles parameter:\n"
            '  `goggles: ["https://old-url"]`\n'
            "- Other stuff\n"
        ))
        mod.update_claude_md(tmp_path, "myapp")
        content = md.read_text()
        assert "old-url" not in content
        assert "myapp.goggle" in content
        assert "Other stuff" in content

    def test_appends_when_no_existing_directive(self, tmp_path):
        md = self._setup(tmp_path, "# My Project\n\nSome notes.\n")
        mod.update_claude_md(tmp_path, "myapp")
        content = md.read_text()
        assert "myapp.goggle" in content
        assert "brave_web_search" in content

    def test_skips_when_no_claude_md(self, tmp_path, capsys):
        mod.update_claude_md(tmp_path, "myapp")
        assert "Skipped" in capsys.readouterr().err


# ── CLI main ──────────────────────────────────────────────────────────


class TestMain:
    def test_dry_run_single_project(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "requirements.txt").write_text("flask>=2.3\n")
        monkeypatch.setattr(sys, "argv", ["prog", str(tmp_path), "--dry-run"])
        mod.main()
        out = capsys.readouterr()
        assert "! name:" in out.out
        assert "$discard" in out.out
        assert "python" in out.err.lower()

    def test_dry_run_with_extra(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "requirements.txt").write_text("flask\n")
        monkeypatch.setattr(sys, "argv", [
            "prog", str(tmp_path), "--dry-run", "--extra", "integrations",
        ])
        mod.main()
        out = capsys.readouterr().out
        assert "! name:" in out

    @patch("time.sleep")
    def test_writes_goggle_file(self, _sleep, tmp_path, monkeypatch):
        proj = tmp_path / "myapp"
        proj.mkdir()
        (proj / "requirements.txt").write_text("flask\n")
        monkeypatch.setattr(mod, "SCRIPT_DIR", tmp_path)
        monkeypatch.setattr(mod, "build_github_org_rules", lambda p: [])
        monkeypatch.setattr(sys, "argv", ["prog", str(proj)])
        mod.main()
        goggle_file = tmp_path / "myapp.goggle"
        assert goggle_file.is_file()
        content = goggle_file.read_text()
        assert "! name: myapp" in content

    def test_nonexistent_project_errors(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["prog", str(tmp_path / "nope")])
        with pytest.raises(SystemExit):
            mod.main()

    def test_no_args_errors(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["prog"])
        with pytest.raises(SystemExit):
            mod.main()

    @patch("time.sleep")
    def test_all_flag(self, _sleep, tmp_path, monkeypatch, capsys):
        github = tmp_path / "github"
        proj = github / "testproj"
        proj.mkdir(parents=True)
        (proj / "package.json").write_text(json.dumps({"dependencies": {"express": "^4"}}))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(mod, "SCRIPT_DIR", tmp_path)
        monkeypatch.setattr(mod, "build_github_org_rules", lambda p: [])
        monkeypatch.setattr(sys, "argv", ["prog", "--all"])
        mod.main()
        assert (tmp_path / "testproj.goggle").is_file()

    @patch("time.sleep")
    def test_update_claude_md_flag(self, _sleep, tmp_path, monkeypatch, capsys):
        proj = tmp_path / "myapp"
        proj.mkdir()
        (proj / "requirements.txt").write_text("flask\n")
        claude_dir = proj / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text("# My App\n")
        monkeypatch.setattr(mod, "SCRIPT_DIR", tmp_path)
        monkeypatch.setattr(mod, "build_github_org_rules", lambda p: [])
        monkeypatch.setattr(sys, "argv", ["prog", str(proj), "--update-claude-md"])
        mod.main()
        content = (claude_dir / "CLAUDE.md").read_text()
        assert "myapp.goggle" in content
