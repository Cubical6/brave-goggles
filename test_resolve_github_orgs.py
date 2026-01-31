"""Tests for resolve-github-orgs.py — 100% coverage target."""

import importlib
import json
import sys
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import module with hyphenated filename
spec = importlib.util.spec_from_file_location(
    "resolve_github_orgs",
    Path(__file__).parent / "resolve-github-orgs.py",
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


# ── Phase 1: Parsers ──────────────────────────────────────────────────


class TestParseComposer:
    def test_require_and_require_dev(self, tmp_path):
        p = tmp_path / "composer.json"
        p.write_text(json.dumps({
            "require": {"vendor/pkg-a": "^1.0", "vendor/pkg-b": "^2.0"},
            "require-dev": {"vendor/pkg-c": "^3.0"},
        }))
        out = set()
        mod._parse_composer(p, out)
        assert out == {"vendor/pkg-a", "vendor/pkg-b", "vendor/pkg-c"}

    def test_skips_ext_php_no_slash(self, tmp_path):
        p = tmp_path / "composer.json"
        p.write_text(json.dumps({
            "require": {
                "php": "^8.1",
                "ext-mbstring": "*",
                "bare-name": "^1.0",
                "valid/pkg": "^1.0",
            },
        }))
        out = set()
        mod._parse_composer(p, out)
        assert out == {"valid/pkg"}


class TestParsePackageJson:
    def test_deps_and_dev_deps(self, tmp_path):
        p = tmp_path / "package.json"
        p.write_text(json.dumps({
            "dependencies": {"react": "^18.0"},
            "devDependencies": {"jest": "^29.0"},
        }))
        out = set()
        mod._parse_package_json(p, out)
        assert out == {"react", "jest"}

    def test_skips_file_versions(self, tmp_path):
        p = tmp_path / "package.json"
        p.write_text(json.dumps({
            "dependencies": {"local-lib": "file:../lib", "react": "^18.0"},
        }))
        out = set()
        mod._parse_package_json(p, out)
        assert out == {"react"}


class TestParseRequirements:
    def test_version_specifiers_and_extras(self, tmp_path):
        p = tmp_path / "requirements.txt"
        p.write_text("requests>=2.28\nflask==2.3.1\ncelery[redis]\nnumpy\n")
        out = set()
        mod._parse_requirements(p, out)
        assert out == {"requests", "flask", "celery", "numpy"}

    def test_skips_comments_blanks_flags_python(self, tmp_path):
        p = tmp_path / "requirements.txt"
        p.write_text(
            "# comment\n"
            "\n"
            "-r other.txt\n"
            "--index-url https://pypi.org\n"
            "python\n"
            "real-pkg>=1.0\n"
        )
        out = set()
        mod._parse_requirements(p, out)
        assert out == {"real-pkg"}


class TestParsePyproject:
    def test_poetry_deps_and_groups(self, tmp_path):
        p = tmp_path / "pyproject.toml"
        p.write_text(
            '[tool.poetry.dependencies]\npython = "^3.11"\nrequests = "^2.28"\n'
            '[tool.poetry.group.dev.dependencies]\npytest = "^7.0"\n'
        )
        out = set()
        mod._parse_pyproject(p, out)
        assert out == {"requests", "pytest"}

    def test_pep621_deps_and_optional(self, tmp_path):
        p = tmp_path / "pyproject.toml"
        p.write_text(
            '[project]\ndependencies = ["flask>=2.3", "celery[redis]>=5.0"]\n'
            '[project.optional-dependencies]\ndev = ["pytest>=7.0"]\n'
        )
        out = set()
        mod._parse_pyproject(p, out)
        assert out == {"flask", "celery", "pytest"}

    def test_skips_python(self, tmp_path):
        p = tmp_path / "pyproject.toml"
        p.write_text('[project]\ndependencies = ["Python>=3.11"]\n')
        out = set()
        mod._parse_pyproject(p, out)
        assert out == set()


class TestScanProject:
    def test_scans_single_project_root(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "^18"}}))
        (tmp_path / "requirements.txt").write_text("flask>=2.3\n")
        pkgs = mod.scan_project(tmp_path)
        assert "react" in pkgs["npm"]
        assert "flask" in pkgs["pypi"]
        assert pkgs["packagist"] == set()

    def test_does_not_recurse(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "package.json").write_text(json.dumps({"dependencies": {"react": "^18"}}))
        pkgs = mod.scan_project(tmp_path)
        assert pkgs["npm"] == set()

    def test_handles_missing_manifests(self, tmp_path):
        pkgs = mod.scan_project(tmp_path)
        assert all(v == set() for v in pkgs.values())

    def test_handles_invalid_json(self, tmp_path, capsys):
        (tmp_path / "composer.json").write_text("BROKEN")
        pkgs = mod.scan_project(tmp_path)
        assert pkgs["packagist"] == set()
        assert "Warning" in capsys.readouterr().err


