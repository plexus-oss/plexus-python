"""Declared commands — the pieces that don't touch the socket.

`@px.command(...)` declares what a client can be asked to do. The declaration
travels in the `device_auth` frame; a request to run one arrives as a
`command_run` frame and the client answers with `command_status` frames
(`plexus.ws` owns both). This module holds the parts either side of that wire:

    build_schema()   normalise the `params=` dict into the flat `{name: spec}`
                     map the manifest advertises, and refuse a declaration
                     the gateway or the dashboard would reject. Raises at
                     decoration time, not at 2 a.m.
    coerce_params()  check an incoming `params` object against that map and
                     hand the handler real Python types.
    CommandRun       the first argument every handler receives.

The schema subset is deliberately flat (strings, integers, numbers, booleans;
no nesting, no arrays, no oneOf). Anything richer is a command dictionary,
which Plexus does not try to be.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Danger levels. The dashboard turns these into one click, a confirm dialog,
#: or a confirm dialog that makes you type the device's slug.
DANGER_LEVELS = ("normal", "dangerous", "critical")

#: What happens when a run arrives while another run of the same command is
#: still going.
CONCURRENCY_MODES = ("accept", "reject")

#: Types the parameter schema subset allows.
PARAM_TYPES = ("string", "integer", "number", "boolean")

#: Keywords allowed on every parameter, and the ones allowed per type.
_COMMON_KEYWORDS = ("type", "title", "description", "default", "required")
_TYPE_KEYWORDS = {
    "string": ("maxLength", "enum"),
    "integer": ("minimum", "maximum", "unit"),
    "number": ("minimum", "maximum", "unit"),
    "boolean": (),
}

#: The server clamps a run's time-to-live to this range; the SDK refuses a
#: declaration outside it so the clamp never silently changes what you wrote.
EXPIRES_IN_MIN_S = 5.0
EXPIRES_IN_MAX_S = 3600.0

#: A handler may report progress as often as it likes; the SDK sends at most
#: one progress frame a second (100% always goes).
PROGRESS_INTERVAL_S = 1.0

#: Limits the gateway and the dashboard enforce on a declaration.
MAX_PARAMS = 16
MAX_ENUM_VALUES = 64


class CommandDeclarationError(ValueError):
    """A `@px.command(...)` declaration the wire protocol can't carry."""


class CommandParamError(ValueError):
    """Incoming params that don't match the declared schema.

    Reported as `failed` with `error_code: "bad_params"`. The handler never runs.
    """


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def valid_command_name(name: Any) -> bool:
    """`^[a-z][a-z0-9_]{0,63}$` — the name shape every SDK agrees on."""
    return isinstance(name, str) and _NAME_RE.match(name) is not None


