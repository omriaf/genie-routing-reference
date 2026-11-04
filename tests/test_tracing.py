"""Trace contents, durable-write checks, redaction, and MLflow boundaries."""

import json

import pytest


@pytest.fixture
def recorder(modules, runtime_document, install_runtime, trace_backend):
    runtime = install_runtime(runtime_document)
    recorder = modules.tracing.TraceRecorder(runtime)
    recorder.initialize()
    return recorder


def result(**updates):
    value = {
        "trace_id": "trace-123", "question": "  Revenue '雪'\n ", "status": "answered",
        "route": "genie", "domain": "sales", "confidence": 0.9,
        "candidates": {"sales": 0.9}, "genie_space_id": "space-123",
        "config_fingerprint": "f" * 64, "answer": "42", "reasoning": "Sales question",
    }
    value.update(updates)
    return value


def enrichment(modules, **updates):
    values = {"matched_term": "monthly_revenue", "method": "exact_alias", "domain": "sales",
              "score": 1.0, "definitions_revision": "a" * 40}
    values.update(updates)
    return modules.router.RoutingEnrichment(**values)


def parameters(workspace):
    return {p["name"]: p for p in workspace.calls[-1][2]["parameters"]}


def test_trace_contains_all_contract_fields_and_configured_sinks(recorder, modules, workspace, trace_backend):
    answer = result()
    record = recorder.record(answer, enrichment(modules), 123)
    assert record["route"] == "genie"
    assert record["domain"] == "sales"
    assert record["confidence"] == 0.9
    assert record["latency_ms"] == 123
    assert record["defs_commit_sha"] == "a" * 40
    assert record["git_definition_used"] is True
    assert record["matched_definition_term"] == "monthly_revenue"
    assert record["enrichment_method"] == "exact_alias"
    assert record["answering_genie"] == {"space_id": "space-123", "domain": "sales"}
    assert record["uc_function"] is None
    assert record["fail_loud"] is False
    assert record["config_fingerprint"] == "f" * 64
    assert trace_backend.experiment == "/Shared/acme-analytics-router"
    assert trace_backend.uri == "databricks"
    assert trace_backend.spans[0]["attributes"]["routing.defs_commit_sha"] == "a" * 40
    assert trace_backend.spans[0]["attributes"]["routing.config_fingerprint"] == "f" * 64
    assert trace_backend.spans[0]["outputs"]["answer"] == "42"
    call = workspace.calls[-1]
    assert call[:2] == ("POST", "/api/2.0/sql/statements")
    assert "INSERT INTO `acme_catalog`.`analytics_router`.`routing_traces`" in call[2]["statement"]
    assert call[2]["warehouse_id"] == "abc123"
    assert answer["question"] not in call[2]["statement"]
    params = parameters(workspace)
    for field in ("trace_id", "domain", "route", "question", "config_fingerprint"):
        assert params[field]["value"] == answer[field]
    assert params["confidence"]["type"] == "DOUBLE"
    assert params["latency_ms"]["type"] == "BIGINT"
    assert params["git_definition_used"]["value"] == "true"
    assert json.loads(params["answering_genie"]["value"]) == record["answering_genie"]


@pytest.mark.parametrize("route,status,function,fail_loud", [
    ("uc_function", "answered", "acme_catalog.metrics.monthly_revenue", False),
    ("fail_loud", "no_route", None, True),
    ("fail_loud", "undefined_term", None, True),
    ("error", "error", None, False),
])
def test_function_and_fail_loud_paths_keep_provenance(recorder, modules, workspace, route, status, function, fail_loud):
    record = recorder.record(result(route=route, status=status, uc_function=function),
                             enrichment(modules, matched_term=None, method="no_match"), 5)
    assert record["uc_function"] == function
    assert record["fail_loud"] is fail_loud
    assert record["answering_genie"] is None
    assert record["git_definition_used"] is False
    assert record["defs_commit_sha"] == "a" * 40
    assert parameters(workspace)["fail_loud"]["value"] == str(fail_loud).lower()


@pytest.mark.parametrize("state", ["FAILED", "CANCELED", "CLOSED", None])
def test_only_succeeded_statement_counts_as_a_durable_trace(recorder, modules, workspace, state, caplog):
    workspace.statement["status"] = {"state": state, "error": {"message": "DO-NOT-LOG"}}
    recorder.record(result(), enrichment(modules), 1)
    assert recorder.health()["delta"]["status"] == "degraded"
    assert recorder.health()["delta"]["failures"] == 1
    assert "delta" in caplog.text.lower()
    assert "DO-NOT-LOG" not in caplog.text


def test_pending_statement_is_polled_until_success(recorder, modules, workspace, monkeypatch):
    responses = iter([
        {"status": {"state": "PENDING"}, "statement_id": "trace-statement"},
        {"status": {"state": "RUNNING"}, "statement_id": "trace-statement"},
        {"status": {"state": "SUCCEEDED"}},
    ])
    requests = []

    def execute(method, path, **kwargs):
        requests.append((method, path))
        return next(responses)

    monkeypatch.setattr(workspace.api_client, "do", execute)
    monkeypatch.setattr(modules.tracing.time, "sleep", lambda _: None)
    recorder.record(result(), enrichment(modules), 1)
    assert requests == [
        ("POST", "/api/2.0/sql/statements"),
        ("GET", "/api/2.0/sql/statements/trace-statement"),
        ("GET", "/api/2.0/sql/statements/trace-statement"),
    ]
    assert recorder.health()["delta"]["status"] == "ready"


