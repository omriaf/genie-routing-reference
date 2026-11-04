"""Strict customer configuration contract; resource references never imply creation."""

import re
from pathlib import Path
from typing import Annotated, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NonBlank = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=255, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    ),
]
LogicalName = Annotated[
    str, StringConstraints(min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9_]*$")
]
ResourceName = Annotated[
    str, StringConstraints(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.-]+$")
]
SecretIdentifier = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.@-]+$")
]
Threshold = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class CustomerConfig(StrictConfigModel):
    name: NonBlank
    app_name: Annotated[
        str,
        StringConstraints(
            min_length=1, max_length=63, pattern=r"^[a-z](?:[a-z0-9-]*[a-z0-9])?$"
        ),
    ]


class WorkspaceConfig(StrictConfigModel):
    catalog: Identifier
    router_schema: Identifier
    warehouse_id: Annotated[
        str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9]+$")
    ]
    classifier_endpoint: ResourceName
    mlflow_experiment: NonBlank

    @field_validator("mlflow_experiment")
    @classmethod
    def absolute_experiment_path(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or value == "/"
            or any(part in {".", "..", ""} for part in value[1:].split("/"))
            or "\\" in value
        ):
            raise ValueError(
                "MLflow experiment must be an absolute workspace path without traversal"
            )
        return value


class DefinitionsConfig(StrictConfigModel):
    repository_url: NonBlank
    branch: NonBlank
    markdown_path: NonBlank
    secret_scope: SecretIdentifier
    secret_key: SecretIdentifier
    poll_seconds: Annotated[int, Field(gt=0)]

    @field_validator("repository_url")
    @classmethod
    def https_github_repository(cls, value: str) -> str:
        repository = re.fullmatch(
            r"https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/([A-Za-z0-9_.-]+)",
            value,
            flags=re.IGNORECASE | re.ASCII,
        )
        if repository is None or repository[1].removesuffix(".git") in {"", ".", ".."}:
            raise ValueError(
                "Use an HTTPS github.com owner/repository URL without credentials, query, or fragment"
            )
        return value

    @field_validator("markdown_path")
    @classmethod
    def repository_relative_folder(cls, value: str) -> str:
        if value != "." and (
            value.startswith("/")
            or "\\" in value
            or ":" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError(
                "Markdown path must be a relative repository folder without traversal"
            )
        return value


class RoutingConfig(StrictConfigModel):
    confidence_threshold: Threshold
    overlap_threshold: Threshold
    prefer_certified_functions: bool


class GenieAgentConfig(StrictConfigModel):
    name: LogicalName
    description: NonBlank
    space_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{32}$")]
    routing_guidance: NonBlank


class UCFunctionConfig(StrictConfigModel):
    name: LogicalName
    full_name: NonBlank
    domain: LogicalName
    description: NonBlank
    aliases: list[NonBlank] = Field(default_factory=list)

    @field_validator("full_name")
    @classmethod
    def three_part_function_name(cls, value: str) -> str:
        parts = value.split(".")
        if len(parts) != 3 or any(
            len(part) > 255 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part)
            for part in parts
        ):
            raise ValueError(
                "UC function must be a three-component catalog.schema.function identifier"
            )
        return value


class PermissionPrincipal(StrictConfigModel):
    group_name: NonBlank | None = None
    user_name: NonBlank | None = None

    @model_validator(mode="after")
    def exactly_one_principal(self) -> Self:
        if (self.group_name is None) == (self.user_name is None):
            raise ValueError("Specify exactly one group_name or user_name")
        return self


class PermissionsConfig(StrictConfigModel):
    app: list[PermissionPrincipal] = Field(default_factory=list)
    dashboard: list[PermissionPrincipal] = Field(default_factory=list)


class RouterPackageConfig(StrictConfigModel):
    customer: CustomerConfig
    workspace: WorkspaceConfig
    definitions: DefinitionsConfig
    routing: RoutingConfig
    genie_agents: Annotated[list[GenieAgentConfig], Field(min_length=1)]
    uc_functions: list[UCFunctionConfig]
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)

    @model_validator(mode="after")
    def validate_resource_references(self) -> Self:
        domains = [agent.name for agent in self.genie_agents]
        if len(domains) != len(set(domains)):
            raise ValueError(
                "Duplicate Genie Agent names are not allowed; each name defines one domain"
            )
        function_names = [function.name for function in self.uc_functions]
        if len(function_names) != len(set(function_names)):
            raise ValueError("Duplicate UC function names are not allowed")
        known_domains = set(domains)
        for function in self.uc_functions:
            if function.domain not in known_domains:
                raise ValueError(
                    "Unknown function domain; reference a configured Genie Agent name"
                )
            if function.full_name.split(".")[0] != self.workspace.catalog:
                raise ValueError("UC function catalog must match workspace.catalog")
        return self


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML with ambiguous or overwritten mapping entries rejected."""

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                if key in mapping:
                    raise yaml.constructor.ConstructorError(
                        "Duplicate YAML mapping keys are not allowed"
                    )
                mapping[key] = self.construct_object(value_node, deep=deep)
            except TypeError:
                raise yaml.constructor.ConstructorError(
                    "YAML mapping keys must be scalar"
                ) from None
        return mapping


def load_customer_config(path: Path) -> RouterPackageConfig:
    """Read UTF-8 safe YAML and validate without retrieving any secret values."""
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
    except (yaml.YAMLError, UnicodeError):
        # Parser diagnostics can include rejected input, so omit their text and cause.
        raise ValueError(
            "Customer configuration must contain valid UTF-8 safe YAML"
        ) from None
    return RouterPackageConfig.model_validate(data)
