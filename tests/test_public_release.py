"""Public distribution contracts, independent of any development source material."""
import json
import struct
import tomllib
import xml.etree.ElementTree as ET

from conftest import PLUGIN, ROOT


def test_marketplace_and_plugin_metadata():
    marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    assert marketplace["name"] == "hermes-kanban-watch"
    assert marketplace["description"].strip()
    assert marketplace["owner"]["name"] == "Atlas X AI"
    assert len(marketplace["plugins"]) == 1
    entry = marketplace["plugins"][0]
    assert entry["name"] == manifest["name"]
    assert entry["source"] == "./plugin"
    assert entry["version"] == manifest["version"] == (ROOT / "VERSION").read_text().strip()
    assert entry["homepage"] == manifest["homepage"]
    assert entry["repository"] == manifest["repository"]
    assert (ROOT / entry["source"] / ".claude-plugin/plugin.json").is_file()
    assert manifest["author"] == {"name": "Atlas X AI", "url": "https://atlas-ai.au"}
    assert manifest["homepage"] == manifest["repository"] == "https://github.com/Atlas-X-AI/hermes-kanban-watch"
    assert manifest["license"] == "MIT" and manifest["keywords"]


def test_project_version_and_ci():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    version = (ROOT / "VERSION").read_text().strip()
    manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    assert project["project"]["name"] == manifest["name"]
    assert project["project"]["version"] == manifest["version"] == version == "0.2.0"
    assert project["project"]["requires-python"] == ">=3.11"
    assert any(dep.split(">=")[0] == "pytest" for dep in project["dependency-groups"]["dev"])
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "pull_request:" in ci and "branches: [main]" in ci and "secrets: inherit" in ci
    assert "uses: Atlas-X-AI/.github/.github/workflows/" in ci and ".yml@main" in ci


def test_banner_and_public_documents():
    banner = ET.parse(ROOT / "assets/banner.svg").getroot()
    assert banner.attrib["viewBox"] == "0 0 1280 320"
    assert "Hermes Kanban Watch" in " ".join(banner.itertext())
    assert not any(element.tag.rsplit("}", 1)[-1] in {"script", "foreignObject", "image"}
                   for element in banner.iter())
    for name in ("README.md", "CONTRIBUTING.md", "SECURITY.md", "AGENTS.md", "LICENSE"):
        assert (ROOT / name).stat().st_size > 100
    assert "Copyright (c) 2026 Atlas X AI Pty Ltd" in (ROOT / "LICENSE").read_text()
    readme = (ROOT / "README.md").read_text()
    assert "/plugin marketplace add Atlas-X-AI/hermes-kanban-watch" in readme
    assert "/plugin install hermes-kanban-watch@hermes-kanban-watch" in readme
    install = "/plugin install hermes-kanban-watch --marketplace Atlas-X-AI/hermes-kanban-watch"
    assert install in readme.split("## Quick install", 1)[1].split("## How it works", 1)[0]
    assert install in (PLUGIN / "README.md").read_text()
    for phrase in ("ASK OPERATOR:", "waiting on operator", "waiting for operator", "needs operator",
                   "Bring your own models", "Routing", "Supported target", "macOS", "Windows", "UNKNOWN"):
        assert phrase in readme
    doctor = (PLUGIN / "scripts/doctor.py").read_text()
    commands = ("cards", "snapshot", "wait-start", "wait-any", "follow", "events", "active", "scout", "needs")
    for command in commands:
        assert f"Bash(hermes-kanban-live {command}:*)" in readme
        assert f'"{command}"' in doctor


def test_plugin_listing_icon():
    icon = ET.parse(PLUGIN / "assets/icon.svg").getroot()
    assert icon.attrib["viewBox"] == "0 0 512 512"
    assert icon.attrib["width"] == icon.attrib["height"] == "512"
    assert not any(element.tag.rsplit("}", 1)[-1] in {"script", "foreignObject", "image", "text"}
                   for element in icon.iter())
    png = (PLUGIN / ".claude-plugin/icon.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert png[12:16] == b"IHDR"
    assert struct.unpack(">II", png[16:24]) == (1024, 1024)
    assert len(png) < 2_000_000
