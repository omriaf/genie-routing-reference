"""Smoke contracts fail closed without profiles, credentials or remote services."""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from conftest import model_decision
from fastapi.testclient import TestClient
from test_app import app_setup as app_setup, ask, rpc
from test_github_definitions import SHA1

from app.runtime_config import RuntimeConfig
from scripts import smoke_test as smoke


@pytest.fixture
def runtime(runtime_document):
    return RuntimeConfig.model_validate_json(json.dumps(runtime_document))


@pytest.fixture
def plan_document():
    return {
        "app_url": "https://sample.aws." + "databricksapps.com",
        "runtime_config": "runtime.json",
        "definitions_revision": SHA1,
        "cases": [
            {"kind": "genie", "question": "Describe sales channels", "domain": "sales"},
            {"kind": "genie", "question": "Describe ticket volume", "domain": "support"},
            {"kind": "function", "question": "What are monthly sales?", "domain": "sales",
             "function": "monthly_revenue", "expected_rows": [["42"]]},
            {"kind": "fail_loud", "question": "What is the weather forecast?",
             "refusal_status": "no_route"},
            {"kind": "git_enriched", "question": "Show monthly sales", "domain": "sales",
             "definition_term": "monthly_revenue", "function": "monthly_revenue",
             "expected_rows": [["42"]]},
        ],
    }


@pytest.fixture
def plan_file(tmp_path, plan_document, runtime_document):
    (tmp_path / "runtime.json").write_text(json.dumps(runtime_document))
    path = tmp_path / "smoke.json"
    path.write_text(json.dumps(plan_document))
    return path


def answer_for(case, runtime, revision=SHA1):
    result = {
        "trace_id": str(uuid4()), "question": case.question,
        "config_fingerprint": runtime.fingerprint, "defs_commit_sha": revision,
        "status": "answered", "route": "genie", "domain": case.domain,
        "answer": "Synthetic known answer", "enrichment_method": "no_match",
        "matched_definition_term": None,
    }
    if case.kind == "fail_loud":
        result.update(status=case.refusal_status, route="fail_loud", domain="out_of_domain")
    else:
        result.update(genie_space_id=runtime.agents_by_name[case.domain].space_id)
        if case.function and runtime.routing.prefer_certified_functions:
            result.update(route="uc_function", uc_function=runtime.functions_by_name[case.function].full_name,
                          rows=case.expected_rows)
        else:
            result.update(conversation_id="test-conversation", message_id="test-message")
        if case.kind == "git_enriched":
            result.update(matched_definition_term=case.definition_term, enrichment_method="exact_alias")
    return result


def delta_for(answer):
    row = {key: answer.get(key) for key in smoke.EVIDENCE_FIELDS}
    row.update(genie_space=answer.get("genie_space_id"), fail_loud=answer["route"] == "fail_loud",
               git_definition_used=bool(answer.get("matched_definition_term")),
               mlflow_trace_ok=True, latency_ms=17)
    return row


def trace_for(answer, row):
    return SimpleNamespace(
        info=SimpleNamespace(tags={key: str(answer.get(key)) for key in (
            "trace_id", "route", "domain", "config_fingerprint", "defs_commit_sha",
        )}),
        data=SimpleNamespace(spans=[SimpleNamespace(
            name="ask_analytics", inputs={"question": answer["question"]}, outputs=dict(answer),
            start_time_ns=0, end_time_ns=row["latency_ms"] * 1_000_000,
        )]),
    )


def health_for(runtime, *, delta="ready"):
    return {"status": "ready", "ready": True, "config_fingerprint": runtime.fingerprint,
            "definitions": {"ready": True, "status": "ready", "revision": SHA1, "definition_count": 1},
            "tracing": {sink: {"status": delta if sink == "delta" else "ready", "error": None}
                        for sink in ("delta", "mlflow")}}


@pytest.mark.parametrize("profile", [None, "", " ", "<PROFILE>"])
def test_profile_is_required_before_any_config_or_auth(monkeypatch, profile):
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "test-profile")
    monkeypatch.setattr(smoke, "load_plan", lambda *_: pytest.fail("read config before profile validation"))
    monkeypatch.setattr(smoke, "LiveBackend", lambda *_: pytest.fail("authenticated without explicit profile"))
    arguments = ["--config", "missing.json"]
    if profile is not None:
        arguments += ["--profile", profile]
    with pytest.raises(SystemExit) as error:
        smoke.main(arguments)
    assert error.value.code == 2


