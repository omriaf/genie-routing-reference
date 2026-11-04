"""Execute logical function keys as parameterized, allowlisted table calls."""

from dataclasses import dataclass
import math
import re
import time
from typing import Mapping

from app import config
from app.runtime_config import get_runtime_config

LiteralValue = str | int | float | bool | None
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,254}", flags=re.ASCII)


class UnknownFunctionError(ValueError):
    """The requested target is not a configured logical key."""


@dataclass(frozen=True)
class FunctionResult:
    function: str
    statement: str
    columns: list[str]
    rows: list[list]


def bind_arguments(arguments: Mapping[str, LiteralValue] | None) -> tuple[str, list[dict]]:
    """Only identifiers become SQL syntax; every scalar value becomes a binding.

    Signatures remain customer-owned. Named invocation lets the warehouse check
    parameter names/types and use the function's own defaults without guessing.
    """
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise ValueError("Function arguments must be a mapping of named literals")
    if any(type(name) is not str or not _IDENTIFIER.fullmatch(name) for name in arguments):
        raise ValueError("Function parameter names must be SQL identifiers")
    clauses, parameters = [], []
    for index, name in enumerate(sorted(arguments)):
        value = arguments[name]
        if value is None or type(value) is str:
            sql_type, literal = "STRING", value
        elif type(value) is bool:
            sql_type, literal = "BOOLEAN", str(value).lower()
        elif type(value) is int and -(2**63) <= value < 2**63:
            sql_type, literal = "BIGINT", str(value)
        elif type(value) is float and math.isfinite(value):
            sql_type, literal = "DOUBLE", repr(value)
        else:
            raise ValueError("Function values must be finite scalar literals")
        placeholder = f"arg_{index}"
        clauses.append(f"{name} => :{placeholder}")
        parameters.append({"name": placeholder, "value": literal, "type": sql_type})
    return ", ".join(clauses), parameters


def execute_function(logical_name: str, arguments: Mapping[str, LiteralValue] | None = None) -> FunctionResult:
    """Validate everything before constructing or invoking a WorkspaceClient."""
    runtime = get_runtime_config()
    function = runtime.functions_by_name.get(logical_name) if type(logical_name) is str else None
    if function is None:
        raise UnknownFunctionError("Use an exact configured logical function name")
    argument_sql, parameters = bind_arguments(arguments)
    statement = f"SELECT * FROM {function.full_name}({argument_sql})"
    client = config.get_client()
    response = client.api_client.do("POST", "/api/2.0/sql/statements", body={
        "warehouse_id": runtime.workspace.warehouse_id,
        "statement": statement,
        "parameters": parameters,
        "wait_timeout": "50s",
        "on_wait_timeout": "CONTINUE",
        "row_limit": 50,
    })
    for _ in range(30):
        if response.get("status", {}).get("state") not in {"PENDING", "RUNNING"}:
            break
        statement_id = response.get("statement_id")
        if not isinstance(statement_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", statement_id):
            raise RuntimeError("Function execution returned an invalid statement ID")
        time.sleep(2)
        response = client.api_client.do("GET", f"/api/2.0/sql/statements/{statement_id}")
    state = response.get("status", {}).get("state")
    if state in {"PENDING", "RUNNING"}:
        raise RuntimeError("Function execution timed out")
    if state != "SUCCEEDED":
        raise RuntimeError("Configured function execution failed")
    columns = response.get("manifest", {}).get("schema", {}).get("columns", [])
    rows = (response.get("result") or {}).get("data_array") or []
    return FunctionResult(function.full_name, statement, [column["name"] for column in columns], rows[:50])
