"""Offline contracts for the shipped demo, through real package consumers."""

import importlib
import json
import os
import re
import runpy
import shutil
import sqlite3
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import yaml
from conftest import model_decision

from setup.compile_config import compile_config

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"


def demo_module(name):
    assert (DEMO / "generators" / f"{name}.py").is_file(), "Missing demo generator"
    return importlib.import_module(f"demo.generators.{name}")


@pytest.fixture
def prepared(tmp_path):
    demo_module("prepare")
    output = tmp_path / "prepared"
    result = subprocess.run([
        sys.executable, "-m", "demo.generators.prepare",
        "--catalog", "alternate_catalog", "--schema", "alternate_schema",
        "--warehouse-id", "warehouse123", "--classifier-endpoint", "sample-classifier",
        "--experiment-path", "/Shared/sample-router",
        "--repository-url", "https://github.com/example/sample-definitions",
        "--secret-scope", "sample-scope", "--secret-key", "definitions-token",
        "--parent-path", "/Workspace/Shared/sample-agents", "--output", str(output),
    ], check=False, cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return output


@pytest.fixture
def demo_config(prepared):
    document = yaml.safe_load((prepared / "demo.yml").read_text())
    # This is the installer's step, after create-space returns real IDs.
    for index, agent in enumerate(document["genie_agents"], 1):
        assert not re.fullmatch(r"[0-9a-f]{32}", agent["space_id"])
        agent["space_id"] = f"{index:032x}"
    path = prepared / "installed.yml"
    path.write_text(yaml.safe_dump(document))
    return path, document


def snapshot_for(runtime):
    from app.markdown_definitions import build_snapshot, parse_definition

    paths = sorted((DEMO / "definitions").glob("*.md"))
    assert paths, "Missing governed demo Markdown"
    definitions = []
    for path in paths:
        definitions.append(parse_definition(path.read_text(), "demo/definitions/" + path.name))
    return build_snapshot(definitions, runtime, "a" * 40)


def test_preparation_requires_installer_ids_and_parameterizes_all_imports(prepared, demo_config):
    from setup.models import load_customer_config

    with pytest.raises(ValueError):
        load_customer_config(prepared / "demo.yml")
    config = load_customer_config(demo_config[0])
    assert {agent.name for agent in config.genie_agents} == {"retail", "support", "marketing"}
    assert {function.full_name for function in config.uc_functions} == {
        "alternate_catalog.alternate_schema.monthly_revenue",
        "alternate_catalog.alternate_schema.resolution_hours",
        "alternate_catalog.alternate_schema.conversion_rate",
    }
    ids = set()
    for domain, table in [("retail", "orders"), ("support", "support_tickets"), ("marketing", "campaign_daily")]:
        payload = json.loads((prepared / f"{domain}.create.json").read_text())
        assert payload["warehouse_id"] == "warehouse123"
        assert payload["parent_path"] == "/Workspace/Shared/sample-agents"
        assert "space_id" not in payload
        space = json.loads(payload["serialized_space"])
        assert space["version"] == 2
        assert space["data_sources"]["tables"] == [{"identifier": f"alternate_catalog.alternate_schema.{table}"}]
        assert space["config"]["sample_questions"][0]["question"]
        for entries in [space["config"]["sample_questions"], *space["instructions"].values()]:
            assert entries == sorted(entries, key=lambda entry: entry["id"])
            for entry in entries:
                assert re.fullmatch(r"[0-9a-f]{32}", entry["id"])
                assert entry["id"] not in ids
                ids.add(entry["id"])
        sql = space["instructions"]["example_question_sqls"][0]["sql"][0]
        assert f"`alternate_catalog`.`alternate_schema`.`{table}`" in sql
    for path in prepared.iterdir():
        assert "{{" not in path.read_text()


def test_generated_rows_reconcile_across_three_domains_and_metrics_execute(prepared):
    generator = demo_module("generate")
    rows = generator.sample_rows()
    assert rows == generator.sample_rows()
    assert {name: len(records) for name, records in rows.items()} == {
        "orders": 360, "support_tickets": 90, "campaign_daily": 360,
    }
    order_ids = {record[0] for record in rows["orders"]}
    assert all(ticket[1] in order_ids for ticket in rows["support_tickets"])
    # Execute emitted table DDL and each actual SQL function body in SQLite.
    # The queries use common SQL; Databricks-specific DDL needs workspace validation.
    sql = (prepared / "seed.sql").read_text()
    connection = sqlite3.connect(":memory:")
    sql = sql.replace("`alternate_catalog`.`alternate_schema`.", "")
    statements = [statement.strip() for statement in sql.split(";") if statement.strip()]
    queries = {}
    for statement in statements:
        if statement.startswith("CREATE SCHEMA"):
            continue
        if statement.startswith("CREATE OR REPLACE FUNCTION"):
            name = re.search(r"FUNCTION `(\w+)`", statement)[1]
            queries[name] = statement.split("\nRETURN\n", 1)[1]
        else:
            connection.execute(statement.replace("CREATE OR REPLACE TABLE", "CREATE TABLE").replace(" USING DELTA", ""))
    revenue = connection.execute(queries["monthly_revenue"]).fetchall()
    assert [row[0] for row in revenue] == ["2025-01", "2025-02", "2025-03"]
    assert all(row[1] > 0 for row in revenue)
    assert connection.execute(queries["resolution_hours"]).fetchone()[0] > 0
    rates = dict(connection.execute(queries["conversion_rate"]).fetchall())
    assert 0 < rates["paid_social"] < rates["direct"] < 1
    assert connection.execute("SELECT SUM(purchases) FROM campaign_daily").fetchone()[0] == connection.execute(
        "SELECT COUNT(*) FROM orders WHERE status = 'completed'"
    ).fetchone()[0]
    assert {name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")} == set(rows)
    for domain in ("retail", "support", "marketing"):
        payload = json.loads((prepared / f"{domain}.create.json").read_text())
        space = json.loads(payload["serialized_space"])
        query = space["instructions"]["example_question_sqls"][0]["sql"][0]
        query = query.replace("`alternate_catalog`.`alternate_schema`.", "")
        result = connection.execute(query).fetchall()
        assert len(result) == (3 if domain == "support" else 4)
    # Hand-checked fixtures distinguish filters and weighted ratios from merely
    # plausible positive numbers. Run the shipped function bodies unchanged.
    connection.executescript("""
        DELETE FROM orders;
        INSERT INTO orders VALUES
          (1, '2025-01-01', '2025-01', 'direct', 40, 'completed'),
          (2, '2025-01-02', '2025-01', 'direct', 60, 'completed'),
          (3, '2025-01-03', '2025-01', 'direct', 900, 'cancelled');
        DELETE FROM support_tickets;
        INSERT INTO support_tickets VALUES
          (1, 1, 'low', 'resolved', 4), (2, 2, 'high', 'resolved', 8),
          (3, 3, 'normal', 'open', 100);
        DELETE FROM campaign_daily;
        INSERT INTO campaign_daily VALUES
          ('2025-01-01', 'direct', 10, 2, 0),
          ('2025-01-02', 'direct', 90, 3, 0);
    """)
    assert connection.execute(queries["monthly_revenue"]).fetchall() == [("2025-01", 100)]
    assert connection.execute(queries["resolution_hours"]).fetchall() == [(6.0,)]
    assert connection.execute(queries["conversion_rate"]).fetchall() == [("direct", 0.05)]


def test_demo_notebook_executes_parameterized_sql_with_job_widgets(monkeypatch):
    assert (DEMO / "generators/seed.py").is_file(), "Missing demo job notebook"
    parameters = {"catalog": "select", "schema": "table", "generators_dir": str(DEMO / "generators")}
    statements = []
    monkeypatch.syspath_prepend(str(DEMO / "generators"))
    runpy.run_path(str(DEMO / "generators/seed.py"), init_globals={
        "dbutils": SimpleNamespace(widgets=SimpleNamespace(text=lambda *args: None, get=parameters.__getitem__)),
        "spark": SimpleNamespace(sql=statements.append),
    })
    assert len(statements) == 10  # schema + three tables/inserts + three functions
    assert statements[0].strip() == "CREATE SCHEMA IF NOT EXISTS `select`.`table`"
    functions = [statement for statement in statements if "CREATE OR REPLACE FUNCTION" in statement]
    assert len(functions) == 3
    assert all("`select`.`table`." in statement for statement in functions)
    assert all("{{" not in statement for statement in statements)


def test_real_bundle_loader_accepts_shipped_demo_without_credentials(tmp_path, demo_config):
    executable = shutil.which("databricks")
    if executable is None:
        pytest.skip("Databricks CLI unavailable")
    for directory in ("sql", "resources", "dashboards", "demo"):
        shutil.copytree(ROOT / directory, tmp_path / directory)
    shutil.copyfile(ROOT / "databricks.yml", tmp_path / "databricks.yml")
    compile_config(demo_config[0], tmp_path / ".generated", True)
    credentials = tmp_path / "empty.cfg"
    credentials.write_text("")
    result = subprocess.run([
        executable, "bundle", "validate", "--strict", "--target", "customer", "--output", "json",
    ], check=False, cwd=tmp_path, env={"PATH": os.environ["PATH"], "DATABRICKS_CONFIG_FILE": str(credentials)},
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 or (result.returncode == 1 and "cannot configure default credentials" in result.stderr), result.stderr
    assert "Warning:" not in result.stderr
    resources = json.loads(result.stdout)["resources"]
    assert "demo_seed" in resources["jobs"]
    assert resources["dashboards"]["router_dashboard"]["dataset_catalog"] == "alternate_catalog"


def test_compile_mode_controls_actual_bundle_upload_set(tmp_path, demo_config, monkeypatch):
    """Catch files uploaded by default even when no resource references them."""
    executable = shutil.which("databricks")
    if executable is None:
        pytest.skip("Databricks CLI unavailable")
    compiler = importlib.import_module("setup.compile_config")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for name in ("databricks.yml", ".gitignore", "resources", "sql", "dashboards", "demo", "app", "setup"):
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, checkout / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copyfile(source, checkout / name)
    for name in ("customer_config", "tests", ".venv"):
        directory = checkout / name
        directory.mkdir()
        (directory / "not-for-upload.txt").write_text("Synthetic exclusion marker")
    subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True, capture_output=True)
    monkeypatch.setattr(compiler, "__file__", str(checkout / "setup/compile_config.py"))
    for attribute, path in [("_STATIC_RESOURCE_DIR", "resources"), ("_DEMO_RESOURCE_DIR", "demo/resources"), ("_APP_SOURCE_DIR", "app")]:
        monkeypatch.setattr(compiler, attribute, checkout / path)
    requests = []

    class WorkspaceMock(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            path = urlsplit(self.path).path
            if path == "/.well-known/databricks-config":
                body = {"oidc_endpoint": f"http://127.0.0.1:{self.server.server_port}/oidc"}
            elif path == "/api/2.0/preview/scim/v2/Me":
                body = {"id": "123", "userName": "sync-test@example.com", "displayName": "Sync Test", "active": True, "groups": []}
            elif self.path.startswith("/api/2.0/workspace/get-status"):
                body = {"object_type": "DIRECTORY", "path": "/Workspace/Shared/sync-test"}
            elif self.path.startswith("/api/2.0/workspace/list"):
                body = {"objects": []}
            else:
                self.send_error(404, "Unexpected mock request")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), WorkspaceMock)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    credentials = tmp_path / "mock.cfg"
    credentials.write_text(f"[sync-test]\nhost = http://127.0.0.1:{server.server_port}\ntoken = synthetic-sync-test\nauth_type = pat\n")
    try:
        for demo in (False, True, False):
            compile_config(demo_config[0], checkout / ".generated", demo)
            result = subprocess.run([
                executable, "bundle", "sync", "--dry-run", "--full", "--target", "customer",
                "--profile", "sync-test", "--output", "json",
            ], check=False, cwd=checkout,
                env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "DATABRICKS_CONFIG_FILE": str(credentials)},
                capture_output=True, text=True, timeout=15)
            assert result.returncode == 0, (result.stdout, result.stderr, requests)
            assert "Warning:" not in result.stderr, result.stderr
            events = [json.loads(line) for line in result.stdout.splitlines()]
            completed = [event for event in events if event["type"] == "complete"]
            assert len(completed) == 1 and completed[0]["dry_run"] is True
            uploads = set(completed[0]["put"])
            assert {".generated/app_source/app.yaml", ".generated/app_source/runtime_config.json", "sql/bootstrap.sql"} <= uploads
            assert not any(path.startswith(("customer_config/", "tests/", ".venv/")) for path in uploads)
            if demo:
                assert {"demo/generators/seed.py", "demo/generators/generate.py", "demo/sql/functions.sql"} <= uploads
                resources = yaml.safe_load((checkout / ".generated/customer.resources.yml").read_text())["resources"]
                seed = resources["jobs"]["demo_seed"]["tasks"][0]["notebook_task"]
                source = (checkout / ".generated" / seed["notebook_path"]).resolve()
                assert source.is_file() and source.relative_to(checkout).as_posix() in uploads
            else:
                assert not any(path.startswith("demo/") for path in uploads), sorted(uploads)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("catalog,schema", [("bad.catalog", "safe"), ("safe", "bad;schema"), ("safe", "")])
