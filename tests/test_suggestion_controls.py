"""Prove the safety assertions reject specific wrong implementations, in temp copies."""
import shutil

import pytest

from conftest import PLUGIN
import test_suggest_routes as cases

# Reuse the disposable fixture, not any live Hermes configuration.
from test_suggest_routes import suggestions  # noqa: F401


@pytest.mark.parametrize("old,new,check", [
    (
        'passing = [entry for entry, passed in results.items() if passed]',
        'passing = list(results)',
        cases.test_suggestions_replace_failed_lane_and_keep_passing_classes,
    ),
    (
        'providers[entry[2]] != family',
        'providers[entry[2]] == family',
        cases.test_different_hosts_same_family_cannot_form_verifier_pair,
    ),
    (
        'os.O_WRONLY | os.O_CREAT | os.O_EXCL',
        'os.O_WRONLY | os.O_CREAT | os.O_TRUNC',
        cases.test_destination_created_during_checks_is_not_overwritten,
    ),
], ids=["reject-failed-lane", "reject-same-family", "reject-overwrite-race"])
def test_known_red_suggestion_controls(suggestions, tmp_path, monkeypatch, old, new, check):
    install = tmp_path / "mutant-plugin"
    shutil.copytree(PLUGIN, install, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    doctor = install / "scripts/doctor.py"
    source = doctor.read_text()
    assert source.count(old) == 1
    doctor.write_text(source.replace(old, new))
    monkeypatch.setattr(cases, "PLUGIN", install)
    with pytest.raises(AssertionError):
        check(suggestions, monkeypatch)