class TestScanDirectories:
    def test_walks_and_finds_manifests(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "package.json").write_text(json.dumps({"dependencies": {"react": "^18"}}))
        (proj / "requirements.txt").write_text("flask>=2.3\n")
        (proj / "pyproject.toml").write_text('[project]\ndependencies = ["celery>=5.0"]\n')
        (proj / "composer.json").write_text(json.dumps({"require": {"vendor/pkg": "^1"}}))
        pkgs = mod.scan_directories([str(tmp_path)])
        assert "react" in pkgs["npm"]
        assert "flask" in pkgs["pypi"]
        assert "celery" in pkgs["pypi"]
        assert "vendor/pkg" in pkgs["packagist"]

    def test_skips_skip_dirs(self, tmp_path):
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        (vendor / "composer.json").write_text(json.dumps({"require": {"a/b": "^1"}}))
        pkgs = mod.scan_directories([str(tmp_path)])
        assert pkgs["packagist"] == set()

    def test_warns_on_non_dir_root(self, tmp_path, capsys):
        pkgs = mod.scan_directories([str(tmp_path / "nope")])
        assert "Warning" in capsys.readouterr().err

    def test_handles_parse_error(self, tmp_path, capsys):
        (tmp_path / "composer.json").write_text("NOT JSON")
        pkgs = mod.scan_directories([str(tmp_path)])
        assert pkgs["packagist"] == set()
        assert "Warning" in capsys.readouterr().err


# ── Phase 2: Resolvers ────────────────────────────────────────────────


class TestHttpGetJson:
    def _mock_response(self, data):
        resp = MagicMock()
        resp.read.return_value = json.dumps(data).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    @patch("time.sleep")
    def test_success(self, _sleep):
        resp = self._mock_response({"ok": True})
        with patch.object(mod.urllib.request, "urlopen", return_value=resp):
            assert mod._http_get_json("https://example.com") == {"ok": True}

    @patch("time.sleep")
    def test_404_returns_none(self, _sleep):
        err = urllib.error.HTTPError("url", 404, "Not Found", {}, BytesIO(b""))
        with patch.object(mod.urllib.request, "urlopen", side_effect=err):
            assert mod._http_get_json("https://example.com") is None

    @patch("time.sleep")
    def test_429_retries_then_none(self, mock_sleep):
        err = urllib.error.HTTPError("url", 429, "Too Many", {}, BytesIO(b""))
        with patch.object(mod.urllib.request, "urlopen", side_effect=err):
            assert mod._http_get_json("https://example.com") is None
            mock_sleep.assert_called_once_with(mod.RETRY_DELAY)

    @patch("time.sleep")
    def test_500_retries_then_none(self, mock_sleep):
        err = urllib.error.HTTPError("url", 500, "Server Error", {}, BytesIO(b""))
        with patch.object(mod.urllib.request, "urlopen", side_effect=err):
            assert mod._http_get_json("https://example.com") is None
            mock_sleep.assert_called_once_with(mod.RETRY_DELAY)

    @patch("time.sleep")
    def test_other_http_error_returns_none(self, _sleep):
        err = urllib.error.HTTPError("url", 403, "Forbidden", {}, BytesIO(b""))
        with patch.object(mod.urllib.request, "urlopen", side_effect=err):
            assert mod._http_get_json("https://example.com") is None

    @patch("time.sleep")
    def test_generic_exception_returns_none(self, _sleep):
        with patch.object(mod.urllib.request, "urlopen", side_effect=OSError("fail")):
            assert mod._http_get_json("https://example.com") is None


