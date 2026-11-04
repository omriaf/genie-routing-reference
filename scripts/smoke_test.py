"""Explicit-profile live smoke checks. Import and --help are entirely offline.

Run only after approved deployment, using the private plan in validation/README.md.
This tool submits analytics requests and reads their Delta/MLflow evidence. It
never deploys, lists profiles, reads GitHub secrets, or changes definitions.
"""

import argparse
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

# Support the documented direct-script command from an uninstalled source clone.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import Field, model_validator

from app.runtime_config import RuntimeConfig, strict_json
from setup.models import NonBlank, StrictConfigModel


class SmokeFailure(Exception):
    """Failures expose fixed check names only, never remote data or diagnostics."""


def require(condition, check):
    if not condition:
        raise SmokeFailure(check)


class Case(StrictConfigModel):
    kind: Literal["genie", "function", "fail_loud", "git_enriched"]
    question: NonBlank
    domain: str | None = None
    function: str | None = None
    definition_term: str | None = None
    refusal_status: Literal["no_route", "undefined_term"] | None = None
    expected_rows: list[list[object]] | None = None


class Plan(StrictConfigModel):
    app_url: NonBlank
    runtime_config: NonBlank
    definitions_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    cases: list[Case] = Field(min_length=4)

    @model_validator(mode="after")
    def valid_app_url(self):
        url = urlsplit(self.app_url)
        if (url.scheme != "https" or not url.hostname
                or not url.hostname.endswith(".databricksapps.com")
                or url.username or url.password or url.port not in {None, 443}
                or url.path not in {"", "/"} or url.query or url.fragment
                or any(character.isspace() for character in self.app_url)):
            raise ValueError("Use the HTTPS App origin")
        return self


def load_plan(path: Path) -> tuple[Plan, RuntimeConfig]:
    plan = Plan.model_validate(strict_json(path.read_text(encoding="utf-8")))
    runtime_path = path.parent / plan.runtime_config
    runtime_text = runtime_path.read_text(encoding="utf-8")
    strict_json(runtime_text)
    runtime = RuntimeConfig.model_validate_json(runtime_text)
    require({case.kind for case in plan.cases} == {"genie", "function", "fail_loud", "git_enriched"},
            "all four smoke scenarios are required")
    require({case.domain for case in plan.cases if case.kind == "genie"}
            == set(runtime.agents_by_name), "a Genie case is required for each configured domain")
    for case in plan.cases:
        if case.kind == "fail_loud":
            require(case.refusal_status is not None and case.function is None,
                    "explicit refusal expectation")
            continue
        require(case.domain in runtime.agents_by_name, "configured smoke domain")
        if case.kind == "genie":
            require(case.function is None, "Genie scenario must not nominate a function")
        if case.kind == "function":
            require(case.function is not None, "certified function scenario")
        if case.function is not None:
            function = runtime.functions_by_name.get(case.function)
            require(function is not None and function.domain == case.domain, "allowlisted smoke function")
            if runtime.routing.prefer_certified_functions:
                require(case.expected_rows is not None, "known certified function result")
        if case.kind == "git_enriched":
            require(bool(case.definition_term), "explicit governed definition term")
    return plan, runtime


def check_health(health: dict, runtime: RuntimeConfig, revision: str, *, initial=False):
    require(health.get("ready") is True and health.get("status") == "ready", "App health")
    require(health.get("config_fingerprint") == runtime.fingerprint, "health configuration fingerprint")
    definitions = health.get("definitions", {})
    require(definitions.get("ready") is True and definitions.get("status") == "ready"
            and definitions.get("revision") == revision
            and type(definitions.get("definition_count")) is int
            and definitions["definition_count"] > 0, "definitions health and pinned revision")
    for sink in ("delta", "mlflow"):
        state = health.get("tracing", {}).get(sink, {})
        allowed = {"ready", "not_ready"} if initial and sink == "delta" else {"ready"}
        require(state.get("status") in allowed and state.get("error") is None, "trace sink health")


