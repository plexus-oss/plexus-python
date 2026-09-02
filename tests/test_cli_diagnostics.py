"""The CLI has to help you debug the CLI.

Every case here came out of one evening of real use: a four-month-old pipx
shim shadowing a fresh install, `plexus --version` refusing to answer, and
`whoami` cheerfully printing a credential it had never checked.
"""

import json
from unittest import mock

import pytest

from plexus import __version__
from plexus.cli import build_parser, cmd_whoami


def _args(**kw):
    ns = mock.Mock()
    ns.no_verify = kw.get("no_verify", False)
    return ns


def test_version_flag_answers_instead_of_demanding_a_subcommand(capsys):
    # It used to exit 2 with "the following arguments are required: command",
    # which is the least useful possible reply to "what version am I on".
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_unknown_subcommand_names_the_version_and_the_upgrade(capsys):
    """`invalid choice: 'skills'` sends people hunting for a typo when the real
    cause is an old install. Say which version is running."""
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["skills-that-do-not-exist-yet"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert __version__ in err
    assert "pip install --upgrade" in err
    assert "pipx upgrade" in err  # the shadowing case that caused this


def test_whoami_reports_a_valid_key_with_its_org(monkeypatch, capsys):
    monkeypatch.setenv("PLEXUS_API_KEY", "plx_" + "a" * 32)
    with mock.patch(
        "plexus.cli._verify_key",
        return_value=(200, {"org_id": "org_abc", "scopes": ["read", "write"]}),
    ):
        assert cmd_whoami(_args()) == 0
    out = capsys.readouterr().out
    assert "org_abc" in out and "valid" in out


def test_whoami_fails_loudly_on_a_rejected_key(monkeypatch, capsys):
    """The whole point. A revoked key used to print a confident summary and
    exit 0, so the 401s that followed looked unrelated."""
    monkeypatch.setenv("PLEXUS_API_KEY", "plx_" + "b" * 32)
    with mock.patch("plexus.cli._verify_key", return_value=(401, {})):
        assert cmd_whoami(_args()) == 1
    out = capsys.readouterr().out
    assert "REJECTED" in out
    assert "plexus init --force" in out  # tell them the fix, not just the fault


def test_whoami_distinguishes_disabled_from_invalid(monkeypatch, capsys):
    monkeypatch.setenv("PLEXUS_API_KEY", "plx_" + "c" * 32)
    with mock.patch("plexus.cli._verify_key", return_value=(403, {})):
        assert cmd_whoami(_args()) == 1
    assert "DISABLED" in capsys.readouterr().out


def test_unreachable_is_not_reported_as_invalid(monkeypatch, capsys):
    """Offline is not rejected. Conflating them turns a flaky connection into
    an afternoon spent regenerating perfectly good credentials."""
    monkeypatch.setenv("PLEXUS_API_KEY", "plx_" + "d" * 32)
    with mock.patch("plexus.cli._verify_key", return_value=(None, "connection refused")):
        assert cmd_whoami(_args()) == 0  # unknown is not failure
    out = capsys.readouterr().out
    assert "unknown" in out
    assert "REJECTED" not in out


def test_no_verify_skips_the_network(monkeypatch, capsys):
    monkeypatch.setenv("PLEXUS_API_KEY", "plx_" + "e" * 32)
    with mock.patch("plexus.cli._verify_key") as v:
        assert cmd_whoami(_args(no_verify=True)) == 0
        v.assert_not_called()


def test_whoami_without_a_key_says_what_to_run(monkeypatch, capsys):
    monkeypatch.delenv("PLEXUS_API_KEY", raising=False)
    with mock.patch("plexus.config.load_config", return_value={}):
        assert cmd_whoami(_args()) == 1
    assert "plexus init" in capsys.readouterr().out
