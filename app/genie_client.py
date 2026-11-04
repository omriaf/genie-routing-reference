"""Conversation API access through configured logical domains only."""

from datetime import timedelta

from app import config
from app.runtime_config import get_runtime_config


def _state(status):
    return getattr(status, "value", status)


def ask_genie(domain: str, question: str) -> dict:
    runtime = get_runtime_config()
    agent = runtime.agents_by_name.get(domain) if type(domain) is str else None
    if agent is None:
        raise ValueError("Use an exact configured logical domain name")
    client = config.get_client()
    message = client.genie.start_conversation_and_wait(
        space_id=agent.space_id, content=question, timeout=timedelta(minutes=5),
    )
    if _state(getattr(message, "status", None)) != "COMPLETED":
        raise RuntimeError("Configured Genie conversation failed")
    conversation_id = getattr(message, "conversation_id", None)
    message_id = getattr(message, "id", None) or getattr(message, "message_id", None)
    answer_parts, columns, rows = [], [], []
    sql = description = None
    for attachment in getattr(message, "attachments", None) or []:
        text = getattr(getattr(attachment, "text", None), "content", None)
        if text:
            answer_parts.append(text)
        query = getattr(attachment, "query", None)
        if query is not None and sql is None:
            sql = getattr(query, "query", None)
            description = getattr(query, "description", None)
            attachment_id = getattr(attachment, "attachment_id", None)
            if attachment_id and conversation_id and message_id:
                result = client.genie.get_message_attachment_query_result(
                    agent.space_id, conversation_id, message_id, attachment_id,
                )
                statement = getattr(result, "statement_response", None)
                if statement is None or _state(getattr(getattr(statement, "status", None), "state", None)) != "SUCCEEDED":
                    raise RuntimeError("Configured Genie result retrieval failed")
                schema = getattr(getattr(statement, "manifest", None), "schema", None)
                columns = [column.name for column in getattr(schema, "columns", None) or []]
                rows = (getattr(getattr(statement, "result", None), "data_array", None) or [])[:20]
    return {
        "answer": "\n".join(answer_parts).strip() or description or "Genie returned a result set without a text summary.",
        "sql": sql, "description": description, "columns": columns, "rows": rows,
        "conversation_id": conversation_id, "message_id": message_id,
        "genie_space_id": agent.space_id,
    }
