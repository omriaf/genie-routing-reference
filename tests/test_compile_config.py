"""Compiler boundary tests use synthetic references and never contact a workspace."""

import copy
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from setup.models import load_customer_config

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = Path(__file__).with_name("snapshots")


@pytest.fixture
def compiler():
    # A missing implementation is an explicit red assertion, not a collection error.
    try:
        return importlib.import_module("setup.compile_config")
    except ModuleNotFoundError as error:
        if error.name == "setup.compile_config":
            pytest.fail("compile_config is missing")
        raise


@pytest.fixture
def customer():
    return yaml.safe_load(
        (ROOT / "customer_config/customer.example.yml").read_text(encoding="utf-8")
    )


@pytest.fixture
def demo_resources(compiler, tmp_path, monkeypatch):
    directory = tmp_path / "demo/resources"
    directory.mkdir(parents=True)
    generators = directory.parent / "generators"
    generators.mkdir()
    (generators / "seed.py").write_text("# Databricks notebook source\nprint('synthetic data')\n")
    (directory / "demo.job.yml").write_text(yaml.safe_dump({
        "resources": {"jobs": {"demo_seed": {
            "name": "synthetic-seed",
            "tasks": [{"task_key": "seed", "notebook_task": {
                "notebook_path": "../generators/seed.py", "source": "WORKSPACE"
            }}],
        }}}
    }))
    monkeypatch.setattr(compiler, "_DEMO_RESOURCE_DIR", directory, raising=False)
    return directory


def write_config(directory, data):
    path = directory / "customer.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def files(directory):
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in sorted(directory.rglob("*")) if path.is_file()}


def portable_resources(path):
    document = yaml.safe_load(path.read_text())
    for task in document["resources"]["jobs"]["router_bootstrap"]["tasks"]:
        notebook = task["notebook_task"]
        if "notebook_path" in notebook:
            source = (path.parent / notebook["notebook_path"]).resolve()
            assert source == ROOT / "sql" / source.name
            assert source.is_file()
            notebook["notebook_path"] = f"../sql/{source.name}"
    dashboard = document["resources"]["dashboards"]["router_dashboard"]
    source = (path.parent / dashboard["file_path"]).resolve()
    assert source == ROOT / "dashboards/router_performance.lvdash.json"
    assert source.is_file()
    dashboard["file_path"] = "../dashboards/router_performance.lvdash.json"
    for patterns in document["sync"].values():
        for index, pattern in enumerate(patterns):
            assert Path(pattern).name == "**"
            assert (path.parent / Path(pattern).parent).resolve().name == "demo"
            patterns[index] = "../demo/**"
    return document


def compile_documents(compiler, tmp_path, customer, demo=False):
    config = write_config(tmp_path, customer)
    output = tmp_path / ".generated"
    result = compiler.compile_config(config, output, demo)
    return (
        result,
        json.loads((output / "runtime_config.json").read_text()),
        yaml.safe_load((output / "customer.resources.yml").read_text()),
        json.loads((output / "summary.json").read_text()),
    )


def test_compiled_artifacts_match_reviewed_snapshots(compiler, tmp_path, customer):
    result, runtime, resources, _ = compile_documents(compiler, tmp_path, customer)

    assert result.runtime_config.read_bytes() == (SNAPSHOTS / "compiled_runtime.json").read_bytes()
    assert portable_resources(result.resources) == yaml.safe_load((SNAPSHOTS / "compiled_resources.yml").read_text())
    assert [path.name for path in result.generated_paths] == [
        "customer.resources.yml", "runtime_config.json", "summary.json", "app_source"
    ]
    assert result.summary.name == "summary.json"
    assert runtime["routing"]["confidence_threshold"] == 0.75
    assert resources["resources"]["apps"]["router_app"]["name"] == "acme-analytics-router"
    assert load_customer_config(result.runtime_config) == load_customer_config(
        tmp_path / "customer.yml"
    )


def test_generated_source_is_complete_minimal_and_relative_to_its_resource(compiler, tmp_path, customer):
    result, _, document, _ = compile_documents(compiler, tmp_path, customer)
    source = (result.resources.parent / document["resources"]["apps"]["router_app"]["source_code_path"]).resolve()
    assert source.is_relative_to(result.resources.parent)
    assert source.is_dir()
    assert result.app_source == source
    assert sorted(files(source)) == json.loads((SNAPSHOTS / "compiled_app_files.json").read_text())
    manifest = yaml.safe_load((source / "app.yaml").read_text())
    runtime_path = next(item["value"] for item in manifest["env"] if item["name"] == "ROUTER_CONFIG_PATH")
    assert (source / runtime_path).read_bytes() == result.runtime_config.read_bytes()
    assert load_customer_config(source / runtime_path) == load_customer_config(tmp_path / "customer.yml")


