# Plexus agent skills

Three skills that teach a coding agent — Claude Code, or anything that reads
the same format — how to build against Plexus without guessing at the API.

| Skill              | For                                                                       |
| ------------------ | ------------------------------------------------------------------------- |
| `plexus`           | The API itself: hosts, auth, every endpoint, the live stream, the pitfalls |
| `plexus-firmware`  | Device-side ingest — ESP32, Pi, Jetson, autopilots, OBCs                   |
| `plexus-dashboard` | Scaffolding a web dashboard on the read API                               |

They are plain Markdown with YAML frontmatter. No install, no server, no
credentials — an agent reads them and writes correct code.

## Install

```bash
pip install plexus-python
plexus skills install
```

That writes all three into `~/.claude/skills`. Use `--project` to install into
`./.claude/skills` instead, so they travel with the repo, `--dir` to pick a
target, or `--list` to see what ships without writing anything.

Re-running refreshes them. These are reference docs, not config: a stale copy
is the failure this command exists to fix, so an existing skill is replaced —
reported as `updated`, never silently.

Then just ask for what you want — "send my ESP32's battery voltage to Plexus",
"build me a fleet dashboard" — and the agent picks the right one from its
`description`.

## Why these exist

An agent that has not read these invents a plausible Plexus API and gets it
wrong in ways that fail quietly. The three that cost the most real time:

- The ingest array is **`points`**, not `metrics`, and every point needs a
  `class`. Getting this wrong is a 400 on every write.
- `timestamp` must be a **number**. An ISO-8601 string is rejected outright.
- The query response is **columnar** — `series[m].avg[i]`, not
  `series[m][i].v`. Guessing wrong yields `undefined` with no error: an empty
  chart and no clue why.

Each skill front-loads its pitfalls for that reason.

## Keeping them true

Prose does not compile, so a stale line here survives until it breaks
someone's code. Two checks stop that:

```bash
python scripts/verify_skills.py     # every route, against the live API
pytest tests/test_skills.py         # body shapes and response types, offline
```

`verify_skills.py` needs no API key. It asserts that every route the skills
quote exists in the live OpenAPI spec, that every WebSocket route completes a
real handshake, and — the useful part — that every route documented as **dead**
is still dead. The skills name non-existent routes on purpose, because agents
invent them otherwise; if one ever ships for real, the warning has become a lie
and the check fails.

`tests/test_skills.py` runs offline and guards the request/response shapes that
have actually shipped broken code. It deliberately touches no network:
`tests/conftest.py` points the suite at an unroutable address so nothing in it
can reach production, and that holds here too.

Both were written after an audit on 2026-08-28 found that all three skills had
drifted — the firmware one badly enough that every template in it would have
returned a 400.
