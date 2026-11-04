"""App contracts exercised through real FastAPI and MCP HTTP requests."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient
from test_github_definitions import SHA1, SHA2, TEXT, TOKEN, FakeGitHub

HEADERS = {"Accept": "application/json, text/event-stream"}


def rpc(client, method, params=None):
    response = client.post("/mcp", headers=HEADERS, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
    }, follow_redirects=False)
    assert response.status_code == 200, response.text
    assert "location" not in response.headers
    return response.json()["result"]


def ask(client, question):
    result = rpc(client, "tools/call", {
        "name": "ask_analytics", "arguments": {"question": question},
    })
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def app_setup(modules, runtime_document, install_runtime, monkeypatch, trace_backend):
    runtime = install_runtime(runtime_document)
    github = FakeGitHub()
    monkeypatch.setenv("GITHUB_DEFINITIONS_TOKEN", TOKEN)
    monkeypatch.setattr(modules.definitions, "GitHubTransport", lambda: github)
    app = modules.app.create_app()
    return app, github, runtime


def test_exact_mcp_endpoint_exposes_only_one_analytics_tool(app_setup):
    app, _, _ = app_setup
    with TestClient(app) as client:
        initialized = rpc(client, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "contract-test", "version": "1"},
        })
        assert "tools" in initialized["capabilities"]
        tools = rpc(client, "tools/list")["tools"]
        assert [tool["name"] for tool in tools] == ["ask_analytics"]
        schema = tools[0]["inputSchema"]
        assert schema["required"] == ["question"]
        assert set(schema["properties"]) == {"question"}
        assert schema["properties"]["question"]["type"] == "string"
        assert client.post("/mcp/mcp", headers=HEADERS, json={}).status_code == 404


def test_verbatim_question_reaches_classifier_genie_and_trace(app_setup, workspace, trace_backend):
    app, _, runtime = app_setup
    question = "  Explain\tΔ sales?\n Keep THIS punctuation!  "
    with TestClient(app) as client:
        answer = ask(client, question)
    assert answer["question"] == question
    assert answer["answer"] == "Configured Genie answer"
    assert workspace.calls[0][2]["messages"][1]["content"] == question
    assert workspace.calls[1][2]["content"] == question
    trace_call = workspace.calls[-1]
    parameters = {p["name"]: p["value"] for p in trace_call[2]["parameters"]}
    assert parameters["question"] == question
    assert parameters["defs_commit_sha"] == SHA1
    assert parameters["config_fingerprint"] == runtime.fingerprint
    assert trace_backend.spans[0]["inputs"]["question"] == question


def test_definition_enrichment_uses_one_snapshot_for_answer_and_trace(app_setup, workspace):
    app, github, _ = app_setup

    def refresh_during_classification(method, path, body=None, **kwargs):
        if path.startswith("/serving-endpoints/"):
            github.sha = SHA2
            github.files[SHA2] = {"definitions/monthly_revenue.md": TEXT.replace("excluding refunds", "including refunds")}
            assert app.state.definitions.refresh()
        return original(method, path, body=body, **kwargs)

    original = workspace.api_client.do
    workspace.api_client.do = refresh_during_classification
    question = "  monthly SALES  "
    with TestClient(app) as client:
        answer = ask(client, question)
        assert client.get("/health").json()["definitions"]["revision"] == SHA2
    assert answer["route"] == "uc_function"
    assert answer["question"] == question
    assert answer["defs_commit_sha"] == SHA1
    assert answer["matched_definition_term"] == "monthly_revenue"
    classifier_input = json.loads(workspace.calls[0][2]["messages"][1]["content"])
    assert classifier_input["question"] == question
    assert "excluding refunds" in classifier_input["governed_context"]
    params = {p["name"]: p["value"] for p in workspace.calls[-1][2]["parameters"]}
    assert params["defs_commit_sha"] == SHA1
    assert params["uc_function"] == "acme_catalog.metrics.monthly_revenue"
    assert params["answering_genie"] is None


def test_initial_load_gates_readiness_and_shutdown_joins_worker(app_setup):
    app, github, _ = app_setup
    with TestClient(app) as client:
        worker = app.state.definitions._worker
        assert worker.is_alive()
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ready"
        assert health.json()["ready"] is True
        assert len(github.calls) == 3
    assert not worker.is_alive()
    assert TestClient(app).get("/health").status_code == 503


def test_health_is_not_ready_before_lifespan(app_setup):
    app, _, _ = app_setup
    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 503
    assert health.json()["ready"] is False


def test_failed_initial_load_prevents_startup_and_stops_store(app_setup):
    app, github, _ = app_setup
    github.failures = [401]
    with pytest.raises(RuntimeError, match="Application startup failed"), TestClient(app):
        pytest.fail("Startup accepted an invalid initial snapshot")
    assert app.state.ready is False
    assert app.state.definitions._worker is None
    assert app.state.definitions._stop.is_set()


def test_failed_refresh_degrades_health_but_retains_answers_and_recovers(app_setup):
    app, github, _ = app_setup
    with TestClient(app) as client:
        github.failures = [401]
        assert not app.state.definitions.refresh()
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "degraded"
        assert health.json()["ready"] is True
        assert health.json()["definitions"]["revision"] == SHA1
        assert ask(client, "monthly sales")["route"] == "uc_function"
        assert app.state.definitions.refresh()
        assert client.get("/health").json()["status"] == "ready"


def test_root_health_and_logs_never_echo_credentials_or_rejected_diagnostics(
    app_setup, workspace, monkeypatch, caplog,
):
    app, github, _ = app_setup
    private = "synthetic-databricks-client-secret-DO-NOT-LOG"
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", private)
    with TestClient(app) as client:
        github.failures = [RuntimeError(TOKEN), 401]
        assert not app.state.definitions.refresh()
        workspace.statement["status"] = {"state": "FAILED", "error": {"message": private}}
        assert ask(client, "Sales trends")["status"] == "answered"
        public = json.dumps([client.get("/").json(), client.get("/health").json()])
    for forbidden in (TOKEN, private, "github.com/acme", "abc123", "secret_scope", "warehouse_id"):
        assert forbidden not in public
        assert forbidden not in caplog.text


@pytest.mark.parametrize("sink", ["delta", "mlflow"])
def test_trace_failure_preserves_success_and_degrades_health(app_setup, workspace, trace_backend, caplog, sink):
    app, _, _ = app_setup
    with TestClient(app) as client:
        if sink == "delta":
            workspace.statement["status"] = {"state": "FAILED"}
        else:
            trace_backend.failure = "synthetic-secret-in-backend-error"
        assert ask(client, "Sales trends")["status"] == "answered"
        health = client.get("/health").json()
        assert health["status"] == "degraded"
        assert health["tracing"][sink]["status"] == "degraded"
        assert health["tracing"][sink]["failures"] >= 1
        assert sink in caplog.text.lower()
        assert "synthetic-secret-in-backend-error" not in caplog.text


def test_slow_analytics_does_not_block_health(app_setup, workspace):
    app, _, _ = app_setup
    entered, release = Event(), Event()
    original = workspace.genie.start_conversation_and_wait

    def slow_genie(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    workspace.genie.start_conversation_and_wait = slow_genie
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        request = pool.submit(ask, client, "Sales trends")
        try:
            assert entered.wait(2)
            health = pool.submit(client.get, "/health").result(timeout=2)
            assert health.status_code == 200
        finally:
            release.set()
        assert request.result(timeout=3)["status"] == "answered"


@pytest.mark.parametrize("question,method", [
    ("Show MONTHLY-sales?", "exact_alias"),
    ("Compare revenue for each month", "word_overlap"),
    ("A monthly salesforce report", "no_match"),
    ("by the month", "no_match"),
])
def test_enrichment_matching_and_no_match_provenance(app_setup, question, method):
    app, _, _ = app_setup
    with TestClient(app) as client:
        answer = ask(client, question)
    assert answer["enrichment_method"] == method
    assert answer["defs_commit_sha"] == SHA1
    assert answer["matched_definition_term"] == (None if method == "no_match" else "monthly_revenue")


def test_ambiguous_governed_matches_do_not_nominate_a_function(app_setup):
    app, github, _ = app_setup
    github.files[SHA1]["definitions/other.md"] = TEXT.replace("monthly_revenue", "other_metric").replace(
        "certified_function: other_metric", "certified_function: monthly_revenue",
    ).replace("monthly sales", "quarterly sales").replace("revenue by month", "revenue by quarter")
    with TestClient(app) as client:
        answer = ask(client, "Compare monthly sales and quarterly sales")
    assert answer["route"] == "genie"
    assert answer["enrichment_method"] == "no_match"


@pytest.mark.parametrize("status,decision_updates", [
    ("no_route", {"confidence": 0.1}),
    ("undefined_term", {"undefined_terms": ["imaginary_metric"]}),
    ("error", None),
])
def test_fail_loud_and_route_errors_are_traced(app_setup, workspace, status, decision_updates):
    app, _, _ = app_setup
    if decision_updates is None:
        workspace.message.status = "FAILED"
    else:
        workspace.decision.update(decision_updates)
    with TestClient(app) as client:
        answer = ask(client, "Sales trends")
    assert answer["status"] == status
    params = {p["name"]: p["value"] for p in workspace.calls[-1][2]["parameters"]}
    assert params["status"] == status
    assert params["trace_id"] == answer["trace_id"]
    assert params["defs_commit_sha"] == SHA1
    assert params["fail_loud"] == ("false" if status == "error" else "true")


def test_failed_function_trace_identifies_the_attempted_allowlisted_target(app_setup, workspace):
    app, _, _ = app_setup
    workspace.statement["status"] = {"state": "FAILED"}
    with TestClient(app) as client:
        answer = ask(client, "monthly sales")
    assert answer["status"] == "error"
    assert answer["failed_route"] == "uc_function"
    params = {p["name"]: p["value"] for p in workspace.calls[-1][2]["parameters"]}
    assert params["uc_function"] == "acme_catalog.metrics.monthly_revenue"
    assert params["answering_genie"] is None
