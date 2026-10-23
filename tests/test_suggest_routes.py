import importlib.util
import json
import os
import shlex
import subprocess
import sys

import pytest

from conftest import PLUGIN
from helpers import fake_hermes


@pytest.fixture
def suggestions(tmp_path, isolated_home, monkeypatch):
    _, hermes = isolated_home
    bindir = tmp_path / "bin"
    fake_hermes(bindir)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CALLS", str(log))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.defpath)
    worker = {"profile": "a-worker", "model": "unavailable", "provider": "openai"}
    backup = {"profile": "b-backup", "model": "working", "provider": "compatible"}
    verifier = {"profile": "c-reviewer", "model": "review-working", "provider": "anthropic"}
    data = {"providers": {"openai": "gpt", "compatible": "gpt", "anthropic": "claude"},
            "classes": {"app-code": worker, "docs": backup},
            "verifiers": {"gpt": verifier, "claude": worker}}
    for entry in (worker, backup, verifier):
        directory = hermes / "profiles" / entry["profile"]
        directory.mkdir(parents=True)
        (directory / "config.yaml").write_text(f"model:\n  provider: {entry['provider']}\n  default: {entry['model']}\n")
    (hermes / "kanban.db").write_bytes(b"fake board")
    shim = bindir / "hermes-kanban-live"
    shim.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " +
                    shlex.quote(str(PLUGIN / "scripts/kanban_live.py")) + ' "$@"\n')
    shim.chmod(0o755)
    source = tmp_path / "routes.json"
    source.write_text(json.dumps(data))
    output = tmp_path / "suggested.yaml"
    return data, source, output, log


def lane(entry):
    return "|".join(entry[key] for key in ("profile", "model", "provider"))


def run(suggestions, *extra):
    _, source, output, _ = suggestions
    return subprocess.run([sys.executable, str(PLUGIN / "scripts/doctor.py"),
                           "--routes", str(source), "--suggest-routes", str(output), *extra],
                          capture_output=True, text=True, timeout=20)


