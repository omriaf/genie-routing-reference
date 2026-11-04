"""Compile local customer references without credentials or workspace access."""

import argparse
import ctypes
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError
import yaml

from setup.models import RouterPackageConfig, load_customer_config


_TEMPLATE_DIR = Path(__file__).with_name("templates")
_FILENAMES = ("customer.resources.yml", "runtime_config.json", "summary.json")


@dataclass(frozen=True)
class CompileResult:
    runtime_config: Path
    resources: Path
    summary: Path

    @property
    def generated_paths(self) -> tuple[Path, ...]:
        return (self.resources, self.runtime_config, self.summary)


def _json_text(value: dict) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def _yaml_text(value: dict) -> str:
    return yaml.safe_dump(value, sort_keys=True, allow_unicode=True)


def _canonical_runtime(config: RouterPackageConfig) -> dict:
    runtime = config.model_dump(mode="json", exclude_none=True)
    for field in ("genie_agents", "uc_functions"):
        runtime[field].sort(key=lambda item: item["name"])
    for function in runtime["uc_functions"]:
        function["aliases"].sort()
    for principals in runtime["permissions"].values():
        principals.sort(key=lambda principal: tuple(principal.items()))
    return runtime


def _resource_document(runtime: dict, demo: bool) -> dict:
    workspace = runtime["workspace"]
    definitions = runtime["definitions"]
    bindings = [
        {
            "name": "sql_warehouse",
            "sql_warehouse": {"id": workspace["warehouse_id"], "permission": "CAN_USE"},
        },
        {
            "name": "classifier_endpoint",
            "serving_endpoint": {
                "name": workspace["classifier_endpoint"], "permission": "CAN_QUERY"
            },
        },
        {
            "name": "trace_table",
            "uc_securable": {
                "securable_full_name": (
                    f"{workspace['catalog']}.{workspace['router_schema']}.routing_traces"
                ),
                "securable_type": "TABLE",
                "permission": "MODIFY",
            },
        },
        {
            "name": "definitions_secret",
            "secret": {
                "scope": definitions["secret_scope"],
                "key": definitions["secret_key"],
                "permission": "READ",
            },
        },
    ]
    bindings.extend(
        {
            "name": f"genie_{agent['name']}",
            "genie_space": {
                "name": agent["name"],
                "space_id": agent["space_id"],
                "permission": "CAN_RUN",
            },
        }
        for agent in runtime["genie_agents"]
    )
    bindings.extend(
        {
            "name": f"function_{function['name']}",
            "uc_securable": {
                "securable_full_name": function["full_name"],
                "securable_type": "FUNCTION",
                "permission": "EXECUTE",
            },
        }
        for function in runtime["uc_functions"]
    )
    app = {
        "name": runtime["customer"]["app_name"],
        "source_code_path": "../app",
        "resources": sorted(bindings, key=lambda binding: binding["name"]),
    }
    if runtime["permissions"]["app"]:
        app["permissions"] = [
            {**principal, "level": "CAN_USE"}
            for principal in runtime["permissions"]["app"]
        ]
    resources = {"apps": {"router_app": app}}
    if runtime["permissions"]["dashboard"]:
        # The dashboard definition is supplied by the static bundle resources.
        resources["dashboards"] = {
            "router_dashboard": {
                "permissions": [
                    {**principal, "level": "CAN_READ"}
                    for principal in runtime["permissions"]["dashboard"]
                ]
            }
        }
    document = {"resources": resources}
    if demo:
        document["include"] = ["../demo/resources/*.yml"]
    return document


def _resource_summary(runtime: dict, demo: bool) -> dict:
    # Only counts and fixed filenames leave the generated configuration boundary.
    return {
        "demo": demo,
        "files": list(_FILENAMES),
        "permissions": {
            resource: len(principals)
            for resource, principals in sorted(runtime["permissions"].items())
        },
        "resource_counts": {
            "genie_spaces": len(runtime["genie_agents"]),
            "secrets": 1,
            "serving_endpoints": 1,
            "sql_warehouses": 1,
            "trace_tables": 1,
            "uc_functions": len(runtime["uc_functions"]),
        },
    }