def test_generator_rejects_invalid_identifiers_before_building_sql(catalog, schema):
    with pytest.raises(ValueError):
        demo_module("generate").seed_sql(catalog, schema)


def test_demo_resources_only_appear_with_compiler_flag_and_paths_resolve(tmp_path, demo_config):
    config, _ = demo_config
    output = tmp_path / "compiled"
    compile_config(config, output, False)
    production = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    compile_config(config, output, True)
    resources = yaml.safe_load((output / "customer.resources.yml").read_text())["resources"]
    tasks = resources["jobs"]["demo_seed"]["tasks"]
    assert len(tasks) == 1
    seed = tasks[0]["notebook_task"]
    assert (output / seed["notebook_path"]).resolve() == DEMO / "generators" / "seed.py"
    assert seed["base_parameters"]["catalog"] == "${resources.dashboards.router_dashboard.dataset_catalog}"
    assert seed["base_parameters"]["schema"] == "${resources.dashboards.router_dashboard.dataset_schema}"
    assert seed["base_parameters"]["generators_dir"] == "${workspace.file_path}/demo/generators"
    compile_config(config, output, False)
    assert {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()} == production


@pytest.mark.parametrize("domain,question,function", [
    ("retail", "monthly revenue", "monthly_revenue"),
    ("support", "average resolution hours", "resolution_hours"),
    ("marketing", "purchase conversion rate", "conversion_rate"),
])
def test_shipped_definitions_route_only_to_declared_certified_functions(
    demo_config, install_runtime, modules, workspace, domain, question, function,
):
    runtime = install_runtime(demo_config[1])
    snapshot = snapshot_for(runtime)
    enrichment = modules.app.enrich(question, snapshot, runtime.routing.overlap_threshold)
    assert enrichment.method == "exact_alias"
    assert enrichment.certified_function == function
    workspace.decision = model_decision(domain, candidates={domain: 0.95})
    answer = modules.agent.ask_analytics(question, enrichment=enrichment)
    assert answer["status"] == "answered"
    assert answer["uc_function"] == f"alternate_catalog.alternate_schema.{function}"
    assert workspace.calls[-1][2]["statement"] == f"SELECT * FROM alternate_catalog.alternate_schema.{function}()"


