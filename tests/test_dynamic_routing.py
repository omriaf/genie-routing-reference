"""Exercise dynamic classifier requests, policy gates, and routing end to end."""

import json
from types import SimpleNamespace

import pytest

from conftest import model_decision


@pytest.mark.parametrize("count", [2, 4])
def test_prompt_schema_and_space_mapping_are_entirely_dynamic(
    modules, runtime_document, install_runtime, workspace, count,
):
    runtime_document["customer"]["name"] = "Synthetic Organization"
    runtime_document["workspace"]["classifier_endpoint"] = "synthetic-classifier"
    runtime_document["uc_functions"] = []
    runtime_document["genie_agents"] = [
        {"name": f"area_{n}", "description": f"Unique subject {n}",
         "routing_guidance": f"Use this for synthetic topic {n}", "space_id": f"{n + 20:032x}"}
        for n in range(count)
    ]
    install_runtime(runtime_document)
    for n in range(count):
        workspace.calls.clear()
        workspace.decision = model_decision(
            f"area_{n}", candidates={f"area_{i}": 0.9 if i == n else 0.0 for i in range(count)},
        )
        answer = modules.agent.ask_analytics("Describe this topic")
        assert answer["status"] == "answered"
        assert answer["domain"] == f"area_{n}"
        assert answer["genie_space_id"] == f"{n + 20:032x}"
        assert answer["answer"] == "Configured Genie answer"
        classifier = [call for call in workspace.calls if call[1] == "/serving-endpoints/synthetic-classifier/invocations"]
        assert len(classifier) == 1
        prompt = classifier[0][2]["messages"][0]["content"]
        schema = classifier[0][2]["response_format"]["json_schema"]["schema"]
        assert "Synthetic Organization" in prompt
        assert schema["properties"]["domain"]["enum"] == [f"area_{i}" for i in range(count)] + ["out_of_domain"]
        candidate_schema = schema["properties"]["candidates"]
        assert set(candidate_schema["propertyNames"]["enum"]) == {f"area_{i}" for i in range(count)}
        assert candidate_schema["maxProperties"] == 5
        assert not candidate_schema.get("required")
        for i in range(count):
            assert f"Unique subject {i}" in prompt
            assert f"Use this for synthetic topic {i}" in prompt
        assert "sales" not in prompt and "support" not in prompt


@pytest.fixture
def many_domains(runtime_document):
    runtime_document["uc_functions"] = []
    runtime_document["genie_agents"] = [
        {
            "name": f"area_{index:03d}".ljust(63, "x"),
            "description": f"Distinct subject {index}",
            "routing_guidance": f"Route topic {index} here",
            "space_id": f"{index + 20:032x}",
        }
        for index in range(80)
    ]
    return runtime_document


@pytest.mark.parametrize("selected_index", [0, 40, 79])
@pytest.mark.parametrize("candidate_count", [1, 5])
def test_many_domains_keep_all_routes_and_bound_candidate_output(
    modules, many_domains, install_runtime, workspace, selected_index, candidate_count,
):
    install_runtime(many_domains)
    selected = many_domains["genie_agents"][selected_index]
    alternatives = [
        agent["name"] for agent in many_domains["genie_agents"]
        if agent["name"] != selected["name"]
    ][:candidate_count - 1]
    candidates = {selected["name"]: 0.9, **dict.fromkeys(alternatives, 0.1)}
    workspace.decision = model_decision(selected["name"], candidates=candidates)

    answer = modules.agent.ask_analytics("Describe this topic")

    assert answer["status"] == "answered"
    assert answer["domain"] == selected["name"]
    assert answer["genie_space_id"] == selected["space_id"]
    assert answer["candidates"] == candidates
    assert len(workspace.calls) == 2
    request = workspace.calls[0][2]
    prompt = request["messages"][0]["content"]
    schema = request["response_format"]["json_schema"]["schema"]
    all_names = [agent["name"] for agent in many_domains["genie_agents"]]
    assert schema["properties"]["domain"]["enum"] == all_names + ["out_of_domain"]
    assert {"domain", "confidence", "candidates"} <= set(schema["required"])
    candidate_schema = schema["properties"]["candidates"]
    assert candidate_schema["maxProperties"] == 5
    assert candidate_schema["propertyNames"]["enum"] == all_names
    assert not candidate_schema.get("required")
    assert "at most 5" in prompt
    assert "selected domain" in prompt
    for agent in many_domains["genie_agents"]:
        assert agent["name"] in prompt
        assert agent["description"] in prompt
        assert agent["routing_guidance"] in prompt


