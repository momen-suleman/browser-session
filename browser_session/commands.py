"""The command language: a closed allow-list of browser actions.

Every command a controller sends (REST, WebSocket or the Orqen text
contract) is validated here into a normalized dict before it touches the
browser. There is deliberately no `evaluate`/script action: a controller can
drive the page like a person would, never run code in it.
"""

from __future__ import annotations

import json
import os
import re

MAX_TEXT = 10_000
MAX_URL = 4_096
DEFAULT_TIMEOUT_MS = 15_000
MAX_TIMEOUT_MS = 60_000
MAX_WAIT_MS = 60_000

WAIT_UNTIL = ("load", "domcontentloaded", "networkidle", "commit")
EXTRACT = ("text", "html", "value", "attribute", "links", "title", "url")
MOUSE_BUTTONS = ("left", "right", "middle")
TARGET_KEYS = ("selector", "text", "role", "label", "placeholder", "test_id")
SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
KEY_NAME = re.compile(r"^[A-Za-z0-9+_\- ]{1,40}$")
SHOT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

# action -> (target: "required" | "optional" | "point" | None)
ACTIONS = {
    "goto": None,
    "click": "point",
    "dblclick": "point",
    "fill": "required",
    "type": "optional",
    "press": "optional",
    "hover": "point",
    "select": "required",
    "check": "required",
    "uncheck": "required",
    "scroll": None,
    "wait": "optional",
    "screenshot": "optional",
    "extract": "optional",
    "back": None,
    "forward": None,
    "reload": None,
    "note": None,
    "mark": None,
}


