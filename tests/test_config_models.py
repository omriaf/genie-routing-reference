"""Exercise the public configuration boundary with synthetic resource references."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from setup.models import RouterPackageConfig, load_customer_config


@pytest.fixture
def valid_config():
    return {
        "customer": {"name": "acme", "app_name": "acme-analytics-router"},
        "workspace": {
            "catalog": "acme_catalog",
            "router_schema": "analytics_router",
            "warehouse_id": "abc123",
            "classifier_endpoint": "databricks-claude-sonnet-4-5",
            "mlflow_experiment": "/Shared/acme-analytics-router",
        },
        "definitions": {
            "access": "private",
            "repository_url": "https://github.com/acme/business-definitions.git",
            "branch": "main",
            "markdown_path": "definitions",
            "secret_scope": "analytics-router",
            "secret_key": "github-token",
            "poll_seconds": 90,
        },
        "routing": {
            "confidence_threshold": 0.75,
            "overlap_threshold": 0.70,
            "prefer_certified_functions": True,
        },
        "genie_agents": [
            {
                "name": "sales",
                "description": "Sales pipeline and revenue analytics",
                "space_id": "01abc000000000000000000000000001",
                "routing_guidance": "Route sales, pipeline, and revenue questions here",
            },
            {
                "name": "support",
                "description": "Customer support analytics",
                "space_id": "01abc000000000000000000000000002",
                "routing_guidance": "Route support and resolution questions here",
            },
        ],
        "uc_functions": [
            {
                "name": "monthly_revenue",
                "full_name": "acme_catalog.metrics.monthly_revenue",
                "domain": "sales",
                "description": "Certified monthly revenue",
                "aliases": ["monthly sales", "revenue by month"],
            },
            {
                "name": "resolution_time",
                "full_name": "acme_catalog.metrics.resolution_time",
                "domain": "support",
                "description": "Certified average resolution time",
                "aliases": ["time to resolution"],
            },
        ],
    }


def test_accepts_multiple_agents_and_functions(valid_config):
    config = RouterPackageConfig.model_validate(valid_config)

    assert [agent.name for agent in config.genie_agents] == ["sales", "support"]
    assert [function.domain for function in config.uc_functions] == ["sales", "support"]
    assert config.uc_functions[0].full_name == "acme_catalog.metrics.monthly_revenue"
    assert config.routing.prefer_certified_functions is True


def test_public_definitions_require_no_secret_reference(valid_config):
    valid_config["definitions"]["access"] = "public"
    valid_config["definitions"].pop("secret_scope")
    valid_config["definitions"].pop("secret_key")

    config = RouterPackageConfig.model_validate(valid_config)

    assert config.definitions.access == "public"
    assert config.definitions.secret_scope is None


def test_private_definitions_require_both_secret_references(valid_config):
    valid_config["definitions"].pop("secret_key")

    with pytest.raises(ValidationError, match="private.*secret"):
        RouterPackageConfig.model_validate(valid_config)


def test_rejects_duplicate_agent_names(valid_config):
    valid_config["genie_agents"][1]["name"] = "sales"
    with pytest.raises(ValidationError, match="[Dd]uplicate.*[Aa]gent"):
        RouterPackageConfig.model_validate(valid_config)


def test_agent_name_cannot_shadow_the_runtime_refusal_marker(valid_config):
    valid_config["genie_agents"][0]["name"] = "out_of_domain"
    valid_config["uc_functions"][0]["domain"] = "out_of_domain"
    with pytest.raises(ValidationError):
        RouterPackageConfig.model_validate(valid_config)


def test_rejects_function_with_unknown_domain(valid_config):
    valid_config["uc_functions"][0]["domain"] = "unknown"
    with pytest.raises(ValidationError, match="[Uu]nknown.*domain"):
        RouterPackageConfig.model_validate(valid_config)


@pytest.mark.parametrize("field", ["token", "password", "secret_value", "github_token"])
@pytest.mark.parametrize(
    "section",
    [
        None,
        "customer",
        "workspace",
        "definitions",
        "routing",
        "agent",
        "function",
        "permissions",
        "principal",
    ],
)
def test_rejects_secret_value_field(valid_config, field, section):
    valid_config["permissions"] = {"app": [{"group_name": "analytics-users"}]}
    target = _section(valid_config, section)
    target[field] = "REJECTED_TEST_PLACEHOLDER"

    with pytest.raises(ValidationError, match="[Ee]xtra inputs") as exc_info:
        RouterPackageConfig.model_validate(valid_config)
    assert "REJECTED_TEST_PLACEHOLDER" not in str(exc_info.value)


def _section(config, section):
    if section is None:
        return config
    if section == "agent":
        return config["genie_agents"][0]
    if section == "function":
        return config["uc_functions"][0]
    if section == "principal":
        return config["permissions"]["app"][0]
    return config[section]


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/business-definitions.git",
        "git@github.com:acme/business-definitions.git",
        "https://example.com/acme/business-definitions.git",
        "https://github.com.example.com/acme/business-definitions.git",
        "https://github.com/acme",
        "https://github.com/acme/business-definitions/tree/main",
        "https://placeholder@github.com/acme/business-definitions.git",
        "https://github.com/acme/business-definitions.git?token=PLACEHOLDER",
        "https://github.com/acme/business-definitions.git#fragment",
        "https://github.com:8443/acme/business-definitions.git",
        "https://github.com/acme/..",
        "https://github.com/acme/business-\ndefinitions.git",
        "https://github.com/acme/business-definitions.git\t",
        "https://g\u0130thub.com/acme/business-definitions.git",
    ],
)
def test_requires_https_github_repository(valid_config, url):
    valid_config["definitions"]["repository_url"] = url
    with pytest.raises(ValidationError):
        RouterPackageConfig.model_validate(valid_config)


@pytest.mark.parametrize("suffix", ["", ".git"])
def test_accepts_https_github_repository(valid_config, suffix):
    url = f"https://github.com/acme/business-definitions{suffix}"
    valid_config["definitions"]["repository_url"] = url
    assert (
        RouterPackageConfig.model_validate(valid_config).definitions.repository_url
        == url
    )


@pytest.mark.parametrize("field", ["confidence_threshold", "overlap_threshold"])
@pytest.mark.parametrize(
    "value", [-0.01, 1.01, float("nan"), float("inf"), True, "0.75"]
)
def test_thresholds_are_between_zero_and_one(valid_config, field, value):
    valid_config["routing"][field] = value
    with pytest.raises(ValidationError):
        RouterPackageConfig.model_validate(valid_config)


@pytest.mark.parametrize("value", [0, 1, 0.5])
def test_thresholds_include_boundaries(valid_config, value):
    valid_config["routing"].update(confidence_threshold=value, overlap_threshold=value)
    config = RouterPackageConfig.model_validate(valid_config)
    assert config.routing.confidence_threshold == value
    assert config.routing.overlap_threshold == value


@pytest.mark.parametrize("field", ["profile", "databricks_profile"])
@pytest.mark.parametrize("section", [None, "workspace"])
def test_profile_is_not_part_of_committed_config(valid_config, field, section):
    _section(valid_config, section)[field] = "PROFILE_PLACEHOLDER"
    with pytest.raises(ValidationError, match="[Ee]xtra inputs"):
        RouterPackageConfig.model_validate(valid_config)


def test_rejects_duplicate_function_names(valid_config):
    valid_config["uc_functions"][1]["name"] = "monthly_revenue"
    with pytest.raises(ValidationError, match="[Dd]uplicate.*function"):
        RouterPackageConfig.model_validate(valid_config)


@pytest.mark.parametrize(
    "full_name",
    [
        "monthly_revenue",
        "metrics.monthly_revenue",
        "a.b.c.d",
        "acme_catalog..metric",
        "acme_catalog.metrics.metric()",
        "acme_catalog.metrics.metric;",
        "acme_catalog.metrics.metric --comment",
    ],
)
def test_rejects_malformed_function_fqn(valid_config, full_name):
    valid_config["uc_functions"][0]["full_name"] = full_name
    with pytest.raises(ValidationError):
        RouterPackageConfig.model_validate(valid_config)


def test_function_must_use_configured_catalog(valid_config):
    valid_config["uc_functions"][0]["full_name"] = (
        "other_catalog.metrics.monthly_revenue"
    )
    with pytest.raises(ValidationError, match="catalog"):
        RouterPackageConfig.model_validate(valid_config)


@pytest.mark.parametrize("value", [0, -1, 1.5, True, "90"])
def test_polling_requires_positive_integer(valid_config, value):
    valid_config["definitions"]["poll_seconds"] = value
    with pytest.raises(ValidationError):
        RouterPackageConfig.model_validate(valid_config)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("customer", "app_name", "Uppercase-app"),
        ("customer", "app_name", "app_name"),
        ("customer", "app_name", "a" * 64),
        ("workspace", "catalog", "catalog;drop"),
        ("workspace", "router_schema", "schema.name"),
        ("workspace", "warehouse_id", "has whitespace"),
        ("workspace", "classifier_endpoint", "endpoint/name"),
        ("workspace", "mlflow_experiment", "relative/path"),
        ("definitions", "secret_scope", "scope/name"),
        ("definitions", "secret_key", "key with spaces"),
        ("agent", "name", "invalid domain"),
        ("agent", "space_id", "01abc..."),
        ("agent", "space_id", "g" * 32),
        ("agent", "space_id", "0" * 31),
        ("function", "name", "metric.name"),
    ],
)
def test_rejects_malformed_identifiers(valid_config, section, field, value):
    _section(valid_config, section)[field] = value
    with pytest.raises(ValidationError):
        RouterPackageConfig.model_validate(valid_config)


@pytest.mark.parametrize(
    "path",
    [
        "/definitions",
        "../definitions",
        "definitions/../private",
        "definitions\\private",
        "https://example.com/definitions",
        "definitions//metrics",
    ],
)
def test_markdown_folder_must_be_relative_and_without_traversal(valid_config, path):
    valid_config["definitions"]["markdown_path"] = path
    with pytest.raises(ValidationError):
        RouterPackageConfig.model_validate(valid_config)


@pytest.mark.parametrize("path", [".", "definitions", "definitions/metrics"])
def test_accepts_markdown_repository_folders(valid_config, path):
    valid_config["definitions"]["markdown_path"] = path
    assert (
        RouterPackageConfig.model_validate(valid_config).definitions.markdown_path
        == path
    )


@pytest.mark.parametrize(
    "section",
    ["customer", "workspace", "definitions", "routing", "genie_agents", "uc_functions"],
)
def test_requires_contract_sections(valid_config, section):
    del valid_config[section]
    with pytest.raises(ValidationError):
        RouterPackageConfig.model_validate(valid_config)


@pytest.mark.parametrize(
    "section,field",
    [
        ("definitions", "secret_scope"),
        ("definitions", "secret_key"),
        ("agent", "routing_guidance"),
        ("agent", "space_id"),
        ("function", "domain"),
    ],
)
def test_requires_resource_and_routing_fields(valid_config, section, field):
    del _section(valid_config, section)[field]
    with pytest.raises(ValidationError):
        RouterPackageConfig.model_validate(valid_config)


@pytest.mark.parametrize(
    "section,field",
    [
        ("customer", "name"),
        ("definitions", "branch"),
        ("definitions", "secret_scope"),
        ("agent", "description"),
        ("agent", "routing_guidance"),
        ("function", "description"),
    ],
)
def test_rejects_blank_required_strings(valid_config, section, field):
    _section(valid_config, section)[field] = "   "
    with pytest.raises(ValidationError):
        RouterPackageConfig.model_validate(valid_config)


def test_requires_at_least_one_genie_agent(valid_config):
    valid_config["genie_agents"] = []
    with pytest.raises(ValidationError):
        RouterPackageConfig.model_validate(valid_config)


def test_allows_genie_only_routing(valid_config):
    valid_config["uc_functions"] = []
    assert RouterPackageConfig.model_validate(valid_config).uc_functions == []


def test_function_aliases_are_optional(valid_config):
    del valid_config["uc_functions"][0]["aliases"]
    assert (
        RouterPackageConfig.model_validate(valid_config).uc_functions[0].aliases == []
    )


def test_rejects_blank_function_alias(valid_config):
    valid_config["uc_functions"][0]["aliases"] = [" "]
    with pytest.raises(ValidationError):
        RouterPackageConfig.model_validate(valid_config)


def test_permissions_are_optional(valid_config):
    config = RouterPackageConfig.model_validate(valid_config)
    assert config.permissions.app == []
    assert config.permissions.dashboard == []


def test_accepts_optional_group_and_user_access(valid_config):
    valid_config["permissions"] = {
        "app": [{"group_name": "analytics-users"}],
        "dashboard": [{"user_name": "analyst@example.com"}],
    }
    config = RouterPackageConfig.model_validate(valid_config)
    assert config.permissions.app[0].group_name == "analytics-users"
    assert config.permissions.dashboard[0].user_name == "analyst@example.com"


@pytest.mark.parametrize(
    "principal",
    [
        {},
        {"group_name": "analytics-users", "user_name": "analyst@example.com"},
        {"group_name": " "},
    ],
)
def test_permission_requires_exactly_one_nonempty_principal(valid_config, principal):
    valid_config["permissions"] = {"app": [principal]}
    with pytest.raises(ValidationError):
        RouterPackageConfig.model_validate(valid_config)


def test_load_customer_config_reads_yaml(tmp_path, valid_config):
    path = tmp_path / "customer.yml"
    path.write_text(yaml.safe_dump(valid_config), encoding="utf-8")
    config = load_customer_config(path)

    assert config.customer.app_name == "acme-analytics-router"
    assert config.definitions.secret_scope == "analytics-router"
    assert config.genie_agents[1].name == "support"


@pytest.mark.parametrize(
    "contents", ["", "[]", "customer: [", "!!python/object/apply:os.getcwd []"]
)
def test_loader_rejects_invalid_or_unsafe_yaml(tmp_path, contents):
    path = tmp_path / "customer.yml"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        load_customer_config(path)


def test_loader_validates_secret_fields(tmp_path, valid_config):
    valid_config["definitions"]["token"] = "REJECTED_TEST_PLACEHOLDER"
    path = tmp_path / "customer.yml"
    path.write_text(yaml.safe_dump(valid_config), encoding="utf-8")
    with pytest.raises(ValidationError) as exc_info:
        load_customer_config(path)
    assert "REJECTED_TEST_PLACEHOLDER" not in str(exc_info.value)


def test_loader_does_not_echo_malformed_yaml(tmp_path):
    path = tmp_path / "customer.yml"
    path.write_text("token: [REJECTED_TEST_PLACEHOLDER", encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        load_customer_config(path)
    assert "REJECTED_TEST_PLACEHOLDER" not in str(exc_info.value)


@pytest.mark.parametrize(
    "extra_yaml",
    [
        "routing:\n  confidence_threshold: 0.5\n  overlap_threshold: 0.5\n  prefer_certified_functions: true\n",
        "permissions:\n  app: []\n  app: []\n",
    ],
)
def test_loader_rejects_duplicate_yaml_keys(tmp_path, valid_config, extra_yaml):
    path = tmp_path / "customer.yml"
    path.write_text(yaml.safe_dump(valid_config) + extra_yaml, encoding="utf-8")
    with pytest.raises(ValueError):
        load_customer_config(path)


def test_example_is_loadable():
    example = (
        Path(__file__).resolve().parents[1] / "customer_config" / "customer.example.yml"
    )
    config = load_customer_config(example)
    assert config.workspace.catalog == "acme_catalog"
    assert len(config.genie_agents) >= 2
    assert len(config.uc_functions) >= 2
