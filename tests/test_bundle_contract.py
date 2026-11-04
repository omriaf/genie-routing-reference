"""Exercise the merged customer bundle and its local deployment artifacts."""

import copy
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml

from setup.compile_config import compile_config

ROOT = Path(__file__).resolve().parents[1]


def test_static_router_infrastructure_is_available():
    assert (ROOT / "resources/router_app.app.yml").is_file()
    assert (ROOT / "resources/bootstrap.job.yml").is_file()
    assert (ROOT / "resources/router_dashboard.dashboard.yml").is_file()


@pytest.fixture
def bundle(tmp_path):
    checkout = tmp_path / "bundle"
    checkout.mkdir()
    for name in ("databricks.yml", "resources", "sql", "dashboards"):
        source = ROOT / name
        assert source.exists(), f"Missing bundle artifact: {name}"
        if source.is_dir():
            shutil.copytree(source, checkout / name)
        else:
            shutil.copy(source, checkout / name)
    config = yaml.safe_load((ROOT / "customer_config/customer.example.yml").read_text())
    config["workspace"].update(catalog="customer_b", router_schema="router_b",
                               warehouse_id="987abc", mlflow_experiment="/Shared/router-b")
    for function in config["uc_functions"]:
        function["full_name"] = function["full_name"].replace("acme_catalog", "customer_b")
    config_path = checkout / "customer.yml"
    config_path.write_text(yaml.safe_dump(config))
    compile_config(config_path, checkout / ".generated", False)
    root = yaml.safe_load((checkout / "databricks.yml").read_text())
    documents = [(checkout / "databricks.yml", root)]
    for pattern in root.get("include", []):
        paths = sorted(checkout.glob(pattern))
        assert paths, f"Include does not resolve: {pattern}"
        documents.extend((path, yaml.safe_load(path.read_text())) for path in paths)
    return checkout, config, documents


def bundle_document(documents):
    """Combine root includes, rejecting duplicate declarations like the CLI."""
    result = copy.deepcopy(documents[0][1])
    resources = result.setdefault("resources", {})
    for _, document in documents[1:]:
        for kind, declarations in document["resources"].items():
            target = resources.setdefault(kind, {})
            assert not target.keys() & declarations.keys(), "Duplicate root resource declarations"
            target.update(copy.deepcopy(declarations))
    return result


def test_bundle_has_no_implicit_workspace_or_demo(bundle):
    _, _, documents = bundle
    root = documents[0][1]
    assert "customer" in root["targets"]
    assert not any(target.get("default") for target in root["targets"].values())
    assert ".generated/customer.resources.yml" in root["include"]
    for path, document in documents:
        if path.name != "databricks.yml":
            assert set(document) == {"resources"}
        assert not re.search(r"(?m)^\s*(host|profile):", yaml.safe_dump(document))
    assert not any("demo" in include for include in root["include"])
    assert all("default" not in variable for variable in root.get("variables", {}).values())


def test_bundle_paths_resolve_relative_to_declaring_file(bundle):
    _, _, documents = bundle
    found = set()

    def check(value, parent):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"source_code_path", "file_path", "notebook_path"}:
                    assert (parent / child).exists(), (parent, child)
                    found.add(key)
                check(child, parent)
        elif isinstance(value, list):
            for child in value:
                check(child, parent)

    for path, document in documents:
        check(document, path.parent)
    assert found == {"source_code_path", "file_path", "notebook_path"}


def test_compiled_customer_settings_drive_all_router_resources(bundle):
    checkout, config, documents = bundle
    merged = bundle_document(documents)
    resources = merged["resources"]
    assert set(resources) == {"apps", "jobs", "dashboards", "experiments"}
    app = resources["apps"]["router_app"]
    assert app["name"] == config["customer"]["app_name"]
    bindings = {item["name"]: item for item in app["resources"]}
    assert bindings["sql_warehouse"]["sql_warehouse"] == {"id": "987abc", "permission": "CAN_USE"}
    assert bindings["classifier_endpoint"]["serving_endpoint"]["permission"] == "CAN_QUERY"
    assert bindings["trace_table"]["uc_securable"] == {
        "securable_full_name": "customer_b.router_b.routing_traces",
        "securable_type": "TABLE", "permission": "MODIFY",
    }
    assert bindings["definitions_secret"]["secret"]["permission"] == "READ"
    assert {b["genie_space"]["space_id"] for b in bindings.values() if "genie_space" in b} == {
        agent["space_id"] for agent in config["genie_agents"]
    }
    assert all(b["genie_space"]["permission"] == "CAN_RUN"
               for b in bindings.values() if "genie_space" in b)
    assert {b["uc_securable"]["securable_full_name"] for b in bindings.values()
            if b.get("uc_securable", {}).get("securable_type") == "FUNCTION"} == {
        function["full_name"] for function in config["uc_functions"]
    }
    assert bindings["mlflow_experiment"]["experiment"] == {
        "experiment_id": "${resources.experiments.router_experiment.id}", "permission": "CAN_EDIT",
    }
    assert resources["experiments"]["router_experiment"]["name"] == "/Shared/router-b"
    dashboard = resources["dashboards"]["router_dashboard"]
    assert (dashboard["dataset_catalog"], dashboard["dataset_schema"], dashboard["warehouse_id"]) == (
        "customer_b", "router_b", "987abc",
    )
    assert dashboard["embed_credentials"] is False
    tasks = {task["task_key"]: task for task in resources["jobs"]["router_bootstrap"]["tasks"]}
    bootstrap = tasks["bootstrap"]["notebook_task"]
    assert bootstrap["warehouse_id"] == "987abc"
    assert bootstrap["base_parameters"] == {"catalog": "customer_b", "schema": "router_b"}
    grants = [task["notebook_task"]["base_parameters"] for key, task in tasks.items() if key != "bootstrap"]
    assert {p["principal"] for p in grants} == {
        "${resources.apps.router_app.service_principal_client_id}", "analyst@example.com",
    }
    assert {p["schema"] for p in grants} == {"router_b", "metrics"}
    assert "analytics-users" not in {p["principal"] for p in grants}
    assert (checkout / ".generated/app_source/runtime_config.json").exists()


