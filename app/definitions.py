"""Private GitHub HTTPS loading with atomic, commit-pinned definition refreshes.

App lifespan: construct DefinitionsStore(runtime), start() before readiness,
use snapshot()/health(), and stop() on shutdown. The App secret resource must
bind GITHUB_DEFINITIONS_TOKEN (or the explicit token_env variable). Importing
performs no I/O. The bound token is read only when preparing an API request.

refresh() is synchronous and serialized, returning success/failure. Readers use
only a short state lock; every candidate is downloaded and validated off-lock.
An unsuccessful refresh preserves the complete last valid snapshot.
"""

import base64
from dataclasses import dataclass
import math
import os
import re
from threading import Event, Lock, Thread
from typing import Mapping, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.markdown_definitions import (
    MAX_MARKDOWN_BYTES, DefinitionsSnapshot, build_snapshot, parse_definition, repository_path,
)
from app.runtime_config import RuntimeConfig, strict_json
from setup.models import DefinitionsConfig


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 2000
MAX_DEPTH = 16
REFRESH_ERROR = "GitHub definitions refresh failed"


class DefinitionsError(RuntimeError):
    """Public, redacted definitions lifecycle failure."""


@dataclass(frozen=True)
class GitHubResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class Transport(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> GitHubResponse: ...


def _validate_api_url(url: str) -> None:
    if (
        not isinstance(url, str) or url != url.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in url)
    ):
        raise ValueError("Invalid GitHub API URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or parsed.netloc != "api.github.com"
        or parsed.fragment or not parsed.path.startswith("/")
    ):
        raise ValueError("Invalid GitHub API URL")
    repository_path(unquote(parsed.path[1:]))


