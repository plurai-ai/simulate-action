"""A small in-process stand-in for the GitHub REST API: routes by method and path regex, and a log of every request."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

Reply = tuple[int, Any, dict[str, str]]
#: Status values that make the server misbehave at the transport level instead of answering.
DROP = 'drop'
STALL = 'stall'
Handler = Callable[['Request', re.Match[str]], Reply | tuple[Any, ...]]


def drop(_request: Request, _match: re.Match[str]) -> tuple[str, None]:
    """A handler that closes the connection without a reply (the client sees ``RemoteDisconnected``)."""
    return DROP, None


def stall(seconds: float) -> Handler:
    """A handler that sleeps ``seconds`` before it replies, so a client with a shorter timeout gives up."""

    def handler(_request: Request, _match: re.Match[str]) -> tuple[str, float]:
        return STALL, seconds

    return handler


def garbage(_request: Request, _match: re.Match[str]) -> tuple[int, bytes]:
    """A 200 whose body is not JSON (a bad escape inside a string)."""
    return 200, b'{"message": "\\x"}'


@dataclass
class Request:
    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: Any


@dataclass
class FakeGitHub:
    routes: list[tuple[str, re.Pattern[str], Handler]] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    url: str = ''
    _server: ThreadingHTTPServer | None = None

    def route(self, method: str, pattern: str, handler: Handler | Any, status: int = 200) -> None:
        """Register a handler, or a constant JSON body when ``handler`` is not callable."""
        if not callable(handler):
            body = handler

            def handler(_request: Request, _match: re.Match[str], _body: Any = body, _status: int = status) -> tuple[int, Any]:
                return _status, _body

        self.routes.append((method, re.compile(pattern), handler))

    def calls(self, method: str, pattern: str) -> list[Request]:
        compiled = re.compile(pattern)
        return [r for r in self.requests if r.method == method and compiled.fullmatch(r.path)]

    def start(self) -> None:
        fake = self

        class _Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(length) if length else b''
                body = json.loads(raw) if raw else None
                request = Request(self.command, parts.path, parts.query, {k.lower(): v for k, v in self.headers.items()}, body)
                fake.requests.append(request)
                for method, pattern, handler in fake.routes:
                    match = pattern.fullmatch(parts.path)
                    if method == self.command and match:
                        reply = handler(request, match)
                        status, payload = reply[0], reply[1]
                        headers = reply[2] if len(reply) > 2 else {}
                        break
                else:
                    status, payload, headers = 404, {'message': 'Not Found'}, {}
                if status == DROP:
                    self.close_connection = True
                    return
                if status == STALL:
                    time.sleep(payload if isinstance(payload, int | float) else 0)
                    self.close_connection = True
                    return
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = do_DELETE = _serve

            def log_message(self, *_args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.url = f'http://127.0.0.1:{self._server.server_address[1]}'
        threading.Thread(target=lambda: self._server.serve_forever(poll_interval=0.02), daemon=True).start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