def test_plan_loads_offline_and_rejects_missing_scenarios(plan_file, plan_document):
    plan, runtime = smoke.load_plan(plan_file)
    assert plan.definitions_revision == SHA1
    assert len(runtime.genie_agents) == 2
    plan_document["cases"] = [case for case in plan_document["cases"] if case["kind"] != "git_enriched"]
    plan_file.write_text(json.dumps(plan_document))
    with pytest.raises(smoke.SmokeFailure, match="four"):
        smoke.load_plan(plan_file)


@pytest.mark.parametrize("mutation", [
    lambda plan: plan["cases"].pop(1),
    lambda plan: plan["cases"][2].update(function="not_allowlisted"),
    lambda plan: plan["cases"][2].update(expected_rows=None),
    lambda plan: plan["cases"][3].update(refusal_status=None),
    lambda plan: plan["cases"][4].update(definition_term=None),
])
def test_incomplete_private_expectations_fail(plan_file, plan_document, mutation):
    mutation(plan_document)
    plan_file.write_text(json.dumps(plan_document))
    with pytest.raises(smoke.SmokeFailure):
        smoke.load_plan(plan_file)


@pytest.mark.parametrize("url", [
    "http://sample.example.com", "https://sample.example.com", "https://sample." + "databricksapps.com.evil.test",
    "https://user:password@" + "sample." + "databricksapps.com", "https://sample." + "databricksapps.com?token=PLACEHOLDER",
    "https://sample." + "databricksapps.com/path", "https://sample." + "databricksapps.com:8443",
])
def test_app_origin_validation(url, plan_document):
    plan_document["app_url"] = url
    with pytest.raises(ValueError):
        smoke.Plan.model_validate(plan_document)


@pytest.mark.parametrize("prefer", [True, False])
def test_function_policy_and_git_enrichment(runtime_document, plan_document, prefer):
    runtime_document["routing"]["prefer_certified_functions"] = prefer
    runtime = RuntimeConfig.model_validate_json(json.dumps(runtime_document))
    for case in smoke.Plan.model_validate(plan_document).cases:
        answer = answer_for(case, runtime)
        smoke.check_answer(answer, case, runtime, SHA1)
        if case.function:
            answer["route"] = "genie" if prefer else "uc_function"
            with pytest.raises(smoke.SmokeFailure, match="route"):
                smoke.check_answer(answer, case, runtime, SHA1)


@pytest.mark.parametrize("field,value", [
    ("status", "error"), ("domain", "support"), ("defs_commit_sha", "b" * 40),
    ("config_fingerprint", "wrong"), ("trace_id", "invalid"), ("uc_function", "unapproved.function"),
    ("rows", [["wrong"]]), ("question", "changed"), ("answer", ""),
    ("error", "failure"), ("failed_route", "genie"),
])
def test_route_and_provenance_mismatches_fail(runtime, plan_document, field, value):
    case = smoke.Plan.model_validate(plan_document).cases[2]
    answer = answer_for(case, runtime)
    answer[field] = value
    with pytest.raises(smoke.SmokeFailure):
        smoke.check_answer(answer, case, runtime, SHA1)


def test_loud_failure_cannot_pass_as_backend_error(runtime, plan_document):
    case = smoke.Plan.model_validate(plan_document).cases[3]
    answer = answer_for(case, runtime)
    answer.update(route="error", status="error")
    with pytest.raises(smoke.SmokeFailure):
        smoke.check_answer(answer, case, runtime, SHA1)


def test_git_match_and_method_are_required(runtime, plan_document):
    case = smoke.Plan.model_validate(plan_document).cases[4]
    for update in ({"matched_definition_term": None}, {"enrichment_method": "word_overlap"}):
        answer = answer_for(case, runtime)
        answer.update(update)
        with pytest.raises(smoke.SmokeFailure, match="Git enrichment"):
            smoke.check_answer(answer, case, runtime, SHA1)


