"""Real setup flows with a synthetic external CLI transport; no credentials."""

import importlib
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_metadata_preflight_entrypoint_is_available():
    assert (ROOT / "setup/preflight.py").is_file()


@pytest.fixture
def preflight():
    try:
        return importlib.import_module("setup.preflight")
    except ModuleNotFoundError as error:
        if error.name == "setup.preflight":
            pytest.fail("The metadata-only preflight implementation is missing")
        raise


@pytest.fixture
def customer_file(tmp_path):
    path = tmp_path / "customer.yml"
    path.write_bytes((ROOT / "customer_config/customer.example.yml").read_bytes())
    return path


class WorkspaceCLI:
    """Only documented metadata commands are allowed; anything else fails closed."""

    def __init__(self):
        self.calls = []
        self.version = "Databricks CLI v1.23.0"
        self.denied = None
        self.missing_schema = False
        self.missing_table = False
        self.missing_secret = False
        self.privileges = ("USE_CATALOG", "CREATE_SCHEMA", "USE_SCHEMA", "CREATE_TABLE", "EXECUTE", "MODIFY", "MANAGE")
        self.missing_status_column = False
        self.overrides = {}

    def __call__(self, args, **kwargs):
        assert kwargs["capture_output"] and kwargs["text"]
        assert 0 < kwargs["timeout"] <= 60
        self.calls.append(args)
        command = args[1:]
        if command == ["--version"]:
            return subprocess.CompletedProcess(args, 0, self.version, "")
        assert command[-4:] == ["--profile", "chosen", "--output", "json"]
        command = command[:-4]
        family, operation, *values = command
        if (family, operation) in self.overrides:
            return subprocess.CompletedProcess(args, 0, json.dumps(self.overrides[(family, operation)]), "")
        if self.denied == (family, operation):
            return subprocess.CompletedProcess(args, 1, "", "SENSITIVE_BACKEND_PAYLOAD")
        if (family, operation) == ("current-user", "me"):
            data = {"userName": "installer@example.com", "groups": [{"display": "installers"}]}
        elif (family, operation) == ("warehouses", "get"):
            data = {"id": values[0], "warehouse_type": "PRO", "enable_serverless_compute": False}
        elif (family, operation) == ("serving-endpoints", "get"):
            data = {"id": "endpoint-id", "name": values[0]}
        elif operation == "get-permissions" or (family, operation) == ("permissions", "get"):
            data = {"access_control_list": [{"group_name": "installers", "all_permissions": [
                {"permission_level": "CAN_MANAGE", "inherited": False},
            ]}]}
        elif (family, operation) == ("genie", "get-space"):
            data = {"space_id": values[0]}
        elif family in {"catalogs", "schemas", "functions", "tables"} and operation == "get":
            if (family == "schemas" and self.missing_schema and "analytics_router" in values[0]) or (
                family == "tables" and self.missing_table
            ):
                return subprocess.CompletedProcess(args, 1, "", "RESOURCE_DOES_NOT_EXIST")
            data = {"full_name": values[0], "owner": "data-owner"}
            if family == "tables":
                from app.tracing import TRACE_COLUMNS

                data["columns"] = [{"name": name, "type_name": "LONG" if kind == "BIGINT" else kind}
                                   for name, kind in {"request_ts": "TIMESTAMP", **TRACE_COLUMNS}.items()
                                   if not (self.missing_status_column and name == "status")]
                data["data_source_format"] = "DELTA"
        elif (family, operation) == ("grants", "get-effective"):
            data = {"privilege_assignments": [{"principal": "installers", "privileges": [
                {"privilege": p} for p in self.privileges
            ]}]}
        elif (family, operation) == ("secrets", "list-secrets"):
            data = [] if self.missing_secret else [{"key": "github-token", "last_updated_timestamp": 1}]
        else:
            raise AssertionError(f"Unexpected or unsafe CLI command: {command}")
        return subprocess.CompletedProcess(args, 0, json.dumps(data), "")


