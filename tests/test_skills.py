"""Offline guards for the agent skills in `skills/`.

The skills are prose, so nothing compiles them. Twice now a stale line in one
has produced confidently broken code: ingest bodies keyed `metrics` instead of
`points`, ISO-8601 timestamp strings that the gateway rejects outright, and a
dashboard client calling a route that 404s.

These tests cover only what is checkable without a network — `conftest.py`
points the whole suite at an unroutable address on purpose, and that stays
true here. The live half is `scripts/verify_skills.py`, which is deliberately
not a pytest.

Each assertion below corresponds to a real 400 or a real empty chart.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
SKILLS = sorted(SKILLS_DIR.glob("*/SKILL.md"))

_FENCE = re.compile(r"^```(\w*)\n(.*?)^```", re.MULTILINE | re.DOTALL)


def code_blocks(text: str, lang: str | None = None) -> list[str]:
    return [body for tag, body in _FENCE.findall(text) if lang is None or tag == lang]


def ingest_bodies(text: str) -> list[str]:
    """Code samples that construct an ingest request body."""
    return [b for b in code_blocks(text) if '"points"' in b or '"metrics"' in b]


def test_skills_exist():
    assert SKILLS, f"no SKILL.md found under {SKILLS_DIR}"


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_frontmatter(skill: Path):
    """Name and description are what the agent matches on; both are required."""
    text = skill.read_text()
    assert text.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    front = text.split("---", 2)[1]
    assert re.search(r"^name:\s*\S+", front, re.MULTILINE), "missing `name:`"
    assert re.search(r"^description:\s*\S+", front, re.MULTILINE), "missing `description:`"

    name = re.search(r"^name:\s*(\S+)", front, re.MULTILINE).group(1)
    assert name == skill.parent.name, f"frontmatter name {name!r} != dir {skill.parent.name!r}"


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_ingest_array_is_points(skill: Path):
    """`"metrics": [...]` is the single most common 400 against /ingest."""
    for body in ingest_bodies(skill.read_text()):
        assert '"metrics": [' not in body and '"metrics":[' not in body, (
            "ingest body uses `metrics` as the array — the gateway requires "
            "`points` and returns 400 {'error':\"'points' array is required\"}"
        )


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_ingest_points_declare_class(skill: Path):
    """`class` is required on every point; there is no default."""
    for body in ingest_bodies(skill.read_text()):
        if '"metric"' in body and '"value"' in body:
            assert '"class"' in body or "class" in body, (
                "an ingest sample builds points without `class` — the "
                "validator requires 'metric' or 'event' on every point"
            )


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_timestamps_are_numeric(skill: Path):
    """An ISO-8601 timestamp is a hard 400: `timestamp must be a number`."""
    iso = re.compile(r'"timestamp"\s*:\s*"')
    for body in ingest_bodies(skill.read_text()):
        assert not iso.search(body), (
            "an ingest sample sends a string timestamp — the gateway rejects "
            "it with `points[i].timestamp must be a number` (epoch ms)"
        )


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_ingest_response_shape(skill: Path):
    """The response is {success, count, source_id}, never {accepted, rejected}."""
    text = skill.read_text()
    assert '"accepted"' not in text and '"rejected"' not in text, (
        "documents an ingest response of {accepted, rejected}; the gateway "
        "returns {success, count, source_id}"
    )


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_json_samples_parse(skill: Path):
    """A ```json block that does not parse will be copied verbatim anyway."""
    for body in code_blocks(skill.read_text(), lang="json"):
        json.loads(body)


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_query_response_treated_as_columnar(skill: Path):
    """`series[m]` is parallel arrays. Indexing it as a list of points yields
    `undefined` with no error — an empty chart and no clue why."""
    text = skill.read_text()
    if "series" not in text:
        return
    assert not re.search(r"series:\s*Record<string,\s*\w*Point\[\]>", text), (
        "types the query response as an array of points; it is columnar "
        "(`series[m].timestamp_ms[i]` / `series[m].avg[i]`)"
    )


# --- `plexus skills install` -------------------------------------------------
#
# The packaging half of this is what actually breaks: the skills live at the
# repo root but must ship *inside* the wheel for the CLI to find them. A test
# that only exercises the source checkout would pass while the shipped package
# was empty — which is the exact shape of a bug this SDK has already had once.
# So `_bundled_skills_dir` is tested through both of its branches.


def test_bundled_skills_dir_found_in_checkout():
    from plexus.cli import _bundled_skills_dir

    found = _bundled_skills_dir()
    assert found is not None, "skills should resolve from a source checkout"
    assert (found / "plexus" / "SKILL.md").is_file()


def test_bundled_skills_prefers_packaged_copy(tmp_path, monkeypatch):
    """An installed wheel has `plexus/_skills`; it must win over the sibling."""
    import plexus.cli as cli

    fake_pkg = tmp_path / "plexus"
    (fake_pkg / "_skills" / "demo").mkdir(parents=True)
    (fake_pkg / "_skills" / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n")
    (tmp_path / "skills").mkdir()

    monkeypatch.setattr(cli, "__file__", str(fake_pkg / "cli.py"))
    assert cli._bundled_skills_dir() == fake_pkg / "_skills"


def test_skills_list_writes_nothing(tmp_path, capsys):
    from plexus.cli import main

    target = tmp_path / "skills"
    assert main(["skills", "--list", "--dir", str(target)]) == 0
    assert not target.exists(), "--list must not create the target directory"
    assert "plexus-firmware" in capsys.readouterr().out


def test_skills_install_writes_every_skill(tmp_path):
    from plexus.cli import main

    target = tmp_path / "skills"
    assert main(["skills", "install", "--dir", str(target)]) == 0

    installed = sorted(p.parent.name for p in target.glob("*/SKILL.md"))
    assert installed == [s.parent.name for s in SKILLS]
    for skill in SKILLS:
        assert (target / skill.parent.name / "SKILL.md").read_text() == skill.read_text()


def test_skills_install_refreshes_existing(tmp_path, capsys):
    """Second run reports `updated`, and a stale copy is replaced.

    Refreshing is the point: these are reference docs, and a stale one is the
    failure this command exists to fix. It must not be silent about it.
    """
    from plexus.cli import main

    target = tmp_path / "skills"
    main(["skills", "install", "--dir", str(target)])
    capsys.readouterr()

    stale = target / "plexus" / "SKILL.md"
    stale.write_text("stale content")
    assert main(["skills", "install", "--dir", str(target)]) == 0

    out = capsys.readouterr().out
    assert "updated" in out and "installed" not in out
    assert stale.read_text() != "stale content"


def test_skills_install_project_scope(tmp_path, monkeypatch):
    from plexus.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["skills", "install", "--project"]) == 0
    assert (tmp_path / ".claude" / "skills" / "plexus" / "SKILL.md").is_file()


def test_default_target_is_home_unless_project(tmp_path, monkeypatch):
    """The default path writes to the real home, so assert it without writing."""
    import argparse

    from plexus.cli import _default_skills_target

    monkeypatch.chdir(tmp_path)
    assert _default_skills_target(argparse.Namespace(project=False)) == (
        Path.home() / ".claude" / "skills"
    )
    assert _default_skills_target(argparse.Namespace(project=True)) == (
        tmp_path / ".claude" / "skills"
    )