@pytest.mark.parametrize("field,value", [
    ("trace_id", "wrong"), ("question", "wrong"), ("status", "error"),
    ("defs_commit_sha", "wrong"), ("genie_space", "wrong"), ("mlflow_trace_ok", False),
    ("fail_loud", True), ("git_definition_used", True), ("latency_ms", -1), ("latency_ms", True),
])
def test_delta_mismatches_fail(runtime, plan_document, field, value):
    answer = answer_for(smoke.Plan.model_validate(plan_document).cases[0], runtime)
    row = delta_for(answer)
    row[field] = value
    with pytest.raises(smoke.SmokeFailure):
        smoke.check_delta(row, answer)


@pytest.mark.parametrize("mutation", [
    lambda trace: trace.info.tags.update(trace_id="wrong"),
    lambda trace: trace.data.spans.clear(),
    lambda trace: trace.data.spans[0].outputs.update(defs_commit_sha="wrong"),
    lambda trace: trace.data.spans[0].inputs.update(question="wrong"),
    lambda trace: setattr(trace.data.spans[0], "end_time_ns", 0),
])
def test_mlflow_mismatches_fail(runtime, plan_document, mutation):
    answer = answer_for(smoke.Plan.model_validate(plan_document).cases[0], runtime)
    row = delta_for(answer)
    trace = trace_for(answer, row)
    smoke.check_mlflow(trace, answer, row)
    mutation(trace)
    with pytest.raises(smoke.SmokeFailure):
        smoke.check_mlflow(trace, answer, row)


def test_only_initial_delta_not_ready_is_tolerated(runtime):
    health = health_for(runtime, delta="not_ready")
    smoke.check_health(health, runtime, SHA1, initial=True)
    with pytest.raises(smoke.SmokeFailure):
        smoke.check_health(health, runtime, SHA1)
    health["tracing"]["mlflow"]["status"] = "not_ready"
    with pytest.raises(smoke.SmokeFailure):
        smoke.check_health(health, runtime, SHA1, initial=True)


@pytest.mark.parametrize("update", [{"ready": False}, {"status": "degraded"},
                                    {"config_fingerprint": "wrong"}])
def test_unhealthy_app_fails(runtime, update):
    health = health_for(runtime)
    health.update(update)
    with pytest.raises(smoke.SmokeFailure):
        smoke.check_health(health, runtime, SHA1)


@pytest.mark.parametrize("prefer", [True, False])
def test_complete_runner_against_real_app_and_in_memory_services(
    app_setup, runtime_document, plan_document, workspace, trace_backend, monkeypatch, prefer,
):
    app, _, _ = app_setup
    runtime_document["routing"]["prefer_certified_functions"] = prefer
    runtime = RuntimeConfig.model_validate_json(json.dumps(runtime_document))
    monkeypatch.setattr("app.app.get_runtime_config", lambda: runtime)
    monkeypatch.setattr("app.config.get_runtime_config", lambda: runtime)
    monkeypatch.setattr("app.router.get_runtime_config", lambda: runtime)
    monkeypatch.setattr("app.uc_functions.get_runtime_config", lambda: runtime)
    plan = smoke.Plan.model_validate(plan_document)
    cases = {case.question: case for case in plan.cases}

    class AppBackend:
        def health(self):
            return client.get("/health").json()

        def discover(self):
            assert rpc(client, "tools/list")["tools"][0]["name"] == "ask_analytics"

        def ask(self, question):
            case = cases[question]
            workspace.decision = model_decision(domain=case.domain or "out_of_domain",
                                                candidates={case.domain: 0.9} if case.domain else {})
            return ask(client, question)

        def delta(self, trace_id):
            call = next(body for method, path, body in reversed(workspace.calls)
                        if path == "/api/2.0/sql/statements" and "INSERT" in body.get("statement", ""))
            row = {item["name"]: item["value"] for item in call["parameters"]}
            assert row["trace_id"] == trace_id
            row["latency_ms"] = int(row["latency_ms"])
            for key in ("fail_loud", "git_definition_used", "mlflow_trace_ok"):
                row[key] = row[key] == "true"
            return row

        def trace(self, trace_id):
            entry = trace_backend.spans[-1]
            assert entry["tags"]["trace_id"] == trace_id
            return SimpleNamespace(info=SimpleNamespace(tags=entry["tags"]),
                                   data=SimpleNamespace(spans=[SimpleNamespace(**entry)]))

    with TestClient(app) as client:
        smoke.run_checks(AppBackend(), plan, runtime)


