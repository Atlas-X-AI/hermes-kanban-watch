import json
import os
import subprocess
import sys

import pytest

from conftest import PLUGIN


def route(*args):
    return subprocess.run([sys.executable, str(PLUGIN / "scripts/route.py"), *args],
                          capture_output=True, text=True, timeout=10)


def test_default_route_cross_family():
    out = route("--class", "app-code")
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert set(data) == {"class", "profile", "model", "provider", "verifier", "reason"}
    assert data["class"] == "app-code" and data["provider"] != data["verifier"]["provider"]
    assert data["profile"] and data["reason"]


def test_unknown_class():
    out = route("--class", "no-such-class")
    assert out.returncode == 1 and "Unknown class" in out.stderr
    assert not out.stdout


@pytest.mark.parametrize("provider", ["openai", "compatible"])
def test_same_family_refused(tmp_path, routes, provider):
    routes["verifiers"]["gpt"]["provider"] = provider
    path = tmp_path / "routes.yaml"
    path.write_text(json.dumps(routes))
    out = route("--routes", str(path), "--class", "app-code")
    assert out.returncode == 1 and "same family" in out.stderr


def test_overrides_choose_verifier_by_effective_family(tmp_path, routes):
    path = tmp_path / "routes.yaml"
    path.write_text(json.dumps(routes))
    out = route("--routes", str(path), "--class", "app-code", "--worker-provider", "anthropic", "--worker-model", "claude-test")
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["provider"] == "anthropic" and data["model"] == "claude-test"
    assert data["verifier"]["provider"] == "openai"


def test_routes_precedence(tmp_path, isolated_home, monkeypatch, routes):
    home, _ = isolated_home
    config = home / ".config/hermes-kanban-watch/routes.yaml"
    config.parent.mkdir(parents=True)
    routes["classes"]["app-code"]["profile"] = "user-config"
    config.write_text(json.dumps(routes))
    assert json.loads(route("--class", "app-code").stdout)["profile"] == "user-config"
    env_path = tmp_path / "env.yaml"
    routes["classes"]["app-code"]["profile"] = "env-config"
    env_path.write_text(json.dumps(routes))
    monkeypatch.setenv("KANBAN_WATCH_ROUTES", str(env_path))
    assert json.loads(route("--class", "app-code").stdout)["profile"] == "env-config"
    explicit = tmp_path / "explicit.yaml"
    routes["classes"]["app-code"]["profile"] = "explicit-config"
    explicit.write_text(json.dumps(routes))
    assert json.loads(route("--routes", str(explicit), "--class", "app-code").stdout)["profile"] == "explicit-config"
    monkeypatch.setenv("KANBAN_WATCH_ROUTES", str(tmp_path / "missing.yaml"))
    assert route("--class", "app-code").returncode == 1


def test_yaml_mapping_input(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text('''# User-editable mappings
providers:
  p1: family-one
  p2: family-two
classes:
  app-code:
    profile: "worker"
    model: 'model-one'
    provider: p1
verifiers:
  family-one:
    profile: verifier
    model: model-two
    provider: p2
''')
    out = route("--routes", str(path), "--class", "app-code")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["verifier"]["profile"] == "verifier"


def test_unknown_provider_and_malformed_routes_fail_closed(tmp_path, routes):
    path = tmp_path / "routes.yaml"
    path.write_text(json.dumps(routes))
    out = route("--routes", str(path), "--class", "app-code", "--worker-provider", "unknown")
    assert out.returncode == 1
    path.write_text("classes: [broken\n")
    assert route("--routes", str(path), "--class", "app-code").returncode == 1
