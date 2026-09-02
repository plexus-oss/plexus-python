"""
Plexus CLI — `plexus init` style auth, plus a few sibling commands.

Designed to feel like fly.io / vercel CLIs:
    $ pip install plexus-python
    $ plexus init
    Opening browser to https://app.plexus.company/auth/cli...
    ✓ Saved API key as cli-<host>. You're set up.

Implementation:
- Spin up a local HTTP listener on a random free port.
- Open the browser to /auth/cli with the callback URL embedded.
- Block until the browser POSTs (well — redirects with key) to /callback.
- Verify the `state` parameter matches what we generated.
- Persist the key via plexus.config.save_config; the SDK already reads
  `~/.plexus/config.json` for `PLEXUS_API_KEY`.

Stdlib only — keep dependency footprint minimal.
"""

from __future__ import annotations

import argparse
import http.server
import secrets
import shutil
import socket
import socketserver
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

from . import config

DEFAULT_TIMEOUT_SECONDS = 300
SUCCESS_REDIRECT_SECONDS = 10
SUCCESS_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Plexus CLI</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta http-equiv="refresh" content="{seconds};url={target}" />
    <style>
      :root {{ color-scheme: dark; }}
      * {{ box-sizing: border-box; }}
      html, body {{ height: 100%; }}
      body {{
        margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, 'Inter',
          'Segoe UI', system-ui, sans-serif;
        background: #000;
        color: #fafafa;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 24px;
        -webkit-font-smoothing: antialiased;
      }}
      .shell {{
        width: 100%;
        max-width: 380px;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 24px;
      }}
      .brand {{
        display: flex;
        align-items: center;
        gap: 10px;
        color: #e4e4e7;
        font-size: 14px;
        font-weight: 500;
        letter-spacing: -0.01em;
      }}
      .brand .mark {{
        width: 22px;
        height: 22px;
        border-radius: 6px;
        background: linear-gradient(135deg, #fafafa 0%, #71717a 100%);
        display: inline-block;
      }}
      .card {{
        width: 100%;
        background: #09090b;
        border: 1px solid #27272a;
        border-radius: 12px;
        padding: 28px 28px 24px;
        text-align: center;
      }}
      .check {{
        width: 36px;
        height: 36px;
        border-radius: 999px;
        background: rgba(34, 197, 94, 0.12);
        color: #4ade80;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        margin: 0 auto 16px;
      }}
      .check svg {{ width: 18px; height: 18px; }}
      h1 {{
        margin: 0 0 6px;
        font-size: 16px;
        font-weight: 600;
        color: #fafafa;
        letter-spacing: -0.01em;
      }}
      .lede {{
        margin: 0 0 20px;
        color: #a1a1aa;
        font-size: 13px;
        line-height: 1.5;
      }}
      .meta {{
        margin-top: 20px;
        padding-top: 16px;
        border-top: 1px solid #18181b;
        color: #71717a;
        font-size: 12px;
      }}
      .meta a {{
        color: #a1a1aa;
        text-decoration: none;
        font-family: ui-monospace, SFMono-Regular, 'SF Mono',
          Menlo, Consolas, monospace;
      }}
      .meta a:hover {{ color: #fafafa; }}
      #countdown {{
        font-variant-numeric: tabular-nums;
        color: #e4e4e7;
      }}
    </style>
  </head>
  <body>
    <div class="shell">
      <div class="brand">
        <span class="mark" aria-hidden="true"></span>
        <span>Plexus</span>
      </div>
      <div class="card">
        <div class="check" aria-hidden="true">
          <svg viewBox="0 0 20 20" fill="none"
               stroke="currentColor" stroke-width="2.5"
               stroke-linecap="round" stroke-linejoin="round">
            <polyline points="5 10.5 8.5 14 15 7" />
          </svg>
        </div>
        <h1>You&rsquo;re all set</h1>
        <p class="lede">
          Return to your terminal &mdash; the CLI has your key.
        </p>
        <div class="meta">
          Opening <a href="{target}">{target_label}</a> in
          <span id="countdown">{seconds}</span>s&hellip;
        </div>
      </div>
    </div>
    <script>
      (function () {{
        var n = {seconds};
        var el = document.getElementById('countdown');
        var t = setInterval(function () {{
          n -= 1;
          if (el) el.textContent = n;
          if (n <= 0) {{
            clearInterval(t);
            window.location.replace({target_js!s});
          }}
        }}, 1000);
      }})();
    </script>
  </body>
</html>"""


def _success_html(target: str) -> bytes:
    label = target.replace("https://", "").replace("http://", "").rstrip("/")
    return SUCCESS_HTML_TEMPLATE.format(
        seconds=SUCCESS_REDIRECT_SECONDS,
        target=target,
        target_label=label,
        target_js=repr(target),
    ).encode("utf-8")

ERROR_HTML = b"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Plexus CLI</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      :root { color-scheme: dark; }
      * { box-sizing: border-box; }
      html, body { height: 100%; }
      body {
        margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, 'Inter',
          'Segoe UI', system-ui, sans-serif;
        background: #000;
        color: #fafafa;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 24px;
        -webkit-font-smoothing: antialiased;
      }
      .shell {
        width: 100%;
        max-width: 380px;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 24px;
      }
      .brand {
        display: flex; align-items: center; gap: 10px;
        color: #e4e4e7; font-size: 14px; font-weight: 500;
        letter-spacing: -0.01em;
      }
      .brand .mark {
        width: 22px; height: 22px; border-radius: 6px;
        background: linear-gradient(135deg, #fafafa 0%, #71717a 100%);
        display: inline-block;
      }
      .card {
        width: 100%;
        background: #09090b;
        border: 1px solid #27272a;
        border-radius: 12px;
        padding: 28px;
        text-align: center;
      }
      .icon {
        width: 36px; height: 36px; border-radius: 999px;
        background: rgba(239, 68, 68, 0.12);
        color: #f87171;
        display: inline-flex; align-items: center; justify-content: center;
        margin: 0 auto 16px;
      }
      .icon svg { width: 18px; height: 18px; }
      h1 {
        margin: 0 0 6px; font-size: 16px; font-weight: 600;
        color: #fafafa; letter-spacing: -0.01em;
      }
      p {
        margin: 0; color: #a1a1aa; font-size: 13px; line-height: 1.5;
      }
    </style>
  </head>
  <body>
    <div class="shell">
      <div class="brand">
        <span class="mark" aria-hidden="true"></span>
        <span>Plexus</span>
      </div>
      <div class="card">
        <div class="icon" aria-hidden="true">
          <svg viewBox="0 0 20 20" fill="none"
               stroke="currentColor" stroke-width="2.5"
               stroke-linecap="round" stroke-linejoin="round">
            <line x1="6" y1="6" x2="14" y2="14" />
            <line x1="14" y1="6" x2="6" y2="14" />
          </svg>
        </div>
        <h1>Authorization failed</h1>
        <p>Return to your terminal for details.</p>
      </div>
    </div>
  </body>
</html>"""


class _CallbackResult:
    key: str | None = None
    state: str | None = None
    error: str | None = None


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_handler(
    result: _CallbackResult,
    expected_state: str,
    done: threading.Event,
    redirect_target: str,
):
    success_html = _success_html(redirect_target)

    class Handler(http.server.BaseHTTPRequestHandler):
        # Silence the default request log — we don't want CLI noise.
        def log_message(self, *_args, **_kwargs):  # type: ignore[override]
            return

        def do_GET(self):  # type: ignore[override]
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return

            params = urllib.parse.parse_qs(parsed.query)
            got_state = (params.get("state") or [""])[0]
            got_key = (params.get("key") or [""])[0]

            if got_state != expected_state:
                result.error = "state mismatch"
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(ERROR_HTML)
                done.set()
                return

            if not got_key:
                result.error = "no key in callback"
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(ERROR_HTML)
                done.set()
                return

            result.key = got_key
            result.state = got_state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(success_html)
            done.set()

    return Handler


def _hostname_label() -> str:
    try:
        host = socket.gethostname() or "device"
    except Exception:
        host = "device"
    # Strip the trailing .local etc. and clean it for display.
    safe = host.split(".")[0].lower().replace(" ", "-")
    return f"cli-{safe}" if safe else "cli"


def cmd_init(args: argparse.Namespace) -> int:
    """Open the browser, capture an API key, save it locally."""
    existing = config.get_api_key()
    if existing and not args.force:
        print(
            "An API key is already configured. "
            "Re-run with --force to replace it.",
            file=sys.stderr,
        )
        return 1

    endpoint = config.get_endpoint().rstrip("/")
    state = secrets.token_urlsafe(24)
    name = args.name or _hostname_label()
    port = _pick_free_port()
    callback = f"http://127.0.0.1:{port}/callback"

    auth_url = (
        f"{endpoint}/auth/cli"
        f"?state={urllib.parse.quote(state)}"
        f"&callback={urllib.parse.quote(callback)}"
        f"&name={urllib.parse.quote(name)}"
    )

    result = _CallbackResult()
    done = threading.Event()
    handler = _make_handler(result, state, done, redirect_target=endpoint)

    server = socketserver.TCPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        print(f"Opening {auth_url}")
        try:
            webbrowser.open(auth_url, new=1, autoraise=True)
        except Exception:
            pass  # User can copy the URL manually.

        print("Waiting for browser confirmation...", flush=True)
        finished = done.wait(timeout=args.timeout)
        if not finished:
            print(
                f"Timed out after {args.timeout}s. Re-run `plexus init`.",
                file=sys.stderr,
            )
            return 2
    finally:
        server.shutdown()
        server.server_close()

    if result.error or not result.key:
        print(
            f"Authorization failed: {result.error or 'no key returned'}",
            file=sys.stderr,
        )
        return 3

    cfg = config.load_config()
    cfg["api_key"] = result.key
    config.save_config(cfg)
    print(f"✓ Saved API key as {name}.")
    print("  ~/.plexus/config.json")
    return 0


def cmd_logout(_args: argparse.Namespace) -> int:
    """Forget the locally stored API key."""
    cfg = config.load_config()
    if not cfg.get("api_key"):
        print("Nothing to do — no key on file.")
        return 0
    cfg["api_key"] = None
    config.save_config(cfg)
    print("✓ Cleared local API key.")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    """Report the local key, and whether the server still accepts it.

    Printing the key alone was worse than printing nothing: a revoked or
    expired credential produced confident-looking output and a zero exit, and
    the 401s that followed from the SDK looked unrelated. `whoami` is what
    someone runs precisely when they suspect their auth — it has to answer
    that question rather than confirm a file exists.
    """
    key = config.get_api_key()
    endpoint = config.get_endpoint()
    if not key:
        print("Not signed in. Run `plexus init` to authorize this machine.")
        return 1
    masked = f"{key[:8]}…{key[-4:]}" if len(key) > 12 else key
    print(f"key:      {masked}")
    print(f"endpoint: {endpoint}")

    if args.no_verify:
        return 0

    status, body = _verify_key(endpoint, key)
    if status == 200:
        org = body.get("org_id") or "unknown"
        scopes = ", ".join(body.get("scopes") or []) or "default"
        print(f"org:      {org}")
        print(f"scopes:   {scopes}")
        print("status:   valid")
        return 0
    if status == 401:
        print("status:   REJECTED — this key is invalid, revoked or expired.")
        print("          Run `plexus init --force` to authorize this machine again.")
        return 1
    if status == 403:
        print("status:   DISABLED — the key is real but access is switched off.")
        print("          Usually billing; check with your org admin.")
        return 1
    if status is None:
        # Could not ask. Say so rather than implying either answer.
        print(f"status:   unknown — could not reach {endpoint} ({body})")
        return 0
    print(f"status:   unexpected response ({status})")
    return 1


def _verify_key(endpoint: str, key: str) -> tuple[int | None, Any]:
    """Ask the server whether a key is good. Returns (status, parsed_or_reason).

    A network failure returns (None, reason): unreachable is not the same as
    rejected, and reporting one as the other is how a flaky connection gets
    mistaken for a credentials problem.
    """
    import json as _json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"{endpoint.rstrip('/')}/api/auth/verify-key",
        headers={"x-api-key": key},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, _json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, _json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:  # DNS, TLS, timeout, offline
        return None, str(e)


def _bundled_skills_dir() -> Path | None:
    """Where the shipped skills live, wheel or checkout.

    An installed wheel force-includes them at `plexus/_skills` (see
    pyproject.toml). An editable install or a source checkout has no such
    directory, so fall back to the repo root, which is where they are
    authored and where `scripts/verify_skills.py` checks them.
    """
    here = Path(__file__).resolve().parent
    for candidate in (here / "_skills", here.parent / "skills"):
        if candidate.is_dir():
            return candidate
    return None


def _skill_names(root: Path) -> list[str]:
    return sorted(p.parent.name for p in root.glob("*/SKILL.md"))


def cmd_skills(args: argparse.Namespace) -> int:
    """Copy the bundled agent skills into a skills directory."""
    source = _bundled_skills_dir()
    if source is None:
        print(
            "No bundled skills found. This build did not ship them — "
            "reinstall with `pip install --upgrade plexus-python`.",
            file=sys.stderr,
        )
        return 1

    names = _skill_names(source)
    if not names:
        print(f"No skills in {source}.", file=sys.stderr)
        return 1

    if args.list:
        print(f"Bundled skills ({source}):")
        for name in names:
            print(f"  {name}")
        return 0

    target = Path(args.dir).expanduser() if args.dir else _default_skills_target(args)
    target.mkdir(parents=True, exist_ok=True)

    for name in names:
        dest = target / name
        # These are canonical reference docs, not user config: a stale copy is
        # the failure mode we are fixing, so refresh rather than skip. Local
        # edits are reported so an overwrite is never silent.
        existed = dest.exists()
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(source / name, dest)
        print(f"  {'updated' if existed else 'installed'}  {name}")

    print(f"\n✓ {len(names)} skills in {target}")
    print("Ask your agent for what you want — it picks the right one.")
    return 0


def _default_skills_target(args: argparse.Namespace) -> Path:
    """`~/.claude/skills` normally; `.claude/skills` here with --project."""
    if args.project:
        return Path.cwd() / ".claude" / "skills"
    return Path.home() / ".claude" / "skills"


class _VersionAwareParser(argparse.ArgumentParser):
    """Turns an unknown subcommand into a version diagnosis.

    argparse says `invalid choice: 'skills'` and stops. When the real cause is
    an old install — a pipx shim from months ago shadowing a fresh pip
    install, say — that message sends people looking for a typo instead of at
    their version. The command they were told to run genuinely does not exist
    *here*, and the fix is an upgrade, so say which version is running and how
    to move it.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        if "invalid choice" in message:
            from plexus import __version__

            self.print_usage(sys.stderr)
            print(f"\n{self.prog}: error: {message}", file=sys.stderr)
            print(
                f"\nYou are running plexus-python {__version__}. If you were "
                "following a doc that\nnames this command, your install is "
                "probably older than the doc:\n"
                "\n    pip install --upgrade plexus-python"
                "\n    pipx upgrade plexus-python      # if you installed with pipx"
                "\n\nThen check with: plexus --version",
                file=sys.stderr,
            )
            self.exit(2)
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    from plexus import __version__

    parser = _VersionAwareParser(
        prog="plexus",
        description="Plexus CLI — auth, send, query telemetry from your terminal.",
    )
    # The first thing anyone types when a CLI misbehaves, and previously an
    # error: `plexus --version` demanded a subcommand and told you nothing.
    parser.add_argument(
        "--version",
        action="version",
        version=f"plexus-python {__version__}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser(
        "init",
        help="Authorize this machine and save an API key locally.",
        aliases=["login"],
    )
    init.add_argument("--name", help="Label for the issued key (default: cli-<hostname>).")
    init.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Seconds to wait for the browser callback.",
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing local key.",
    )
    init.set_defaults(func=cmd_init)

    logout = sub.add_parser("logout", help="Forget the local API key.")
    logout.set_defaults(func=cmd_logout)

    whoami = sub.add_parser("whoami", help="Show the local credential summary.")
    whoami.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the server check and only print what is stored locally.",
    )
    whoami.set_defaults(func=cmd_whoami)

    skills = sub.add_parser(
        "skills",
        help="Install the bundled agent skills into your skills directory.",
        description=(
            "Copy the Plexus agent skills (plexus, plexus-firmware, "
            "plexus-dashboard) into ~/.claude/skills so a coding agent knows "
            "the API. Existing copies are refreshed — they are reference "
            "docs, and a stale one is what this fixes."
        ),
    )
    skills.add_argument(
        "action",
        nargs="?",
        default="install",
        choices=["install"],
        help="Only `install` today; positional so `plexus skills install` reads naturally.",
    )
    skills.add_argument("--dir", help="Target directory (default: ~/.claude/skills).")
    skills.add_argument(
        "--project",
        action="store_true",
        help="Install into ./.claude/skills so they travel with the repo.",
    )
    skills.add_argument(
        "--list",
        action="store_true",
        help="List the bundled skills and exit without writing anything.",
    )
    skills.set_defaults(func=cmd_skills)

    return parser


def main(argv: list | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
