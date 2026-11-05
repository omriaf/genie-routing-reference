"""Exercise the real loader/cache with an in-memory GitHub HTTPS boundary."""

import base64
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
from threading import Event
import traceback
from urllib.parse import parse_qs, unquote, urlsplit

import pytest


TEXT = (Path(__file__).parent / "fixtures/definitions/monthly_revenue.md").read_text()
TOKEN = "synthetic-bound-token-DO-NOT-LOG"
SHA1, SHA2 = "a" * 40, "b" * 40
API = "https://api.github.com/repos/acme/business-definitions"


class FakeGitHub:
    """Pin directory and file responses to the requested commit, as GitHub does."""

    def __init__(self):
        self.sha = SHA1
        self.files = {SHA1: {"definitions/monthly_revenue.md": TEXT}}
        self.calls = []
        self.failures = []
        self.override_listing = None
        self.before_get = None
        self.expected_auth = True

    def get(self, url, *, headers, timeout):
        from app.definitions import GitHubResponse

        assert url.startswith(API + "/") or url.startswith(API + "?")
        if self.expected_auth:
            assert headers["Authorization"] == "Bearer " + TOKEN
        else:
            assert "Authorization" not in headers
        assert 0 < timeout <= 30
        self.calls.append(url)
        if self.before_get:
            self.before_get(url)
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return GitHubResponse(failure, b'{"message":"private diagnostics"}', {})
        parsed = urlsplit(url)
        if "/commits/" in parsed.path:
            return GitHubResponse(200, json.dumps({"sha": self.sha}).encode(), {})
        assert "/contents" in parsed.path
        assert parse_qs(parsed.query) == {"ref": [self.sha]}
        path = unquote(parsed.path.split("/contents", 1)[1].lstrip("/"))
        files = self.files[self.sha]
        if path in files:
            content = files[path].encode()
            body = {
                "type": "file", "path": path, "name": path.rsplit("/", 1)[-1],
                "sha": "c" * 40, "size": len(content), "encoding": "base64",
                "content": base64.b64encode(content).decode(),
                "download_url": "https://raw.githubusercontent.com/ignored",
            }
        else:
            entries = {}
            for file_path in files:
                relative = file_path.removeprefix(path + "/") if path else file_path
                if relative == file_path and path:
                    continue
                name = relative.split("/")[0]
                entry_path = (path + "/" if path else "") + name
                entries[name] = {
                    "type": "dir" if "/" in relative else "file", "name": name,
                    "path": entry_path, "sha": "c" * 40, "size": 0,
                    "url": "https://untrusted.example/ignored", "download_url": None,
                }
            body = list(reversed(list(entries.values())))
            if self.override_listing is not None:
                body = self.override_listing
        return GitHubResponse(200, json.dumps(body).encode(), {})


@pytest.fixture
def store_setup(modules, runtime_document, install_runtime, monkeypatch):
    monkeypatch.setenv("GITHUB_DEFINITIONS_TOKEN", TOKEN)
    runtime = install_runtime(runtime_document)
    transport = FakeGitHub()

    def create(**options):
        return modules.definitions.DefinitionsStore(runtime, transport=transport, retry_delay_seconds=0, **options)

    return runtime, transport, create


def test_initial_load_uses_bound_token_and_commit_pinned_api_only(store_setup):
    _, github, create = store_setup
    store = create()
    assert store.health()["ready"] is False
    with pytest.raises(RuntimeError):
        store.snapshot()
    try:
        store.start()
        assert store.health()["status"] == "ready"
        assert store.snapshot().revision == SHA1
        assert store.snapshot().by_name["monthly_revenue"].domain == "sales"
        assert github.calls == [
            API + "/commits/main", API + "/contents/definitions?ref=" + SHA1,
            API + "/contents/definitions/monthly_revenue.md?ref=" + SHA1,
        ]
        store.start()  # Idempotent: no second worker or load.
        assert len(github.calls) == 3
    finally:
        store.stop()


