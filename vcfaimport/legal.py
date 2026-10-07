"""Terms of use: nobody runs vcfa-import without accepting DISCLAIMER.md first.

The terms ship with the tool (package data in the .pyz/.exe, the repository root in a
checkout). An acceptance is bound to the exact text: its SHA-256 is recorded, so any
change to the terms -- a new release, an edited file -- requires accepting again.

Two places record acceptances:
  CLI      one file per OS account (acceptance_path()); written by `vcfa-import accept`
           or the first-run prompt. Scheduled and service accounts accept once, explicitly.
  console  one row per console user in the workspace (the acceptances table), written
           when that person accepts in the browser; the server refuses every other API
           call for them until then (HTTP 451).

Everything fails closed: terms that cannot be found, a record that cannot be read or that
names different terms, mean "not accepted".
"""

from __future__ import annotations

import hashlib
import json
import os
import pkgutil
import platform
import re
import socket
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DISCLAIMER_FILE = "DISCLAIMER.md"
ACCEPT_PHRASE = "I ACCEPT"
ENV_FILE = "VCFA_ACCEPTANCE_FILE"          # where the CLI keeps this account's acceptance
EXIT_NOT_ACCEPTED = 8
LICENSE_NAME = "Apache License 2.0"
# What a person confirms, word for word, in the CLI prompt and the console checkboxes.
CONFIRMATIONS = (
    "I have read the full terms of use (DISCLAIMER.md) and understand them.",
    "I understand vcfa-import is experimental, unsupported, AI-assisted software that changes "
    "production infrastructure, some of it irreversibly, and I accept all of that risk.",
    "I am authorised to accept these terms for myself and for any organisation I use it for.",
)


class NotAccepted(Exception):
    """The terms have not been accepted (or cannot be shown)."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- the terms
def _bundled(name: str) -> Optional[str]:
    """A legal document shipped with the tool: package data in a build, the repo root in a checkout."""
    data = None
    try:
        data = pkgutil.get_data("vcfaimport.web", "docs/" + name)
    except (OSError, ValueError):
        data = None
    if data is None:
        src = Path(__file__).resolve().parents[1] / name
        if src.is_file():
            data = src.read_bytes()
    return data.decode("utf-8") if data is not None else None


def terms() -> Dict[str, Any]:
    """{text, sha256, version, summary}. Raises NotAccepted when the terms are not bundled:
    a build without them must not run, because nobody could read what they accept."""
    text = _bundled(DISCLAIMER_FILE)
    if not text or not text.strip():
        raise NotAccepted("this build of vcfa-import does not include its terms of use ({}); it cannot be "
                          "used. Use an official build.".format(DISCLAIMER_FILE))
    norm = text.replace("\r\n", "\n")
    m = re.search(r"Disclaimer version\s+([\w.\-]+)", norm)
    return {"text": norm, "sha256": hashlib.sha256(norm.encode("utf-8")).hexdigest(),
            "version": m.group(1).rstrip(".") if m else "unversioned", "summary": summary(norm)}


def summary(text: str) -> List[str]:
    """The 'short version' bullets, as plain sentences."""
    m = re.search(r"^## The short version\s*\n(.*?)(?:^---|^## )", text, re.M | re.S)
    if not m:
        return []
    out: List[str] = []
    for line in m.group(1).splitlines():
        line = line.rstrip()
        if line.startswith("- "):
            out.append(line[2:])
        elif line.startswith("  ") and out:
            out[-1] += " " + line.strip()
    return [re.sub(r"\*\*|`", "", s).strip() for s in out]


# ---------------------------------------------------------------- CLI: per OS account
def acceptance_path() -> Path:
    env = os.environ.get(ENV_FILE)
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "vcfa-import" / "acceptance.json"


def read_record() -> Optional[Dict[str, Any]]:
    try:
        rec = json.loads(acceptance_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def valid_record() -> Optional[Dict[str, Any]]:
    """This account's acceptance of the current terms, or None."""
    t = terms()
    rec = read_record()
    if rec and rec.get("sha256") == t["sha256"] and str(rec.get("name") or "").strip() \
            and rec.get("phrase") == ACCEPT_PHRASE:
        return rec
    return None


