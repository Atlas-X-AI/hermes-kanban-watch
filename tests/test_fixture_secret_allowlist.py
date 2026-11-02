"""A synthetic channel fixture must not exempt another file or another value."""
import shutil
import subprocess
import uuid

import pytest

from conftest import ROOT
from test_watcher_discord import CHANNEL


@pytest.mark.parametrize("relative,value,expected", [
    ("tests/test_inbox_pane_binding.py", CHANNEL, 0),
    ("other.py", CHANNEL, 1),
    ("tests/test_inbox_pane_binding.py", CHANNEL[::-1], 1),
])
def test_channel_fixture_allowlist_is_exact(tmp_path, relative, value, expected):
    if shutil.which("gitleaks") is None:
        pytest.skip("gitleaks unavailable; fixture exemption scanner proof not run")
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('--discord-channel", "' + value + '"\n')
    config = ROOT / ".gitleaks.toml"
    args = ["gitleaks", "dir", ".", "--redact=100", "--no-banner", "--no-color"]
    if config.exists():
        args += ["--config", str(config)]
    result = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == expected, result.stdout + result.stderr
    if expected:
        assert "leaks found: 1" in result.stderr


def test_other_secret_rules_still_scan_the_fixture_file(tmp_path):
    if shutil.which("gitleaks") is None:
        pytest.skip("gitleaks unavailable; other-rule scanner proof not run")
    path = tmp_path / "tests/test_inbox_pane_binding.py"
    path.parent.mkdir()
    # Generate a fake high-entropy value only in the disposable scanner input.
    path.write_text("api_key = " + repr(uuid.uuid4().hex + uuid.uuid4().hex) + "\n")
    result = subprocess.run(["gitleaks", "dir", ".", "--config", str(ROOT / ".gitleaks.toml"),
                             "--redact=100", "--no-banner", "--no-color"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and "leaks found: 1" in result.stderr, result.stdout + result.stderr