class TestExtractGithubOrg:
    def test_https_url(self):
        assert mod._extract_github_org("https://github.com/Laravel/framework") == "laravel"

    def test_ssh_url(self):
        assert mod._extract_github_org("git@github.com:pallets/flask.git") == "pallets"

    def test_case_insensitive(self):
        assert mod._extract_github_org("https://GitHub.COM/Org/repo") == "org"

    def test_non_github_returns_none(self):
        assert mod._extract_github_org("https://gitlab.com/user/repo") is None


class TestResolvePackagist:
    @patch("time.sleep")
    def test_found_org(self, _sleep, monkeypatch):
        data = {"package": {"versions": {"1.0": {"source": {"url": "https://github.com/laravel/framework"}}}}}
        monkeypatch.setattr(mod, "_http_get_json", lambda url: data)
        assert mod._resolve_packagist("laravel/framework") == "laravel"

    @patch("time.sleep")
    def test_no_data(self, _sleep, monkeypatch):
        monkeypatch.setattr(mod, "_http_get_json", lambda url: None)
        assert mod._resolve_packagist("x/y") is None

    @patch("time.sleep")
    def test_no_github_source(self, _sleep, monkeypatch):
        data = {"package": {"versions": {"1.0": {"source": {"url": "https://gitlab.com/x/y"}}}}}
        monkeypatch.setattr(mod, "_http_get_json", lambda url: data)
        assert mod._resolve_packagist("x/y") is None


class TestResolveNpm:
    @patch("time.sleep")
    def test_repo_as_dict(self, _sleep, monkeypatch):
        data = {"repository": {"url": "https://github.com/facebook/react"}}
        monkeypatch.setattr(mod, "_http_get_json", lambda url: data)
        assert mod._resolve_npm("react") == "facebook"

    @patch("time.sleep")
    def test_repo_as_string(self, _sleep, monkeypatch):
        data = {"repository": "https://github.com/facebook/react"}
        monkeypatch.setattr(mod, "_http_get_json", lambda url: data)
        assert mod._resolve_npm("react") == "facebook"

    @patch("time.sleep")
    def test_repo_neither(self, _sleep, monkeypatch):
        data = {"repository": 42}
        monkeypatch.setattr(mod, "_http_get_json", lambda url: data)
        assert mod._resolve_npm("x") is None

    @patch("time.sleep")
    def test_no_data(self, _sleep, monkeypatch):
        monkeypatch.setattr(mod, "_http_get_json", lambda url: None)
        assert mod._resolve_npm("x") is None


class TestResolvePypi:
    @patch("time.sleep")
    def test_found_in_known_key(self, _sleep, monkeypatch):
        data = {"info": {"project_urls": {"Repository": "https://github.com/pallets/flask"}}}
        monkeypatch.setattr(mod, "_http_get_json", lambda url: data)
        assert mod._resolve_pypi("flask") == "pallets"

    @patch("time.sleep")
    def test_fallback_to_any_url(self, _sleep, monkeypatch):
        data = {"info": {"project_urls": {"Tracker": "https://github.com/org/repo/issues"}}}
        monkeypatch.setattr(mod, "_http_get_json", lambda url: data)
        assert mod._resolve_pypi("pkg") == "org"

    @patch("time.sleep")
    def test_no_data(self, _sleep, monkeypatch):
        monkeypatch.setattr(mod, "_http_get_json", lambda url: None)
        assert mod._resolve_pypi("x") is None

    @patch("time.sleep")
    def test_no_matching_urls(self, _sleep, monkeypatch):
        data = {"info": {"project_urls": {"Homepage": "https://example.com"}}}
        monkeypatch.setattr(mod, "_http_get_json", lambda url: data)
        assert mod._resolve_pypi("x") is None