def check_answer(answer: dict, case: Case, runtime: RuntimeConfig, revision: str):
    require(answer.get("question") == case.question, "verbatim question")
    try:
        require(str(UUID(answer["trace_id"])) == answer["trace_id"], "request trace ID")
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SmokeFailure("request trace ID") from None
    require(answer.get("config_fingerprint") == runtime.fingerprint, "answer configuration fingerprint")
    require(answer.get("defs_commit_sha") == revision, "answer Git revision")
    require(isinstance(answer.get("answer"), str) and answer["answer"].strip(), "nonempty answer")
    require(not answer.get("error") and not answer.get("failed_route"), "analytics execution")
    if case.kind == "fail_loud":
        require(answer.get("route") == "fail_loud" and answer.get("status") == case.refusal_status,
                "loud refusal")
        require(not answer.get("uc_function") and not answer.get("genie_space_id"), "refusal isolation")
        return
    route = "uc_function" if case.function and runtime.routing.prefer_certified_functions else "genie"
    require(answer.get("status") == "answered" and answer.get("route") == route, "expected route")
    require(answer.get("domain") == case.domain, "expected domain")
    require(answer.get("genie_space_id") == runtime.agents_by_name[case.domain].space_id, "configured Genie ID")
    if route == "uc_function":
        require(answer.get("uc_function") == runtime.functions_by_name[case.function].full_name,
                "certified function identity")
        require(answer.get("rows") == case.expected_rows, "certified function result")
    else:
        require(not answer.get("uc_function"), "function disabled or unmatched")
        require(bool(answer.get("conversation_id")) and bool(answer.get("message_id")), "Genie evidence")
    if case.kind == "git_enriched":
        require(answer.get("matched_definition_term") == case.definition_term
                and answer.get("enrichment_method") == "exact_alias", "Git enrichment")


EVIDENCE_FIELDS = (
    "trace_id", "question", "status", "domain", "route", "config_fingerprint", "defs_commit_sha",
    "matched_definition_term", "enrichment_method", "uc_function",
)


def check_delta(row: dict, answer: dict):
    require(all(row.get(key) == answer.get(key) for key in EVIDENCE_FIELDS), "Delta request provenance")
    require(row.get("genie_space") == answer.get("genie_space_id"), "Delta Genie identity")
    require(row.get("fail_loud") is (answer["route"] == "fail_loud"), "Delta refusal evidence")
    require(row.get("git_definition_used") is bool(answer.get("matched_definition_term")), "Delta Git evidence")
    require(row.get("mlflow_trace_ok") is True, "Delta MLflow persistence status")
    require(type(row.get("latency_ms")) is int and row["latency_ms"] >= 0, "Delta request latency")


def check_mlflow(trace, answer: dict, row: dict):
    require(all(trace.info.tags.get(key) == str(answer.get(key)) for key in (
        "trace_id", "route", "domain", "config_fingerprint", "defs_commit_sha",
    )), "MLflow request tags")
    spans = [span for span in trace.data.spans if span.name == "ask_analytics"]
    require(len(spans) == 1, "MLflow analytics span")
    span = spans[0]
    require(span.inputs == {"question": answer["question"]}, "MLflow question")
    require(isinstance(span.outputs, dict) and all(
        span.outputs.get(key) == answer.get(key) for key in (*EVIDENCE_FIELDS, "answer")
    ), "MLflow response provenance")
    require(span.end_time_ns - span.start_time_ns == row["latency_ms"] * 1_000_000,
            "MLflow request latency")