def test_public_repository_uses_anonymous_github_requests(modules, runtime_document, install_runtime, monkeypatch):
    monkeypatch.delenv("GITHUB_DEFINITIONS_TOKEN", raising=False)
    runtime_document["definitions"]["access"] = "public"
    runtime_document["definitions"].pop("secret_scope")
    runtime_document["definitions"].pop("secret_key")
    runtime = install_runtime(runtime_document)
    transport = FakeGitHub()
    transport.expected_auth = False

    store = modules.definitions.DefinitionsStore(runtime, transport=transport, retry_delay_seconds=0)

    assert store.refresh() is True
    assert store.snapshot().revision == SHA1


def test_unchanged_revision_retains_snapshot_identity_without_content_fetch(store_setup):
    _, github, create = store_setup
    store = create()
    assert store.refresh() is True
    original = store.snapshot()
    assert store.refresh() is True
    assert store.snapshot() is original
    assert github.calls[3:] == [API + "/commits/main"]


def test_changed_revision_recurses_and_replaces_complete_generation(store_setup):
    _, github, create = store_setup
    store = create()
    store.refresh()
    old = store.snapshot()
    github.sha = SHA2
    github.files[SHA2] = {
        "definitions/nested/z.md": TEXT.replace("monthly_revenue", "z_metric").replace("certified_function: z_metric", "certified_function: monthly_revenue"),
        "definitions/a.md": TEXT.replace("monthly_revenue", "a_metric").replace("certified_function: a_metric", "certified_function: monthly_revenue").replace("monthly sales", "a sales").replace("revenue by month", "a revenue"),
        "definitions/README.txt": "Ignored non-Markdown",
    }
    assert store.refresh() is True
    current = store.snapshot()
    assert current is not old
    assert current.revision == SHA2
    assert [item.name for item in current.definitions] == ["a_metric", "z_metric"]
    assert old.by_name["monthly_revenue"].source_path == "definitions/monthly_revenue.md"
    assert all("README.txt" not in url for url in github.calls)


def test_snapshot_reads_do_not_wait_for_candidate_and_never_see_partial_load(store_setup):
    _, github, create = store_setup
    store = create()
    store.refresh()
    original = store.snapshot()
    github.sha = SHA2
    github.files[SHA2] = {"definitions/new.md": TEXT.replace("# Monthly revenue", "# New meaning")}
    fetching, release = Event(), Event()

    def block_file(url):
        if "/contents/definitions/new.md?" in url:
            fetching.set()
            assert release.wait(3)

    github.before_get = block_file
    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh = pool.submit(store.refresh)
        try:
            assert fetching.wait(2)
            assert pool.submit(store.snapshot).result(timeout=1) is original
        finally:
            release.set()
        assert refresh.result(timeout=2) is True
    assert store.snapshot().revision == SHA2
    assert "# New meaning" in store.snapshot().definitions[0].body


@pytest.mark.parametrize("bad_text", [
    "Missing front matter", TEXT.replace("domain: sales", "domain: unknown"),
    TEXT.replace("certified_function: monthly_revenue", "certified_function: unknown"),
    TEXT.replace("certified_function: monthly_revenue", "certified_function: resolution_time"),
    TEXT,  # Duplicate names and aliases in a second file.
])
def test_any_invalid_file_retains_previous_snapshot_and_retries_same_revision(store_setup, bad_text):
    _, github, create = store_setup
    store = create()
    store.refresh()
    original = store.snapshot()
    github.sha = SHA2
    github.files[SHA2] = {"definitions/valid.md": TEXT, "definitions/invalid.md": bad_text}
    assert store.refresh() is False
    assert store.snapshot() is original
    assert store.health()["status"] == "degraded"
    assert store.health()["ready"] is True
    assert store.health()["revision"] == SHA1
    github.files[SHA2] = {"definitions/fixed.md": TEXT}
    assert store.refresh() is True
    assert store.snapshot().revision == SHA2
    assert store.health()["status"] == "ready"


def test_initial_failure_is_not_ready_and_start_can_be_retried(store_setup):
    _, github, create = store_setup
    github.failures = [401]
    store = create()
    with pytest.raises(RuntimeError):
        store.start()
    assert store.health()["status"] == "not_ready"
    assert store.health()["ready"] is False
    with pytest.raises(RuntimeError):
        store.snapshot()
    assert len(github.calls) == 1
    try:
        store.start()
        assert store.health()["status"] == "ready"
    finally:
        store.stop()


