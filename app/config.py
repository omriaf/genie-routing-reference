"""Runtime lifecycle and ambient Databricks client; no environment defaults."""

from functools import lru_cache

from app.runtime_config import RuntimeConfig, get_runtime_config


def initialize_runtime() -> RuntimeConfig:
    """Call from application startup, before creating clients or serving requests."""
    return get_runtime_config()


def runtime_health() -> dict:
    runtime = get_runtime_config()
    return {
        "status": "ready",
        "config_fingerprint": runtime.fingerprint,
        "domain_count": len(runtime.genie_agents),
        "function_count": len(runtime.uc_functions),
    }


@lru_cache(maxsize=1)
def get_client():
    """Use SDK ambient authentication; never enumerate or choose profiles."""
    initialize_runtime()
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient()