class TestResolveAll:
    @patch("time.sleep")
    def test_cached_hit(self, _sleep):
        cache = {"npm:react": "facebook"}
        pkgs = {"npm": {"react"}, "packagist": set(), "pypi": set()}
        orgs = mod.resolve_all(pkgs, cache)
        assert orgs == {"facebook": {"react"}}

    @patch("time.sleep")
    def test_uncached_calls_resolver_and_caches(self, mock_sleep, monkeypatch):
        monkeypatch.setattr(mod, "_resolve_npm", lambda name: "facebook")
        cache = {}
        pkgs = {"npm": {"react"}, "packagist": set(), "pypi": set()}
        orgs = mod.resolve_all(pkgs, cache)
        assert orgs == {"facebook": {"react"}}
        assert cache["npm:react"] == "facebook"
        mock_sleep.assert_called_with(mod.REQUEST_DELAY)

    @patch("time.sleep")
    def test_groups_by_org(self, _sleep, monkeypatch):
        monkeypatch.setattr(mod, "_resolve_npm", lambda name: "facebook")
        cache = {}
        pkgs = {"npm": {"react", "react-dom"}, "packagist": set(), "pypi": set()}
        orgs = mod.resolve_all(pkgs, cache)
        assert orgs["facebook"] == {"react", "react-dom"}


# ── Phase 3: Aggregation ──────────────────────────────────────────────


class TestBuildBoostRules:
    def test_boost_tiers(self):
        orgs = {
            "big": {f"p{i}" for i in range(5)},      # boost=5
            "mid": {"a", "b", "c"},                    # boost=4
            "small": {"x"},                            # boost=3
        }
        rules = mod.build_boost_rules(orgs)
        assert "/big/$boost=5,site=github.com" in rules
        assert "/mid/$boost=4,site=github.com" in rules
        assert "/small/$boost=3,site=github.com" in rules

    def test_sorted_by_count_desc_then_name(self):
        orgs = {
            "bbb": {"p1", "p2"},
            "aaa": {"p1", "p2"},
            "zzz": {f"p{i}" for i in range(5)},
        }
        rules = mod.build_boost_rules(orgs)
        assert rules[0].startswith("/zzz/")
        assert rules[1].startswith("/aaa/")
        assert rules[2].startswith("/bbb/")


class TestGenerateBlock:
    def test_wraps_with_markers(self):
        rules = ["/org/$boost=5,site=github.com"]
        block = mod.generate_block(rules)
        assert block.startswith(mod.MARKER_START)
        assert block.endswith(mod.MARKER_END)
        assert "/org/$boost=5,site=github.com" in block


# ── Phase 4: Goggle update ────────────────────────────────────────────


class TestUpdateGoggle:
    def test_replaces_existing_marker_block(self, tmp_path, monkeypatch):
        goggle = tmp_path / "dev-docs.goggle"
        old_block = f"{mod.MARKER_START}\n/old/$boost=3,site=github.com\n{mod.MARKER_END}"
        goggle.write_text(f"! header\n{old_block}\n$boost=5,site=github.com\n")
        monkeypatch.setattr(mod, "GOGGLE_FILE", goggle)
        mod.update_goggle(f"{mod.MARKER_START}\n/new/$boost=5,site=github.com\n{mod.MARKER_END}")
        content = goggle.read_text()
        assert "/new/" in content
        assert "/old/" not in content

    def test_inserts_before_generic_github_line(self, tmp_path, monkeypatch):
        goggle = tmp_path / "dev-docs.goggle"
        goggle.write_text("! header\n$boost=5,site=github.com\n")
        monkeypatch.setattr(mod, "GOGGLE_FILE", goggle)
        block = mod.generate_block(["/org/$boost=5,site=github.com"])
        mod.update_goggle(block)
        content = goggle.read_text()
        assert content.index(mod.MARKER_START) < content.index("$boost=1,site=github.com")

    def test_appends_if_neither_found(self, tmp_path, monkeypatch):
        goggle = tmp_path / "dev-docs.goggle"
        goggle.write_text("! header\nsome rule\n")
        monkeypatch.setattr(mod, "GOGGLE_FILE", goggle)
        block = mod.generate_block(["/org/$boost=3,site=github.com"])
        mod.update_goggle(block)
        content = goggle.read_text()
        assert content.endswith(block + "\n")

    def test_lowers_generic_github_boost_to_1(self, tmp_path, monkeypatch):
        goggle = tmp_path / "dev-docs.goggle"
        goggle.write_text(f"! header\n$boost=5,site=github.com\n")
        monkeypatch.setattr(mod, "GOGGLE_FILE", goggle)
        block = mod.generate_block(["/org/$boost=5,site=github.com"])
        mod.update_goggle(block)
        content = goggle.read_text()
        assert "$boost=1,site=github.com" in content
        assert "$boost=5,site=github.com" not in content or "/org/$boost=5" in content


