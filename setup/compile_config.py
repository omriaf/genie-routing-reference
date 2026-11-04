"""Compile local customer references without credentials or workspace access."""

import argparse
import ctypes
import errno
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

from setup.models import RouterPackageConfig, _UniqueKeySafeLoader, load_customer_config

_TEMPLATE_DIR = Path(__file__).with_name("templates")
_DEMO_RESOURCE_DIR = Path(__file__).resolve().parents[1] / "demo/resources"
_STATIC_RESOURCE_DIR = Path(__file__).resolve().parents[1] / "resources"
_APP_SOURCE_DIR = Path(__file__).resolve().parents[1] / "app"
_APP_MODULES = (
    "__init__.py", "agent.py", "app.py", "config.py", "definitions.py",
    "genie_client.py", "markdown_definitions.py", "router.py",
    "runtime_config.py", "tracing.py", "uc_functions.py",
)
_FILENAMES = ("customer.resources.yml", "runtime_config.json", "summary.json", "app_source")


@dataclass(frozen=True)
class CompileResult:
    runtime_config: Path
    resources: Path
    summary: Path
    app_source: Path

    @property
    def generated_paths(self) -> tuple[Path, ...]:
        return (self.resources, self.runtime_config, self.summary, self.app_source)


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


def _validate_resource_document(document: object, *, generated: bool = False) -> dict:
    allowed = {"resources", "sync"} if generated else {"resources"}
    if not isinstance(document, dict) or set(document) != allowed:
        raise ValueError("Resource document has invalid top-level fields")
    if generated:
        sync = document["sync"]
        if (not isinstance(sync, dict) or not sync or not sync.keys() <= {"include", "exclude"}
                or any(not isinstance(patterns, list) or any(
                    not isinstance(pattern, str) or not pattern.strip() for pattern in patterns
                ) for patterns in sync.values())):
            raise ValueError("Generated sync configuration must contain path pattern lists")
    resources = document["resources"]
    if not isinstance(resources, dict):
        raise ValueError("Resource groups must be mappings")  # noqa: TRY004 - compiler validation contract
    for kind, declarations in resources.items():
        if not isinstance(kind, str) or not isinstance(declarations, dict):
            raise ValueError("Resource groups must be named mappings")  # noqa: TRY004 - compiler validation contract
        for name, body in declarations.items():
            if not isinstance(name, str) or not isinstance(body, dict):
                raise ValueError("Resource declarations must be named mappings")  # noqa: TRY004 - compiler validation contract

    def reject_include(value):
        if isinstance(value, dict):
            if "include" in value:
                raise ValueError("Includes are supported only in the root bundle file")
            for child in value.values():
                reject_include(child)
        elif isinstance(value, list):
            for child in value:
                reject_include(child)

    reject_include(resources)
    return resources


def _rebase_demo_paths(value, source_dir: Path, output_dir: Path, path=(), git_source=False):
    """Keep local source references valid when flattening demo resource files."""
    if isinstance(value, list):
        return [_rebase_demo_paths(item, source_dir, output_dir, path, git_source) for item in value]
    if not isinstance(value, dict):
        return value
    if isinstance(value.get("git_source"), dict):
        git_source = True
    if value.get("source") in {"GIT", "WORKSPACE"}:
        git_source = value["source"] == "GIT"
    rebased = {}
    for key, child in value.items():
        is_path = (
            (path[-1:] == ("notebook_task",) and key == "notebook_path")
            or (path[-1:] == ("spark_python_task",) and key == "python_file")
            or (path[-1:] == ("dbt_task",) and key == "project_directory")
            or (path[-1:] == ("libraries",) and key in {"whl", "jar", "egg"})
            or (path[-2:] in {("sql_task", "file"), ("libraries", "notebook"), ("libraries", "file")} and key == "path")
            or (len(path) == 2 and path[0] == "apps" and key == "source_code_path")
            or (len(path) == 2 and path[0] == "dashboards" and key == "file_path")
        )
        if (
            is_path and isinstance(child, str) and not git_source
            and not child.startswith(("/", "${", "~")) and ":" not in child
        ):
            child = Path(os.path.relpath((source_dir / child).resolve(), output_dir)).as_posix()
        rebased[key] = _rebase_demo_paths(child, source_dir, output_dir, (*path, key), git_source)
    return rebased


