"""Compile configuration and check workspace metadata; never retrieve secret values.

Run: python -m setup.preflight --config customer_config/customer.yml --profile NAME
This checks the selected installer identity, not the future App's live behavior.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from setup.compile_config import _ArgumentParser, compile_config
from setup.models import RouterPackageConfig, load_customer_config


class CheckFailure(Exception):
    """Only fixed, non-sensitive labels may leave this boundary."""


class CLI:
    def __init__(self, profile: str):
        if not profile.strip():
            raise ValueError("An explicit profile is required")
        self.profile = profile

    def _run(self, arguments: list[str]):
        try:
            return subprocess.run(
                ["databricks", *arguments], capture_output=True, text=True, timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise CheckFailure("CLI execution") from None

    def version(self):
        response = self._run(["--version"])
        match = re.search(r"\bv(\d+)\.(\d+)\.(\d+)\b", response.stdout)
        if response.returncode or not match or tuple(map(int, match.groups())) < (1, 0, 0):
            raise CheckFailure("CLI version (requires >= 1.0.0)")

    def metadata(self, *arguments: str, optional=False):
        response = self._run([*arguments, "--profile", self.profile, "--output", "json"])
        if response.returncode:
            diagnostic = response.stderr.strip()
            denied = re.search(r"\b(?:PERMISSION_DENIED|UNAUTHENTICATED|UNAUTHORIZED|FORBIDDEN|401|403)\b", diagnostic)
            missing_code = re.match(
                r"^(?:Error:\s*)?\[?(?:RESOURCE_DOES_NOT_EXIST|SCHEMA_DOES_NOT_EXIST|SCHEMA_NOT_FOUND|TABLE_OR_VIEW_NOT_FOUND)\b",
                diagnostic,
            )
            missing_text = re.fullmatch(r"(?:Error:\s*)?(?:Schema|Table) '[^\n]+' does not exist\.?", diagnostic)
            # Denials always fail, even if they mention a missing object.
            if optional and not denied and (missing_code or missing_text):
                return None
            raise CheckFailure("workspace metadata or permissions")
        try:
            data = json.loads(response.stdout)
        except (ValueError, TypeError):
            raise CheckFailure("CLI metadata response") from None
        if not isinstance(data, dict) and not (
            arguments[:2] == ("secrets", "list-secrets")
            and isinstance(data, list) and all(isinstance(item, dict) for item in data)
        ):
            raise CheckFailure("CLI metadata response")
        return data


def _principals(identity: dict) -> set[str]:
    principals = {identity.get("userName"), identity.get("applicationId")}
    principals.update(group.get("display") for group in identity.get("groups", []))
    return {value for value in principals if isinstance(value, str) and value}


def _acl(data: dict, principals: set[str], allowed: set[str]) -> None:
    for entry in data.get("access_control_list", []):
        if (any(entry.get(key) in principals for key in ("user_name", "group_name", "service_principal_name"))
                and any(p.get("permission_level") in allowed for p in entry.get("all_permissions", []))):
            return
    raise CheckFailure("resource access permissions")


def _uc(cli: CLI, kind: str, name: str, metadata: dict, principals: set[str], required: set[str]):
    if metadata.get("owner") in principals:
        return
    granted = set()
    token = None
    seen = set()
    while True:
        arguments = ["grants", "get-effective", kind, name, "--max-results", "0"]
        if token:
            arguments.extend(["--page-token", token])
        data = cli.metadata(*arguments)
        for assignment in data.get("privilege_assignments", []):
            if assignment.get("principal") in principals:
                for item in assignment.get("privileges", []):
                    privilege = item.get("privilege") if isinstance(item, dict) else item
                    if isinstance(privilege, str):
                        granted.add(privilege.replace(" ", "_"))
        token = data.get("next_page_token")
        if not token:
            break
        if not isinstance(token, str) or token in seen:
            raise CheckFailure("UC permission pagination")
        seen.add(token)
    # MANAGE is not a data privilege. ALL_PRIVILEGES excludes MANAGE.
    if "ALL_PRIVILEGES" in granted:
        granted.update(required - {"MANAGE"})
    if not required <= granted:
        raise CheckFailure("UC access permissions")


def _trace_table(metadata: dict) -> None:
    from app.tracing import TRACE_COLUMNS

    columns = {column["name"]: column["type_name"] for column in metadata.get("columns", [])}
    required = {"request_ts": "TIMESTAMP", **TRACE_COLUMNS}
    if metadata.get("data_source_format") != "DELTA" or any(
        columns.get(name) not in ({"LONG", "BIGINT"} if kind == "BIGINT" else {kind})
        for name, kind in required.items()
    ):
        raise CheckFailure("existing trace table is incompatible; explicit migration is required")


def check_workspace(cli: CLI, config: RouterPackageConfig) -> list[str]:
    cli.version()
    identity = cli.metadata("current-user", "me")
    principals = _principals(identity)
    if not principals:
        raise CheckFailure("authenticated identity")
    notes = []
    workspace = config.workspace
    warehouse = cli.metadata("warehouses", "get", workspace.warehouse_id)
    if warehouse.get("warehouse_type") != "PRO" and not warehouse.get("enable_serverless_compute"):
        raise CheckFailure("bootstrap requires a Pro or Serverless SQL warehouse")
    _acl(cli.metadata("warehouses", "get-permissions", workspace.warehouse_id), principals,
         {"CAN_USE", "CAN_MANAGE", "IS_OWNER"})
    endpoint = cli.metadata("serving-endpoints", "get", workspace.classifier_endpoint)
    endpoint_id = endpoint.get("id")
    if not isinstance(endpoint_id, str) or not endpoint_id:
        raise CheckFailure("model endpoint metadata")
    _acl(cli.metadata("serving-endpoints", "get-permissions", endpoint_id), principals,
         {"CAN_QUERY", "CAN_MANAGE", "IS_OWNER"})
    for agent in config.genie_agents:
        cli.metadata("genie", "get-space", agent.space_id)
        _acl(cli.metadata("permissions", "get", "genie", agent.space_id), principals,
             {"CAN_RUN", "CAN_EDIT", "CAN_MANAGE", "IS_OWNER"})

    catalog = cli.metadata("catalogs", "get", workspace.catalog)
    schema_name = f"{workspace.catalog}.{workspace.router_schema}"
    schema = cli.metadata("schemas", "get", schema_name, optional=True)
    catalog_required = {"USE_CATALOG", "MANAGE"}
    if schema is None:
        catalog_required.add("CREATE_SCHEMA")
        notes.append("router schema requires bootstrap")
    _uc(cli, "catalog", workspace.catalog, catalog, principals, catalog_required)
    if schema is not None:
        table_name = f"{schema_name}.routing_traces"
        table = cli.metadata("tables", "get", table_name, optional=True)
        schema_required = {"USE_SCHEMA", "MANAGE"}
        if table is None:
            schema_required.add("CREATE_TABLE")
            notes.append("router trace table requires bootstrap")
        _uc(cli, "schema", schema_name, schema, principals, schema_required)
        if table is not None:
            _trace_table(table)
            _uc(cli, "table", table_name, table, principals, {"MODIFY", "MANAGE"})

    for function in config.uc_functions:
        function_metadata = cli.metadata("functions", "get", function.full_name)
        function_schema = ".".join(function.full_name.split(".")[:2])
        parent = cli.metadata("schemas", "get", function_schema)
        _uc(cli, "schema", function_schema, parent, principals, {"USE_SCHEMA", "MANAGE"})
        _uc(cli, "function", function.full_name, function_metadata, principals, {"EXECUTE", "MANAGE"})
    secrets = cli.metadata("secrets", "list-secrets", config.definitions.secret_scope)
    keys = secrets.get("secrets", []) if isinstance(secrets, dict) else secrets
    if not isinstance(keys, list) or not any(item.get("key") == config.definitions.secret_key for item in keys):
        raise CheckFailure("Git secret key metadata")
    return notes


def arguments(description: str) -> argparse.ArgumentParser:
    parser = _ArgumentParser(description=description, allow_abbrev=False)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--config", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = arguments(__doc__)
    parser.add_argument("--output", type=Path, default=Path(".generated"))
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args(argv)
    try:
        cli = CLI(args.profile)
        compile_config(args.config, args.output, args.demo)
        config = load_customer_config(args.output / "runtime_config.json")
        notes = check_workspace(cli, config)
    except (ValueError, OSError):
        print("FAIL: configuration compilation or explicit profile", file=sys.stderr)
        return 1
    except CheckFailure as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    except (AttributeError, KeyError, TypeError):
        print("FAIL: malformed workspace metadata", file=sys.stderr)
        return 1
    print("PASS: configuration, CLI, auth, resource metadata, ACLs, UC permissions, secret key metadata")
    for note in notes:
        print(f"NOTE: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