def build_schema(params: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Normalise the `params=` argument into the manifest's flat param map.

    Input and output have the same shape — one entry per parameter, keyed by
    name — which is what the gateway and the dashboard both parse::

        {"outlet": {"type": "integer", "minimum": 1, "maximum": 8}}
        → {"outlet": {"type": "integer", "minimum": 1, "maximum": 8,
                      "required": True}}

    Every entry comes out with an explicit `required` boolean: required unless
    it carries a `default` or says `"required": False`. The keyword rules are
    the ones both servers enforce, so a declaration either passes all three
    layers or fails here, at import time, with `CommandDeclarationError`.
    """
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise CommandDeclarationError(
            f"params must be a dict of {{name: spec}}, got {type(params).__name__}"
        )
    if len(params) > MAX_PARAMS:
        raise CommandDeclarationError(
            f"at most {MAX_PARAMS} params per command, got {len(params)}"
        )

    out: dict[str, dict[str, Any]] = {}
    for name, spec in params.items():
        if not valid_command_name(name):
            raise CommandDeclarationError(
                f"parameter name {name!r} must match ^[a-z][a-z0-9_]{{0,63}}$"
            )
        if not isinstance(spec, dict):
            raise CommandDeclarationError(
                f"parameter {name!r}: spec must be a dict, got {type(spec).__name__}"
            )

        ptype = spec.get("type")
        if ptype not in PARAM_TYPES:
            raise CommandDeclarationError(
                f"parameter {name!r}: type must be one of {', '.join(PARAM_TYPES)}, "
                f"got {ptype!r}"
            )

        allowed = _COMMON_KEYWORDS + _TYPE_KEYWORDS[ptype]
        for keyword in spec:
            if keyword in allowed:
                continue
            if any(keyword in kws for kws in _TYPE_KEYWORDS.values()):
                raise CommandDeclarationError(
                    f"parameter {name!r}: {keyword} does not apply to {ptype}"
                )
            raise CommandDeclarationError(
                f"parameter {name!r} ({ptype}): unsupported keyword {keyword!r}. "
                f"Allowed: {', '.join(allowed)}"
            )

        prop: dict[str, Any] = {k: spec[k] for k in allowed if k in spec and k != "required"}
        _check_prop(name, prop)

        required = spec.get("required")
        if required is None:
            required = "default" not in prop
        if not isinstance(required, bool):
            raise CommandDeclarationError(
                f"parameter {name!r}: required must be True or False, got {required!r}"
            )
        if required and "default" in prop:
            raise CommandDeclarationError(
                f"parameter {name!r}: a required parameter cannot also have a default"
            )
        prop["required"] = required
        out[name] = prop

    return out


def _check_prop(name: str, prop: dict[str, Any]) -> None:
    """Validate the constraint keywords, and the default, of one parameter."""
    ptype = prop["type"]
    for text_key in ("title", "description", "unit"):
        if text_key in prop and not isinstance(prop[text_key], str):
            raise CommandDeclarationError(f"parameter {name!r}: {text_key} must be a string")

    if ptype == "string":
        if "maxLength" in prop:
            max_length = prop["maxLength"]
            if not isinstance(max_length, int) or isinstance(max_length, bool) or max_length < 1:
                raise CommandDeclarationError(
                    f"parameter {name!r}: maxLength must be a positive integer"
                )
        if "enum" in prop:
            choices = prop["enum"]
            if (
                not isinstance(choices, list)
                or not choices
                or len(choices) > MAX_ENUM_VALUES
                or not all(isinstance(c, str) for c in choices)
            ):
                raise CommandDeclarationError(
                    f"parameter {name!r}: enum must be a list of 1 to "
                    f"{MAX_ENUM_VALUES} strings"
                )
    elif ptype in ("integer", "number"):
        for bound in ("minimum", "maximum"):
            if bound in prop:
                value = prop[bound]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise CommandDeclarationError(
                        f"parameter {name!r}: {bound} must be a number"
                    )
        low, high = prop.get("minimum"), prop.get("maximum")
        if low is not None and high is not None and low > high:
            raise CommandDeclarationError(
                f"parameter {name!r}: minimum ({low}) is above maximum ({high})"
            )

    if "default" in prop:
        # A declared default is already typed: no coercion, as both servers
        # insist. `default="8"` on an integer is a typo, not a value.
        default = prop["default"]
        typed = {
            "boolean": isinstance(default, bool),
            "string": isinstance(default, str),
            "integer": isinstance(default, int) and not isinstance(default, bool),
            "number": isinstance(default, (int, float)) and not isinstance(default, bool),
        }[ptype]
        if not typed:
            raise CommandDeclarationError(
                f"parameter {name!r}: default must be a {ptype}, got {default!r}"
            )
        try:
            coerce_value(name, prop, default)
        except CommandParamError as e:
            raise CommandDeclarationError(f"parameter {name!r}: bad default — {e}") from None


def coerce_params(schema: dict[str, dict[str, Any]], values: Any) -> dict[str, Any]:
    """Check incoming params against the declared map and return typed kwargs.

    Strict on purpose: an unknown parameter is an error, not something to
    ignore. A misspelled `outlet` that quietly powers off the wrong thing is
    worse than a rejected run.

    A parameter with a declared default is filled in. An optional parameter
    with no default is left out entirely, so the handler's own default applies.
    """
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise CommandParamError(f"params must be an object, got {type(values).__name__}")

    unknown = sorted(set(values) - set(schema))
    if unknown:
        raise CommandParamError(f"unknown parameter(s): {', '.join(unknown)}")

    out: dict[str, Any] = {}
    for name, prop in schema.items():
        if name in values and values[name] is not None:
            out[name] = coerce_value(name, prop, values[name])
        elif "default" in prop:
            out[name] = prop["default"]
        elif prop.get("required", "default" not in prop):
            raise CommandParamError(f"missing required parameter: {name}")
    return out


def coerce_value(name: str, prop: dict[str, Any], value: Any) -> Any:
    """Coerce one value to its declared type, or raise `CommandParamError`.

    JSON gives us the right types most of the time, but a form field or a
    hand-written curl gives strings. `"8"` for an integer parameter is
    accepted; `"eight"` is not, and a bool is never a number.
    """
    ptype = prop["type"]

    if ptype == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise CommandParamError(f"{name} must be a boolean, got {value!r}")

    if ptype == "integer":
        if isinstance(value, bool):
            raise CommandParamError(f"{name} must be an integer, got a boolean")
        if isinstance(value, int):
            out: int | float = value
        elif isinstance(value, float) and value.is_integer():
            out = int(value)
        elif isinstance(value, str):
            try:
                out = int(value.strip())
            except ValueError:
                raise CommandParamError(
                    f"{name} must be an integer, got {value!r}"
                ) from None
        else:
            raise CommandParamError(f"{name} must be an integer, got {value!r}")
        return _check_range(name, prop, out)

    if ptype == "number":
        if isinstance(value, bool):
            raise CommandParamError(f"{name} must be a number, got a boolean")
        if isinstance(value, (int, float)):
            out = float(value)
        elif isinstance(value, str):
            try:
                out = float(value.strip())
            except ValueError:
                raise CommandParamError(f"{name} must be a number, got {value!r}") from None
        else:
            raise CommandParamError(f"{name} must be a number, got {value!r}")
        return _check_range(name, prop, out)

    # string
    if not isinstance(value, str):
        raise CommandParamError(f"{name} must be a string, got {type(value).__name__}")
    max_length = prop.get("maxLength")
    if max_length is not None and len(value) > max_length:
        raise CommandParamError(f"{name} must be at most {max_length} characters")
    choices = prop.get("enum")
    if choices is not None and value not in choices:
        raise CommandParamError(f"{name} must be one of: {', '.join(choices)}")
    return value


def _check_range(name: str, prop: dict[str, Any], value: int | float) -> int | float:
    low = prop.get("minimum")
    high = prop.get("maximum")
    if low is not None and value < low:
        raise CommandParamError(f"{name} must be at least {low}")
    if high is not None and value > high:
        raise CommandParamError(f"{name} must be at most {high}")
    return value


class Cancelled(Exception):
    """Raise from a handler to end a run as cancelled.

    Cooperative cancellation (`run.cancel_requested`) is phase 2 — nothing in
    Plexus sets it yet — but a handler that raises this is reported as
    `failed` with `error_code: "cancelled"` rather than as a crash.
    """


@dataclass
class CommandRun:
    """The run in progress. Every handler's first argument.

        @px.command("power_off", params={"outlet": {"type": "integer"}})
        def power_off(run, outlet):
            print(run.id, run.time_remaining)
            return {"outlet": outlet}

    `id` is also the idempotency key: the same id never runs twice on this
    client, however many times it is delivered.
    """

    id: str
    command: str
    params: dict[str, Any]
    attempt: int = 1
    #: `time.monotonic()` value the run's time-to-live ran out at, or None if
    #: the server sent no ttl. Monotonic on purpose — a device whose wall clock
    #: jumps when NTP lands must not mistake that for an expiry.
    deadline: float | None = None
    Cancelled = Cancelled

    _emit: Callable[..., None] | None = field(default=None, repr=False)
    _last_progress_at: float = field(default=0.0, repr=False)
    _progress_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def time_remaining(self) -> float | None:
        """Seconds left before the run's ttl runs out, or None if it had none.

        Informational: once a run is acknowledged, Plexus does not kill it.
        A long job that outlives its ttl is the handler's call, not ours.
        """
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    @property
    def cancel_requested(self) -> bool:
        """Always False in phase 1. `command_cancel` is phase 2."""
        return False

    def progress(self, percent: float | int, message: str | None = None) -> None:
        """Report progress, 0–100. Throttled to one frame a second.

        Safe to call in a tight loop. 100 is always sent.
        """
        try:
            pct = max(0.0, min(100.0, float(percent)))
        except (TypeError, ValueError):
            logger.debug("ignoring non-numeric progress %r", percent)
            return
        now = time.monotonic()
        with self._progress_lock:
            if pct < 100.0 and now - self._last_progress_at < PROGRESS_INTERVAL_S:
                return
            self._last_progress_at = now
        if self._emit is None:
            return
        extra: dict[str, Any] = {"progress": pct}
        if message:
            extra["message"] = str(message)
        self._emit(self.id, "progress", **extra)


@dataclass
class DeclaredCommand:
    """One `@px.command(...)` declaration plus its handler."""

    name: str
    handler: Callable[..., Any]
    title: str | None = None
    description: str | None = None
    #: The flat param map from `build_schema()`: {name: {type, ..., required}}.
    schema: dict[str, dict[str, Any]] = field(default_factory=dict)
    danger: str = "normal"
    idempotent: bool = False
    expires_in_s: float = 30.0
    concurrency: str = "accept"
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_manifest(self) -> dict[str, Any]:
        """The entry advertised in `device_auth.commands`."""
        manifest: dict[str, Any] = {
            "name": self.name,
            "params": self.schema,
            "danger": self.danger,
            "idempotent": self.idempotent,
            "expires_in_s": self.expires_in_s,
            "concurrency": self.concurrency,
        }
        if self.title:
            manifest["title"] = self.title
        if self.description:
            manifest["description"] = self.description
        return manifest


def declare(
    name: str,
    handler: Callable[..., Any],
    *,
    title: str | None = None,
    description: str | None = None,
    params: dict[str, dict[str, Any]] | None = None,
    danger: str = "normal",
    idempotent: bool = False,
    expires_in: float = 30.0,
    concurrency: str = "accept",
) -> DeclaredCommand:
    """Validate a declaration and build it. Raises `CommandDeclarationError`."""
    if not valid_command_name(name):
        raise CommandDeclarationError(
            f"command name {name!r} must match ^[a-z][a-z0-9_]{{0,63}}$"
        )
    if not callable(handler):
        raise CommandDeclarationError(f"command {name!r}: handler must be callable")
    if danger not in DANGER_LEVELS:
        raise CommandDeclarationError(
            f"command {name!r}: danger must be one of {', '.join(DANGER_LEVELS)}, got {danger!r}"
        )
    if concurrency not in CONCURRENCY_MODES:
        raise CommandDeclarationError(
            f"command {name!r}: concurrency must be one of {', '.join(CONCURRENCY_MODES)}, "
            f"got {concurrency!r}"
        )
    if not isinstance(idempotent, bool):
        raise CommandDeclarationError(f"command {name!r}: idempotent must be a bool")
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
        raise CommandDeclarationError(f"command {name!r}: expires_in must be a number of seconds")
    if not EXPIRES_IN_MIN_S <= float(expires_in) <= EXPIRES_IN_MAX_S:
        raise CommandDeclarationError(
            f"command {name!r}: expires_in must be between {EXPIRES_IN_MIN_S:g}s and "
            f"{EXPIRES_IN_MAX_S:g}s (the server clamps it to that range), got {expires_in!r}"
        )
    for text_key, text in (("title", title), ("description", description)):
        if text is not None and not isinstance(text, str):
            raise CommandDeclarationError(f"command {name!r}: {text_key} must be a string")

    return DeclaredCommand(
        name=name,
        handler=handler,
        title=title,
        description=description,
        schema=build_schema(params),
        danger=danger,
        idempotent=bool(idempotent),
        expires_in_s=float(expires_in),
        concurrency=concurrency,
    )