@pytest.mark.parametrize("failure", [429, 500, 503, TimeoutError("private timeout")])
def test_transient_failures_retry_then_recover(store_setup, failure):
    _, github, create = store_setup
    github.failures = [failure, failure]
    store = create()
    assert store.refresh() is True
    assert github.calls[:3] == [API + "/commits/main"] * 3
    assert store.snapshot().revision == SHA1


@pytest.mark.parametrize("failure, attempts", [(401, 1), (403, 1), (404, 1), (500, 3), (TimeoutError(TOKEN), 3)])
def test_bounded_failures_and_all_diagnostics_are_redacted(store_setup, caplog, capsys, failure, attempts):
    _, github, create = store_setup
    github.failures = [failure] * 5
    store = create()
    with pytest.raises(RuntimeError) as error:
        store.start()
    assert len(github.calls) == attempts
    output = capsys.readouterr()
    diagnostics = " ".join((
        str(error.value), repr(error.value), "".join(traceback.format_exception(error.value)),
        repr(store), json.dumps(store.health()), caplog.text, output.out, output.err,
    ))
    assert TOKEN not in diagnostics
    assert "Authorization" not in diagnostics
    assert "private diagnostics" not in diagnostics


def test_later_network_failure_degrades_and_unchanged_success_recovers(store_setup):
    _, github, create = store_setup
    store = create()
    store.refresh()
    original = store.snapshot()
    github.failures = [TimeoutError(TOKEN)] * 3
    assert store.refresh() is False
    assert store.health()["status"] == "degraded"
    assert store.snapshot() is original
    assert store.refresh() is True
    assert store.snapshot() is original
    assert store.health()["status"] == "ready"


def test_missing_bound_token_fails_before_any_request(store_setup, monkeypatch):
    _, github, create = store_setup
    monkeypatch.delenv("GITHUB_DEFINITIONS_TOKEN")
    store = create()
    with pytest.raises(RuntimeError):
        store.start()
    assert github.calls == []
    assert store.health()["ready"] is False


@pytest.mark.parametrize("entry", [
    {"type": "file", "name": "metric.md", "path": "definitions/../metric.md"},
    {"type": "file", "name": "metric.md", "path": "outside/metric.md"},
    {"type": "file", "name": "metric.md", "path": "definitions/nested/metric.md"},
    {"type": "file", "name": "metric.md", "path": "definitions/%2e%2e/metric.md"},
    {"type": "file", "name": "metric.md", "path": "https://other.example/metric.md"},
    {"type": "symlink", "name": "metric.md", "path": "definitions/metric.md"},
    {"type": "submodule", "name": "nested", "path": "definitions/nested"},
])
def test_untrusted_listing_cannot_escape_folder_or_follow_links(store_setup, entry):
    _, github, create = store_setup
    github.override_listing = [entry]
    store = create()
    assert store.refresh() is False
    assert len(github.calls) == 2
    assert store.health()["ready"] is False


@pytest.mark.parametrize("url", [
    "http://api.github.com/repos/acme/repo", "https://api.github.com.evil.test/x",
    "https://github.com@evil.test/x", "https://user:secret@api.github.com/x",
    "https://api.github.com:444/x", "https://raw.githubusercontent.com/x",
    "https://api.github.com/repos/acme/../other", "https://api.github.com/x#fragment",
    "https://api.github.com/\nrepos/acme/repo", " https://api.github.com/repos/acme/repo",
])
def test_default_transport_rejects_unsafe_urls_before_open(modules, monkeypatch, url):
    def forbidden(*args, **kwargs):
        pytest.fail("Unsafe URL reached HTTP opener")
    transport = modules.definitions.GitHubTransport()
    monkeypatch.setattr(transport._opener, "open", forbidden)
    with pytest.raises(ValueError):
        transport.get(url, headers={"Authorization": "Bearer " + TOKEN}, timeout=10)