@pytest.mark.parametrize("statement_id", ["trace-statement", "../private?token=DO-NOT-LOG", None])
def test_pending_write_is_bounded_and_invalid_ids_are_not_followed(
    recorder, modules, workspace, monkeypatch, statement_id,
):
    workspace.statement.update(status={"state": "RUNNING"}, statement_id=statement_id)
    monkeypatch.setattr(modules.tracing.time, "sleep", lambda _: None)
    recorder.record(result(), enrichment(modules), 1)
    assert recorder.health()["delta"]["status"] == "degraded"
    assert len(workspace.calls) <= 21
    if statement_id != "trace-statement":
        assert len(workspace.calls) == 1


def test_backend_error_is_nonfatal_redacted_and_recovers(recorder, modules, workspace, monkeypatch, caplog):
    original = workspace.api_client.do

    def broken(*args, **kwargs):
        raise RuntimeError("Authorization: Bearer DO-NOT-LOG")

    monkeypatch.setattr(workspace.api_client, "do", broken)
    recorder.record(result(), enrichment(modules), 1)
    assert recorder.health()["delta"]["status"] == "degraded"
    assert "DO-NOT-LOG" not in caplog.text + json.dumps(recorder.health())
    monkeypatch.setattr(workspace.api_client, "do", original)
    recorder.record(result(), enrichment(modules), 1)
    assert recorder.health()["delta"]["status"] == "ready"
    assert recorder.health()["delta"]["failures"] == 1


def test_mlflow_initialization_failure_is_visible_and_does_not_block_delta(recorder, modules, workspace, trace_backend, caplog):
    trace_backend.failure = "DO-NOT-LOG"
    recorder.initialize()
    assert recorder.health()["mlflow"]["status"] == "degraded"
    recorder.record(result(), enrichment(modules), 1)
    assert parameters(workspace)["mlflow_trace_ok"]["value"] == "false"
    assert recorder.health()["delta"]["status"] == "ready"
    assert "DO-NOT-LOG" not in caplog.text + json.dumps(recorder.health())


def test_mlflow_export_failure_is_nonfatal_and_visible(recorder, modules, trace_backend, caplog):
    trace_backend.failure = "DO-NOT-LOG"
    recorder.record(result(), enrichment(modules), 1)
    assert recorder.health()["mlflow"]["status"] == "degraded"
    assert "DO-NOT-LOG" not in caplog.text


def test_completed_request_emits_mlflow_trace_with_actual_latency(recorder, modules, trace_backend):
    recorder.record(result(), enrichment(modules), 123)
    assert len(trace_backend.spans) == 1
    assert trace_backend.spans[0]["execution_time_ms"] == 123


def test_trace_table_quotes_valid_identifiers_that_are_sql_keywords(
    modules, runtime_document, install_runtime, workspace, trace_backend,
):
    runtime_document["workspace"].update(catalog="select", router_schema="from")
    for function in runtime_document["uc_functions"]:
        function["full_name"] = function["full_name"].replace("acme_catalog.", "select.")
    runtime = install_runtime(runtime_document)
    recorder = modules.tracing.TraceRecorder(runtime)
    recorder.record(result(), enrichment(modules), 1)
    assert "INSERT INTO `select`.`from`.`routing_traces`" in workspace.calls[-1][2]["statement"]


def test_silently_dropped_mlflow_trace_is_visible_in_delta_and_health(
    recorder, modules, workspace, trace_backend, caplog,
):
    trace_backend.persisted = False
    recorder.record(result(), enrichment(modules), 123)
    assert recorder.health()["mlflow"]["status"] == "degraded"
    assert parameters(workspace)["mlflow_trace_ok"]["value"] == "false"
    assert "mlflow" in caplog.text.lower()


def test_real_mlflow_persists_the_request_and_complete_provenance(
    modules, runtime_document, install_runtime, tmp_path, monkeypatch,
):
    # Only the destination changes: exercise the installed MLflow serializer,
    # exporter, and read-back against a local SQLite store without credentials.
    monkeypatch.setenv("MLFLOW_DISABLE_AGENT_HINT", "1")
    monkeypatch.setenv("MLFLOW_ENABLE_ASYNC_TRACE_LOGGING", "false")
    import mlflow

    runtime = install_runtime(runtime_document)
    local_uri = "sqlite:///" + str(tmp_path / "traces.db")
    set_tracking_uri = mlflow.set_tracking_uri
    monkeypatch.setattr(mlflow, "set_tracking_uri", lambda _: set_tracking_uri(local_uri))
    recorder = modules.tracing.TraceRecorder(runtime)
    recorder.initialize()
    record = recorder.record(result(), enrichment(modules), 123)
    assert record["mlflow_trace_ok"] is True
    experiment = mlflow.get_experiment_by_name("/Shared/acme-analytics-router")
    traces = mlflow.search_traces(locations=[experiment.experiment_id], return_type="list")
    assert len(traces) == 1
    span = traces[0].data.spans[0]
    assert span.inputs == {"question": "  Revenue '雪'\n "}
    assert span.outputs["answer"] == "42"
    assert span.attributes["routing.defs_commit_sha"] == "a" * 40
    assert span.attributes["routing.matched_definition_term"] == "monthly_revenue"
    assert span.attributes["routing.config_fingerprint"] == "f" * 64
    assert span.attributes["routing.latency_ms"] == 123
    assert (span.end_time_ns - span.start_time_ns) // 1_000_000 == 123
