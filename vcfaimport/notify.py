"""Notifications: tell people when something needs them.

Channels are `[[notify]]` tables in the config (or edited on the console's
Settings page):

    name = "ops"              # a label for logs
    type = "teams"            # teams | slack | webhook | email
    url  = "https://..."      # teams / slack / webhook
    events = ["job_failed", "circuit_breaker"]     # omit for all events

    # email: smtp_host, smtp_port (587), starttls (true), from, to (list or
    # comma string), username, password_env (NAME of an environment variable;
    # the password itself is never written to the config)

Delivery runs in a background thread with retries and never blocks or fails a
run. Each delivery's outcome is reported through the `record` callback (the
console and CLI turn it into an event), so a dead webhook shows up in the log.
"""

from __future__ import annotations

import json
import os
import smtplib
import threading
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Any, Callable, Dict, List, Optional

EVENTS = {
    "job_succeeded": "a console job finished cleanly",
    "job_warning": "a job finished with failures or warnings",
    "job_failed": "a job crashed or could not run",
    "circuit_breaker": "a run halted because too many operations failed",
    "awaiting_commit": "imports are waiting for someone to commit them",
    "approval_requested": "an action is waiting for a second person",
    "approval_decided": "an approval was granted or rejected",
    "schedule_started": "a change window opened and its run started",
    "schedule_finished": "a scheduled run finished (or the window closed)",
    "schedule_missed": "a change window passed without its run starting",
    "verify_failed": "post-import verification found a problem",
    "test": "a test message from the Settings page",
}
SEVERITY = {"job_failed": "bad", "circuit_breaker": "bad", "verify_failed": "bad", "schedule_missed": "bad",
            "job_warning": "warn", "awaiting_commit": "warn", "approval_requested": "warn"}
COLOR = {"bad": "D42A2A", "warn": "E5A000", "info": "2F6BFF"}
TYPES = ("teams", "slack", "webhook", "email")
Record = Callable[[str, str], None]


def validate(channels: Any) -> List[Dict[str, Any]]:
    """Clean, checked channel list; raises ValueError on a bad one."""
    if not isinstance(channels, list):
        raise ValueError("notification channels must be a list")
    out = []
    for i, ch in enumerate(channels):
        if not isinstance(ch, dict):
            raise ValueError("channel {} is not an object".format(i + 1))
        kind = str(ch.get("type") or "").lower()
        name = str(ch.get("name") or kind or "channel {}".format(i + 1))
        if kind not in TYPES:
            raise ValueError("{}: type must be one of {}".format(name, ", ".join(TYPES)))
        events = ch.get("events") or []
        if isinstance(events, str):
            events = [e.strip() for e in events.split(",") if e.strip()]
        unknown = sorted(set(events) - set(EVENTS))
        if unknown:
            raise ValueError("{}: unknown event(s) {}".format(name, ", ".join(unknown)))
        clean = dict(ch, name=name, type=kind, events=events, enabled=ch.get("enabled", True) is not False)
        if kind == "email":
            to = clean.get("to") or []
            if isinstance(to, str):
                to = [t.strip() for t in to.split(",") if t.strip()]
            if not clean.get("smtp_host") or not to or not clean.get("from"):
                raise ValueError("{}: email needs smtp_host, from and to".format(name))
            if clean.get("password"):
                raise ValueError("{}: put the SMTP password in an environment variable and set "
                                 "password_env to its name; it is never stored".format(name))
            clean["to"] = to
        elif not str(clean.get("url") or "").startswith(("http://", "https://")):
            raise ValueError("{}: url must start with http:// or https://".format(name))
        out.append(clean)
    return out


def payload(kind: str, event: str, title: str, text: str, fields: Dict[str, Any],
            context: Dict[str, Any]) -> Dict[str, Any]:
    sev = SEVERITY.get(event, "info")
    facts = [{"name": str(k), "value": str(v)} for k, v in fields.items() if v not in (None, "")]
    if kind == "teams":
        # Office 365 connector card: accepted by Teams incoming webhooks.
        return {"@type": "MessageCard", "@context": "http://schema.org/extensions", "summary": title,
                "themeColor": COLOR[sev], "title": title, "text": text,
                "sections": [{"facts": facts}] if facts else []}
    if kind == "slack":
        lines = ["*{}*".format(title), text] + ["{}: {}".format(f["name"], f["value"]) for f in facts]
        return {"text": "\n".join(x for x in lines if x)}
    return {"event": event, "severity": sev, "title": title, "text": text, "fields": fields,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **context}


def _post(url: str, body: Dict[str, Any], timeout: int = 10) -> None:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status >= 300:
            raise OSError("HTTP {}".format(resp.status))


def _email(ch: Dict[str, Any], title: str, text: str, fields: Dict[str, Any]) -> None:
    msg = EmailMessage()
    msg["Subject"] = "[vcfa-import] " + title
    msg["From"] = ch["from"]
    msg["To"] = ", ".join(ch["to"])
    body = [text, ""] + ["{}: {}".format(k, v) for k, v in fields.items() if v not in (None, "")]
    msg.set_content("\n".join(body))
    port = int(ch.get("smtp_port") or 587)
    with smtplib.SMTP(ch["smtp_host"], port, timeout=15) as smtp:
        if ch.get("starttls", port == 587):
            smtp.starttls()
        if ch.get("username"):
            smtp.login(ch["username"], os.environ.get(str(ch.get("password_env") or ""), ""))
        smtp.send_message(msg)


def deliver(ch: Dict[str, Any], event: str, title: str, text: str, fields: Dict[str, Any],
            context: Dict[str, Any], attempts: int = 3, pause: float = 2.0) -> None:
    last: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            if ch["type"] == "email":
                _email(ch, title, text, fields)
            else:
                _post(ch["url"], payload(ch["type"], event, title, text, fields, context))
            return
        except (OSError, urllib.error.URLError, smtplib.SMTPException, ValueError) as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(pause * (attempt + 1))
    raise OSError(str(last))


def send(channels: List[Dict[str, Any]], event: str, title: str, text: str = "",
         fields: Optional[Dict[str, Any]] = None, context: Optional[Dict[str, Any]] = None,
         record: Optional[Record] = None, wait: bool = False) -> List[threading.Thread]:
    """Fan an event out to every enabled channel that wants it."""
    threads = []
    for ch in channels or []:
        if not ch.get("enabled", True) or (ch.get("events") and event not in ch["events"] and event != "test"):
            continue

        def run(ch=ch):
            try:
                deliver(ch, event, title, text, fields or {}, context or {})
                if record:
                    record("info", "notified {} ({}): {}".format(ch["name"], ch["type"], title))
            except Exception as exc:  # noqa: BLE001 -- a notification must never break a run
                if record:
                    record("warn", "notification to {} failed: {}".format(ch["name"], str(exc)[:200]))
        t = threading.Thread(target=run, name="notify-" + ch["name"], daemon=True)
        t.start()
        threads.append(t)
    if wait:
        for t in threads:
            t.join(60)
    return threads