def test_default_transport_does_not_follow_redirects(modules):
    from urllib.request import BaseHandler
    from urllib.response import addinfourl

    requests = []
    class FakeHTTPS(BaseHandler):
        handler_order = 100

        def https_open(self, request):
            requests.append(request.full_url)
            headers = Message()
            headers["Location"] = "https://untrusted.example/"
            response = addinfourl(BytesIO(b"private diagnostics"), headers, request.full_url, 302)
            response.msg = "Found"
            return response

    transport = modules.definitions.GitHubTransport()
    transport._opener.add_handler(FakeHTTPS())
    result = transport.get(API + "/commits/main", headers={"Authorization": "Bearer " + TOKEN}, timeout=10)
    assert result.status == 302
    assert result.body == b""
    assert requests == [API + "/commits/main"]


def test_store_rejects_redirect_response_without_forwarding_credentials(store_setup):
    _, github, create = store_setup
    github.failures = [302]
    store = create()
    assert store.refresh() is False
    assert len(github.calls) == 1


def test_root_folder_and_slash_branch_are_encoded(modules, runtime_document, install_runtime, monkeypatch):
    monkeypatch.setenv("GITHUB_DEFINITIONS_TOKEN", TOKEN)
    runtime_document["definitions"].update(markdown_path=".", branch="release/customer")
    runtime = install_runtime(runtime_document)
    github = FakeGitHub()
    github.files[SHA1] = {"metric.md": TEXT}
    store = modules.definitions.DefinitionsStore(runtime, transport=github)
    assert store.refresh() is True
    assert github.calls == [
        API + "/commits/release%2Fcustomer", API + "/contents?ref=" + SHA1,
        API + "/contents/metric.md?ref=" + SHA1,
    ]


def test_empty_repository_cannot_replace_valid_snapshot(store_setup):
    _, github, create = store_setup
    store = create()
    store.refresh()
    original = store.snapshot()
    github.sha = SHA2
    github.files[SHA2] = {}
    assert store.refresh() is False
    assert store.snapshot() is original


def test_polling_refreshes_and_stop_joins_worker(modules, runtime_document, install_runtime, monkeypatch):
    monkeypatch.setenv("GITHUB_DEFINITIONS_TOKEN", TOKEN)
    runtime_document["definitions"]["poll_seconds"] = 1
    runtime = install_runtime(runtime_document)
    github = FakeGitHub()
    polled = Event()
    store = modules.definitions.DefinitionsStore(runtime, transport=github)
    try:
        store.start()
        github.sha = SHA2
        github.files[SHA2] = {"definitions/new.md": TEXT}
        def observe(url):
            if "/contents/definitions/new.md?" in url:
                polled.set()
        github.before_get = observe
        assert polled.wait(3)
    finally:
        store.stop()
    assert store.snapshot().revision == SHA2
    call_count = len(github.calls)
    store.stop()
    assert len(github.calls) == call_count


@pytest.mark.parametrize("contents", [
    b"not JSON", b"[]", b"null", b'{"sha":"invalid"}',
    b'{"sha":"' + SHA1.encode() + b'","sha":"' + SHA2.encode() + b'"}',
    b'{"sha":NaN}', b"\xff",
])
def test_malformed_revision_response_is_not_ready(store_setup, modules, contents):
    _, github, create = store_setup
    github.get = lambda *args, **kwargs: modules.definitions.GitHubResponse(200, contents, {})
    store = create()
    assert store.refresh() is False
    assert store.health()["ready"] is False


@pytest.mark.parametrize("mutation", [
    {"encoding": "none"}, {"content": "%%% invalid %%%"}, {"size": 0},
    {"size": True}, {"size": 1024 * 1024 + 1}, {"path": "outside/metric.md"},
    {"type": "symlink"}, {"submodule_git_url": "https://other.example/repo"},
    {"content": base64.b64encode(b"\xff").decode(), "size": 1},
])
def test_invalid_content_envelope_preserves_cache(store_setup, modules, mutation):
    _, github, create = store_setup
    store = create()
    store.refresh()
    original = store.snapshot()
    github.sha = SHA2
    github.files[SHA2] = {"definitions/new.md": TEXT}
    get = github.get
    def mutate(url, **kwargs):
        response = get(url, **kwargs)
        if "/contents/definitions/new.md?" in url:
            body = json.loads(response.body)
            body.update(mutation)
            return modules.definitions.GitHubResponse(200, json.dumps(body).encode(), {})
        return response
    github.get = mutate
    assert store.refresh() is False
    assert store.snapshot() is original