def _validate_artifacts(staging: Path, runtime: dict, resources: dict, summary: dict) -> None:
    try:
        loaded_runtime = json.loads((staging / "runtime_config.json").read_text(encoding="utf-8"))
        RouterPackageConfig.model_validate(loaded_runtime)
        loaded_resources = yaml.safe_load(
            (staging / "customer.resources.yml").read_text(encoding="utf-8")
        )
        loaded_summary = json.loads((staging / "summary.json").read_text(encoding="utf-8"))
        if (loaded_runtime, loaded_resources, loaded_summary) != (runtime, resources, summary):
            raise ValueError("Rendered artifacts differ from the validated configuration")
    except (ValueError, yaml.YAMLError, UnicodeError):
        raise ValueError("Generated artifacts failed validation") from None


def _exchange_directories(source: Path, destination: Path) -> None:
    """Swap two directory names in one syscall; never fall back to two renames."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        exchange = getattr(libc, "renamex_np", None)
        argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        arguments = (os.fsencode(source), os.fsencode(destination), 2)  # RENAME_SWAP
    elif sys.platform == "linux":
        exchange = getattr(libc, "renameat2", None)
        argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        # AT_FDCWD = -100; RENAME_EXCHANGE = 2.
        arguments = (-100, os.fsencode(source), -100, os.fsencode(destination), 2)
    else:
        exchange = None
    if exchange is None:
        raise OSError(errno.ENOSYS, "Atomic directory exchange is unavailable")
    exchange.argtypes = argtypes
    exchange.restype = ctypes.c_int
    if exchange(*arguments) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _output_destination(config_path: Path, output_dir: Path) -> Path:
    if output_dir.is_symlink() or (output_dir.exists() and not output_dir.is_dir()):
        raise ValueError("Output must be a directory, not a symlink or file")
    output = output_dir.resolve()
    if config_path.resolve().is_relative_to(output):
        raise ValueError("Output must not contain the input configuration")
    return output


def compile_config(config_path: Path, output_dir: Path, demo: bool) -> CompileResult:
    """Render, validate, then atomically publish the complete configuration directory.

    Replacing an existing directory requires macOS/Linux exchange support. If the
    filesystem does not support it, the previous output remains intact.
    """
    output = _output_destination(config_path, output_dir)
    runtime = _canonical_runtime(load_customer_config(config_path))
    resources = _resource_document(runtime, demo)
    summary = _resource_summary(runtime, demo)
    environment = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    environment.filters.update(json_text=_json_text, yaml_text=_yaml_text)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f"{output.name}-", dir=output.parent) as directory:
        staging = Path(directory)
        try:
            for filename, template, context in (
                ("runtime_config.json", "runtime-config.json.j2", {"runtime": runtime}),
                ("customer.resources.yml", "app-resource.yml.j2", {"document": resources}),
            ):
                rendered = environment.get_template(template).render(**context)
                (staging / filename).write_text(rendered, encoding="utf-8")
        except TemplateError:
            raise ValueError("Configuration template rendering failed") from None
        (staging / "summary.json").write_text(_json_text(summary), encoding="utf-8")
        _validate_artifacts(staging, runtime, resources, summary)
        # Recheck immediately before publication as well as before staging.
        _output_destination(config_path, output)
        if output.exists():
            _exchange_directories(staging, output)
        else:
            os.replace(staging, output)
        # After an exchange, TemporaryDirectory removes the previous generation.
    return CompileResult(
        runtime_config=output / "runtime_config.json",
        resources=output / "customer.resources.yml",
        summary=output / "summary.json",
    )


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # Argparse normally echoes rejected option values, which may be sensitive.
        self.exit(2, "Invalid compiler arguments; use --help for usage.\n")


def main(argv: list[str] | None = None) -> int:
    parser = _ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = compile_config(args.config, args.output, args.demo)
    except (ValueError, OSError):
        print("Configuration compilation failed; check the configuration and output directory.", file=sys.stderr)
        return 1
    for path in result.generated_paths:
        print(path)
    print(result.summary.read_text(encoding="utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