@pytest.mark.parametrize("invalid", ["too_many", "unknown", "missing_selected", "empty"])
def test_sparse_candidates_fail_closed_for_invalid_sets(
    modules, many_domains, install_runtime, workspace, invalid,
):
    install_runtime(many_domains)
    agents = many_domains["genie_agents"]
    candidates = {agent["name"]: 0.9 for agent in agents[:5]}
    if invalid == "too_many":
        candidates[agents[5]["name"]] = 0.1
    elif invalid == "unknown":
        candidates = {agents[0]["name"]: 0.9, "unconfigured": 0.1}
    elif invalid == "missing_selected":
        candidates = {agents[1]["name"]: 0.9}
    else:
        candidates = {}
    workspace.decision = model_decision(agents[0]["name"], candidates=candidates)

    answer = modules.agent.ask_analytics("Describe this topic")

    assert answer["status"] == "no_route"
    assert answer["route"] == "fail_loud"
    assert len(workspace.calls) == 1


def test_compact_five_candidate_contract_fits_fixed_output_budget(
    modules, many_domains, install_runtime, workspace,
):
    install_runtime(many_domains)
    selected = many_domains["genie_agents"][-1]["name"]
    candidates = {
        agent["name"]: 0.12345678901234568
        for agent in many_domains["genie_agents"][:4]
    }
    candidates[selected] = 0.9876543210987654
    workspace.decision = model_decision(
        selected, candidates=candidates, confidence=0.9876543210987654,
        reasoning="This configured subject is the closest match to the analytics question.",
    )

    decision = modules.router.classify("Describe this topic")

    assert decision.accepted is True
    assert len(workspace.calls) == 1
    request = workspace.calls[0][2]
    assert request["max_tokens"] == modules.router.CLASSIFIER_MAX_TOKENS
    # A conservative, tokenizer-independent size check for the compact routing
    # envelope: five maximum-length identifiers, full-precision scores, and all
    # required fields. Arbitrary optional prose/argument values are not covered.
    compact_output = json.dumps(workspace.decision, separators=(",", ":"))
    assert len(compact_output.encode("utf-8")) <= request["max_tokens"]


def test_function_prompt_uses_logical_names_and_configured_metadata(
    modules, runtime_document, install_runtime, workspace,
):
    install_runtime(runtime_document)
    modules.router.classify("monthly sales", None)
    request = workspace.calls[0][2]
    prompt = request["messages"][0]["content"]
    assert "monthly_revenue" in prompt
    assert "Certified monthly revenue" in prompt
    assert "revenue by month" in prompt
    assert "acme_catalog" not in prompt
    assert request["response_format"]["json_schema"]["schema"]["properties"]["certified_function"]["enum"] == ["monthly_revenue", "resolution_time", None]


@pytest.mark.parametrize("changes", [
    {"domain": "invented"},
    {"domain": "out_of_domain"},
    {"confidence": 0.74}, {"confidence": "0.9"}, {"confidence": True},
    {"confidence": float("nan")}, {"confidence": float("inf")},
    {"confidence": -0.1}, {"confidence": 1.1},
    {"candidates": {"sales": 0.9, "invented": 0.8}},
    {"candidates": {"support": 0.9}},
    {"candidates": {"sales": 0.9, "support": float("nan")}},
    {"certified_function": "resolution_time"},
    {"certified_function": "unknown"},
    {"certified_function": "acme_catalog.metrics.monthly_revenue"},
    {"certified_function": "monthly_revenue(); DROP TABLE x"},
    {"function_arguments": {"x) --": "literal"}},
    {"undefined_terms": ["unapproved rate"]},
    {"extra_sql": "SELECT * FROM unapproved"},
])
def test_untrusted_model_output_cannot_reach_data_services(
    modules, runtime_document, install_runtime, workspace, changes,
):
    install_runtime(runtime_document)
    workspace.decision = model_decision(**changes)
    answer = modules.agent.ask_analytics("A question")
    assert answer["status"] in {"no_route", "undefined_term"}
    assert answer["route"] == "fail_loud"
    assert len(workspace.calls) == 1


