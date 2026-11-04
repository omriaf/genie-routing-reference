"""FastAPI host for the single analytics MCP tool and redacted health contract.

Run with `python -m app.app`. The compiler stages the complete deployment root
under .generated/app_source/, including both packages and runtime_config.json.
"""

import logging
import os
import re
import time
from contextlib import asynccontextmanager

from anyio import to_thread
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

from app import agent
from app.definitions import DefinitionsStore
from app.markdown_definitions import DefinitionsSnapshot
from app.router import RoutingEnrichment
from app.runtime_config import get_runtime_config
from app.tracing import TraceRecorder

logger = logging.getLogger(__name__)
_STOP_WORDS = frozenset({"by", "the", "of", "per", "in", "for", "and", "a", "an", "to", "with", "at"})


def _tokens(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", text.casefold()))


def enrich(question: str, snapshot: DefinitionsSnapshot, threshold: float) -> RoutingEnrichment:
    """Match governed phrases, then meaningful word overlap; ties fail closed."""
    normalized = " " + _tokens(question) + " "
    words = set(normalized.split()) - _STOP_WORDS
    exact, overlap = {}, {}
    for definition in snapshot.definitions:
        for term in (definition.name, *definition.aliases):
            phrase = _tokens(term)
            if phrase and " " + phrase + " " in normalized:
                exact[definition.name] = definition
            meaningful = set(phrase.split()) - _STOP_WORDS
            common = meaningful & words
            score = len(common) / max(len(meaningful), 1)
            if len(common) >= 2 and score >= threshold:
                previous = overlap.get(definition.name)
                if previous is None or score > previous[0]:
                    overlap[definition.name] = (score, definition)
    selected, method, score = None, "no_match", 0.0
    if len(exact) == 1:
        selected, method, score = next(iter(exact.values())), "exact_alias", 1.0
    elif not exact and overlap:
        best_score = max(item[0] for item in overlap.values())
        best = [item[1] for item in overlap.values() if item[0] == best_score]
        if len(best) == 1:
            selected, method, score = best[0], "word_overlap", best_score
    return RoutingEnrichment(
        definitions_revision=snapshot.revision, method=method, score=score,
        matched_term=selected.name if selected else None,
        domain=selected.domain if selected else None,
        certified_function=selected.certified_function if selected else None,
        context=selected.body if selected else "",
    )


def create_app() -> FastAPI:
    """Create transport routes without loading config or authenticating at import."""
    mcp = FastMCP("Analytics Router", host="0.0.0.0", streamable_http_path="/mcp",
                  stateless_http=True, json_response=True)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        app.state.ready = False
        store = None
        try:
            try:
                runtime = get_runtime_config()
                app.state.runtime = runtime
                store = app.state.definitions = DefinitionsStore(runtime)
                await to_thread.run_sync(store.start)
                recorder = app.state.tracing = TraceRecorder(runtime)
                await to_thread.run_sync(recorder.initialize)
            except Exception:  # noqa: BLE001 - startup diagnostics must not expose credentials
                logger.error("Application startup failed")
                raise RuntimeError("Application startup failed") from None
            async with mcp.session_manager.run():
                app.state.ready = True
                try:
                    yield
                finally:
                    app.state.ready = False
        finally:
            if store is not None:
                await to_thread.run_sync(store.stop)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ready = False

    @app.get("/")
    def root():
        return {"name": "Analytics Router", "mcp_endpoint": "/mcp", "health_endpoint": "/health"}

    @app.get("/health")
    def health():
        if not app.state.ready:
            return JSONResponse({"status": "not_ready", "ready": False}, status_code=503)
        definitions = app.state.definitions.health()
        tracing = app.state.tracing.health()
        degraded = definitions["status"] == "degraded" or any(
            sink["status"] == "degraded" for sink in tracing.values()
        )
        return {
            "status": "degraded" if degraded else "ready", "ready": True,
            "config_fingerprint": app.state.runtime.fingerprint,
            "definitions": definitions, "tracing": tracing,
        }

    def run_question(question: str) -> dict:
        started = time.perf_counter()
        snapshot = app.state.definitions.snapshot()
        enrichment = enrich(question, snapshot, app.state.runtime.routing.overlap_threshold)
        recorder = app.state.tracing
        result = agent.ask_analytics(question, enrichment=enrichment)
        result.update(
            defs_commit_sha=enrichment.definitions_revision,
            matched_definition_term=enrichment.matched_term,
            enrichment_method=enrichment.method,
        )
        recorder.record(result, enrichment, int((time.perf_counter() - started) * 1000))
        return result

    @mcp.tool()
    async def ask_analytics(question: str) -> dict:
        """Route a question through governed definitions and configured analytics."""
        if not app.state.ready:
            raise RuntimeError("Analytics service is not ready")
        return await to_thread.run_sync(run_question, question)

    # The inner route is /mcp. Mount at root AFTER the REST endpoints to avoid
    # Starlette's /mcp -> /mcp/ redirect and accidental /mcp/mcp composition.
    app.mount("/", mcp_app)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("DATABRICKS_APP_PORT", "8000")))
