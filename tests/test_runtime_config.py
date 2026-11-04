"""Runtime loading must reject invalid input and expose an immutable generation."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

import pytest

from setup.compile_config import compile_config
import yaml


def test_loads_compiler_output(modules, runtime_document, tmp_path, monkeypatch):
    source = tmp_path / "customer.yml"
    source.write_text(yaml.safe_dump(runtime_document))
    result = compile_config(source, tmp_path / ".generated", demo=False)
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(result.runtime_config))
    modules.runtime_config._load_runtime_config.cache_clear()
    try:
        config = modules.runtime_config.get_runtime_config()
        assert config.agents_by_name["support"].space_id == "01abc000000000000000000000000002"
        assert config.functions_by_name["monthly_revenue"].full_name == "acme_catalog.metrics.monthly_revenue"
    finally:
        modules.runtime_config._load_runtime_config.cache_clear()


def test_config_is_deeply_immutable(modules, runtime_document, install_runtime):
    config = install_runtime(runtime_document)
    for target, field, value in (
        (config, "genie_agents", ()),
        (config.workspace, "catalog", "elsewhere"),
        (config.customer, "name", "elsewhere"),
        (config.definitions, "branch", "elsewhere"),
        (config.routing, "confidence_threshold", 0.0),
        (config.genie_agents[0], "space_id", "f" * 32),
        (config.uc_functions[0], "aliases", ("elsewhere",)),
        (config.permissions.app[0], "group_name", "elsewhere"),
        (config.permissions, "app", ()),
    ):
        with pytest.raises((ValueError, AttributeError, TypeError)):
            setattr(target, field, value)
    with pytest.raises(TypeError):
        config.agents_by_name["sales"] = config.genie_agents[1]
    with pytest.raises(TypeError):
        config.functions_by_name["monthly_revenue"] = config.uc_functions[1]
    assert isinstance(config.genie_agents, tuple)
    assert isinstance(config.uc_functions[0].aliases, tuple)
    assert isinstance(config.permissions.app, tuple)


def test_loaded_generation_is_cached_even_if_file_and_environment_change(
    modules, runtime_document, install_runtime, monkeypatch, tmp_path,
):
    config = install_runtime(runtime_document)
    (tmp_path / "runtime.json").write_text("{}")
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(tmp_path / "missing.json"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        configs = list(pool.map(lambda _: modules.runtime_config.get_runtime_config(), range(12)))
    assert all(loaded is config for loaded in configs)


def test_concurrent_startup_reads_one_generation_once(modules, runtime_document, install_runtime, monkeypatch):
    install_runtime(runtime_document)
    modules.runtime_config._load_runtime_config.cache_clear()
    read_text = Path.read_text
    reads = []
    lock = Lock()
    def read_once(path, *args, **kwargs):
        with lock:
            reads.append(path)
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read_once)
    with ThreadPoolExecutor(max_workers=8) as pool:
        configs = list(pool.map(lambda _: modules.config.initialize_runtime(), range(32)))
    assert len(reads) == 1
    assert all(config is configs[0] for config in configs)


def test_startup_failure_prevents_ready_health_without_client_access(modules, tmp_path, monkeypatch):
    path = tmp_path / "invalid.json"
    path.write_text('{"token":"REJECTED_TEST_PLACEHOLDER"}')
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(path))
    modules.runtime_config._load_runtime_config.cache_clear()
    def forbidden():
        pytest.fail("Invalid startup configuration accessed the SDK")
    monkeypatch.setattr(modules.config, "get_client", forbidden)
    for action in (modules.config.initialize_runtime, modules.config.runtime_health):
        with pytest.raises(ValueError) as error:
            action()
        assert "REJECTED_TEST_PLACEHOLDER" not in str(error.value)


def test_refusal_sentinel_cannot_be_a_configured_domain(runtime_document, install_runtime):
    runtime_document["genie_agents"][0]["name"] = "out_of_domain"
    runtime_document["uc_functions"][0]["domain"] = "out_of_domain"
    with pytest.raises(ValueError):
        install_runtime(runtime_document)


@pytest.mark.parametrize("contents", ["", "[]", "{}", "null", '{"customer":', '{"x":1,"x":2}', b"\xff"])
def test_invalid_json_fails_closed(modules, tmp_path, monkeypatch, contents):
    path = tmp_path / "invalid.json"
    path.write_bytes(contents if isinstance(contents, bytes) else contents.encode())
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(path))
    modules.runtime_config._load_runtime_config.cache_clear()
    with pytest.raises(ValueError, match="runtime configuration"):
        modules.runtime_config.get_runtime_config()


@pytest.mark.parametrize("path", [None, "", "missing.json"])
def test_requires_explicit_readable_path(modules, monkeypatch, path):
    modules.runtime_config._load_runtime_config.cache_clear()
    monkeypatch.delenv("ROUTER_CONFIG_PATH", raising=False)
    if path is not None:
        monkeypatch.setenv("ROUTER_CONFIG_PATH", path)
    with pytest.raises(ValueError, match="ROUTER_CONFIG_PATH|runtime configuration"):
        modules.runtime_config.get_runtime_config()


@pytest.mark.parametrize("mutation", ["duplicate_domain", "duplicate_function", "unknown_domain", "foreign_catalog", "sql_fqn", "threshold", "profile", "secret"])
def test_runtime_revalidates_shared_contract(modules, runtime_document, install_runtime, mutation):
    if mutation == "duplicate_domain":
        runtime_document["genie_agents"][1]["name"] = "sales"
    elif mutation == "duplicate_function":
        runtime_document["uc_functions"][1]["name"] = "monthly_revenue"
    elif mutation == "unknown_domain":
        runtime_document["uc_functions"][0]["domain"] = "unknown"
    elif mutation == "foreign_catalog":
        runtime_document["uc_functions"][0]["full_name"] = "foreign.metrics.monthly_revenue"
    elif mutation == "sql_fqn":
        runtime_document["uc_functions"][0]["full_name"] += "(); DROP TABLE x"
    elif mutation == "threshold":
        runtime_document["routing"]["confidence_threshold"] = float("nan")
    elif mutation == "profile":
        runtime_document["workspace"]["profile"] = "REJECTED_TEST_PLACEHOLDER"
    else:
        runtime_document["definitions"]["token"] = "REJECTED_TEST_PLACEHOLDER"
    with pytest.raises(ValueError) as error:
        install_runtime(runtime_document)
    assert "REJECTED_TEST_PLACEHOLDER" not in str(error.value)


def test_fingerprint_and_health_are_stable_and_sanitized(modules, runtime_document, install_runtime):
    first = install_runtime(runtime_document)
    health = modules.config.runtime_health()
    assert health == {
        "status": "ready", "config_fingerprint": first.fingerprint,
        "domain_count": 2, "function_count": 2,
    }
    assert len(first.fingerprint) == 64
    int(first.fingerprint, 16)
    runtime_document["genie_agents"].reverse()
    runtime_document["uc_functions"].reverse()
    runtime_document["uc_functions"][1]["aliases"].reverse()
    assert install_runtime(runtime_document).fingerprint == first.fingerprint
    runtime_document["genie_agents"][0]["space_id"] = "f" * 32
    assert install_runtime(runtime_document).fingerprint != first.fingerprint
    for private in ("acme", "github", "abc123", "secret", "metrics", "/Shared"):
        assert private not in json.dumps(health)