def test_profile_is_required_before_compilation_or_auth(preflight, customer_file, tmp_path, monkeypatch, capsys):
    cli = WorkspaceCLI()
    monkeypatch.setattr(preflight.subprocess, "run", cli)
    with pytest.raises(SystemExit) as error:
        preflight.main(["--config", str(customer_file), "--output", str(tmp_path / "output")])
    assert error.value.code == 2
    assert not cli.calls
    assert not (tmp_path / "output").exists()
    assert "SENSITIVE" not in capsys.readouterr().err


def test_preflight_compiles_and_checks_every_external_reference(preflight, customer_file, tmp_path, monkeypatch, capsys):
    cli = WorkspaceCLI()
    monkeypatch.setattr(preflight.subprocess, "run", cli)
    output = tmp_path / ".generated"
    assert preflight.main(["--config", str(customer_file), "--output", str(output), "--profile", "chosen"]) == 0
    assert (output / "app_source/runtime_config.json").exists()
    commands = [call[1:-4] for call in cli.calls[1:]]
    assert ["genie", "get-space", "01abc000000000000000000000000001"] in commands
    assert ["genie", "get-space", "01abc000000000000000000000000002"] in commands
    assert ["functions", "get", "acme_catalog.metrics.monthly_revenue"] in commands
    assert ["functions", "get", "acme_catalog.metrics.resolution_time"] in commands
    assert ["secrets", "list-secrets", "analytics-router"] in commands
    assert "PASS" in capsys.readouterr().out


@pytest.mark.parametrize("denied", [
    ("current-user", "me"), ("warehouses", "get"), ("warehouses", "get-permissions"),
    ("serving-endpoints", "get"), ("serving-endpoints", "get-permissions"),
    ("genie", "get-space"), ("permissions", "get"), ("functions", "get"),
    ("catalogs", "get"), ("grants", "get-effective"), ("secrets", "list-secrets"),
])
def test_preflight_fails_closed_and_redacts_backend_errors(preflight, customer_file, tmp_path, monkeypatch, capsys, denied):
    cli = WorkspaceCLI()
    cli.denied = denied
    monkeypatch.setattr(preflight.subprocess, "run", cli)
    assert preflight.main(["--config", str(customer_file), "--output", str(tmp_path / "out"), "--profile", "chosen"]) == 1
    captured = capsys.readouterr()
    assert "FAIL" in captured.out + captured.err
    assert "SENSITIVE_BACKEND_PAYLOAD" not in captured.out + captured.err


def test_missing_router_schema_is_bootstrap_work_not_missing_customer_resource(preflight, customer_file, tmp_path, monkeypatch, capsys):
    cli = WorkspaceCLI()
    cli.missing_schema = cli.missing_table = True
    monkeypatch.setattr(preflight.subprocess, "run", cli)
    assert preflight.main(["--config", str(customer_file), "--output", str(tmp_path / "out"), "--profile", "chosen"]) == 0
    assert "bootstrap" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("version", ["Databricks CLI v0.200.0", "unknown"])
def test_unsupported_cli_stops_before_auth(preflight, customer_file, tmp_path, monkeypatch, capsys, version):
    cli = WorkspaceCLI()
    cli.version = version
    monkeypatch.setattr(preflight.subprocess, "run", cli)
    assert preflight.main(["--config", str(customer_file), "--output", str(tmp_path / "out"), "--profile", "chosen"]) == 1
    assert len(cli.calls) == 1


def test_missing_secret_metadata_is_a_failure(preflight, customer_file, tmp_path, monkeypatch):
    cli = WorkspaceCLI()
    cli.missing_secret = True
    monkeypatch.setattr(preflight.subprocess, "run", cli)
    assert preflight.main(["--config", str(customer_file), "--output", str(tmp_path / "out"), "--profile", "chosen"]) == 1