def route_module():
    spec = importlib.util.spec_from_file_location("suggestion_route", PLUGIN / "scripts/route.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_suggestions_replace_failed_lane_and_keep_passing_classes(suggestions, monkeypatch):
    data, source, output, log = suggestions
    failed = data["classes"]["app-code"]
    monkeypatch.setenv("FAKE_PROBES", json.dumps({lane(failed): "EXIT_1"}))
    before = source.read_bytes()
    out = run(suggestions, "--probe")
    assert output.is_file(), out.stdout + out.stderr
    # Doctor still reports the source installation's failed probe.
    assert out.returncode == 1 and "FAIL probe a-worker unavailable openai" in out.stdout
    module = route_module()
    result = module.load_routes(output)
    assert result["classes"] == {"app-code": data["classes"]["docs"], "docs": data["classes"]["docs"]}
    for name in result["classes"]:
        resolved = module.resolve(result, name)
        assert resolved["profile"] == "b-backup"
        assert resolved["verifier"] == data["verifiers"]["gpt"]
    for section in ("classes", "verifiers"):
        assert all(entry != failed for entry in result[section].values())
    text = output.read_text()
    assert "#" in text and "PONG" in text and "FAIL a-worker unavailable openai" in text
    assert "PASS b-backup working compatible" in text and "PASS c-reviewer review-working anthropic" in text
    assert str(source) in text and "UTC" in text
    assert source.read_bytes() == before
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len([command for command in calls if "chat" in command]) == 3


def test_failed_verifier_is_replaced_by_first_passing_other_family(suggestions, monkeypatch):
    data, source, output, _ = suggestions
    original = data["verifiers"]["gpt"]
    data["classes"]["review"] = {**original, "profile": "d-reviewer", "model": "alternate"}
    profile = source.parent / "user/.hermes/profiles/d-reviewer"
    profile.mkdir()
    (profile / "config.yaml").write_text("model:\n  provider: anthropic\n  default: alternate\n")
    source.write_text(json.dumps(data))
    monkeypatch.setenv("FAKE_PROBES", json.dumps({lane(original): "PONG extra"}))
    out = run(suggestions, "--probe")
    assert output.exists(), out.stdout + out.stderr
    module = route_module()
    result = module.load_routes(output)
    assert result["verifiers"]["gpt"] == data["classes"]["review"]
    assert all(entry != original for entry in result["verifiers"].values())
    for name in result["classes"]:
        module.resolve(result, name)


@pytest.mark.parametrize("response", ["PONG extra", "", "EXIT_1"])
def test_no_passing_lanes_omits_classes_with_comments(suggestions, monkeypatch, response):
    _, _, output, _ = suggestions
    monkeypatch.setenv("FAKE_PROBE", response)
    out = run(suggestions, "--probe")
    assert output.exists(), out.stdout + out.stderr
    result = route_module().read_mapping(output)
    assert result["classes"] == {} and result["verifiers"] == {}
    text = output.read_text()
    assert "# Omitted app-code:" in text and "# Omitted docs:" in text


def test_different_hosts_same_family_cannot_form_verifier_pair(suggestions, monkeypatch):
    data, _, output, _ = suggestions
    monkeypatch.setenv("FAKE_PROBES", json.dumps({lane(data["verifiers"]["gpt"]): "EXIT_1"}))
    out = run(suggestions, "--probe")
    assert output.exists(), out.stdout + out.stderr
    result = route_module().read_mapping(output)
    assert result["classes"] == {} and result["verifiers"] == {}
    assert "different-family" in output.read_text()


@pytest.mark.parametrize("kind", ["file", "symlink", "directory"])
def test_existing_destination_refused_before_any_calls(suggestions, kind):
    _, source, output, log = suggestions
    if kind == "file":
        output.write_text("preserve me")
    elif kind == "symlink":
        output.symlink_to(source.parent / "missing-target")
    else:
        output.mkdir()
    out = run(suggestions, "--probe")
    assert out.returncode != 0 and "exist" in (out.stdout + out.stderr).lower()
    assert not log.exists()
    if kind == "file":
        assert output.read_text() == "preserve me"
    elif kind == "symlink":
        assert output.is_symlink() and not output.exists()
    else:
        assert output.is_dir()


def test_suggestion_requires_explicit_probe(suggestions):
    _, _, output, log = suggestions
    out = run(suggestions)
    assert out.returncode != 0 and "--probe" in out.stderr
    assert not output.exists() and not log.exists()


def test_destination_created_during_checks_is_not_overwritten(suggestions, monkeypatch):
    _, _, output, log = suggestions
    monkeypatch.setenv("FAKE_CONCURRENT_FILE", str(output))
    out = run(suggestions, "--probe")
    assert out.returncode == 1 and "FAIL suggest routes: FILE already exists" in out.stdout
    assert output.read_text() == "concurrent writer"
    assert '"chat"' in log.read_text() and "PASS probe" in out.stdout


def test_suggestions_preserve_valid_original_lanes(suggestions):
    data, _, output, _ = suggestions
    out = run(suggestions, "--probe")
    assert out.returncode == 0, out.stdout + out.stderr
    result = route_module().load_routes(output)
    assert result["classes"] == data["classes"]
    for name in result["classes"]:
        route_module().resolve(result, name)


def test_suggestions_round_trip_identifier_keys_from_json(suggestions):
    data, source, output, _ = suggestions
    data["classes"]["app/code"] = data["classes"].pop("app-code")
    data["providers"]["compatible:api"] = data["providers"].pop("compatible")
    data["classes"]["docs"]["provider"] = "compatible:api"
    source.write_text(json.dumps(data))
    out = run(suggestions, "--probe")
    assert out.returncode == 0, out.stdout + out.stderr
    result = route_module().load_routes(output)
    assert result["classes"] == data["classes"]
    for name in result["classes"]:
        route_module().resolve(result, name)