@pytest.mark.parametrize("listing", [
    {}, None,
    [{"type": "file", "path": "definitions/metric.md", "name": "metric.md"}] * 2,
    [{"type": "file", "path": "definitions/metric.md", "name": "metric.md"}] * 1000,
    [{"type": "file", "path": "definitions/metric.md", "name": "metric.md", "submodule_git_url": "https://other.example"}],
])
def test_invalid_duplicate_or_truncated_listing_fails_closed(store_setup, modules, listing):
    _, github, create = store_setup
    get = github.get
    def respond(url, **kwargs):
        if "/contents/definitions?" in url:
            return modules.definitions.GitHubResponse(200, json.dumps(listing).encode(), {})
        return get(url, **kwargs)
    github.get = respond
    store = create()
    assert store.refresh() is False
    assert store.health()["ready"] is False


@pytest.mark.parametrize("settings", [
    {"repository_url": "https://api.github.com/acme/repo"},
    {"repository_url": "https://github.com.evil.example/acme/repo"},
    {"repository_url": "https://user:password@github.com/acme/repo"},
    {"markdown_path": "definitions/%2e%2e/private"},
    {"markdown_path": "../private"}, {"markdown_path": "definitions\nprivate"},
    {"branch": "../main"}, {"branch": "main?ref=other"}, {"branch": "main\n"},
])
def test_unchecked_runtime_settings_are_revalidated_before_credentials(modules, store_setup, monkeypatch, settings):
    runtime, github, _ = store_setup
    unchecked = runtime.model_copy(update={"definitions": runtime.definitions.model_copy(update=settings)})
    def forbidden(*args):
        pytest.fail("Invalid configuration accessed environment credentials")
    with monkeypatch.context() as context:
        context.setattr(modules.definitions.os.environ, "get", forbidden)
        with pytest.raises(ValueError):
            modules.definitions.DefinitionsStore(unchecked, transport=github)
    assert github.calls == []


@pytest.mark.parametrize("token", ["", "   ", TOKEN + "\r\nInjected: header"])
def test_blank_or_control_character_token_is_rejected_before_transport(store_setup, monkeypatch, token):
    _, github, create = store_setup
    monkeypatch.setenv("GITHUB_DEFINITIONS_TOKEN", token)
    assert create().refresh() is False
    assert github.calls == []


def test_explicit_environment_binding_can_be_used(store_setup, monkeypatch):
    _, _, create = store_setup
    monkeypatch.delenv("GITHUB_DEFINITIONS_TOKEN")
    monkeypatch.setenv("CUSTOM_DEFINITIONS_TOKEN", TOKEN)
    assert create(token_env="CUSTOM_DEFINITIONS_TOKEN").refresh() is True


def test_rate_limit_403_is_retried_with_bounded_attempts(store_setup, modules):
    _, github, create = store_setup
    get = github.get
    limited = [True, True]
    def respond(url, **kwargs):
        if limited:
            limited.pop()
            return modules.definitions.GitHubResponse(403, b"", {"X-RateLimit-Remaining": "0", "Retry-After": "999999999"})
        return get(url, **kwargs)
    github.get = respond
    assert create().refresh() is True
    assert not limited


def test_default_transport_passes_get_timeout_and_closes_response(modules, monkeypatch):
    transport = modules.definitions.GitHubTransport()
    response = BytesIO(b'{"sha":"' + SHA1.encode() + b'"}')
    response.code, response.headers = 200, {"ETag": '"commit"'}
    calls = []
    def open_request(request, *, timeout):
        calls.append((request.full_url, request.get_method(), request.get_header("Authorization"), timeout))
        return response
    monkeypatch.setattr(transport._opener, "open", open_request)
    result = transport.get(API + "/commits/main", headers={"Authorization": "Bearer " + TOKEN}, timeout=7)
    assert calls == [(API + "/commits/main", "GET", "Bearer " + TOKEN, 7)]
    assert json.loads(result.body) == {"sha": SHA1}
    assert result.status == 200
    assert response.closed