class NoRedirect(HTTPRedirectHandler):
    """Never forward an authenticated request, including to another GitHub host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubTransport:
    """Bounded HTTPS GETs. No HTTP debug logging or server-supplied URL following."""

    def __init__(self):
        self._opener = build_opener(NoRedirect())

    def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> GitHubResponse:
        _validate_api_url(url)
        response = None
        try:
            try:
                response = self._opener.open(Request(url, headers=dict(headers)), timeout=timeout)
            except HTTPError as error:
                response = error
            with response:
                # Do not read error bodies: they are untrusted diagnostics.
                body = response.read(MAX_RESPONSE_BYTES + 1) if response.code == 200 else b""
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError
                return GitHubResponse(response.code, body, dict(response.headers))
        except Exception:
            pass
        raise DefinitionsError("GitHub HTTPS request failed")


class DefinitionsStore:
    def __init__(
        self, runtime: RuntimeConfig, *, token_env: str = "GITHUB_DEFINITIONS_TOKEN",
        transport: Transport | None = None, timeout_seconds: float = 10,
        max_attempts: int = 3, retry_delay_seconds: float = 0.25,
    ):
        # Revalidate even if a caller constructed an unchecked Pydantic instance.
        settings = DefinitionsConfig.model_validate(runtime.definitions.model_dump())
        parts = urlsplit(settings.repository_url).path.strip("/").split("/")
        owner, repository = parts[0], parts[1].removesuffix(".git")
        repository_path(owner + "/" + repository)
        if settings.markdown_path != ".":
            repository_path(settings.markdown_path)
        # A branch is a Git ref, not an arbitrary URL path or query.
        branch = settings.branch
        repository_path(branch)
        if (
            any(character.isspace() or character in "~^*[@" for character in branch)
            or ".." in branch or branch.endswith(".")
        ):
            raise ValueError("Invalid definitions branch")
        if (
            not isinstance(token_env, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env) is None
            or isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 5
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30
            or not math.isfinite(retry_delay_seconds) or not 0 <= retry_delay_seconds <= 5
        ):
            raise ValueError("Invalid definitions transport settings")
        self._runtime = runtime
        self._settings = settings
        self._api_base = "https://api.github.com/repos/" + owner + "/" + repository
        self._token_env = token_env
        self._transport = transport if transport is not None else GitHubTransport()
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay_seconds
        self._state_lock = Lock()
        self._refresh_lock = Lock()
        self._lifecycle_lock = Lock()
        self._stop = Event()
        self._worker: Thread | None = None
        self._snapshot: DefinitionsSnapshot | None = None
        self._failed = False

    def start(self) -> None:
        """Require a valid initial load, then start one polling worker."""
        with self._lifecycle_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self.refresh()
            with self._state_lock:
                if self._snapshot is None:
                    raise DefinitionsError("Initial GitHub definitions load failed")
            self._worker = Thread(target=self._poll, name="github-definitions", daemon=True)
            self._worker.start()

    def stop(self) -> None:
        """Interrupt polling/retry waits and join the owned worker before returning."""
        with self._lifecycle_lock:
            self._stop.set()
            if self._worker is not None:
                self._worker.join()
                self._worker = None

    def _poll(self) -> None:
        while not self._stop.wait(self._settings.poll_seconds):
            self.refresh()

    def snapshot(self) -> DefinitionsSnapshot:
        with self._state_lock:
            snapshot = self._snapshot
        if snapshot is None:
            raise DefinitionsError("GitHub definitions are not ready")
        return snapshot

    def health(self) -> dict:
        with self._state_lock:
            snapshot, failed = self._snapshot, self._failed
        return {
            "status": "not_ready" if snapshot is None else "degraded" if failed else "ready",
            "ready": snapshot is not None,
            "revision": snapshot.revision if snapshot else None,
            "definition_count": len(snapshot.definitions) if snapshot else 0,
            "error": REFRESH_ERROR if failed else None,
        }

    def refresh(self) -> bool:
        """Build a complete candidate off the reader lock, then publish once."""
        with self._refresh_lock:
            try:
                branch = self._json("/commits/" + quote(self._settings.branch, safe=""))
                revision = branch["sha"]
                if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
                    raise ValueError
                with self._state_lock:
                    current = self._snapshot
                if current is not None and current.revision == revision:
                    candidate = current
                else:
                    candidate = build_snapshot(self._load_definitions(revision), self._runtime, revision)
                with self._state_lock:
                    self._snapshot, self._failed = candidate, False
                return True
            except Exception:
                # Do not log exceptions, request objects, response bodies, headers,
                # or parser diagnostics. No failed revision is recorded as loaded.
                with self._state_lock:
                    self._failed = True
                return False

    def _json(self, endpoint: str):
        url = self._api_base + endpoint
        _validate_api_url(url)
        # Read the bound environment only after URL validation; never store a
        # token in an instance repr, exception, health response, or log message.
        token = os.environ.get(self._token_env, "")
        if not token.strip() or any(ord(character) < 32 or ord(character) == 127 for character in token):
            raise DefinitionsError("GitHub definitions credential is unavailable")
        headers = {
            "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "genie-routing-reference",
        }
        for attempt in range(self._max_attempts):
            if self._stop.is_set():
                break
            response = None
            try:
                response = self._transport.get(url, headers=headers, timeout=self._timeout)
            except Exception:
                pass
            if response is not None:
                if response.status == 200:
                    if len(response.body) > MAX_RESPONSE_BYTES:
                        break
                    # Decode/parse failures are handled by refresh, with no raw
                    # text escaping to the lifecycle caller or health endpoint.
                    return strict_json(response.body.decode("utf-8"))
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                rate_limited = response.status == 403 and (
                    response_headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response_headers
                )
                if response.status != 429 and not 500 <= response.status <= 599 and not rate_limited:
                    break
            if attempt + 1 < self._max_attempts:
                # Server-provided delays cannot cause unbounded startup/shutdown.
                if self._stop.wait(self._retry_delay * (2 ** attempt)):
                    break
        raise DefinitionsError(REFRESH_ERROR)

    def _contents(self, path: str, revision: str):
        suffix = "/" + quote(path, safe="/") if path else ""
        return self._json("/contents" + suffix + "?ref=" + revision)

    def _load_definitions(self, revision: str):
        root = "" if self._settings.markdown_path == "." else self._settings.markdown_path
        pending, seen, definitions = [(root, 0)], set(), []
        entry_count, total_bytes = 0, 0
        while pending:
            folder, depth = pending.pop()
            if depth > MAX_DEPTH:
                raise ValueError("Definitions directory depth exceeded")
            listing = self._contents(folder, revision)
            # Contents API is capped at 1000 entries and cannot be paginated.
            # Fail closed rather than publishing an incomplete generation.
            if not isinstance(listing, list) or len(listing) >= 1000:
                raise ValueError("Invalid or truncated GitHub directory listing")
            entry_count += len(listing)
            if entry_count > MAX_ENTRIES:
                raise ValueError("Definitions entry limit exceeded")
            validated = []
            for entry in listing:
                path = repository_path(entry["path"])
                parent, _, name = path.rpartition("/")
                if parent != folder or name != entry["name"] or path in seen:
                    raise ValueError("Invalid GitHub contents path")
                if entry["type"] not in {"file", "dir"} or entry.get("submodule_git_url") or entry.get("target"):
                    raise ValueError("Definition links and submodules are not supported")
                seen.add(path)
                validated.append((path, entry["type"]))
            for path, kind in sorted(validated):
                if kind == "dir":
                    pending.append((path, depth + 1))
                elif path.lower().endswith(".md"):
                    document = self._contents(path, revision)
                    size = document["size"]
                    if (
                        document["type"] != "file" or document["path"] != path
                        or document.get("submodule_git_url") or document.get("target")
                        or document["encoding"] != "base64" or type(size) is not int
                        or not 0 <= size <= MAX_MARKDOWN_BYTES
                    ):
                        raise ValueError("Invalid GitHub Markdown content")
                    raw = base64.b64decode("".join(document["content"].split()), validate=True)
                    total_bytes += len(raw)
                    if len(raw) != size or total_bytes > MAX_TOTAL_BYTES:
                        raise ValueError("Definitions content limit exceeded")
                    definitions.append(parse_definition(raw.decode("utf-8"), path))
        return definitions