def test_staging_uses_an_allowlist_and_never_copies_private_or_unrelated_files(
    compiler, tmp_path, customer, monkeypatch,
):
    source = tmp_path / "package/app"
    shutil.copytree(ROOT / "app", source, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("customer.yml", ".env", "private.py", "tests/test_private.py", ".git/config", "docs/internal.md"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SOURCE_SECRET_MUST_NOT_BE_STAGED")
    monkeypatch.setattr(compiler, "_APP_SOURCE_DIR", source, raising=False)
    result, _, document, _ = compile_documents(compiler, tmp_path, customer)
    staged = result.resources.parent / document["resources"]["apps"]["router_app"]["source_code_path"]
    assert staged.is_dir()
    assert sorted(files(staged)) == json.loads((SNAPSHOTS / "compiled_app_files.json").read_text())
    assert all(b"SOURCE_SECRET_MUST_NOT_BE_STAGED" not in content for content in files(staged).values())


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("problem", ["missing_module", "symlink_module", "write_failure", "corrupt_runtime", "bad_manifest"])
def test_app_staging_failure_preserves_the_complete_generation(
    compiler, tmp_path, customer, monkeypatch, existing, problem,
):
    config = write_config(tmp_path, customer)
    output = tmp_path / ".generated"
    if existing:
        compiler.compile_config(config, output, False)
    before = files(output) if existing else None
    source = tmp_path / "package/app"
    shutil.copytree(ROOT / "app", source, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(compiler, "_APP_SOURCE_DIR", source, raising=False)
    if problem in {"missing_module", "symlink_module"}:
        (source / "router.py").unlink()
        if problem == "symlink_module":
            private = tmp_path / "private"
            private.write_text("SOURCE_SECRET_MUST_NOT_BE_STAGED")
            (source / "router.py").symlink_to(private)
    elif problem == "bad_manifest":
        manifest = yaml.safe_load((source / "app.yaml").read_text())
        manifest["env"][0]["value"] = "missing-runtime.json"
        (source / "app.yaml").write_text(yaml.safe_dump(manifest))
    else:
        write_bytes = Path.write_bytes

        def broken_stage(path, data):
            if path.parent.name == "app_source" and path.name == "runtime_config.json":
                if problem == "write_failure":
                    raise OSError("Simulated staged App write failure")
                data = b"{}"
            return write_bytes(path, data)

        monkeypatch.setattr(Path, "write_bytes", broken_stage)
    with pytest.raises((ValueError, OSError)):
        compiler.compile_config(config, output, False)
    assert (files(output) if output.exists() else None) == before
    assert not list(tmp_path.glob(".generated-*"))


def test_staged_manifest_launches_with_its_own_packages_and_runtime(compiler, tmp_path, customer):
    result, _, document, _ = compile_documents(compiler, tmp_path, customer)
    source = result.resources.parent / document["resources"]["apps"]["router_app"]["source_code_path"]
    assert source.is_dir()
    # Isolated Python excludes the checkout and PYTHONPATH. Only the generated
    # source root is added; network and server binding are the external doubles.
    script = '''
import base64
import json
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
sys.path.insert(0, str(Path.cwd()))
import yaml
import uvicorn
from fastapi.testclient import TestClient
from app import definitions
from app.runtime_config import get_runtime_config
from setup import models
assert Path(definitions.__file__).is_relative_to(Path.cwd())
assert Path(models.__file__).is_relative_to(Path.cwd())
manifest = yaml.safe_load(Path('app.yaml').read_text())
for setting in manifest['env']:
    if 'value' in setting:
        os.environ[setting['name']] = setting['value']
os.environ['GITHUB_DEFINITIONS_TOKEN'] = 'synthetic-bound-token'
os.environ['DATABRICKS_APP_PORT'] = '8123'
runtime = get_runtime_config()
assert runtime.customer.app_name == 'acme-analytics-router'
text = '---\\nname: monthly_revenue\\ndomain: sales\\naliases: []\\ncertified_function: monthly_revenue\\n---\\nMonthly revenue.\\n'
class GitHub:
    def get(self, url, **kwargs):
        if '/commits/' in url:
            body = {'sha': 'a' * 40}
        elif '/contents/definitions?' in url:
            body = [{'name': 'revenue.md', 'path': 'definitions/revenue.md', 'type': 'file'}]
        else:
            body = {'type': 'file', 'path': 'definitions/revenue.md', 'size': len(text.encode()),
                    'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode()}
        return definitions.GitHubResponse(200, json.dumps(body).encode(), {})
definitions.GitHubTransport = GitHub
sys.modules['mlflow'] = SimpleNamespace(set_tracking_uri=lambda _: None, set_experiment=lambda _: None)
def serve(app, *, host, port):
    assert (host, port) == ('0.0.0.0', 8123)
    with TestClient(app) as client:
        assert client.get('/health').json()['ready'] is True
        response = client.post('/mcp', headers={'Accept': 'application/json, text/event-stream'},
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, follow_redirects=False)
        assert response.status_code == 200
        assert [tool['name'] for tool in response.json()['result']['tools']] == ['ask_analytics']
uvicorn.run = serve
assert manifest['command'][:2] == ['python', '-m']
runpy.run_module(manifest['command'][2], run_name='__main__')
print('staged manifest startup passed')
'''
    completed = subprocess.run([sys.executable, "-I", "-c", script], check=False, cwd=source,
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert "staged manifest startup passed" in completed.stdout


def test_permuted_lists_and_mapping_order_produce_identical_bytes(compiler, tmp_path, customer):
    customer["permissions"] = {
        "app": [{"user_name": "z@example.com"}, {"group_name": "z-users"}, {"group_name": "a-users"}],
        "dashboard": [{"user_name": "z@example.com"}, {"user_name": "a@example.com"}],
    }
    path = write_config(tmp_path, customer)
    first = tmp_path / "first"
    compiler.compile_config(path, first, False)
    changed = copy.deepcopy(customer)
    for key in ("genie_agents", "uc_functions"):
        changed[key].reverse()
    for function in changed["uc_functions"]:
        function["aliases"].reverse()
    for principals in changed["permissions"].values():
        principals.reverse()
    changed = dict(reversed(list(changed.items())))
    path = write_config(tmp_path, changed)
    second = tmp_path / "second"
    compiler.compile_config(path, second, False)

    assert files(first) == files(second)
    runtime = json.loads(files(first)["runtime_config.json"])
    assert runtime["permissions"]["app"] == [
        {"group_name": "a-users"}, {"group_name": "z-users"}, {"user_name": "z@example.com"}
    ]
    bindings = yaml.safe_load(files(first)["customer.resources.yml"])["resources"]["apps"]["router_app"]["resources"]
    assert [entry["name"] for entry in bindings] == sorted(entry["name"] for entry in bindings)


def test_one_genie_binding_per_agent_and_exact_function_allowlist(compiler, tmp_path, customer):
    customer["genie_agents"].append({
        "name": "operations", "description": "Operations", "space_id": "f" * 32,
        "routing_guidance": "Operational questions",
    })
    customer["uc_functions"][0]["full_name"] = "acme_catalog.Certified.MonthlyRevenue"
    _, runtime, resource, _ = compile_documents(compiler, tmp_path, customer)
    bindings = resource["resources"]["apps"]["router_app"]["resources"]

    assert {item["name"]: item["genie_space"] for item in bindings if "genie_space" in item} == {
        "genie_operations": {"name": "operations", "space_id": "f" * 32, "permission": "CAN_RUN"},
        "genie_sales": {"name": "sales", "space_id": "01abc000000000000000000000000001", "permission": "CAN_RUN"},
        "genie_support": {"name": "support", "space_id": "01abc000000000000000000000000002", "permission": "CAN_RUN"},
    }
    assert {item["name"]: (item["full_name"], item["domain"]) for item in runtime["uc_functions"]} == {
        "monthly_revenue": ("acme_catalog.Certified.MonthlyRevenue", "sales"),
        "resolution_time": ("acme_catalog.metrics.resolution_time", "support"),
    }
    function_bindings = [item["uc_securable"] for item in bindings
                         if item.get("uc_securable", {}).get("securable_type") == "FUNCTION"]
    assert function_bindings == [
        {"permission": "EXECUTE", "securable_full_name": "acme_catalog.Certified.MonthlyRevenue", "securable_type": "FUNCTION"},
        {"permission": "EXECUTE", "securable_full_name": "acme_catalog.metrics.resolution_time", "securable_type": "FUNCTION"},
    ]


def test_secret_is_reference_only_and_summary_is_sanitized(compiler, tmp_path, customer, monkeypatch):
    marker = "ENVIRONMENT_CONTENT_MUST_STAY_PRIVATE"
    for name in ("GITHUB_TOKEN", "DATABRICKS_TOKEN", "DATABRICKS_HOST", "DATABRICKS_CONFIG_PROFILE"):
        monkeypatch.setenv(name, marker)
    customer["genie_agents"][0]["description"] = "Private business description"
    _, runtime, resource, summary = compile_documents(compiler, tmp_path, customer)
    bindings = resource["resources"]["apps"]["router_app"]["resources"]

    assert next(item["secret"] for item in bindings if "secret" in item) == {
        "scope": "analytics-router", "key": "github-token", "permission": "READ"
    }
    assert runtime["definitions"]["secret_scope"] == "analytics-router"
    assert summary == {
        "demo": False,
        "files": ["customer.resources.yml", "runtime_config.json", "summary.json", "app_source"],
        "permissions": {"app": 1, "dashboard": 1},
        "resource_counts": {"genie_spaces": 2, "secrets": 1, "serving_endpoints": 1,
                            "sql_warehouses": 1, "trace_tables": 1, "uc_functions": 2},
    }
    for content in files(tmp_path / ".generated").values():
        assert marker.encode() not in content
        assert b"DATABRICKS_HOST" not in content
        assert b"DATABRICKS_CONFIG_PROFILE" not in content
    assert "Private business description" not in json.dumps(summary)
    assert "github.com" not in json.dumps(summary)


def test_empty_functions_and_permissions_need_no_extra_grants(compiler, tmp_path, customer):
    customer["uc_functions"] = []
    customer.pop("permissions")
    _, runtime, resources, _ = compile_documents(compiler, tmp_path, customer)

    assert runtime["uc_functions"] == []
    assert runtime["permissions"] == {"app": [], "dashboard": []}
    assert "permissions" not in resources["resources"]["apps"]["router_app"]
    assert "permissions" not in resources["resources"]["dashboards"]["router_dashboard"]
    tasks = resources["resources"]["jobs"]["router_bootstrap"]["tasks"]
    assert {task["task_key"] for task in tasks} == {"bootstrap", "app_access_0"}
    assert load_customer_config(tmp_path / ".generated/runtime_config.json").uc_functions == []


def test_quoted_multiline_and_template_text_round_trip(compiler, tmp_path, customer):
    text = 'Quoted "text": yes\n{{ dangerous }}\n- false\nUnicode: café'
    customer["genie_agents"][0]["routing_guidance"] = text
    customer["uc_functions"][0]["aliases"] = [text, "001", "false"]
    _, runtime, _, _ = compile_documents(compiler, tmp_path, customer)

    assert runtime["genie_agents"][0]["routing_guidance"] == text
    assert runtime["uc_functions"][0]["aliases"] == ["001", text, "false"]


def test_demo_resources_are_opt_in_and_removed_on_recompile(compiler, tmp_path, customer, demo_resources):
    path = write_config(tmp_path, customer)
    output = tmp_path / ".generated"
    compiler.compile_config(path, output, True)
    document = yaml.safe_load((output / "customer.resources.yml").read_text())
    assert set(document) == {"resources", "sync"}
    assert document["sync"] == {"include": ["../demo/**"]}
    assert document["resources"]["jobs"]["demo_seed"] == {
        "name": "synthetic-seed",
        "tasks": [{"task_key": "seed", "notebook_task": {
            "notebook_path": "../demo/generators/seed.py", "source": "WORKSPACE"
        }}],
    }
    summary = json.loads((output / "summary.json").read_text())
    assert summary["demo"] is True
    assert summary["demo_resource_counts"] == {"jobs": 1}
    compiler.compile_config(path, output, False)
    document = yaml.safe_load((output / "customer.resources.yml").read_text())
    assert set(document) == {"resources", "sync"}
    assert document["sync"] == {"exclude": ["../demo/**"]}
    assert set(document["resources"]["jobs"]) == {"router_bootstrap"}
    assert json.loads((output / "summary.json").read_text())["demo"] is False
    assert portable_resources(output / "customer.resources.yml") == yaml.safe_load((SNAPSHOTS / "compiled_resources.yml").read_text())


@pytest.mark.parametrize("demo", [False, True])
def test_generated_resource_document_has_no_nested_include(compiler, tmp_path, customer, demo_resources, demo):
    _, _, document, _ = compile_documents(compiler, tmp_path, customer, demo)

    def check_shape(value):
        if isinstance(value, dict):
            assert "include" not in value
            for child in value.values():
                check_shape(child)
        elif isinstance(value, list):
            for child in value:
                check_shape(child)

    assert set(document) == {"resources", "sync"}
    assert "include" not in document
    assert set(document["resources"]) == {"apps", "dashboards", "jobs", "experiments"}
    for declarations in document["resources"].values():
        assert isinstance(declarations, dict)
        assert all(isinstance(body, dict) for body in declarations.values())
    # sync.include is a DAB path list, not a recursive bundle-file include.
    check_shape(document["resources"])


def test_demo_merge_is_deterministic_across_file_and_mapping_order(compiler, tmp_path, customer, demo_resources):
    path = write_config(tmp_path, customer)
    first = tmp_path / "first"
    other = {"resources": {"jobs": {"demo_other": {"name": "synthetic-other", "tasks": []}}}}
    (demo_resources / "a.yml").write_text(yaml.safe_dump(other))
    compiler.compile_config(path, first, True)
    (demo_resources / "a.yml").unlink()
    other["resources"]["jobs"]["demo_other"] = {"tasks": [], "name": "synthetic-other"}
    (demo_resources / "z.yaml").write_text(yaml.safe_dump(other, sort_keys=False))
    second = tmp_path / "second"
    compiler.compile_config(path, second, True)

    assert files(first) == files(second)
    assert json.loads(files(first)["summary.json"])["demo_resource_counts"] == {"jobs": 2}


@pytest.mark.parametrize("bad_yaml", [
    "resources: [",
    "include: [child.yml]\nresources: {}\n",
    "resources: []\n",
    "resources:\n  jobs: []\n",
    "resources:\n  jobs:\n    broken: []\n",
    "resources:\n  jobs:\n    broken:\n      include: [child.yml]\n",
    "resources:\n  jobs:\n    duplicate: {name: first}\n    duplicate: {name: second}\n",
    "resources:\n  apps:\n    router_app: {name: unexpected-app}\n",
])
def test_invalid_demo_resources_preserve_output_and_are_ignored_in_production(
    compiler, tmp_path, customer, demo_resources, bad_yaml
):
    path = write_config(tmp_path, customer)
    output = tmp_path / ".generated"
    compiler.compile_config(path, output, False)
    before = files(output)
    (demo_resources / "demo.job.yml").write_text(bad_yaml)

    with pytest.raises(ValueError):
        compiler.compile_config(path, output, True)

    assert files(output) == before
    assert not list(tmp_path.glob(".generated-*"))
    compiler.compile_config(path, output, False)
    assert files(output) == before


def test_demo_resource_name_collisions_are_rejected(compiler, tmp_path, customer, demo_resources):
    path = write_config(tmp_path, customer)
    output = tmp_path / ".generated"
    compiler.compile_config(path, output, False)
    before = files(output)
    (demo_resources / "other.yml").write_bytes((demo_resources / "demo.job.yml").read_bytes())

    with pytest.raises(ValueError):
        compiler.compile_config(path, output, True)

    assert files(output) == before


def test_missing_demo_resources_fail_without_replacing_output(compiler, tmp_path, customer, demo_resources):
    path = write_config(tmp_path, customer)
    output = tmp_path / ".generated"
    compiler.compile_config(path, output, False)
    before = files(output)
    (demo_resources / "demo.job.yml").unlink()

    with pytest.raises(ValueError):
        compiler.compile_config(path, output, True)

    assert files(output) == before


def test_demo_paths_preserve_absolute_remote_and_git_sources(compiler, tmp_path, customer, demo_resources):
    source = yaml.safe_load((demo_resources / "demo.job.yml").read_text())
    source["resources"]["jobs"]["demo_seed"]["tasks"] = [
        {"task_key": "local", "spark_python_task": {"python_file": "../generators/seed.py"}},
        {"task_key": "git", "notebook_task": {"notebook_path": "relative/git/path", "source": "GIT"}},
        {"task_key": "workspace", "notebook_task": {"notebook_path": "/Shared/seed"}},
        {"task_key": "remote", "spark_python_task": {"python_file": "s3://sample/seed.py"}},
        {"task_key": "substitution", "notebook_task": {"notebook_path": "${workspace.file_path}/seed"}},
    ]
    (demo_resources / "demo.job.yml").write_text(yaml.safe_dump(source))
    _, _, document, _ = compile_documents(compiler, tmp_path, customer, True)
    tasks = document["resources"]["jobs"]["demo_seed"]["tasks"]

    assert tasks == [
        {"task_key": "local", "spark_python_task": {"python_file": "../demo/generators/seed.py"}},
        {"task_key": "git", "notebook_task": {"notebook_path": "relative/git/path", "source": "GIT"}},
        {"task_key": "workspace", "notebook_task": {"notebook_path": "/Shared/seed"}},
        {"task_key": "remote", "spark_python_task": {"python_file": "s3://sample/seed.py"}},
        {"task_key": "substitution", "notebook_task": {"notebook_path": "${workspace.file_path}/seed"}},
    ]


@pytest.mark.parametrize("template,broken", [
    ("runtime-config.json.j2", "{"),
    ("runtime-config.json.j2", "{}"),
    ("app-resource.yml.j2", "resources: ["),
    ("app-resource.yml.j2", "resources: {}"),
    ("app-resource.yml.j2", "resources:\n  apps:\n    router_app:\n      name: wrong-app\n"),
    ("runtime-config.json.j2", "{{ missing_template_variable }}"),
])
@pytest.mark.parametrize("existing", [False, True])
def test_render_failure_preserves_entire_existing_output(compiler, tmp_path, customer, monkeypatch, template, broken, existing):
    path = write_config(tmp_path, customer)
    output = tmp_path / ".generated"
    if existing:
        compiler.compile_config(path, output, False)
    before = files(output) if existing else None
    templates = tmp_path / "templates"
    shutil.copytree(ROOT / "setup/templates", templates)
    (templates / template).write_text(broken, encoding="utf-8")
    monkeypatch.setattr(compiler, "_TEMPLATE_DIR", templates)

    with pytest.raises(ValueError):
        compiler.compile_config(path, output, False)

    assert (files(output) if output.exists() else None) == before
    assert not list(tmp_path.glob(".generated-*"))


def test_publication_failure_preserves_output_and_cleans_staging(compiler, tmp_path, customer, monkeypatch, demo_resources):
    path = write_config(tmp_path, customer)
    output = tmp_path / ".generated"
    compiler.compile_config(path, output, False)
    before = files(output)

    def fail_exchange(source, destination):
        raise OSError("Simulated filesystem swap failure")

    monkeypatch.setattr(compiler, "_exchange_directories", fail_exchange)
    with pytest.raises(OSError):
        compiler.compile_config(path, output, True)

    assert files(output) == before
    assert not list(tmp_path.glob(".generated-*"))


def test_invalid_summary_preserves_output(compiler, tmp_path, customer, monkeypatch):
    path = write_config(tmp_path, customer)
    output = tmp_path / ".generated"
    compiler.compile_config(path, output, False)
    before = files(output)
    write_text = Path.write_text

    def corrupt_summary(self, data, *args, **kwargs):
        return write_text(self, "{" if self.name == "summary.json" else data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", corrupt_summary)
    with pytest.raises(ValueError):
        compiler.compile_config(path, output, False)

    assert files(output) == before
    assert not list(tmp_path.glob(".generated-*"))


def test_staging_write_failure_preserves_output(compiler, tmp_path, customer, monkeypatch):
    path = write_config(tmp_path, customer)
    output = tmp_path / ".generated"
    compiler.compile_config(path, output, False)
    before = files(output)
    write_text = Path.write_text

    def fail_summary_write(self, data, *args, **kwargs):
        if self.name == "summary.json":
            raise OSError("Simulated full filesystem")
        return write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_summary_write)
    with pytest.raises(OSError):
        compiler.compile_config(path, output, False)

    assert files(output) == before
    assert not list(tmp_path.glob(".generated-*"))


def test_success_replaces_directory_instead_of_overwriting_files(compiler, tmp_path, customer, demo_resources):
    path = write_config(tmp_path, customer)
    output = tmp_path / ".generated"
    compiler.compile_config(path, output, True)
    inode = output.stat().st_ino
    (output / "obsolete.yml").write_text("obsolete: true\n")
    (output / "app_source/obsolete.py").write_text("PRIVATE_STALE_SOURCE")
    customer["routing"]["confidence_threshold"] = 0.85
    write_config(tmp_path, customer)
    with (output / "summary.json").open() as old_summary, (output / "app_source/runtime_config.json").open() as old_runtime:
        compiler.compile_config(path, output, False)
        assert json.load(old_summary)["demo"] is True
        assert json.load(old_runtime)["routing"]["confidence_threshold"] == 0.75

    assert output.stat().st_ino != inode
    assert sorted(path.name for path in output.iterdir()) == [
        "app_source", "customer.resources.yml", "runtime_config.json", "summary.json"
    ]
    assert sorted(files(output / "app_source")) == json.loads((SNAPSHOTS / "compiled_app_files.json").read_text())
    assert (output / "runtime_config.json").read_bytes() == (output / "app_source/runtime_config.json").read_bytes()
    assert json.loads((output / "app_source/runtime_config.json").read_text())["routing"]["confidence_threshold"] == 0.85
    assert not list(tmp_path.glob(".generated-*"))


@pytest.mark.parametrize("target", ["symlink", "input_parent", "file"])
def test_unsafe_output_destination_is_rejected(compiler, tmp_path, customer, target):
    path = write_config(tmp_path, customer)
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "keep").write_text("preserve")
    output = tmp_path / ".generated"
    if target == "symlink":
        output.symlink_to(protected, target_is_directory=True)
    elif target == "input_parent":
        output = tmp_path
    else:
        output.write_text("preserve")

    with pytest.raises(ValueError):
        compiler.compile_config(path, output, False)

    assert (protected / "keep").read_text() == "preserve"
    assert path.exists()


def test_config_inside_output_through_symlink_is_preserved(compiler, tmp_path, customer):
    output = tmp_path / ".generated"
    output.mkdir()
    real_config = write_config(output, customer)
    config_link = tmp_path / "config-link.yml"
    config_link.symlink_to(real_config)
    before = files(output)

    with pytest.raises(ValueError):
        compiler.compile_config(config_link, output, False)

    assert files(output) == before


def test_invalid_config_preserves_existing_output(compiler, tmp_path, customer):
    path = write_config(tmp_path, customer)
    output = tmp_path / ".generated"
    compiler.compile_config(path, output, False)
    before = files(output)
    customer["uc_functions"][0]["domain"] = "missing"
    write_config(tmp_path, customer)

    with pytest.raises(ValueError):
        compiler.compile_config(path, output, False)
    assert files(output) == before


def test_cli_prints_generated_paths_and_redacted_summary(tmp_path, customer, demo_resources):
    shutil.copytree(ROOT / "setup", tmp_path / "setup", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "app", tmp_path / "app", ignore=shutil.ignore_patterns("__pycache__"))
    for directory in ("resources", "sql", "dashboards"):
        shutil.copytree(ROOT / directory, tmp_path / directory)
    path = write_config(tmp_path, customer)
    output = tmp_path / ".generated"
    command = [sys.executable, "-m", "setup.compile_config", "--config", str(path), "--output", str(output)]
    env = dict(os.environ, GITHUB_TOKEN="ENVIRONMENT_CONTENT_MUST_STAY_PRIVATE")
    completed = subprocess.run(command, check=False, cwd=tmp_path, env=env, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    for name in ("customer.resources.yml", "runtime_config.json", "summary.json", "app_source"):
        assert str(output / name) in completed.stdout
    for forbidden in ("ENVIRONMENT_CONTENT_MUST_STAY_PRIVATE", "01abc", "acme_catalog", "github.com", "analytics-users", "github-token"):
        assert forbidden not in completed.stdout + completed.stderr
    assert json.loads((output / "summary.json").read_text())["demo"] is False
    demo = subprocess.run(command + ["--demo"], check=False, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert demo.returncode == 0, demo.stderr
    assert json.loads((output / "summary.json").read_text())["demo"] is True
    assert "demo_seed" in yaml.safe_load((output / "customer.resources.yml").read_text())["resources"]["jobs"]
    assert "ENVIRONMENT_CONTENT_MUST_STAY_PRIVATE" not in demo.stdout + demo.stderr


@pytest.mark.parametrize("bad_yaml", [
    "token: [REJECTED_CONTENT", "token: REJECTED_CONTENT", "REJECTED_CONTENT: value"
])
def test_cli_error_does_not_echo_rejected_input(tmp_path, bad_yaml):
    path = tmp_path / "customer.yml"
    path.write_text(bad_yaml)
    completed = subprocess.run(
        [sys.executable, "-m", "setup.compile_config", "--config", str(path), "--output", str(tmp_path / ".generated")],
        check=False, cwd=ROOT, capture_output=True, text=True,
    )
    assert completed.returncode != 0
    assert "REJECTED_CONTENT" not in completed.stdout + completed.stderr
    assert "Traceback" not in completed.stderr
    assert not (tmp_path / ".generated").exists()


@pytest.mark.parametrize("flag", ["--conf", "--profile", "--host"])
def test_cli_rejects_unsupported_options(tmp_path, customer, flag):
    path = write_config(tmp_path, customer)
    output = tmp_path / ".generated"
    completed = subprocess.run(
        [sys.executable, "-m", "setup.compile_config", "--config", str(path),
         "--output", str(output), flag, "REJECTED_CONTENT"],
        check=False, cwd=ROOT, capture_output=True, text=True,
    )

    assert completed.returncode != 0
    assert "REJECTED_CONTENT" not in completed.stdout + completed.stderr
    assert not output.exists()


def test_root_bundle_directly_includes_generated_resource_document(compiler, tmp_path, customer):
    root_config = ROOT / "databricks.yml"
    assert root_config.is_file(), "The generated resource document must be included by the root bundle"
    shutil.copyfile(root_config, tmp_path / "databricks.yml")
    compile_documents(compiler, tmp_path, customer)
    root = yaml.safe_load((tmp_path / "databricks.yml").read_text())

    assert root["bundle"]["name"] == "genie-routing-reference"
    assert ".generated/customer.resources.yml" in root["include"]
    document = yaml.safe_load((tmp_path / ".generated/customer.resources.yml").read_text())
    assert set(document) == {"resources", "sync"}
    assert "router_app" in document["resources"]["apps"]
    for path in (ROOT / "resources").glob("*.yml"):
        assert set(yaml.safe_load(path.read_text())) == {"resources"}


@pytest.mark.parametrize("demo", [False, True])
def test_local_bundle_loader_loads_generated_resources_without_profile(
    compiler, tmp_path, customer, demo_resources, demo
):
    executable = shutil.which("databricks")
    if executable is None:
        pytest.skip("Databricks CLI unavailable; root/resource contract is tested separately")
    root_config = ROOT / "databricks.yml"
    assert root_config.is_file(), "Root bundle is missing"
    shutil.copyfile(root_config, tmp_path / "databricks.yml")
    for directory in ("resources", "sql", "dashboards"):
        shutil.copytree(ROOT / directory, tmp_path / directory)
    _, _, document, _ = compile_documents(compiler, tmp_path, customer, demo)
    empty_credentials = tmp_path / "empty.cfg"
    empty_credentials.write_text("")
    completed = subprocess.run(
        [executable, "bundle", "validate", "--target", "customer", "--output", "json"],
        check=False, cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "DATABRICKS_CONFIG_FILE": str(empty_credentials)},
        capture_output=True, text=True, timeout=15,
    )

    # The CLI exposes the locally loaded configuration before its auth visitor.
    assert completed.returncode == 0 or (
        completed.returncode == 1 and "cannot configure default credentials" in completed.stderr
    ), completed.stderr
    assert "Warning:" not in completed.stderr
    loaded = json.loads(completed.stdout)
    assert ".generated/customer.resources.yml" in loaded["include"]
    assert loaded["resources"]["apps"]["router_app"]["resources"] == document["resources"]["apps"]["router_app"]["resources"]
    assert ("demo_seed" in loaded["resources"].get("jobs", {})) is demo
    bootstrap = loaded["resources"]["jobs"]["router_bootstrap"]["tasks"][0]
    assert bootstrap["task_key"] == "bootstrap"
    assert Path(bootstrap["notebook_task"]["notebook_path"]).name == "bootstrap.sql"
    assert bootstrap["notebook_task"]["warehouse_id"] == "abc123"
    if demo:
        assert loaded["resources"]["jobs"]["demo_seed"]["tasks"] == document["resources"]["jobs"]["demo_seed"]["tasks"]