def test_default_transport_bounds_response_and_redacts_exception_context(modules, monkeypatch):
    transport = modules.definitions.GitHubTransport()
    response = BytesIO(b"x" * (2 * 1024 * 1024 + 1))
    response.code, response.headers = 200, {}
    monkeypatch.setattr(transport._opener, "open", lambda *args, **kwargs: response)
    with pytest.raises(RuntimeError) as error:
        transport.get(API + "/commits/main", headers={"Authorization": "Bearer " + TOKEN}, timeout=10)
    assert error.value.__context__ is None
    assert response.closed


def test_default_transport_sanitizes_network_exception(modules, monkeypatch):
    transport = modules.definitions.GitHubTransport()
    def fail(*args, **kwargs):
        raise OSError("Authorization: Bearer " + TOKEN)
    monkeypatch.setattr(transport._opener, "open", fail)
    with pytest.raises(RuntimeError) as error:
        transport.get(API + "/commits/main", headers={"Authorization": "Bearer " + TOKEN}, timeout=10)
    assert TOKEN not in "".join(traceback.format_exception(error.value))
    assert error.value.__context__ is None


def test_stop_interrupts_pending_poll_and_restart_loads_again(store_setup):
    _, github, create = store_setup
    store = create()
    store.start()
    store.stop()
    github.sha = SHA2
    github.files[SHA2] = {"definitions/next.md": TEXT}
    try:
        store.start()
        assert store.snapshot().revision == SHA2
    finally:
        store.stop()


def test_restart_with_valid_cache_tolerates_refresh_failure(store_setup):
    _, github, create = store_setup
    store = create()
    store.start()
    original = store.snapshot()
    store.stop()
    github.failures = [401]
    try:
        store.start()
        assert store.snapshot() is original
        assert store.health()["status"] == "degraded"
        assert store.health()["ready"] is True
        assert store.refresh() is True
        assert store.health()["status"] == "ready"
    finally:
        store.stop()


def test_concurrent_refreshes_share_one_content_generation(store_setup):
    _, github, create = store_setup
    store = create()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.refresh(), range(16)))
    assert all(results)
    assert store.snapshot().revision == SHA1
    assert len([url for url in github.calls if "/contents/" in url]) == 2


@pytest.mark.parametrize("limit, value, files", [
    ("MAX_TOTAL_BYTES", 10, {"definitions/metric.md": TEXT}),
    ("MAX_ENTRIES", 1, {"definitions/metric.md": TEXT, "definitions/other.txt": "ignored"}),
    ("MAX_DEPTH", 0, {"definitions/nested/metric.md": TEXT}),
])
def test_repository_resource_limits_fail_closed(store_setup, modules, monkeypatch, limit, value, files):
    _, github, create = store_setup
    monkeypatch.setattr(modules.definitions, limit, value)
    github.files[SHA1] = files
    store = create()
    assert store.refresh() is False
    assert store.health()["ready"] is False


def test_stop_interrupts_retry_backoff_in_polling_thread(modules, runtime_document, install_runtime, monkeypatch):
    monkeypatch.setenv("GITHUB_DEFINITIONS_TOKEN", TOKEN)
    runtime_document["definitions"]["poll_seconds"] = 1
    runtime = install_runtime(runtime_document)
    github = FakeGitHub()
    failed = Event()
    store = modules.definitions.DefinitionsStore(runtime, transport=github, retry_delay_seconds=5)
    try:
        store.start()
        github.failures = [503, 503, 503]
        github.before_get = lambda url: failed.set()
        assert failed.wait(3)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(store.stop).result(timeout=1)
        assert len(github.failures) == 2
        assert store.snapshot().revision == SHA1
    finally:
        store.stop()