class LiveBackend:
    """Only constructed after argument/plan validation; credentials stay in memory."""

    def __init__(self, profile: str, plan: Plan, runtime: RuntimeConfig):
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", profile), "explicit profile")
        # Unified auth merges environment fields before profile fields. Fail on
        # competing auth inputs rather than silently contacting another host.
        require(not AUTH_ENVIRONMENT_OVERRIDES.intersection(os.environ),
                "remove ambient auth overrides before using the selected profile")
        import httpx
        from databricks.sdk import WorkspaceClient
        from mlflow.tracking import MlflowClient

        self.workspace = WorkspaceClient(profile=profile)
        self.mlflow = MlflowClient(tracking_uri=f"databricks://{profile}")
        self.origin = plan.app_url.rstrip("/")
        self.runtime = runtime
        experiment = self.mlflow.get_experiment_by_name(runtime.workspace.mlflow_experiment)
        require(experiment is not None, "configured MLflow experiment")
        self.experiment_id = experiment.experiment_id
        self.http = httpx.Client(timeout=120, follow_redirects=False, trust_env=False)

    def request(self, method, path, **kwargs):
        response = self.http.request(method, self.origin + path,
                                     headers=self.workspace.config.authenticate(), **kwargs)
        require(response.status_code == 200, "authenticated App HTTP response")
        return response.json()

    def health(self):
        return self.request("GET", "/health")

    def rpc(self, method, params):
        response = self.http.post(
            self.origin + "/mcp", headers={**self.workspace.config.authenticate(),
                                            "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        require(response.status_code == 200, "MCP HTTP response")
        document = response.json()
        require(not document.get("error") and isinstance(document.get("result"), dict), "MCP RPC response")
        return document["result"]

    def discover(self):
        self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                "clientInfo": {"name": "routing-smoke", "version": "1"}})
        tools = self.rpc("tools/list", {}).get("tools", [])
        require([tool.get("name") for tool in tools] == ["ask_analytics"], "analytics tool discovery")

    def ask(self, question):
        result = self.rpc("tools/call", {"name": "ask_analytics", "arguments": {"question": question}})
        require(not result.get("isError"), "MCP analytics tool")
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        return strict_json(result["content"][0]["text"])

    def delta(self, trace_id):
        # All identifiers were validated by RuntimeConfig; values are bound.
        fields = (*EVIDENCE_FIELDS, "genie_space", "fail_loud", "git_definition_used",
                  "mlflow_trace_ok", "latency_ms")
        table = f"`{self.runtime.workspace.catalog}`.`{self.runtime.workspace.router_schema}`.routing_traces"
        response = self.workspace.api_client.do("POST", "/api/2.0/sql/statements", body={
            "warehouse_id": self.runtime.workspace.warehouse_id,
            "statement": f"SELECT to_json(struct({', '.join(fields)})) FROM {table} WHERE trace_id = :trace_id LIMIT 2",
            "parameters": [{"name": "trace_id", "type": "STRING", "value": trace_id}],
            "wait_timeout": "5s", "on_wait_timeout": "CONTINUE",
        })
        for _ in range(30):
            if response.get("status", {}).get("state") not in {"PENDING", "RUNNING"}:
                break
            statement_id = response.get("statement_id", "")
            require(re.fullmatch(r"[A-Za-z0-9_-]+", statement_id), "SQL statement ID")
            time.sleep(1)
            response = self.workspace.api_client.do("GET", f"/api/2.0/sql/statements/{statement_id}")
        require(response.get("status", {}).get("state") == "SUCCEEDED", "Delta evidence query")
        rows = response.get("result", {}).get("data_array", [])
        require(len(rows) == 1, "unique persisted Delta request")
        return strict_json(rows[0][0])

    def trace(self, trace_id):
        # UUID validation precedes construction of the MLflow filter.
        traces = self.mlflow.search_traces(experiment_ids=[self.experiment_id],
                                          filter_string=f"tags.trace_id = '{trace_id}'",
                                          max_results=2, include_spans=True)
        require(len(traces) == 1, "unique persisted MLflow request")
        return traces[0]

    def close(self):
        self.http.close()


AUTH_ENVIRONMENT_OVERRIDES = {
    "DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET",
    "DATABRICKS_AUTH_TYPE", "DATABRICKS_ACCOUNT_ID", "DATABRICKS_AZURE_RESOURCE_ID",
}


def run_checks(backend, plan: Plan, runtime: RuntimeConfig):
    check_health(backend.health(), runtime, plan.definitions_revision, initial=True)
    backend.discover()
    seen = set()
    for case in plan.cases:
        answer = backend.ask(case.question)
        check_answer(answer, case, runtime, plan.definitions_revision)
        require(answer["trace_id"] not in seen, "distinct request trace IDs")
        seen.add(answer["trace_id"])
        row = backend.delta(answer["trace_id"])
        check_delta(row, answer)
        check_mlflow(backend.trace(answer["trace_id"]), answer, row)
        check_health(backend.health(), runtime, plan.definitions_revision)
        print(f"PASS: {case.kind} route, Delta, MLflow and health")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid smoke arguments; use --help. An explicit --profile is required.\n")


def main(argv=None):
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--config", type=Path, required=True, help="ignored private smoke plan JSON")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.profile):
        parser.error("Invalid profile")
    backend = None
    previous_logging = logging.root.manager.disable
    # Vendor diagnostics can include private headers, query text or resource IDs.
    logging.disable(logging.CRITICAL)
    try:
        plan, runtime = load_plan(args.config)
        backend = LiveBackend(args.profile, plan, runtime)
        run_checks(backend, plan, runtime)
    except SmokeFailure as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    except Exception:
        print("FAIL: smoke configuration, transport or evidence; inspect privately.", file=sys.stderr)
        return 1
    finally:
        if backend is not None:
            backend.close()
        logging.disable(previous_logging)
    print("PASS: automated smoke checks; dashboard reader access and Git refresh still require manual verification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
