"""`plexus dashboards` — keep Plexus dashboards as JSON files in your repo.

    plexus dashboards list
    plexus dashboards pull [UID ...] [--all]
    plexus dashboards diff [PATH ...]
    plexus dashboards push [PATH ...] [--dry-run] [--force]

One file per dashboard, `<uid>.json`, in `plexus/dashboards/` (override with
--dir or PLEXUS_DASHBOARDS_DIR). The server formats the file; pull writes its
bytes as they come, and push writes back what the server stored, so the file
in your repo is always the canonical text.

`.plexus-lock.json` in the same folder remembers the version (ETag) of each
dashboard you last pulled or pushed. Push sends it, so an edit someone made in
the app since then is reported instead of overwritten (use --force to
overwrite). The lock is local state and is git-ignored.

Needs an API key with the `dashboards` scope: `plexus init` issues one.

Exit codes: 0 ok, 2 an error. `diff` (and `push --dry-run`) exit 1 when
there are changes, like `git diff --exit-code`, so CI can check for drift.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import config

DEFAULT_DIR = "plexus/dashboards"
LOCK_NAME = ".plexus-lock.json"

EXIT_OK, EXIT_CHANGES, EXIT_ERROR = 0, 1, 2


class ApiError(Exception):
    pass


# ─── HTTP ───────────────────────────────────────────────────────────────────


def _request(
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """One request to the app. Returns (status, headers, body); raises on no network."""
    key = config.get_api_key()
    if not key:
        raise ApiError("No API key. Run `plexus init` first.")
    url = f"{config.get_endpoint().rstrip('/')}{path}"
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("x-api-key", key)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read()
    except urllib.error.URLError as e:
        raise ApiError(f"Can't reach {url}: {e.reason}") from e


def _json(data: bytes) -> Any:
    try:
        return json.loads(data.decode() or "{}")
    except ValueError:
        return {}


def _explain(status: int, data: bytes) -> str:
    """One readable message per problem, with a hint for the common ones."""
    body = _json(data)
    msg = body.get("message") or body.get("error") or f"HTTP {status}"
    lines = [msg]
    details = body.get("details")
    if isinstance(details, list):
        for d in details:
            if isinstance(d, dict):
                path = d.get("path")
                lines.append(f"  {path + ': ' if path else ''}{d.get('message', '')}")
    if status == 403 and "scope" in str(msg):
        lines.append("  Your key can't manage dashboards. Run `plexus init --force` for one that can.")
    if status == 401:
        lines.append("  Your key was rejected. Run `plexus init --force`.")
    return "\n".join(lines)


def _ref(uid: str) -> str:
    return urllib.parse.quote(uid, safe="")


# ─── Local files ────────────────────────────────────────────────────────────


def _dir(args: argparse.Namespace) -> Path:
    return Path(args.dir or os.environ.get("PLEXUS_DASHBOARDS_DIR") or DEFAULT_DIR)


def _load_lock(folder: Path) -> dict[str, str]:
    try:
        data = json.loads((folder / LOCK_NAME).read_text())
        return {k: v for k, v in data.items() if isinstance(v, str)}
    except (OSError, ValueError):
        return {}


def _save_lock(folder: Path, lock: dict[str, str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / LOCK_NAME).write_text(json.dumps(dict(sorted(lock.items())), indent=2) + "\n")
    ignore = folder / ".gitignore"
    if not ignore.exists():
        ignore.write_text(f"{LOCK_NAME}\n")


def _files(args: argparse.Namespace, folder: Path) -> list[Path]:
    if args.paths:
        return [Path(p) for p in args.paths]
    return sorted(p for p in folder.glob("*.json") if p.name != LOCK_NAME)


def _read_file(path: Path) -> tuple[str, bytes]:
    """(uid, raw bytes) of a local dashboard file."""
    raw = path.read_bytes()
    try:
        uid = json.loads(raw.decode())["uid"]
    except (ValueError, KeyError, TypeError) as e:
        raise ApiError(f"{path}: not a dashboard file ({e})") from e
    return uid, raw


def _show_diff(remote: str, local: str, label: str) -> bool:
    lines = list(
        difflib.unified_diff(
            remote.splitlines(keepends=True),
            local.splitlines(keepends=True),
            fromfile=f"plexus:{label}",
            tofile=label,
        )
    )
    sys.stdout.writelines(lines)
    return bool(lines)


# ─── Commands ───────────────────────────────────────────────────────────────


def cmd_list(args: argparse.Namespace) -> int:
    status, _, data = _request("GET", "/api/dashboards")
    if status != 200:
        print(_explain(status, data), file=sys.stderr)
        return EXIT_ERROR
    folder = _dir(args)
    rows = _json(data).get("dashboards", [])
    if not rows:
        print("No dashboards yet.")
        return EXIT_OK
    width = max(len(r.get("uid") or "") for r in rows)
    for r in sorted(rows, key=lambda r: r.get("uid") or ""):
        uid = r.get("uid") or ""
        here = "local" if (folder / f"{uid}.json").exists() else "     "
        print(f"{uid.ljust(width)}  {here}  {r.get('panel_count', 0):>3} panels  {r.get('name', '')}")
    return EXIT_OK


def cmd_pull(args: argparse.Namespace) -> int:
    folder = _dir(args)
    uids = list(args.uids)
    if args.all or not uids:
        if not args.all and not uids:
            print("Name the dashboards to pull, or use --all. `plexus dashboards list` shows them.", file=sys.stderr)
            return EXIT_ERROR
        status, _, data = _request("GET", "/api/dashboards")
        if status != 200:
            print(_explain(status, data), file=sys.stderr)
            return EXIT_ERROR
        uids = [r["uid"] for r in _json(data).get("dashboards", []) if r.get("uid")]

    lock = _load_lock(folder)
    failed = False
    folder.mkdir(parents=True, exist_ok=True)
    for uid in uids:
        status, headers, data = _request("GET", f"/api/dashboards/{_ref(uid)}/json")
        if status != 200:
            print(f"{uid}: {_explain(status, data)}", file=sys.stderr)
            failed = True
            continue
        (folder / f"{uid}.json").write_bytes(data)
        lock[uid] = headers.get("etag", "")
        print(f"pulled {folder / (uid + '.json')}")
    _save_lock(folder, lock)
    return EXIT_ERROR if failed else EXIT_OK


def cmd_diff(args: argparse.Namespace) -> int:
    folder = _dir(args)
    result = EXIT_OK
    for path in _files(args, folder):
        try:
            uid, raw = _read_file(path)
        except ApiError as e:
            print(e, file=sys.stderr)
            result = EXIT_ERROR
            continue
        # The server's dry run gives the canonical form of the local file, so
        # formatting-only differences don't show up as changes.
        status, _, data = _request("PUT", f"/api/dashboards/{_ref(uid)}/json?dryRun=true", raw)
        if status not in (200, 201):
            print(f"{path}: {_explain(status, data)}", file=sys.stderr)
            result = EXIT_ERROR
            continue
        planned = _json(data)
        rstatus, _, remote = _request("GET", f"/api/dashboards/{_ref(uid)}/json")
        remote_text = remote.decode() if rstatus == 200 else ""
        if rstatus == 404:
            print(f"{path}: new dashboard")
        changed = _show_diff(remote_text, planned.get("file", ""), str(path))
        for w in planned.get("warnings", []):
            print(f"  warning {w.get('path', '')}: {w.get('message', '')}")
        if changed and result == EXIT_OK:
            result = EXIT_CHANGES
    return result


def cmd_push(args: argparse.Namespace) -> int:
    if args.dry_run:
        return cmd_diff(args)
    folder = _dir(args)
    lock = _load_lock(folder)
    result = EXIT_OK
    for path in _files(args, folder):
        try:
            uid, raw = _read_file(path)
        except ApiError as e:
            print(e, file=sys.stderr)
            result = EXIT_ERROR
            continue
        headers: dict[str, str] = {}
        if not args.force and lock.get(uid):
            headers["If-Match"] = lock[uid]
        query = "?force=true" if args.force else ""
        status, resp_headers, data = _request("PUT", f"/api/dashboards/{_ref(uid)}/json{query}", raw, headers)
        if status == 412:
            print(
                f"{path}: changed in Plexus since you last pulled it. "
                f"Run `plexus dashboards pull {uid}` and redo your change, or push with --force to overwrite.",
                file=sys.stderr,
            )
            result = EXIT_ERROR
            continue
        if status not in (200, 201):
            print(f"{path}: {_explain(status, data)}", file=sys.stderr)
            result = EXIT_ERROR
            continue
        body = _json(data)
        # Write back what the server stored: canonical formatting, assigned ids.
        path.write_text(body.get("file", raw.decode()))
        lock[uid] = body.get("etag") or resp_headers.get("etag", "")
        if body.get("unchanged"):
            print(f"{path}: unchanged")
        else:
            verb = "created" if body.get("created") else "updated"
            print(f"{path}: {verb}  {config.get_endpoint().rstrip('/')}/dashboards/{uid}")
            if result == EXIT_OK:
                result = EXIT_CHANGES
        for w in body.get("warnings", []):
            print(f"  warning {w.get('path', '')}: {w.get('message', '')}")
    _save_lock(folder, lock)
    # A push that changed things succeeded; only errors are failures.
    return EXIT_ERROR if result == EXIT_ERROR else EXIT_OK


def _guard(fn):
    def run(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except ApiError as e:
            print(e, file=sys.stderr)
            return EXIT_ERROR

    return run


def add_parser(sub: argparse._SubParsersAction) -> None:
    dash = sub.add_parser(
        "dashboards",
        help="Keep dashboards as JSON files in your repo (pull, diff, push).",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
    )
    dsub = dash.add_subparsers(dest="dashboards_command", required=True)

    def with_dir(p: argparse.ArgumentParser) -> None:
        p.add_argument("--dir", help=f"Folder for dashboard files (default: {DEFAULT_DIR}).")

    p = dsub.add_parser("list", help="List dashboards and which ones you have locally.")
    with_dir(p)
    p.set_defaults(func=_guard(cmd_list))

    p = dsub.add_parser("pull", help="Download dashboards as <uid>.json.")
    p.add_argument("uids", nargs="*", metavar="UID")
    p.add_argument("--all", action="store_true", help="Pull every dashboard.")
    with_dir(p)
    p.set_defaults(func=_guard(cmd_pull))

    p = dsub.add_parser("diff", help="Show what push would change. Exit 1 if anything would.")
    p.add_argument("paths", nargs="*", metavar="PATH")
    with_dir(p)
    p.set_defaults(func=_guard(cmd_diff))

    p = dsub.add_parser("push", help="Upload local files. Creates dashboards with new uids.")
    p.add_argument("paths", nargs="*", metavar="PATH")
    p.add_argument("--dry-run", action="store_true", help="Same as diff; changes nothing.")
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite edits made in Plexus since your last pull, and allow removing embedded panels.",
    )
    with_dir(p)
    p.set_defaults(func=_guard(cmd_push))