# ── CLI ───────────────────────────────────────────────────────────────


class TestLoadCache:
    def test_existing_file(self, tmp_path, monkeypatch):
        cf = tmp_path / "cache.json"
        cf.write_text(json.dumps({"npm:react": "facebook"}))
        monkeypatch.setattr(mod, "CACHE_FILE", cf)
        assert mod.load_cache() == {"npm:react": "facebook"}

    def test_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "CACHE_FILE", tmp_path / "nope.json")
        assert mod.load_cache() == {}


class TestSaveCache:
    def test_writes_formatted_json(self, tmp_path, monkeypatch):
        cf = tmp_path / "cache.json"
        monkeypatch.setattr(mod, "CACHE_FILE", cf)
        mod.save_cache({"b": 2, "a": 1})
        content = cf.read_text()
        assert content.endswith("\n")
        parsed = json.loads(content)
        assert parsed == {"a": 1, "b": 2}
        # Sorted keys
        assert content.index('"a"') < content.index('"b"')


class TestMain:
    @patch("time.sleep")
    def test_dry_run(self, _sleep, tmp_path, monkeypatch, capsys):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "package.json").write_text(json.dumps({"dependencies": {"react": "^18"}}))
        monkeypatch.setattr(mod, "CACHE_FILE", tmp_path / "cache.json")
        monkeypatch.setattr(mod, "GOGGLE_FILE", tmp_path / "goggle")
        monkeypatch.setattr(mod, "_resolve_npm", lambda name: "facebook")
        monkeypatch.setattr(sys, "argv", ["prog", "--dry-run", str(tmp_path)])
        mod.main()
        out = capsys.readouterr().out
        assert mod.MARKER_START in out
        assert "/facebook/" in out

    @patch("time.sleep")
    def test_normal_run_updates_file(self, _sleep, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "package.json").write_text(json.dumps({"dependencies": {"react": "^18"}}))
        goggle = tmp_path / "goggle"
        goggle.write_text("! header\n$boost=5,site=github.com\n")
        monkeypatch.setattr(mod, "CACHE_FILE", tmp_path / "cache.json")
        monkeypatch.setattr(mod, "GOGGLE_FILE", goggle)
        monkeypatch.setattr(mod, "_resolve_npm", lambda name: "facebook")
        monkeypatch.setattr(sys, "argv", ["prog", str(tmp_path)])
        mod.main()
        content = goggle.read_text()
        assert "/facebook/" in content

    @patch("time.sleep")
    def test_clear_cache(self, _sleep, tmp_path, monkeypatch, capsys):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text("{}")
        goggle = tmp_path / "goggle"
        goggle.write_text("! header\n")
        monkeypatch.setattr(mod, "CACHE_FILE", cache_file)
        monkeypatch.setattr(mod, "GOGGLE_FILE", goggle)
        monkeypatch.setattr(sys, "argv", ["prog", "--clear-cache", str(tmp_path)])
        mod.main()
        err = capsys.readouterr().err
        assert "Cache cleared" in err
