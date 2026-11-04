"""Only logical allowlist keys may contribute SQL identifiers."""

import pytest


@pytest.mark.parametrize("name", [
    "unknown", "monthly sales", "acme_catalog.metrics.monthly_revenue",
    "other_catalog.metrics.monthly_revenue", "acme_catalog.other_schema.monthly_revenue",
    "monthly_revenue()", "monthly_revenue; DROP TABLE x", "monthly_revenue -- comment",
    "monthly_revenue/*comment*/", "monthly_revenue\n", " monthly_revenue", "MONTHLY_REVENUE",
    "`monthly_revenue`", "SELECT * FROM monthly_revenue", "monthly_revenue' OR 1=1",
    None, [], {},
])
def test_rejects_non_logical_targets_before_any_sdk_call(modules, runtime_document, install_runtime, monkeypatch, name):
    install_runtime(runtime_document)
    def forbidden():
        pytest.fail("SDK client accessed for a rejected function target")
    monkeypatch.setattr(modules.config, "get_client", forbidden)
    with pytest.raises(ValueError):
        modules.uc_functions.execute_function(name, {})


def test_logical_name_resolves_exact_fqn_without_inferred_schema(modules, runtime_document, install_runtime, workspace):
    runtime_document["uc_functions"][0]["full_name"] = "acme_catalog.certified.Kpi_v2"
    install_runtime(runtime_document)
    result = modules.uc_functions.execute_function("monthly_revenue", {})
    assert result.function == "acme_catalog.certified.Kpi_v2"
    assert result.columns == ["value"]
    assert result.rows == [["42"]]
    assert workspace.calls == [("POST", "/api/2.0/sql/statements", {
        "warehouse_id": "abc123", "statement": "SELECT * FROM acme_catalog.certified.Kpi_v2()",
        "parameters": [], "wait_timeout": "50s", "on_wait_timeout": "CONTINUE",
        "row_limit": 50,
    })]


def test_values_are_bound_not_interpolated(modules, runtime_document, install_runtime, workspace):
    install_runtime(runtime_document)
    literal = "'); DROP TABLE important; -- /* inert literal */"
    modules.uc_functions.execute_function("monthly_revenue", {
        "text": literal, "count": 12, "fraction": 1.25, "active": True, "optional": None,
    })
    body = workspace.calls[0][2]
    assert body["statement"] == (
        "SELECT * FROM acme_catalog.metrics.monthly_revenue("
        "active => :arg_0, count => :arg_1, fraction => :arg_2, optional => :arg_3, text => :arg_4)"
    )
    assert body["parameters"] == [
        {"name": "arg_0", "value": "true", "type": "BOOLEAN"},
        {"name": "arg_1", "value": "12", "type": "BIGINT"},
        {"name": "arg_2", "value": "1.25", "type": "DOUBLE"},
        {"name": "arg_3", "value": None, "type": "STRING"},
        {"name": "arg_4", "value": literal, "type": "STRING"},
    ]
    assert literal not in body["statement"]


@pytest.mark.parametrize("arguments", [
    {"x); DROP TABLE x --": 1}, {"x.y": 1}, {"x /* comment */": 1}, {"x\n": 1},
    {"x": [1]}, {"x": {"sql": "SELECT 1"}}, {"x": float("nan")},
    {"x": float("inf")}, {"x": 2**63}, {"x": -(2**63)-1}, {1: "bad key"}, [], "SELECT 1",
])
def test_rejects_nonliteral_arguments_before_sdk(modules, runtime_document, install_runtime, monkeypatch, arguments):
    install_runtime(runtime_document)
    def forbidden():
        pytest.fail("SDK client accessed for rejected arguments")
    monkeypatch.setattr(modules.config, "get_client", forbidden)
    with pytest.raises(ValueError):
        modules.uc_functions.execute_function("monthly_revenue", arguments)


@pytest.mark.parametrize("state", ["FAILED", "CANCELED", "CLOSED", None])
def test_statement_failures_are_sanitized(modules, runtime_document, install_runtime, workspace, state):
    install_runtime(runtime_document)
    workspace.statement["status"] = {"state": state, "error": {"message": "REJECTED_TEST_PLACEHOLDER"}}
    with pytest.raises(RuntimeError) as error:
        modules.uc_functions.execute_function("monthly_revenue", {})
    assert "REJECTED_TEST_PLACEHOLDER" not in str(error.value)


def test_pending_statement_is_polled_without_reexecuting(modules, runtime_document, install_runtime, workspace, monkeypatch):
    install_runtime(runtime_document)
    requests = []
    original = workspace.do
    def pending_then_success(method, path, body=None, **kwargs):
        requests.append((method, path))
        response = original(method, path, body, **kwargs)
        if method == "POST":
            return {"statement_id": "test-statement", "status": {"state": "PENDING"}}
        return response
    monkeypatch.setattr(workspace.api_client, "do", pending_then_success)
    monkeypatch.setattr(modules.uc_functions.time, "sleep", lambda _: None)
    assert modules.uc_functions.execute_function("monthly_revenue", {}).rows == [["42"]]
    assert requests == [("POST", "/api/2.0/sql/statements"), ("GET", "/api/2.0/sql/statements/test-statement")]


def test_pending_statement_has_bounded_wait(modules, runtime_document, install_runtime, workspace, monkeypatch):
    install_runtime(runtime_document)
    workspace.statement["status"] = {"state": "RUNNING"}
    monkeypatch.setattr(modules.uc_functions.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="timed out"):
        modules.uc_functions.execute_function("monthly_revenue", {})
    assert len(workspace.calls) == 31
