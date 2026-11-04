"""Load one validated, deeply immutable configuration generation per process.

The application lifespan should call get_runtime_config() before marking ready.
Importing this module performs no file, credential, or workspace access.
"""

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Annotated, Mapping

from pydantic import ConfigDict, Field

from setup.models import (
    CustomerConfig, DefinitionsConfig, GenieAgentConfig, NonBlank,
    PermissionPrincipal, PermissionsConfig, RouterPackageConfig, RoutingConfig,
    UCFunctionConfig, WorkspaceConfig,
)


class RuntimeCustomer(CustomerConfig):
    model_config = ConfigDict(frozen=True)


class RuntimeWorkspace(WorkspaceConfig):
    model_config = ConfigDict(frozen=True)


class RuntimeDefinitions(DefinitionsConfig):
    model_config = ConfigDict(frozen=True)


class RuntimeRouting(RoutingConfig):
    model_config = ConfigDict(frozen=True)


class RuntimeAgent(GenieAgentConfig):
    model_config = ConfigDict(frozen=True)


class RuntimeFunction(UCFunctionConfig):
    model_config = ConfigDict(frozen=True)
    aliases: tuple[NonBlank, ...] = ()


class RuntimePrincipal(PermissionPrincipal):
    model_config = ConfigDict(frozen=True)


class RuntimePermissions(PermissionsConfig):
    model_config = ConfigDict(frozen=True)
    app: tuple[RuntimePrincipal, ...] = ()
    dashboard: tuple[RuntimePrincipal, ...] = ()


class RuntimeConfig(RouterPackageConfig):
    """Reuse the compiler's validators, replacing every mutable container."""

    model_config = ConfigDict(frozen=True)
    customer: RuntimeCustomer
    workspace: RuntimeWorkspace
    definitions: RuntimeDefinitions
    routing: RuntimeRouting
    genie_agents: Annotated[tuple[RuntimeAgent, ...], Field(min_length=1)]
    uc_functions: tuple[RuntimeFunction, ...]
    permissions: RuntimePermissions = Field(default_factory=RuntimePermissions)

    @property
    def agents_by_name(self) -> Mapping[str, RuntimeAgent]:
        return MappingProxyType({agent.name: agent for agent in self.genie_agents})

    @property
    def functions_by_name(self) -> Mapping[str, RuntimeFunction]:
        return MappingProxyType({function.name: function for function in self.uc_functions})

    @property
    def fingerprint(self) -> str:
        document = self.model_dump(mode="json", exclude_none=True)
        for field in ("genie_agents", "uc_functions"):
            document[field].sort(key=lambda item: item["name"])
        for function in document["uc_functions"]:
            function["aliases"].sort()
        for principals in document["permissions"].values():
            principals.sort(key=lambda principal: tuple(principal.items()))
        canonical = json.dumps(document, sort_keys=True, ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


def strict_json(text: str):
    """Reject ambiguous duplicate keys and non-standard numeric constants."""
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


_load_lock = Lock()


@lru_cache(maxsize=1)
def _load_runtime_config() -> RuntimeConfig:
    path = os.environ.get("ROUTER_CONFIG_PATH")
    if not path or not path.strip():
        raise ValueError("ROUTER_CONFIG_PATH must identify the compiled runtime configuration")
    try:
        contents = Path(path).read_text(encoding="utf-8")
        strict_json(contents)
        return RuntimeConfig.model_validate_json(contents)
    except (OSError, ValueError, UnicodeError):
        # Never include paths, rejected input, credentials, or parser diagnostics.
        raise ValueError("Invalid or unreadable runtime configuration") from None


def get_runtime_config() -> RuntimeConfig:
    # Lock outside the cache: functools alone may run a cold initializer twice.
    with _load_lock:
        return _load_runtime_config()
