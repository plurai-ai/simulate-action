"""A minimal GitHub REST client on the standard library, for the steps that hold the job's token."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

DEFAULT_API_URL = 'https://api.github.com'
API_VERSION = '2022-11-28'
TIMEOUT_SECONDS = 30.0
#: The environment variable that shortens the per-request timeout (the tests' stalled server).
TIMEOUT_VAR = 'SA_API_TIMEOUT_SECONDS'
#: A downloaded artifact larger than this is not read.
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


class ApiError(Exception):
    """A GitHub API call that did not give a usable answer: an error status, or status ``0`` for a failure below HTTP
    (connection, timeout, TLS, a body that is not JSON). Callers handle this one type for every kind of failure."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f'GitHub API {status}: {message}')
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface a redirect as an error, so the caller decides whether the credential may follow it."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class GitHubApi:
    def __init__(self, token: str, base_url: str = DEFAULT_API_URL, timeout: float = TIMEOUT_SECONDS) -> None:
        self._token = token
        self._base = base_url.rstrip('/')
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> GitHubApi:
        return cls(env.get('GITHUB_TOKEN', ''), env.get('GITHUB_API_URL') or DEFAULT_API_URL, _timeout(env))

    def _request(self, method: str, path: str, body: Any = None) -> urllib.request.Request:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f'{self._base}{path}', data=data, method=method)  # noqa: S310 - https API
        request.add_header('Accept', 'application/vnd.github+json')
        request.add_header('X-GitHub-Api-Version', API_VERSION)
        request.add_header('User-Agent', 'plurai-simulate-action')
        if self._token:
            request.add_header('Authorization', f'Bearer {self._token}')
        if data is not None:
            request.add_header('Content-Type', 'application/json')
        return request

    def call(self, method: str, path: str, body: Any = None) -> Any:
        try:
            with self._opener.open(self._request(method, path, body), timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise ApiError(error.code, _message(error)) from error
        except urllib.error.URLError as error:
            raise ApiError(0, str(error.reason)) from error
        except (OSError, http.client.HTTPException) as error:
            raise ApiError(0, _describe(error)) from error
        try:
            return json.loads(raw) if raw else None
        except ValueError as error:
            raise ApiError(0, f'the reply is not JSON ({error})') from error

    def get(self, path: str) -> Any:
        return self.call('GET', path)

    def pages(self, path: str, key: str, limit: int = 10) -> list[Any]:
        """Every item under ``key`` across up to ``limit`` pages of 100."""
        separator = '&' if '?' in path else '?'
        items: list[Any] = []
        for page in range(1, limit + 1):
            chunk = self.get(f'{path}{separator}per_page=100&page={page}')
            rows = chunk[key] if isinstance(chunk, dict) else chunk
            items += rows
            if len(rows) < 100:
                break
        return items

    def download(self, path: str) -> bytes:
        """The bytes behind an API download route. The redirect to blob storage is followed without the credential."""
        location = ''
        try:
            with self._opener.open(self._request('GET', path), timeout=self._timeout) as response:
                return response.read(MAX_DOWNLOAD_BYTES + 1)
        except urllib.error.HTTPError as error:
            location = error.headers.get('Location') if error.code in (301, 302, 303, 307, 308) else ''
            if not location:
                raise ApiError(error.code, _message(error)) from error
        except urllib.error.URLError as error:
            raise ApiError(0, str(error.reason)) from error
        except (OSError, http.client.HTTPException) as error:
            raise ApiError(0, _describe(error)) from error
        request = urllib.request.Request(location, headers={'User-Agent': 'plurai-simulate-action'})  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                return response.read(MAX_DOWNLOAD_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise ApiError(error.code, _message(error)) from error
        except (OSError, http.client.HTTPException) as error:
            raise ApiError(0, _describe(error)) from error


def _timeout(env: Mapping[str, str]) -> float:
    try:
        value = float(env.get(TIMEOUT_VAR, ''))
    except ValueError:
        return TIMEOUT_SECONDS
    return value if 0 < value <= TIMEOUT_SECONDS else TIMEOUT_SECONDS


def _describe(error: BaseException) -> str:
    """One line for a failure below HTTP: the exception's kind, which says more than its text (``timed out``)."""
    reason = getattr(error, 'reason', None) or error
    return f'{type(error).__name__}: {reason}'.rstrip(': ')


def _message(error: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(error.read()).get('message', error.reason))
    except (ValueError, AttributeError):
        return str(error.reason)
