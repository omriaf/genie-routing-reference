"""Per-request MLflow spans and parameterized Delta rows, with redacted health.

The bootstrap contract is <catalog>.<router_schema>.routing_traces. It must
provide the columns in TRACE_COLUMNS below, plus request_ts TIMESTAMP.
Imports do not access credentials, files, or services.
"""

import json
import logging
import re
import time
from threading import Lock

from app import config
from app.router import RoutingEnrichment
from app.runtime_config import RuntimeConfig

logger = logging.getLogger(__name__)
_logging_boundary_lock = Lock()
TRACE_COLUMNS = {
    "trace_id": "STRING", "question": "STRING", "status": "STRING",
    "domain": "STRING", "confidence": "DOUBLE", "candidates": "STRING",
    "route": "STRING", "genie_space": "STRING", "uc_function": "STRING",
    "reasoning": "STRING", "latency_ms": "BIGINT", "fail_loud": "BOOLEAN",
    "answer": "STRING", "error": "STRING", "failed_route": "STRING",
    "mlflow_trace_ok": "BOOLEAN", "trace_note": "STRING",
    "defs_commit_sha": "STRING", "git_definition_used": "BOOLEAN",
    "matched_definition_term": "STRING", "enrichment_method": "STRING",
    "answering_genie": "STRING", "config_fingerprint": "STRING",
}


def _protect_mlflow_logs() -> None:
    """Redact vendor diagnostics before any logger handler sees their contents.

    Exporters log from background workers too, including after a request ends.
    A persistent record factory covers those threads and future child loggers;
    filters on the parent logger or a temporary context do not. Only MLflow and
    OpenTelemetry diagnostics change; App logs keep their messages and context.
    """
    with _logging_boundary_lock:
        previous = logging.getLogRecordFactory()
        if getattr(previous, "_analytics_mlflow_boundary", False):
            return

        def make_record(name, level, pathname, lineno, msg, args, exc_info, func=None, sinfo=None, **kwargs):
            protected = name.split(".", 1)[0] in {"mlflow", "opentelemetry"}
            if protected:
                msg, args, exc_info, sinfo = "MLflow tracing diagnostic redacted", (), None, None
            record = previous(name, level, pathname, lineno, msg, args, exc_info, func, sinfo, **kwargs)
            if protected:
                record.exc_text = None
            return record

        make_record._analytics_mlflow_boundary = True
        logging.setLogRecordFactory(make_record)


def trace_record(result: dict, enrichment: RoutingEnrichment, latency_ms: int) -> dict:
    """Record the generation used by this request, never the latest Git state."""
    record = {key: result.get(key) for key in TRACE_COLUMNS}
    record.update(
        latency_ms=latency_ms,
        genie_space=result.get("genie_space_id"),
        fail_loud=result.get("route") == "fail_loud",
        defs_commit_sha=enrichment.definitions_revision,
        git_definition_used=enrichment.matched_term is not None,
        matched_definition_term=enrichment.matched_term,
        enrichment_method=enrichment.method,
        answering_genie=(
            {"space_id": result["genie_space_id"], "domain": result["domain"]}
            if result.get("route") == "genie" and result.get("status") == "answered" else None
        ),
    )
    return record


