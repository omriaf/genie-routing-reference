"""Single analytics entry point: classify once, then execute one approved route."""

import uuid

from app import config, genie_client, router, uc_functions


def ask_analytics(question: str, *, enrichment: router.RoutingEnrichment | None = None) -> dict:
    runtime = config.initialize_runtime()
    trace_id = str(uuid.uuid4())
    result = {
        "status": "no_route", "question": question, "trace_id": trace_id,
        "config_fingerprint": runtime.fingerprint, "domain": "out_of_domain",
        "route": "fail_loud", "confidence": 0.0, "candidates": {},
        "answer": "No configured analytics route confidently covers this question.",
    }
    active_route = "classifier"
    try:
        decision = router.classify(question, enrichment)
        result.update(
            domain=decision.domain, confidence=decision.confidence,
            candidates=dict(decision.candidates), reasoning=decision.reasoning,
        )
        if decision.undefined_terms:
            result.update(status="undefined_term", undefined_terms=list(decision.undefined_terms),
                          answer="The question requests an undefined business metric.")
        if not decision.accepted:
            return result
        result["genie_space_id"] = runtime.agents_by_name[decision.domain].space_id
        if decision.certified_function:
            active_route = "uc_function"
            result["uc_function"] = runtime.functions_by_name[decision.certified_function].full_name
            function = uc_functions.execute_function(decision.certified_function, decision.function_arguments)
            text_rows = [" | ".join(function.columns)]
            text_rows.extend(" | ".join("" if item is None else str(item) for item in row) for row in function.rows[:10])
            result.update(
                status="answered", route="uc_function", uc_function=function.function,
                columns=function.columns, rows=function.rows, answer="\n".join(text_rows),
            )
        else:
            active_route = "genie"
            answer = genie_client.ask_genie(decision.domain, router.question_with_context(question, enrichment))
            result.update(status="answered", route="genie", answer=answer["answer"],
                          genie_sql=answer["sql"], columns=answer["columns"], rows=answer["rows"],
                          conversation_id=answer["conversation_id"], message_id=answer["message_id"])
    except Exception:
        # SDK exceptions can contain rejected inputs or credentials. Do not echo
        # them to callers; the MCP host records the sanitized result and health.
        result.update(status="error", route="error", failed_route=active_route,
                      answer="The configured analytics route failed.", error="Analytics service request failed")
    return result