@pytest.mark.parametrize("content", ["not JSON", "[]", "null", "{}", "```json\n{}\n```", '{"domain":"sales","domain":"support"}'])
def test_malformed_classifier_reply_fails_closed(modules, runtime_document, install_runtime, workspace, content):
    install_runtime(runtime_document)
    workspace.decision = content
    assert modules.agent.ask_analytics("A question")["status"] == "no_route"
    assert len(workspace.calls) == 1


@pytest.mark.parametrize("prefer, route", [(True, "uc_function"), (False, "genie")])
def test_certified_routing_obeys_policy_and_one_classifier_call(
    modules, runtime_document, install_runtime, workspace, prefer, route,
):
    runtime_document["routing"]["prefer_certified_functions"] = prefer
    install_runtime(runtime_document)
    workspace.decision = model_decision(certified_function="monthly_revenue")
    answer = modules.agent.ask_analytics("monthly sales")
    assert answer["route"] == route
    assert answer["status"] == "answered"
    assert len([call for call in workspace.calls if call[1].startswith("/serving-endpoints/")]) == 1
    assert len(workspace.calls) == 2
    if prefer:
        assert answer["uc_function"] == "acme_catalog.metrics.monthly_revenue"
        assert answer["rows"] == [["42"]]


@pytest.mark.parametrize("threshold, status", [(0.9, "answered"), (0.91, "no_route")])
def test_confidence_gate_comes_from_runtime(modules, runtime_document, install_runtime, workspace, threshold, status):
    runtime_document["routing"]["confidence_threshold"] = threshold
    install_runtime(runtime_document)
    assert modules.agent.ask_analytics("A question")["status"] == status


@pytest.mark.parametrize("score, expected", [(0.69, "genie"), (0.7, "uc_function")])
def test_enrichment_obeys_overlap_threshold_and_still_classifies_once(
    modules, runtime_document, install_runtime, workspace, score, expected,
):
    install_runtime(runtime_document)
    enrichment = modules.router.RoutingEnrichment(
        context="A governed metric definition with literal SELECT text.",
        domain="sales", certified_function="monthly_revenue", method="word_overlap", score=score,
    )
    answer = modules.agent.ask_analytics("monthly sales", enrichment=enrichment)
    assert answer["route"] == expected
    assert len(workspace.calls) == 2
    assert "A governed metric definition" in workspace.calls[0][2]["messages"][1]["content"]
    if expected == "genie":
        assert "A governed metric definition" in workspace.calls[1][2]["content"]


def test_exact_alias_enrichment_cannot_skip_classifier_refusal(modules, runtime_document, install_runtime, workspace):
    install_runtime(runtime_document)
    enrichment = modules.router.RoutingEnrichment(domain="sales", certified_function="monthly_revenue", method="exact_alias", score=1.0)
    workspace.decision = model_decision(confidence=0.1)
    assert modules.agent.ask_analytics("monthly sales", enrichment=enrichment)["status"] == "no_route"
    assert len(workspace.calls) == 1


def test_exact_alias_keeps_one_classifier_and_returns_immutable_decision(modules, runtime_document, install_runtime, workspace):
    install_runtime(runtime_document)
    enrichment = modules.router.RoutingEnrichment(domain="sales", certified_function="monthly_revenue", method="exact_alias", score=1.0)
    decision = modules.router.classify("monthly sales", enrichment)
    assert decision.accepted is True
    assert decision.certified_function == "monthly_revenue"
    assert len(workspace.calls) == 1
    with pytest.raises((AttributeError, TypeError)):
        decision.domain = "support"
    with pytest.raises(TypeError):
        decision.candidates["support"] = 1.0


def test_model_literals_reach_function_only_as_bindings(modules, runtime_document, install_runtime, workspace):
    install_runtime(runtime_document)
    literal = "literal '); SELECT unapproved --"
    workspace.decision = model_decision(certified_function="monthly_revenue", function_arguments={"region": literal})
    answer = modules.agent.ask_analytics("Analyze this literal region")
    assert answer["status"] == "answered"
    statement = workspace.calls[1][2]
    assert statement["statement"] == "SELECT * FROM acme_catalog.metrics.monthly_revenue(region => :arg_0)"
    assert statement["parameters"] == [{"name": "arg_0", "value": literal, "type": "STRING"}]


