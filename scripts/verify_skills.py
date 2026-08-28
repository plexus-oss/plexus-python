#!/usr/bin/env python3
"""Check the agent skills in `skills/` against the live Plexus API.

These docs have drifted twice, both times silently, and both times the drift
shipped code that 400'd or charted nothing. The failure mode is specific: a
skill is prose, so nothing compiles it and nothing tests it, and an agent
following a stale line produces confidently broken code.

So this script re-derives the truth from the running system:

  * every route the skills quote must exist in the live OpenAPI spec
  * every WebSocket route they quote must complete a real handshake
  * every route they name as DEAD must still be dead

That last check is the one worth explaining. The skills deliberately name
routes that do not exist ("there is no per-source health endpoint") because
agents invent those routes otherwise. A naive checker flags each warning as a
broken link. Inverting it turns the noise into signal: if a route on the dead
list ever comes alive, the warning is now a lie and the skill needs rewriting.
Every route in a skill is therefore asserted either live or dead — never
merely mentioned.

It needs no API key — route existence is provable from 401/4401 responses,
which is the whole point: anyone can run it, including CI, without secrets.

    python scripts/verify_skills.py            # prod
    python scripts/verify_skills.py --api-base http://localhost:8000

Exit code 0 = the docs match reality. Non-zero = fix the docs (or the API).

NOT a pytest. `tests/conftest.py` deliberately points the suite at an
unroutable address so no test can ever touch production; this script is the
opposite by design and stays out of that suite. `tests/test_skills.py` holds
the offline half — the regressions we can catch without a network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

DEFAULT_API_BASE = "https://plexus-data-api.fly.dev"
DEFAULT_GATEWAY_BASE = "https://gateway.plexus.company"

# WebSocket routes. FastAPI omits these from the OpenAPI spec, so they are
# verified by handshake instead.
WS_ROUTES = [
    "/v1/sources/{}/metrics/stream",
    "/v1/sources/{}/logs/stream",
    "/v1/sources/{}/video/stream",
]

# Routes the skills name so that agents stop inventing them. Each MUST stay
# absent from the live spec; if one appears, the skill's warning is wrong.
KNOWN_DEAD = {
    "/v1/sources/{}/health": "no per-source health route — `online` rides on /v1/sources rows",
    "/v1/stream": "the live stream is on the data API, not the gateway",
    "/v1/devices": "deprecated alias; 307 -> /v1/devices/ -> 308 -> /v1/sources/",
    "/v1/devices/{}": "same deprecated alias, with a path",
}

# Gateway routes, which live on a different host and so are not in the data
# API spec. Checked directly.
GATEWAY_ROUTES = {"/api/v1/write", "/ingest"}

_PATH_RE = re.compile(r"(?<![A-Za-z0-9])/(?:api/)?v1/[A-Za-z0-9_.{}$\-/]*")
_PLACEHOLDER = re.compile(r"^(\{[^}]*\}|\$\{[^}]*\}|x|id|test-laptop|drone-001|verify-skills-probe)$")


def fetch_json(url: str, timeout: float = 20.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
        return json.load(r)


def skill_files() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def normalize(path: str) -> str:
    """Collapse ids and template holes so a quoted path can match a spec path.

    `/v1/sources/${sourceId}/metrics/stream${qs}` -> `/v1/sources/{}/metrics/stream`
    """
    path = path.split("?")[0].rstrip("/`.,:;)")
    path = re.sub(r"\$\{[^}]*\}", lambda m: "{}", path)
    segments = []
    for seg in path.split("/"):
        if not seg:
            segments.append("")  # the leading empty segment before the root /
            continue
        # A segment may be a bare hole or a name with a hole glued on
        # (`stream{}` from `stream${qs}`); strip the latter, keep the former.
        stripped = re.sub(r"\{\}$", "", seg)
        seg = stripped or "{}"
        segments.append("{}" if _PLACEHOLDER.match(seg) else seg)
    return "/".join(segments)


def extract_paths(text: str) -> set[str]:
    return {
        norm
        for raw in _PATH_RE.findall(text)
        if (norm := normalize(raw)).count("/") >= 2
    }


_FENCE_RE = re.compile(r"^```", re.MULTILINE)


def code_blocks(text: str) -> str:
    """Just the fenced code, which is what an agent copies verbatim.

    Prose is allowed to name a dead route in order to warn about it. A code
    sample is not: that is the exact shape of the drift that shipped a
    dashboard calling a 404, and it is invisible to the live-route check
    because dead routes are on an allowlist.
    """
    parts = _FENCE_RE.split(text)
    return "\n".join(parts[1::2])  # odd chunks are inside fences


def check_routes(api_base: str, failures: list[str]) -> None:
    try:
        spec = fetch_json(f"{api_base}/openapi.json")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        failures.append(f"could not fetch {api_base}/openapi.json: {e}")
        return

    live = {normalize(p) for p in spec.get("paths", {})}
    print(f"  spec version {spec.get('info', {}).get('version', '?')}, {len(live)} paths")

    # Dead routes must stay dead, or the skills' warnings have become lies.
    for dead, why in KNOWN_DEAD.items():
        if dead in live:
            failures.append(
                f"{dead} is LIVE but the skills document it as dead ({why}) — update them"
            )

    known = live | set(WS_ROUTES) | set(KNOWN_DEAD) | set(GATEWAY_ROUTES)
    for f in skill_files():
        text = f.read_text()
        for path in sorted(extract_paths(text)):
            if path not in known:
                failures.append(f"{f.parent.name}: {path} is in no live route")
        # A dead route inside a code fence is an instruction to call it.
        for path in sorted(extract_paths(code_blocks(text))):
            if path in KNOWN_DEAD:
                failures.append(
                    f"{f.parent.name}: {path} appears in a code sample — "
                    f"{KNOWN_DEAD[path]}. Name it in prose to warn, never in code."
                )


def check_ws(api_base: str, failures: list[str]) -> None:
    """A completed handshake proves the route exists; the close code is noise.

    No key is sent, so the server accepts and then closes with 4401. A missing
    route fails the handshake outright with an HTTP 404 instead.
    """
    try:
        from websocket import WebSocketBadStatusException, create_connection
    except ImportError:
        print("  ! websocket-client not installed, skipping (pip install -e .)")
        return

    ws_base = api_base.replace("https://", "wss://").replace("http://", "ws://")
    for route in WS_ROUTES:
        url = ws_base + route.replace("{}", "verify-skills-probe")
        try:
            create_connection(url, timeout=15).close()
            print(f"  ok  {route}")
        except WebSocketBadStatusException as e:
            failures.append(f"WS {route}: handshake rejected ({e})")
        except Exception as e:  # noqa: BLE001 — closed-by-server is a pass
            if "4401" in str(e) or "closed" in str(e).lower():
                print(f"  ok  {route} (accepted, then closed 4401)")
            else:
                failures.append(f"WS {route}: {e}")


def check_gateway(gateway_base: str, failures: list[str]) -> None:
    """POST-only routes answer a GET with 405. A 404 means one moved."""
    for route in sorted(GATEWAY_ROUTES):
        req = urllib.request.Request(f"{gateway_base}{route}", method="GET")  # noqa: S310
        try:
            urllib.request.urlopen(req, timeout=15)  # noqa: S310
            failures.append(f"gateway {route} answered a GET — expected 405")
        except urllib.error.HTTPError as e:
            if e.code == 405:
                print(f"  ok  POST {route}")
            else:
                failures.append(f"gateway {route} returned {e.code}, expected 405")
        except (urllib.error.URLError, TimeoutError) as e:
            failures.append(f"gateway {route} unreachable: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api-base", default=DEFAULT_API_BASE)
    ap.add_argument("--gateway-base", default=DEFAULT_GATEWAY_BASE)
    args = ap.parse_args()

    files = skill_files()
    if not files:
        print(f"no skills found under {SKILLS_DIR}", file=sys.stderr)
        return 2
    print(f"checking {len(files)} skills: {', '.join(f.parent.name for f in files)}")

    failures: list[str] = []

    print(f"\nHTTP routes vs {args.api_base}")
    check_routes(args.api_base, failures)
    print("\nWebSocket routes")
    check_ws(args.api_base, failures)
    print(f"\ngateway {args.gateway_base}")
    check_gateway(args.gateway_base, failures)

    if failures:
        print(f"\n{len(failures)} problem(s):\n", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("\nskills match the live API.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
