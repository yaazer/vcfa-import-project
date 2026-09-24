"""HTTP front end for the web console: static assets, token check, JSON routing.

Standard library only (http.server), so the console ships inside the same
.pyz as the CLI and runs on a jump box with nothing installed.

Security model: the console can import VMs, so every /api call needs the
access token printed at start-up, sent in the X-VCFA-Token header. A custom
header also means a hostile page in the same browser cannot forge requests
(it would need a CORS preflight, which is never granted). When bound to
loopback, requests naming any other Host are refused (DNS rebinding).
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import pkgutil
import re
import secrets
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlsplit

from ..config import ConfigError
from .api import BUSY_ERRORS, ROUTES, USER_ERRORS, ApiError, WebApp
from .jobs import JobBusy

MAX_BODY = 8 * 1024 * 1024
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'none'")


def _static(name: str) -> Optional[bytes]:
    if not re.fullmatch(r"[\w.-]+", name) or name.startswith("."):
        return None
    try:
        # pkgutil works from a directory, a .pyz zipapp and a PyInstaller bundle.
        return pkgutil.get_data("vcfaimport.web", "static/" + name)
    except (OSError, FileNotFoundError):
        return None


def _is_loopback(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "vcfa-import"
    protocol_version = "HTTP/1.1"
    app: WebApp
    token: str
    loopback_only: bool
    port: int

    # quiet: the console's own log is the job log, not an access log
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        pass

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    # ------------------------------------------------------------ helpers
    def _send(self, status: int, data: bytes, content_type: str,
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _host_ok(self) -> bool:
        if not self.loopback_only:
            return True
        host = (self.headers.get("Host") or "").strip()
        if host.startswith("["):
            name = host[1:].split("]", 1)[0]
        else:
            name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return _is_loopback(name)

    def _token_ok(self, query: Dict[str, str], allow_query: bool) -> bool:
        given = self.headers.get("X-VCFA-Token") or ""
        if not given and allow_query:
            given = query.get("t", "")
        return bool(given) and hmac.compare_digest(given.encode(), self.token.encode())

    def _drain(self) -> Optional[str]:
        """Read the request body now, whatever the route and whether or not it is
        wanted. A body left unread on a keep-alive connection would be parsed as
        the next request. Returns an error message when the body is unusable."""
        self._raw = b""
        raw_len = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self.close_connection = True
            return "chunked request bodies are not supported"
        if not raw_len:
            return None
        try:
            length = int(raw_len)
        except ValueError:
            self.close_connection = True
            return "bad Content-Length"
        if length < 0:
            self.close_connection = True
            return "bad Content-Length"
        if length > MAX_BODY:
            # Too big to drain; the connection cannot be reused.
            self.close_connection = True
            return "request body too large"
        self._raw = self.rfile.read(length)
        return None

    def _body(self) -> Dict[str, Any]:
        raw = self._raw
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "request body is not valid JSON: {}".format(exc))
        if not isinstance(data, dict):
            raise ApiError(400, "request body must be a JSON object")
        return data

    # ----------------------------------------------------------- dispatch
    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        problem = self._drain()
        if problem:
            self._error(413 if "too large" in problem else 400, problem)
            return
        if not self._host_ok():
            self._error(403, "unexpected Host header")
            return

        if method == "GET" and path in ("/", "/index.html"):
            self._send(200, _static("index.html") or b"missing index.html",
                       STATIC_TYPES[".html"])
            return
        if method == "GET" and path.startswith("/static/"):
            name = path[len("/static/"):]
            data = _static(name)
            if data is None:
                self._error(404, "not found")
                return
            ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
            self._send(200, data, STATIC_TYPES.get(ext, "application/octet-stream"))
            return
        if not path.startswith("/api/"):
            self._error(404, "not found")
            return

        for route_method, pattern, handler_name in ROUTES:
            match = pattern.match(path)
            if not match or route_method != method:
                continue
            is_export = handler_name == "export"
            if not self._token_ok(query, allow_query=is_export):
                self._error(401, "missing or wrong access token")
                return
            try:
                body = self._body() if method in ("POST", "PUT") else {}
                result = getattr(self.app, handler_name)(query, body, **match.groupdict())
            except ApiError as exc:
                self._error(exc.status, str(exc))
                return
            except (JobBusy,) + BUSY_ERRORS as exc:
                self._error(409, str(exc))
                return
            except (ConfigError,) + USER_ERRORS as exc:
                self._error(400, str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc(file=sys.stderr)
                self._error(500, "{}: {}".format(type(exc).__name__, exc))
                return
            if is_export:
                data, kind, name = result
                self._send(200, data, kind + "; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="{}"'.format(name)})
            else:
                self._json(200, result)
            return
        self._error(405 if any(p.match(path) for _, p, _ in ROUTES) else 404,
                    "no route for {} {}".format(method, path))


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def make_server(app: WebApp, host: str = "127.0.0.1", port: int = 8765,
                token: Optional[str] = None) -> ConsoleServer:
    handler = type("BoundHandler", (Handler,), {
        "app": app,
        "token": token or secrets.token_urlsafe(24),
        "loopback_only": _is_loopback(host),
    })
    server = ConsoleServer((host, port), handler)
    server.token = handler.token  # type: ignore[attr-defined]
    return server


def console_url(server: ConsoleServer, host: str) -> str:
    port = server.server_address[1]
    shown = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    if ":" in shown:
        shown = "[{}]".format(shown)
    # The token rides in the fragment: it never reaches a server log or a Referer.
    return "http://{}:{}/#t={}".format(shown, port, server.token)  # type: ignore[attr-defined]


def serve(app: WebApp, host: str, port: int, token: Optional[str] = None,
          open_browser: bool = False, log: Optional[Callable[[str], None]] = None) -> int:
    log = log or (lambda msg: print(msg, flush=True))
    server = make_server(app, host, port, token)
    url = console_url(server, host)
    log("vcfa-import web console")
    log("  workdir : {}".format(app.cfg.workdir))
    log("  context : {}".format(app.cfg.context or "(kubectl current-context)"))
    log("")
    log("  open    : {}".format(url))
    log("")
    if not _is_loopback(host):
        log("  warning: listening on {} -- anyone who can reach this port and has the".format(host))
        log("  token can drive imports. Prefer 127.0.0.1 plus an SSH tunnel.")
    log("  Ctrl-C to stop. Batches already on the cluster keep running; the next")
    log("  run (or Watch) picks them up again.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("\nstopping the console")
        active = app.jobs.active
        if active is not None:
            log("  '{}' was running; its in-flight batches continue on the cluster".format(
                active.title))
    finally:
        server.server_close()
        app.close()
    return 0