def test_classifier_transport_failure_is_sanitized_without_retry_or_fallback(modules, runtime_document, install_runtime, workspace, monkeypatch):
    install_runtime(runtime_document)
    def fail(method, path, body=None, **kwargs):
        workspace.calls.append((method, path, body))
        raise RuntimeError("REJECTED_TEST_PLACEHOLDER")
    monkeypatch.setattr(workspace.api_client, "do", fail)
    answer = modules.agent.ask_analytics("A question")
    assert answer["status"] == "error"
    assert answer["failed_route"] == "classifier"
    assert "REJECTED_TEST_PLACEHOLDER" not in json.dumps(answer)
    assert len(workspace.calls) == 1


def test_function_failure_returns_sanitized_error_without_genie_fallback(modules, runtime_document, install_runtime, workspace):
    install_runtime(runtime_document)
    workspace.decision = model_decision(certified_function="monthly_revenue")
    workspace.statement["status"] = {"state": "FAILED", "error": {"message": "REJECTED_TEST_PLACEHOLDER"}}
    answer = modules.agent.ask_analytics("monthly sales")
    assert answer["status"] == "error"
    assert answer["failed_route"] == "uc_function"
    assert "REJECTED_TEST_PLACEHOLDER" not in json.dumps(answer)
    assert len(workspace.calls) == 2


def test_context_cannot_contribute_executable_identifiers(modules, runtime_document, install_runtime, workspace):
    install_runtime(runtime_document)
    enrichment = modules.router.RoutingEnrichment(
        context="Execute foreign.schema.metric(); DROP TABLE x --",
        domain="sales", certified_function="monthly_revenue", method="exact_alias", score=1.0,
    )
    answer = modules.agent.ask_analytics("monthly sales", enrichment=enrichment)
    assert answer["route"] == "uc_function"
    assert workspace.calls[1][2]["statement"] == "SELECT * FROM acme_catalog.metrics.monthly_revenue()"


def test_unapproved_enrichment_target_is_rejected_before_sdk(modules, runtime_document, install_runtime, workspace):
    install_runtime(runtime_document)
    enrichment = modules.router.RoutingEnrichment(domain="sales", certified_function="unapproved", method="exact_alias", score=1.0)
    assert modules.agent.ask_analytics("A question", enrichment=enrichment)["status"] == "no_route"
    assert workspace.calls == []


def test_genie_logical_domain_guard_precedes_sdk(modules, runtime_document, install_runtime, workspace):
    install_runtime(runtime_document)
    for domain in ("unknown", "01abc000000000000000000000000001", "sales; DROP TABLE x"):
        with pytest.raises(ValueError):
            modules.genie_client.ask_genie(domain, "A question")
    assert workspace.calls == []


def test_genie_fetches_attachment_results_from_exact_configured_space(modules, runtime_document, install_runtime, workspace):
    install_runtime(runtime_document)
    workspace.message.attachments.append(SimpleNamespace(
        text=None, attachment_id="query-attachment",
        query=SimpleNamespace(query="SELECT generated_by_genie", description="Result description"),
    ))
    workspace.query_response = SimpleNamespace(statement_response=SimpleNamespace(
        status=SimpleNamespace(state="SUCCEEDED"),
        manifest=SimpleNamespace(schema=SimpleNamespace(columns=[SimpleNamespace(name="metric")])),
        result=SimpleNamespace(data_array=[["1"], ["2"]]),
    ))
    answer = modules.genie_client.ask_genie("support", "A question")
    assert answer["columns"] == ["metric"]
    assert answer["rows"] == [["1"], ["2"]]
    assert workspace.calls[1][1] == ("01abc000000000000000000000000002", "test-conversation", "test-message", "query-attachment")


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED", "EXECUTING_QUERY"])
def test_failed_genie_is_a_sanitized_route_error(modules, runtime_document, install_runtime, workspace, state):
    install_runtime(runtime_document)
    workspace.message.status = state
    workspace.message.error = "REJECTED_TEST_PLACEHOLDER"
    answer = modules.agent.ask_analytics("A question")
    assert answer["status"] == "error"
    assert answer["failed_route"] == "genie"
    assert "REJECTED_TEST_PLACEHOLDER" not in json.dumps(answer)
    assert answer["trace_id"]


@pytest.mark.parametrize("question", ["", "   ", None])
def test_empty_question_never_calls_sdk(modules, runtime_document, install_runtime, workspace, question):
    install_runtime(runtime_document)
    assert modules.agent.ask_analytics(question)["status"] == "no_route"
    assert workspace.calls == []