def test_invalid_config_never_contacts_workspace(preflight, customer_file, tmp_path, monkeypatch, capsys):
    customer_file.write_text("workspace: SENSITIVE_CUSTOMER_INPUT")
    cli = WorkspaceCLI()
    monkeypatch.setattr(preflight.subprocess, "run", cli)
    assert preflight.main(["--config", str(customer_file), "--output", str(tmp_path / "out"), "--profile", "chosen"]) == 1
    assert not cli.calls
    assert "SENSITIVE_CUSTOMER_INPUT" not in str(capsys.readouterr())


def test_catalog_traversal_grants_require_authority_to_grant(preflight, customer_file, tmp_path, monkeypatch):
    cli = WorkspaceCLI()
    cli.privileges = ("USE_CATALOG", "CREATE_SCHEMA", "USE_SCHEMA", "CREATE_TABLE", "EXECUTE", "MODIFY")
    # Child ownership lets the installer configure children but does not imply
    # permission to grant USE CATALOG on the parent catalog to the future App.
    cli.overrides[("schemas", "get")] = {"owner": "installer@example.com"}
    cli.overrides[("tables", "get")] = {"owner": "installer@example.com"}
    cli.overrides[("functions", "get")] = {"owner": "installer@example.com"}
    monkeypatch.setattr(preflight.subprocess, "run", cli)
    assert preflight.main(["--config", str(customer_file), "--output", str(tmp_path / "out"), "--profile", "chosen"]) == 1


def test_existing_incompatible_trace_table_cannot_pass_preflight(preflight, customer_file, tmp_path, monkeypatch):
    cli = WorkspaceCLI()
    cli.missing_status_column = True
    monkeypatch.setattr(preflight.subprocess, "run", cli)
    assert preflight.main(["--config", str(customer_file), "--output", str(tmp_path / "out"), "--profile", "chosen"]) == 1


@pytest.mark.parametrize("metadata", [None, "SENSITIVE_PAYLOAD", ["SENSITIVE_PAYLOAD"]])
def test_malformed_metadata_fails_with_a_redacted_diagnostic(preflight, customer_file, tmp_path, monkeypatch, capsys, metadata):
    cli = WorkspaceCLI()
    cli.overrides[("warehouses", "get")] = metadata
    monkeypatch.setattr(preflight.subprocess, "run", cli)
    assert preflight.main(["--config", str(customer_file), "--output", str(tmp_path / "out"), "--profile", "chosen"]) == 1
    assert "SENSITIVE_PAYLOAD" not in str(capsys.readouterr())


def test_bootstrap_entrypoint_is_available():
    assert (ROOT / "setup/bootstrap.py").is_file()


def test_bootstrap_uses_parameterized_idempotent_ddl_with_runtime_columns(customer_file, monkeypatch):
    if not (ROOT / "setup/bootstrap.py").exists():
        pytest.fail("The pre-deployment bootstrap implementation is missing")
    bootstrap = importlib.import_module("setup.bootstrap")
    from app.tracing import TRACE_COLUMNS
    from setup.models import load_customer_config

    class SQL:
        def __init__(self):
            self.calls = []

        def metadata(self, *args):
            assert args[:3] == ("api", "post", "/api/2.0/sql/statements")
            assert args[3] == "--json"
            body = json.loads(args[4])
            self.calls.append(body)
            return {"status": {"state": "SUCCEEDED"}}

    sql = SQL()
    config = load_customer_config(customer_file)
    bootstrap.bootstrap(sql, config)
    bootstrap.bootstrap(sql, config)
    assert len(sql.calls) == 4
    assert sql.calls[:2] == sql.calls[2:]
    create_schema, create_table = sql.calls[:2]
    assert "CREATE SCHEMA IF NOT EXISTS IDENTIFIER('`' || :catalog || '`.`' || :schema || '`')" in create_schema["statement"]
    assert "CREATE TABLE IF NOT EXISTS IDENTIFIER('`' || :catalog || '`.`' || :schema || '`.`routing_traces`')" in create_table["statement"]
    for name, kind in {"request_ts": "TIMESTAMP", **TRACE_COLUMNS}.items():
        assert f"{name} {kind}" in create_table["statement"]
    for body in sql.calls:
        assert body["warehouse_id"] == "abc123"
        assert body["parameters"] == [
            {"name": "catalog", "value": "acme_catalog", "type": "STRING"},
            {"name": "schema", "value": "analytics_router", "type": "STRING"},
        ]
        assert "GRANT" not in body["statement"]
        assert "acme_catalog" not in body["statement"]


