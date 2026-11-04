"""Create router schema/table before the first App binding is deployed.

Run with --config <compiled runtime_config.json or customer.yml> --profile NAME.
After bundle deployment, run router_bootstrap to apply explicit App traversal and
dashboard-reader grants. MLflow experiment creation is owned by the bundle.
"""

import json
import re
import sys
import time
from pathlib import Path

from setup.models import RouterPackageConfig, load_customer_config
from setup.preflight import CLI, CheckFailure, arguments

_SQL = Path(__file__).resolve().parents[1] / "sql/bootstrap.sql"


def bootstrap(cli: CLI, config: RouterPackageConfig) -> None:
    """Execute the same parameterized SQL used by the rerunnable bundle job."""
    source = _SQL.read_text(encoding="utf-8")
    parameters = [
        {"name": "catalog", "value": config.workspace.catalog, "type": "STRING"},
        {"name": "schema", "value": config.workspace.router_schema, "type": "STRING"},
    ]
    for cell in source.split("-- COMMAND ----------"):
        statement = "\n".join(line for line in cell.splitlines() if not line.lstrip().startswith("--")).strip()
        if not statement:
            continue
        response = cli.metadata("api", "post", "/api/2.0/sql/statements", "--json", json.dumps({
            "warehouse_id": config.workspace.warehouse_id, "statement": statement,
            "parameters": parameters, "wait_timeout": "50s", "on_wait_timeout": "CONTINUE",
        }))
        for _ in range(60):
            if response.get("status", {}).get("state") not in {"PENDING", "RUNNING"}:
                break
            statement_id = response.get("statement_id")
            if not isinstance(statement_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", statement_id):
                raise CheckFailure("bootstrap statement metadata")
            time.sleep(1)
            response = cli.metadata("api", "get", f"/api/2.0/sql/statements/{statement_id}")
        if response.get("status", {}).get("state") != "SUCCEEDED":
            raise CheckFailure("bootstrap SQL statement did not succeed")


def main(argv: list[str] | None = None) -> int:
    args = arguments(__doc__).parse_args(argv)
    try:
        config = load_customer_config(args.config)
        cli = CLI(args.profile)
        cli.version()
        bootstrap(cli, config)
    except (ValueError, OSError, CheckFailure):
        print("FAIL: router bootstrap; check configuration, metadata permissions, and warehouse", file=sys.stderr)
        return 1
    print("PASS: router schema and trace table exist; rerunning is safe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