@pytest.mark.parametrize("domain,question", [
    ("retail", "Compare completed orders by channel"),
    ("support", "Describe ticket volume by priority"),
    ("marketing", "Compare sessions and spend by channel"),
])
def test_demo_genie_questions_remain_genie_routes(demo_config, install_runtime, modules, workspace, domain, question):
    runtime = install_runtime(demo_config[1])
    enrichment = modules.app.enrich(question, snapshot_for(runtime), runtime.routing.overlap_threshold)
    assert enrichment.certified_function is None
    workspace.decision = model_decision(domain, candidates={domain: 0.95})
    answer = modules.agent.ask_analytics(question, enrichment=enrichment)
    assert answer["route"] == "genie"
    assert answer["genie_space_id"] == runtime.agents_by_name[domain].space_id


@pytest.mark.parametrize("decision,status", [
    ({"domain": "out_of_domain", "candidates": {}}, "no_route"),
    ({"domain": "retail", "candidates": {"retail": 0.95}, "undefined_terms": ["engagement efficiency index"]}, "undefined_term"),
])
def test_demo_refuses_unknown_metrics_and_outside_questions(
    demo_config, install_runtime, modules, workspace, decision, status,
):
    runtime = install_runtime(demo_config[1])
    question = "What is the engagement efficiency index?"
    enrichment = modules.app.enrich(question, snapshot_for(runtime), runtime.routing.overlap_threshold)
    workspace.decision = model_decision(**decision)
    answer = modules.agent.ask_analytics(question, enrichment=enrichment)
    assert answer["status"] == status
    assert answer["route"] == "fail_loud"
    assert len(workspace.calls) == 1


