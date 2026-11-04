"""Synthetic runtime fixtures and a workspace transport with no credentials."""

import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def runtime_document():
    return json.loads(
        (Path(__file__).parent / "snapshots/compiled_runtime.json").read_text()
    )


@pytest.fixture
def modules():
    class Modules:
        def __getattr__(self, name):
            try:
                return importlib.import_module(f"app.{name}")
            except ModuleNotFoundError as error:
                if error.name in {"app", f"app.{name}"}:
                    pytest.fail(f"Missing runtime module: app.{name}")
                raise
    return Modules()


@pytest.fixture
def install_runtime(modules, tmp_path, monkeypatch, workspace):
    def install(document):
        path = tmp_path / "runtime.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        monkeypatch.setenv("ROUTER_CONFIG_PATH", str(path))
        modules.runtime_config._load_runtime_config.cache_clear()
        config = modules.runtime_config.get_runtime_config()
        monkeypatch.setattr(modules.config, "get_client", lambda: workspace)
        return config

    yield install
    if "app.runtime_config" in sys.modules:
        modules.runtime_config._load_runtime_config.cache_clear()


def model_decision(domain="sales", **updates):
    payload = {
        "domain": domain,
        "confidence": 0.9,
        "candidates": {"sales": 0.9, "support": 0.1},
        "reasoning": "The question asks about the configured domain.",
        "certified_function": None,
        "function_arguments": {},
        "business_term": None,
        "undefined_terms": [],
    }
    payload.update(updates)
    return payload


class Workspace:
    def __init__(self):
        self.calls = []
        self.decision = model_decision()
        self.statement = {
            "statement_id": "test-statement",
            "status": {"state": "SUCCEEDED"},
            "manifest": {"schema": {"columns": [{"name": "value"}]}},
            "result": {"data_array": [["42"]]},
        }
        self.message = SimpleNamespace(
            status="COMPLETED", conversation_id="test-conversation", id="test-message",
            attachments=[SimpleNamespace(
                text=SimpleNamespace(content="Configured Genie answer"), query=None,
            )],
        )
        self.api_client = SimpleNamespace(do=self.do)
        self.genie = SimpleNamespace(
            start_conversation_and_wait=self.start_conversation,
            get_message_attachment_query_result=self.query_result,
        )
        self.query_response = None

    def do(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body))
        if path.startswith("/serving-endpoints/"):
            content = self.decision if isinstance(self.decision, str) else json.dumps(self.decision)
            return {"choices": [{"message": {"content": content}}]}
        if path.startswith("/api/2.0/sql/statements"):
            return self.statement
        raise AssertionError(f"Unexpected SDK request: {path}")

    def start_conversation(self, **kwargs):
        self.calls.append(("genie", kwargs["space_id"], kwargs))
        return self.message

    def query_result(self, *args):
        self.calls.append(("genie_result", args, None))
        return self.query_response


@pytest.fixture
def workspace():
    return Workspace()