def test_bootstrap_requires_profile_and_surfaces_statement_failure(customer_file, capsys, monkeypatch):
    if not (ROOT / "setup/bootstrap.py").exists():
        pytest.fail("The pre-deployment bootstrap implementation is missing")
    bootstrap = importlib.import_module("setup.bootstrap")
    with pytest.raises(SystemExit) as error:
        bootstrap.main(["--config", str(customer_file)])
    assert error.value.code == 2

    class BrokenSQL:
        def __init__(self, profile):
            assert profile == "chosen"

        def version(self):
            pass

        def metadata(self, *args):
            return {"status": {"state": "FAILED", "error": {"message": "SENSITIVE_SQL"}}}

    monkeypatch.setattr(bootstrap, "CLI", BrokenSQL)
    assert bootstrap.main(["--config", str(customer_file), "--profile", "chosen"]) == 1
    assert "SENSITIVE_SQL" not in str(capsys.readouterr())


def test_optional_metadata_never_treats_denied_access_as_missing(preflight, monkeypatch):
    def denied(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "PERMISSION_DENIED: RESOURCE_DOES_NOT_EXIST")

    monkeypatch.setattr(preflight.subprocess, "run", denied)
    with pytest.raises(preflight.CheckFailure):
        preflight.CLI("chosen").metadata("schemas", "get", "catalog.router", optional=True)


def test_optional_metadata_recognizes_cli_missing_schema_diagnostic(preflight, monkeypatch):
    def absent(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "Error: Schema 'catalog.router' does not exist.\n")

    monkeypatch.setattr(preflight.subprocess, "run", absent)
    assert preflight.CLI("chosen").metadata("schemas", "get", "catalog.router", optional=True) is None


@pytest.mark.parametrize("privileges", [("MANAGE",), ("ALL_PRIVILEGES",), ()])
def test_uc_permissions_cannot_be_satisfied_by_a_partial_privilege_set(preflight, customer_file, tmp_path, monkeypatch, privileges):
    cli = WorkspaceCLI()
    cli.privileges = privileges
    monkeypatch.setattr(preflight.subprocess, "run", cli)
    assert preflight.main(["--config", str(customer_file), "--output", str(tmp_path / "out"), "--profile", "chosen"]) == 1


def test_bootstrap_waits_for_sql_completion_and_rejects_untrusted_statement_ids(customer_file, monkeypatch):
    bootstrap = importlib.import_module("setup.bootstrap")
    from setup.models import load_customer_config

    class PendingSQL:
        def __init__(self, statement_id):
            self.statement_id = statement_id
            self.polls = 0

        def metadata(self, *args):
            if args[1] == "post":
                return {"statement_id": self.statement_id, "status": {"state": "PENDING"}}
            assert args == ("api", "get", "/api/2.0/sql/statements/statement-1")
            self.polls += 1
            return {"status": {"state": "SUCCEEDED"}}

    monkeypatch.setattr(bootstrap.time, "sleep", lambda duration: None)
    sql = PendingSQL("statement-1")
    bootstrap.bootstrap(sql, load_customer_config(customer_file))
    assert sql.polls == 2
    unsafe = PendingSQL("../../SENSITIVE_PATH")
    with pytest.raises(bootstrap.CheckFailure):
        bootstrap.bootstrap(unsafe, load_customer_config(customer_file))
    assert unsafe.polls == 0