class TraceRecorder:
    def __init__(self, runtime: RuntimeConfig):
        self._runtime = runtime
        self._mlflow = None
        self._lock = Lock()
        self._sinks = {
            sink: {"status": "not_ready", "failures": 0, "error": None}
            for sink in ("delta", "mlflow")
        }

    def _success(self, sink):
        with self._lock:
            self._sinks[sink].update(status="ready", error=None)

    def _failure(self, sink):
        # External exceptions may contain tokens, SQL inputs, or HTTP headers.
        # Never log or propagate their diagnostics, even as exception context.
        message = f"{sink} tracing failed"
        logger.error(message)
        with self._lock:
            state = self._sinks[sink]
            state.update(status="degraded", failures=state["failures"] + 1, error=message)

    def health(self) -> dict:
        with self._lock:
            return {sink: dict(state) for sink, state in self._sinks.items()}

    def initialize(self) -> None:
        """Ambient Databricks credentials; no tracking profile or workspace default."""
        self._mlflow = None
        try:
            _protect_mlflow_logs()
            import mlflow

            mlflow.set_tracking_uri("databricks")
            mlflow.set_experiment(self._runtime.workspace.mlflow_experiment)
            self._mlflow = mlflow
            self._success("mlflow")
        except Exception:  # noqa: BLE001 - external diagnostics can contain credentials
            self._failure("mlflow")

    def record(self, result: dict, enrichment: RoutingEnrichment, latency_ms: int) -> dict:
        """Publish a completed request with its measured service latency.

        MLflow may suppress exporter exceptions internally. Read the completed
        trace back (flushing async writes) before declaring success, so a silent
        drop is reflected in both Delta and health. Delta remains independent.
        """
        record = trace_record(result, enrichment, latency_ms)
        mlflow_ok = False
        if self._mlflow is None:
            self.initialize()
        if self._mlflow is not None:
            try:
                _protect_mlflow_logs()
                end_time_ns = time.time_ns()
                span = self._mlflow.start_span_no_context(
                    name="ask_analytics", inputs={"question": result["question"]},
                    attributes={f"routing.{key}": value for key, value in record.items()},
                    tags={key: str(record[key]) for key in (
                        "trace_id", "route", "domain", "config_fingerprint", "defs_commit_sha",
                    )},
                    start_time_ns=end_time_ns - latency_ms * 1_000_000,
                )
                span.end(outputs=result, end_time_ns=end_time_ns)
                if self._mlflow.get_trace(span.trace_id, silent=True, flush=True) is None:
                    raise RuntimeError("MLflow trace was not persisted")
                mlflow_ok = True
                self._success("mlflow")
            except Exception:  # noqa: BLE001 - tracing cannot fail an analytics answer
                self._failure("mlflow")
        record.update(mlflow_trace_ok=mlflow_ok,
                      trace_note=None if mlflow_ok else "MLflow trace unavailable")
        try:
            self._write_delta(record)
            self._success("delta")
        except Exception:  # noqa: BLE001 - tracing cannot fail an analytics answer
            self._failure("delta")
        return record

    def _write_delta(self, record):
        table = f"`{self._runtime.workspace.catalog}`.`{self._runtime.workspace.router_schema}`.`routing_traces`"
        parameters = []
        for name, sql_type in TRACE_COLUMNS.items():
            value = record[name]
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, allow_nan=False)
            elif isinstance(value, bool):
                value = str(value).lower()
            elif value is not None:
                value = str(value)
            parameters.append({"name": name, "type": sql_type, "value": value})
        statement = (
            f"INSERT INTO {table} (request_ts, {', '.join(TRACE_COLUMNS)}) "
            f"VALUES (current_timestamp(), {', '.join(':' + key for key in TRACE_COLUMNS)})"
        )
        client = config.get_client()
        response = client.api_client.do("POST", "/api/2.0/sql/statements", body={
            "warehouse_id": self._runtime.workspace.warehouse_id,
            "statement": statement, "parameters": parameters,
            "wait_timeout": "5s", "on_wait_timeout": "CONTINUE",
        })
        for _ in range(20):
            if response.get("status", {}).get("state") not in {"PENDING", "RUNNING"}:
                break
            statement_id = response.get("statement_id")
            if not isinstance(statement_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", statement_id):
                raise RuntimeError("Invalid trace statement ID")
            time.sleep(0.5)
            response = client.api_client.do("GET", f"/api/2.0/sql/statements/{statement_id}")
        if response.get("status", {}).get("state") != "SUCCEEDED":
            raise RuntimeError("Delta trace write did not succeed")