def _merge_resources(destination: dict, source: dict) -> None:
    for kind, declarations in sorted(source.items()):
        target = destination.setdefault(kind, {})
        for name, body in sorted(declarations.items()):
            if name in target:
                raise ValueError("Duplicate resource declarations are not allowed")
            target[name] = body


def _load_demo_resources(output_dir: Path) -> dict:
    paths = sorted((*_DEMO_RESOURCE_DIR.glob("*.yml"), *_DEMO_RESOURCE_DIR.glob("*.yaml")))
    resources = {}
    for path in paths:
        try:
            document = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
            definitions = _validate_resource_document(document)
        except (ValueError, yaml.YAMLError, UnicodeError):
            raise ValueError("Demo resource definitions failed validation") from None
        _merge_resources(resources, _rebase_demo_paths(definitions, path.parent, output_dir))
    if not any(resources.values()):
        raise ValueError("Demo mode requires definitions under demo/resources")
    return resources


def _load_static_resources(output_dir: Path) -> dict:
    """Flatten static source definitions; DAB rejects duplicate include keys."""
    resources = {}
    for filename in ("router_app.app.yml", "bootstrap.job.yml", "router_dashboard.dashboard.yml"):
        path = _STATIC_RESOURCE_DIR / filename
        try:
            document = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
            definitions = _validate_resource_document(document)
            _merge_resources(resources, _rebase_demo_paths(definitions, path.parent, output_dir))
        except (ValueError, yaml.YAMLError, UnicodeError):
            raise ValueError("Static router resource definitions failed validation") from None
    return resources