def test_dashboard_queries_are_portable_and_match_trace_schema(bundle):
    checkout, _, _ = bundle
    from app.tracing import TRACE_COLUMNS

    dashboard = json.loads((checkout / "dashboards/router_performance.lvdash.json").read_text())
    assert dashboard["datasets"] and dashboard["pages"]
    for dataset in dashboard["datasets"]:
        query = "".join(dataset["queryLines"])
        assert re.search(r"FROM\s+`?routing_traces`?(?:\s|$)", query, re.IGNORECASE)
        assert "acme" not in query and "${" not in query
    assert all(page["layoutVersion"] == "GRID_V1" for page in dashboard["pages"])
    assert "latency_ms" in TRACE_COLUMNS


def test_resources_conform_to_installed_cli_schema_without_credentials(bundle):
    executable = shutil.which("databricks")
    if executable is None:
        pytest.skip("CLI unavailable; YAML/path contracts still run locally")
    import jsonschema

    completed = subprocess.run([executable, "bundle", "schema"], check=True,
                               capture_output=True, text=True, timeout=30)
    schema = json.loads(completed.stdout)

    def python_patterns(value):
        # The CLI emits Go Unicode classes, which Python's regex engine cannot
        # parse. Resource identifiers in these fixtures are deliberately ASCII.
        if isinstance(value, dict):
            if "pattern" in value:
                value["pattern"] = (value["pattern"].replace(r"\p{L}+", "[A-Za-z]+")
                                    .replace(r"\p{L}", "A-Za-z").replace(r"\p{N}", "0-9"))
            for child in value.values():
                python_patterns(child)
        elif isinstance(value, list):
            for child in value:
                python_patterns(child)

    python_patterns(schema)
    jsonschema.Draft202012Validator(schema).validate(bundle_document(bundle[2]))


def test_bundle_command_requires_an_explicit_profile(monkeypatch, capsys):
    import importlib

    if not (ROOT / "setup/bundle.py").exists():
        pytest.fail("The profile-required bundle entrypoint is missing")
    bundle_cli = importlib.import_module("setup.bundle")
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "ambient-profile-must-not-select-workspace")
    with pytest.raises(SystemExit) as error:
        bundle_cli.main(["validate"])
    assert error.value.code == 2
    assert "ambient-profile" not in str(capsys.readouterr())


def test_bundle_command_passes_only_explicit_profile_and_returns_cli_status(monkeypatch):
    import importlib

    if not (ROOT / "setup/bundle.py").exists():
        pytest.fail("The profile-required bundle entrypoint is missing")
    bundle_cli = importlib.import_module("setup.bundle")

    def run(command, **kwargs):
        assert command == ["databricks", "bundle", "validate", "--strict", "--target", "customer", "--profile", "selected"]
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(bundle_cli.subprocess, "run", run)
    assert bundle_cli.main(["validate", "--strict", "--profile", "selected"]) == 7


@pytest.mark.parametrize("filename,expected", [
    ("bootstrap.sql", ["`select`.`table`", "`select`.`table`.`routing_traces`"]),
])
def test_sql_identifier_expressions_quote_names_and_escape_principals(filename, expected):
    # SQLite evaluates the same concatenation/replace expressions with bound
    # values. The emitted identifier strings are the contract, independent of
    # Databricks' remote DDL execution.
    source = (ROOT / "sql" / filename).read_text()
    expressions = re.findall(r"IDENTIFIER\(((?:[^()]|\([^()]*\))*)\)", source)
    with sqlite3.connect(":memory:") as database:
        actual = [database.execute("SELECT " + expression, {
            "catalog": "select", "schema": "table", "principal": "user`name@example.com",
        }).fetchone()[0] for expression in expressions]
    assert actual == expected


@pytest.mark.parametrize("filename,expected", [
    ("schema_access.sql", [
        "GRANT USE CATALOG ON CATALOG `select` TO `user``name@example.com`",
        "GRANT USE SCHEMA ON SCHEMA `select`.`table` TO `user``name@example.com`",
    ]),
    ("dashboard_access.sql", [
        "GRANT USE CATALOG ON CATALOG `select` TO `user``name@example.com`",
        "GRANT USE SCHEMA ON SCHEMA `select`.`table` TO `user``name@example.com`",
        "GRANT SELECT ON TABLE `select`.`table`.`routing_traces` TO `user``name@example.com`",
    ]),
])
def test_grants_render_only_scoped_privileges_and_quote_all_identifiers(filename, expected):
    source = (ROOT / "sql" / filename).read_text()
    expressions = re.findall(r"SET VAR grant_statement\s*=\s*(.*?);", source, re.DOTALL)
    with sqlite3.connect(":memory:") as database:
        actual = [database.execute("SELECT " + expression, {
            "catalog": "select", "schema": "table", "principal": "user`name@example.com",
        }).fetchone()[0] for expression in expressions]
    assert actual == expected