def test_demo_definitions_refresh_from_git_without_redeploy(demo_config, install_runtime, modules, monkeypatch):
    from test_github_definitions import API, SHA1, SHA2, TOKEN, FakeGitHub

    document = demo_config[1]
    document["definitions"]["repository_url"] = "https://github.com/acme/business-definitions"
    runtime = install_runtime(document)
    github = FakeGitHub()
    files = {"demo/definitions/" + path.name: path.read_text() for path in (DEMO / "definitions").glob("*.md")}
    github.files = {SHA1: files, SHA2: {path: text.replace("Retail revenue", "Updated retail revenue") for path, text in files.items()}}
    monkeypatch.setenv("GITHUB_DEFINITIONS_TOKEN", TOKEN)
    store = modules.definitions.DefinitionsStore(runtime, transport=github, retry_delay_seconds=0)
    assert store.refresh()
    before = modules.app.enrich("monthly revenue", store.snapshot(), 0.7)
    github.sha = SHA2
    assert store.refresh()
    after = modules.app.enrich("monthly revenue", store.snapshot(), 0.7)
    assert before.definitions_revision == SHA1 and after.definitions_revision == SHA2
    assert before.context != after.context
    assert after.certified_function == "monthly_revenue"
    assert all(url.startswith(API) for url in github.calls)


def test_demo_has_no_personal_or_workspace_identifiers():
    paths = [path for path in DEMO.rglob("*") if path.is_file() and "__pycache__" not in path.parts]
    assert paths, "Missing demo assets"
    for path in paths:
        text = path.read_text()
        assert not re.search(r"(?i)\bnext\b|omri|adb-field|\b\d{15,16}\b|https://[^\s]*\.azuredatabricks\.net|https://[^\s]*\.cloud\.databricks\.com", text), path