def _resource_document(runtime: dict, demo_resources: dict, output_dir: Path) -> dict:
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
        {
            "name": "mlflow_experiment",
            "experiment": {
                "experiment_id": "${resources.experiments.router_experiment.id}",
                "permission": "CAN_EDIT",
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
        "source_code_path": "app_source",
        "resources": sorted(bindings, key=lambda binding: binding["name"]),
    }
    if runtime["permissions"]["app"]:
        app["permissions"] = [
            {**principal, "level": "CAN_USE"}
            for principal in runtime["permissions"]["app"]
        ]
    # Customer settings are compiled once; bundle variables are never a second
    # source of truth for the runtime's catalog/schema/warehouse/experiment.
    dashboard = {
        "dataset_catalog": workspace["catalog"],
        "dataset_schema": workspace["router_schema"],
        "warehouse_id": workspace["warehouse_id"],
    }
    resources = _load_static_resources(output_dir)
    bootstrap_job = resources["jobs"]["router_bootstrap"]
    tasks = bootstrap_job["tasks"]
    tasks[0]["notebook_task"].update({
        "warehouse_id": workspace["warehouse_id"],
        "base_parameters": {"catalog": workspace["catalog"], "schema": workspace["router_schema"]},
    })
    schemas = sorted({workspace["router_schema"], *(
        function["full_name"].split(".")[1] for function in runtime["uc_functions"]
    )})
    grants = [
        (f"app_access_{index}", schema,
         "${resources.apps.router_app.service_principal_client_id}", "schema_access.sql")
        for index, schema in enumerate(schemas)
    ]
    grants.extend(
        (f"dashboard_access_{index}", workspace["router_schema"],
         next(iter(principal.values())), "dashboard_access.sql")
        for index, principal in enumerate(runtime["permissions"]["dashboard"])
    )
    for task_key, schema, principal, filename in grants:
        tasks.append({
            "task_key": task_key,
            "depends_on": [{"task_key": "bootstrap"}],
            "notebook_task": {
                "notebook_path": Path(os.path.relpath(
                    Path(__file__).resolve().parents[1] / "sql" / filename,
                    output_dir,
                )).as_posix(),
                "source": "WORKSPACE",
                "warehouse_id": workspace["warehouse_id"],
                "base_parameters": {
                    "catalog": workspace["catalog"], "schema": schema, "principal": principal,
                },
            },
        })
    resources["apps"]["router_app"].update(app)
    resources["dashboards"]["router_dashboard"].update(dashboard)
    dashboard = resources["dashboards"]["router_dashboard"]
    resources["experiments"] = {
        "router_experiment": {"name": workspace["mlflow_experiment"]},
    }
    if runtime["permissions"]["dashboard"]:
        dashboard["permissions"] = [
            {**principal, "level": "CAN_READ"}
            for principal in runtime["permissions"]["dashboard"]
        ]
    _merge_resources(resources, demo_resources)
    # Includes add to DAB's default sync set; only exclusion prevents tracked
    # demo files from uploading in production. DAB rebases patterns relative to
    # the fragment declaring them, just like the local resource source paths.
    demo_pattern = (Path(os.path.relpath(_DEMO_RESOURCE_DIR.parent, output_dir)) / "**").as_posix()
    return {
        "resources": resources,
        "sync": {"include" if demo_resources else "exclude": [demo_pattern]},
    }


def _resource_summary(runtime: dict, demo: bool, demo_resources: dict) -> dict:
    # Only counts and fixed filenames leave the generated configuration boundary.
    summary = {
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
    if demo:
        summary["demo_resource_counts"] = {
            kind: len(declarations) for kind, declarations in sorted(demo_resources.items())
        }
    return summary


def _stage_app(staging: Path) -> dict[str, bytes]:
    """Copy only the deployed runtime, never an entire checkout or source folder."""
    sources = {f"app/{name}": _APP_SOURCE_DIR / name for name in _APP_MODULES}
    sources.update({
        "setup/__init__.py": Path(__file__).with_name("__init__.py"),
        "setup/models.py": Path(__file__).with_name("models.py"),
        "app.yaml": _APP_SOURCE_DIR / "app.yaml",
        "requirements.txt": _APP_SOURCE_DIR / "requirements.txt",
    })
    if _APP_SOURCE_DIR.is_symlink():
        raise ValueError("App source must not be a symlink")
    payload = {}
    for name, source in sorted(sources.items()):
        if source.is_symlink() or not source.is_file():
            raise ValueError("Required App source is missing or is a symlink")
        payload[name] = source.read_bytes()
    payload["runtime_config.json"] = (staging / "runtime_config.json").read_bytes()
    for name, data in sorted(payload.items()):
        destination = staging / "app_source" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    return payload


def _validate_artifacts(
    staging: Path, runtime: dict, resources: dict, summary: dict, app_files: dict[str, bytes],
) -> None:
    try:
        loaded_runtime = json.loads((staging / "runtime_config.json").read_text(encoding="utf-8"))
        RouterPackageConfig.model_validate(loaded_runtime)
        loaded_resources = yaml.load(
            (staging / "customer.resources.yml").read_text(encoding="utf-8"),
            Loader=_UniqueKeySafeLoader,
        )
        _validate_resource_document(loaded_resources, generated=True)
        loaded_summary = json.loads((staging / "summary.json").read_text(encoding="utf-8"))
        if (loaded_runtime, loaded_resources, loaded_summary) != (runtime, resources, summary):
            raise ValueError("Rendered artifacts differ from the validated configuration")
        source = staging / "app_source"
        paths = list(source.rglob("*"))
        if any(path.is_symlink() for path in paths):
            raise ValueError("Generated App source contains a symlink")
        staged_files = {path.relative_to(source).as_posix(): path.read_bytes()
                        for path in paths if path.is_file()}
        if staged_files != app_files:
            raise ValueError("Generated App source differs from the staged runtime")
        manifest = yaml.load(staged_files["app.yaml"], Loader=_UniqueKeySafeLoader)
        if not isinstance(manifest, dict) or manifest.get("command") != ["python", "-m", "app.app"]:
            raise ValueError("Generated App manifest has an invalid command")
        settings = manifest.get("env")
        if not isinstance(settings, list) or [
            setting for setting in settings
            if isinstance(setting, dict) and setting.get("name") == "ROUTER_CONFIG_PATH"
        ] != [{"name": "ROUTER_CONFIG_PATH", "value": "runtime_config.json"}]:
            raise ValueError("Generated App manifest has an invalid runtime path")
        if staged_files["runtime_config.json"] != (staging / "runtime_config.json").read_bytes():
            raise ValueError("Generated App configuration differs from compiler output")
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
    demo_resources = _load_demo_resources(output) if demo else {}
    resources = _resource_document(runtime, demo_resources, output)
    summary = _resource_summary(runtime, demo, demo_resources)
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
        app_files = _stage_app(staging)
        _validate_artifacts(staging, runtime, resources, summary, app_files)
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
        app_source=output / "app_source",
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