def require() -> Dict[str, Any]:
    rec = valid_record()
    if rec is None:
        raise NotAccepted(not_accepted_message())
    return rec


def record(name: str, phrase: str, via: str = "cli") -> Dict[str, Any]:
    """Accept the current terms for this OS account. Validates like the console does."""
    rec = check_and_build(name, phrase, confirmations=[True] * len(CONFIRMATIONS), via=via)
    from .access import os_user
    rec.update({"os_user": os_user(), "host": socket.gethostname(), "platform": platform.platform()})
    path = acceptance_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return rec


def revoke() -> bool:
    try:
        acceptance_path().unlink()
        return True
    except FileNotFoundError:
        return False


def check_and_build(name: str, phrase: str, confirmations: Any, via: str,
                    sha256: Optional[str] = None) -> Dict[str, Any]:
    """Validate an acceptance (from the CLI or a browser) and return the record to keep."""
    t = terms()
    name = (name or "").strip()
    if not (2 <= len(name) <= 120):
        raise ValueError("enter your full name (2 to 120 characters)")
    if (phrase or "").strip() != ACCEPT_PHRASE:
        raise ValueError('type exactly "{}" to accept'.format(ACCEPT_PHRASE))
    if not isinstance(confirmations, list) or len(confirmations) != len(CONFIRMATIONS) \
            or not all(c is True for c in confirmations):
        raise ValueError("confirm every statement to accept")
    if sha256 is not None and sha256 != t["sha256"]:
        raise ValueError("the terms changed while you were reading them; read the new version and accept again")
    from . import __version__
    return {"name": name, "phrase": ACCEPT_PHRASE, "accepted_at": _now(), "version": t["version"],
            "sha256": t["sha256"], "via": via, "tool_version": __version__,
            "confirmations": list(CONFIRMATIONS)}


def not_accepted_message() -> str:
    return ("vcfa-import will not run until you accept its terms of use.\n"
            "  Read them:    vcfa-import disclaimer\n"
            "  Accept them:  vcfa-import accept\n"
            "For a scheduled task or service account, accept once as that account, naming the person "
            "responsible:\n"
            '  vcfa-import accept --name "Full Name" --i-accept')


# ------------------------------------------------------------------- CLI: the prompt
def banner(t: Dict[str, Any]) -> str:
    width = 78
    lines = ["=" * width, "  vcfa-import -- TERMS OF USE  (version {})".format(t["version"]).ljust(width),
             "=" * width, "",
             "Before you use vcfa-import you must read and accept its terms of use. In short:", ""]
    for s in t["summary"]:
        lines.extend(textwrap.wrap(s, width - 4, initial_indent="  * ", subsequent_indent="    "))
    lines += ["", "The full terms are in DISCLAIMER.md (or type V below to read them here).",
              "Licence: {} (LICENSE). Third-party components: THIRD-PARTY-NOTICES.md.".format(LICENSE_NAME), ""]
    return "\n".join(lines)


def prompt(inp=input, out=print) -> Optional[Dict[str, Any]]:
    """The interactive acceptance. Returns the record, or None when the person declines."""
    t = terms()
    out(banner(t))
    while True:
        try:
            choice = inp("[V] read the full terms   [A] accept   [Q] quit without accepting: ").strip().lower()
        except EOFError:
            return None
        if choice in ("v", "view", "read"):
            out("\n" + t["text"] + "\n")
            continue
        if choice in ("q", "quit", "n", "no", ""):
            return None
        if choice not in ("a", "accept"):
            continue
        out("\nBy accepting, you confirm that:")
        for i, c in enumerate(CONFIRMATIONS, 1):
            out("\n".join(textwrap.wrap(c, 74, initial_indent="  {}. ".format(i), subsequent_indent="     ")))
        try:
            name = inp("\nYour full name: ").strip()
            phrase = inp('Type "{}" to accept the terms (anything else cancels): '.format(ACCEPT_PHRASE))
        except EOFError:
            return None
        if phrase.strip() != ACCEPT_PHRASE:
            return None
        try:
            return record(name, phrase)
        except ValueError as exc:
            out("Not accepted: {}".format(exc))
            return None