class CommandError(ValueError):
    """A command that is not allowed or not well formed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _text(cmd: dict, key: str, *, required: bool = False, limit: int = MAX_TEXT) -> str | None:
    value = cmd.get(key)
    if value is None:
        if required:
            raise CommandError("missing_field", f"'{key}' is required")
        return None
    if not isinstance(value, str):
        raise CommandError("invalid_field", f"'{key}' must be text")
    if len(value) > limit:
        raise CommandError("invalid_field", f"'{key}' is longer than {limit} characters")
    if required and not value.strip():
        raise CommandError("missing_field", f"'{key}' is empty")
    return value


def _int(cmd: dict, key: str, default: int, lo: int, hi: int) -> int:
    value = cmd.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommandError("invalid_field", f"'{key}' must be a number")
    value = int(value)
    if not lo <= value <= hi:
        raise CommandError("invalid_field", f"'{key}' must be between {lo} and {hi}")
    return value


def _target(cmd: dict, mode: str | None) -> dict | None:
    present = [k for k in TARGET_KEYS if cmd.get(k) is not None]
    point = cmd.get("x") is not None or cmd.get("y") is not None
    if len(present) > 1:
        raise CommandError("invalid_target", f"give one target, not {', '.join(present)}")
    if present and point:
        raise CommandError("invalid_target", "give a target or x/y, not both")
    if point:
        if mode != "point":
            raise CommandError("invalid_target", "x/y is only for click, dblclick and hover")
        return {"x": _int(cmd, "x", 0, 0, 10_000), "y": _int(cmd, "y", 0, 0, 10_000)}
    if not present:
        if mode in ("required", "point"):
            raise CommandError("missing_target", "a target is required (selector, text, role, label, placeholder, test_id or x/y)")
        return None
    key = present[0]
    target = {key: _text(cmd, key, required=True, limit=1_000)}
    if key == "role":
        name = _text(cmd, "name", limit=500)
        if name is not None:
            target["name"] = name
    if cmd.get("exact") is not None:
        target["exact"] = bool(cmd["exact"])
    return target


def _value_or_secret(cmd: dict, key: str, out: dict) -> None:
    secret = cmd.get("secret")
    value = _text(cmd, key)
    if secret is not None and value is not None:
        raise CommandError("invalid_field", f"give '{key}' or 'secret', not both")
    if secret is not None:
        if not isinstance(secret, str) or not SECRET_NAME.match(secret):
            raise CommandError("invalid_field", "'secret' must be an UPPER_CASE name")
        out["secret"] = secret
    elif value is None:
        raise CommandError("missing_field", f"'{key}' or 'secret' is required")
    else:
        out[key] = value
    out["mask"] = bool(cmd.get("mask", False)) or secret is not None


def normalize(cmd: object) -> dict:
    """Validate one command and return its normalized form."""
    if not isinstance(cmd, dict):
        raise CommandError("invalid_command", "a command is a JSON object")
    action = cmd.get("action")
    if action not in ACTIONS:
        raise CommandError("unknown_action", f"unknown action {action!r}; allowed: {', '.join(ACTIONS)}")
    out: dict = {"action": action}
    target = _target(cmd, ACTIONS[action])
    if target:
        out["target"] = target
    if action not in ("note", "mark", "scroll", "back", "forward", "reload"):
        out["timeout_ms"] = _int(cmd, "timeout_ms", DEFAULT_TIMEOUT_MS, 100, MAX_TIMEOUT_MS)

    if action == "goto":
        out["url"] = _text(cmd, "url", required=True, limit=MAX_URL).strip()
        wait = cmd.get("wait_until", "load")
        if wait not in WAIT_UNTIL:
            raise CommandError("invalid_field", f"'wait_until' must be one of {', '.join(WAIT_UNTIL)}")
        out["wait_until"] = wait
    elif action in ("click", "dblclick"):
        button = cmd.get("button", "left")
        if button not in MOUSE_BUTTONS:
            raise CommandError("invalid_field", "'button' must be left, right or middle")
        out["button"] = button
    elif action == "fill":
        _value_or_secret(cmd, "value", out)
    elif action == "type":
        _value_or_secret(cmd, "text", out)
        out["delay_ms"] = _int(cmd, "delay_ms", 0, 0, 500)
    elif action == "press":
        key = _text(cmd, "key", required=True, limit=40)
        if not KEY_NAME.match(key):
            raise CommandError("invalid_field", "'key' is a key name like Enter, Tab, Control+A")
        out["key"] = key
    elif action == "select":
        values = cmd.get("values", cmd.get("value"))
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
            raise CommandError("missing_field", "'value' (text) or 'values' (list of text) is required")
        out["values"] = values[:50]
    elif action == "scroll":
        out["dx"] = _int(cmd, "dx", 0, -20_000, 20_000)
        out["dy"] = _int(cmd, "dy", 600, -20_000, 20_000)
    elif action == "wait":
        if target:
            state = cmd.get("state", "visible")
            if state not in ("attached", "detached", "visible", "hidden"):
                raise CommandError("invalid_field", "'state' must be attached, detached, visible or hidden")
            out["state"] = state
        elif cmd.get("load_state") is not None:
            if cmd["load_state"] not in ("load", "domcontentloaded", "networkidle"):
                raise CommandError("invalid_field", "'load_state' must be load, domcontentloaded or networkidle")
            out["load_state"] = cmd["load_state"]
        else:
            out["ms"] = _int(cmd, "ms", 1_000, 0, MAX_WAIT_MS)
    elif action == "screenshot":
        name = cmd.get("name")
        if name is not None and (not isinstance(name, str) or not SHOT_NAME.match(name)):
            raise CommandError("invalid_field", "'name' is lowercase letters, digits, - and _")
        if name:
            out["name"] = name
        out["full_page"] = bool(cmd.get("full_page", False))
    elif action == "extract":
        what = cmd.get("what", "text")
        if what not in EXTRACT:
            raise CommandError("invalid_field", f"'what' must be one of {', '.join(EXTRACT)}")
        out["what"] = what
        if what == "attribute":
            out["attribute"] = _text(cmd, "attribute", required=True, limit=100)
        if what in ("value", "attribute") and not target:
            raise CommandError("missing_target", f"extract {what} needs a target")
        name = cmd.get("name")
        if name is not None:
            if not isinstance(name, str) or not SHOT_NAME.match(name):
                raise CommandError("invalid_field", "'name' is lowercase letters, digits, - and _")
            out["name"] = name
    elif action in ("note", "mark"):
        out["text"] = _text(cmd, "text", required=True, limit=2_000)
    return out


def normalize_all(commands: object, limit: int = 100) -> list[dict]:
    if isinstance(commands, str):
        # A workflow may pass the list as JSON text.
        try:
            commands = json.loads(commands)
        except ValueError:
            raise CommandError("invalid_command", "'commands' is a list (or its JSON text)") from None
    if isinstance(commands, dict):
        commands = [commands]
    if not isinstance(commands, list) or not commands:
        raise CommandError("invalid_command", "'commands' is a non-empty list")
    if len(commands) > limit:
        raise CommandError("too_many_commands", f"at most {limit} commands per request")
    out = []
    for index, cmd in enumerate(commands):
        try:
            out.append(normalize(cmd))
        except CommandError as err:
            raise CommandError(err.code, f"command {index + 1}: {err.message}") from None
    return out


def resolve_secret(name: str) -> str:
    """Secrets live only in the service's environment: BROWSER_SECRET_<NAME>."""
    value = os.environ.get(f"BROWSER_SECRET_{name}", "")
    if not value:
        raise CommandError("unknown_secret", f"secret {name} is not configured (BROWSER_SECRET_{name})")
    return value


def public_args(cmd: dict) -> dict:
    """The command as it is logged and shown: typed values masked."""
    shown = {k: v for k, v in cmd.items() if k not in ("timeout_ms",)}
    if cmd.get("mask"):
        for key in ("value", "text"):
            if key in shown:
                shown[key] = "•" * 8
    shown.pop("mask", None)
    return shown