def test_live_adapter_uses_selected_profile_and_correlates_evidence(monkeypatch, runtime, plan_document):
    import databricks.sdk
    from mlflow.tracking import MlflowClient

    for key in smoke.AUTH_ENVIRONMENT_OVERRIDES:
        monkeypatch.delenv(key, raising=False)
    profile_calls, sql_calls, trace_calls = [], [], []
    case = smoke.Plan.model_validate(plan_document).cases[0]
    answer = answer_for(case, runtime)
    row = delta_for(answer)
    trace = trace_for(answer, row)

    def query(method, path, body=None):
        sql_calls.append(body)
        return {"status": {"state": "SUCCEEDED"}, "result": {"data_array": [[json.dumps(row)]]}}

    def workspace_client(*, profile):
        profile_calls.append(profile)
        return SimpleNamespace(api_client=SimpleNamespace(do=query),
                               config=SimpleNamespace(authenticate=lambda: {"Authorization": "Bearer PLACEHOLDER"}))

    class Tracking:
        def __init__(self, *, tracking_uri):
            profile_calls.append(tracking_uri)

        def get_experiment_by_name(self, path):
            assert path == runtime.workspace.mlflow_experiment
            return SimpleNamespace(experiment_id="synthetic-experiment")

        def search_traces(self, **kwargs):
            trace_calls.append(kwargs)
            return [trace]

    requests = []

    def transport(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer PLACEHOLDER"
        if request.url.path == "/health":
            return httpx.Response(200, json=health_for(runtime))
        document = json.loads(request.content)
        method = document["method"]
        payload = ({"tools": [{"name": "ask_analytics"}]} if method == "tools/list" else
                   {"content": [{"type": "text", "text": json.dumps(answer)}]} if method == "tools/call" else {})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": payload})

    real_client = httpx.Client
    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", workspace_client)
    monkeypatch.setattr("mlflow.tracking.MlflowClient", Tracking)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(transport), **kwargs))
    backend = smoke.LiveBackend("test-profile", smoke.Plan.model_validate(plan_document), runtime)
    try:
        assert backend.health()["status"] == "ready"
        backend.discover()
        assert backend.ask(case.question) == answer
        assert backend.delta(answer["trace_id"]) == row
        assert backend.trace(answer["trace_id"]) == trace
    finally:
        backend.close()
    assert profile_calls == ["test-profile", "databricks://test-profile"]
    assert sql_calls[0]["parameters"] == [{"name": "trace_id", "type": "STRING", "value": answer["trace_id"]}]
    assert answer["trace_id"] not in sql_calls[0]["statement"]
    assert trace_calls[0]["experiment_ids"] == ["synthetic-experiment"]
    assert all(request.url.path in {"/health", "/mcp"} for request in requests)
    assert MlflowClient is not Tracking


@pytest.mark.parametrize("key", sorted(smoke.AUTH_ENVIRONMENT_OVERRIDES))
def test_ambient_auth_cannot_override_explicit_profile(monkeypatch, runtime, plan_document, key):
    monkeypatch.setenv(key, "PLACEHOLDER")
    with pytest.raises(smoke.SmokeFailure, match="ambient auth"):
        smoke.LiveBackend("test-profile", smoke.Plan.model_validate(plan_document), runtime)


def test_main_redacts_external_errors_and_closes_backend(plan_file, monkeypatch, capsys):
    closed = []

    class Backend:
        def __init__(self, profile, plan, runtime):
            assert profile == "test-profile"

        def health(self):
            raise RuntimeError("PRIVATE_TRANSPORT_DIAGNOSTIC")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(smoke, "LiveBackend", Backend)
    assert smoke.main(["--profile", "test-profile", "--config", str(plan_file)]) == 1
    output = capsys.readouterr()
    assert "PRIVATE_TRANSPORT_DIAGNOSTIC" not in output.out + output.err
    assert closed == [True]


def test_cli_help_needs_no_authentication():
    import subprocess
    import sys

    result = subprocess.run([sys.executable, str(Path(smoke.__file__)), "--help"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "--profile" in result.stdout
