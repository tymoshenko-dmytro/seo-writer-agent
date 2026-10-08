"""Who may open the Studio: a password, set in the Studio or by the server's environment
(UI_PASSWORD), or none (for an install that sits behind its own sign-in, such as an access proxy).

The choice made in the Studio lives in the output root (`_access.json`, owner-only; the password
hashed with scrypt) and wins over UI_PASSWORD. Without either, the Studio answers only this machine
(ui/auth.py). A team locked out sets UI_ACCESS_RESET=1 and restarts: the saved choice is replaced
at start by a fresh salt and UI_PASSWORD rules again.

A session carries the epoch of the credentials it signed in with (`epoch`): a random one per
password set in the Studio, a keyed digest of UI_PASSWORD for the server's own. Epochs never repeat,
so a changed, reset or rotated password signs every other browser out for good (R058-01). A change
is checked against the state it read and written under a lock only while that state still holds,
and a login keeps its epoch only when the password it checked is still the one in force: a request
racing a change cannot undo it (R058-02).
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ACCESS_FILE = "_access.json"
LOCK_FILE = "_access.lock"
MIN_LENGTH = 8
_N, _R, _P = 2 ** 14, 8, 1


class Refused(ValueError):
    """A change the Studio does not make: a wrong current password, a new one too short, a state
    that changed meanwhile."""


def _path(root) -> Path:
    return Path(root) / ACCESS_FILE


def saved(root) -> dict:
    """The choice made in the Studio, or {} (none made, or a record that does not parse)."""
    try:
        data = json.loads(_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _mode_of(state: dict) -> str:
    if state.get("off") is True:
        return "off"
    if state.get("hash") or os.getenv("UI_PASSWORD"):
        return "password"
    return "local"


def mode(root) -> str:
    """"off" (no password), "password" (one is needed), or "local" (none is set: this machine only)."""
    return _mode_of(saved(root))


def source(root) -> str | None:
    """Where the password in force comes from: "studio", "environment", or None."""
    s = saved(root)
    if s.get("off") is True:
        return None
    if s.get("hash"):
        return "studio"
    return "environment" if os.getenv("UI_PASSWORD") else None


def _epoch_of(state: dict, secret: str) -> str:
    if state.get("off") is True:
        return "off"
    if state.get("hash"):
        return "studio:" + str(state.get("epoch") or "")
    env = os.getenv("UI_PASSWORD") or ""
    if env:
        # the salt a reset leaves: the server's password after a reset is a new epoch too
        key = (str(state.get("salt") or "") + "\0" + env).encode("utf-8")
        return "env:" + hmac.new((secret or "").encode("utf-8"), key, hashlib.sha256).hexdigest()[:32]
    return "local"


def epoch(root, secret: str) -> str:
    """The credentials in force, as the token a signed-in session carries; `secret` keys the digest
    of the server's own password (the session secret)."""
    return _epoch_of(saved(root), secret)


def _hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def _matches(password: str, stored: str) -> bool:
    try:
        kind, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        return hmac.compare_digest(_hash(password, bytes.fromhex(salt)).split("$")[2], digest)
    except (ValueError, TypeError):
        return False


def _opens(state: dict, password: str) -> bool:
    if state.get("hash"):
        return _matches(password or "", str(state["hash"]))
    env = os.getenv("UI_PASSWORD") or ""
    return bool(env) and hmac.compare_digest((password or "").encode("utf-8"), env.encode("utf-8"))


def check(root, password: str) -> bool:
    """Whether `password` opens the Studio: the one set in the Studio, else UI_PASSWORD."""
    return _opens(saved(root), password)


@contextmanager
def _locked(root):
    # a volume nothing has written to yet: the first password is set before the Studio ever ran
    # (`cli.py access`, R9F0-07)
    Path(root).mkdir(parents=True, exist_ok=True)
    with open(Path(root) / LOCK_FILE, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def login(root, password: str, secret: str) -> str | None:
    """The epoch a session gets for `password`, or None: only while the password it checked is
    still the one in force (a change between the check and now refuses it)."""
    state = saved(root)
    if _mode_of(state) != "password" or not _opens(state, password):
        return None
    with _locked(root):
        now = saved(root)
        return _epoch_of(now, secret) if now == state else None


def _change(root, current: str | None, check_current: bool, new_state: dict, secret: str) -> str:
    state = saved(root)
    if check_current and _mode_of(state) == "password" and not _opens(state, current or ""):
        raise Refused("the current password is not right")
    with _locked(root):
        if saved(root) != state:
            raise Refused("the access changed meanwhile: look at the page again")
        from .store import atomic_write_text

        data = {**new_state, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        atomic_write_text(_path(root), json.dumps(data, indent=2), mode=0o600)
        return _epoch_of(data, secret)


def set_password(root, new: str, current: str | None = None, check_current: bool = True,
                 secret: str = "") -> str:
    """A new password, set in the Studio (the current one first, when one is needed; the CLI, run
    on the server itself, does not ask). Returns the new epoch."""
    if len(new or "") < MIN_LENGTH:
        raise Refused(f"the new password needs at least {MIN_LENGTH} characters")
    return _change(root, current, check_current, {"hash": _hash(new), "epoch": secrets.token_hex(16)}, secret)


def turn_off(root, current: str | None = None, check_current: bool = True, secret: str = "") -> str:
    """No password: anyone who reaches the Studio can use it (the current one first)."""
    return _change(root, current, check_current, {"off": True}, secret)


def reset_if_asked(root) -> bool:
    """UI_ACCESS_RESET at start: the choice saved in the Studio is replaced by a fresh salt, so
    UI_PASSWORD rules again (or, without it, this machine only) and no session from before the
    reset comes back (R058-01)."""
    if (os.getenv("UI_ACCESS_RESET") or "").strip().lower() not in ("1", "true", "yes"):
        return False
    if not _path(root).exists():
        return False
    from .store import atomic_write_text

    with _locked(root):
        atomic_write_text(_path(root), json.dumps({"salt": secrets.token_hex(16), "at": datetime.now(
            timezone.utc).isoformat(timespec="seconds")}, indent=2), mode=0o600)
    return True
