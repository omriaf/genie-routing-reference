"""Small, deterministic, linked retail/support/marketing samples; no I/O on import."""

import re
from datetime import date, timedelta
from pathlib import Path

CHANNELS = ("direct", "referral", "paid_search", "paid_social")
TABLE_COLUMNS = {
    "orders": "order_id BIGINT, order_date STRING, order_month STRING, channel STRING, amount DECIMAL(18, 2), status STRING",
    "support_tickets": "ticket_id BIGINT, order_id BIGINT, priority STRING, status STRING, resolution_hours DOUBLE",
    "campaign_daily": "activity_date STRING, channel STRING, sessions BIGINT, purchases BIGINT, spend DECIMAL(18, 2)",
}


def validate_namespace(catalog: str, schema: str) -> None:
    for identifier in (catalog, schema):
        if not isinstance(identifier, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,254}", identifier) is None:
            raise ValueError("Catalog and schema must use supported identifiers")


def sample_rows() -> dict[str, list[tuple]]:
    """90 fixed days, with linked tickets and reconciled channel conversions."""
    rows = {name: [] for name in TABLE_COLUMNS}
    for day in range(90):
        activity_date = (date(2025, 1, 1) + timedelta(days=day)).isoformat()
        for channel_index, channel in enumerate(CHANNELS):
            order_id = day * 4 + channel_index + 1
            completed = order_id % 7 != 0
            rows["orders"].append((
                order_id, activity_date, activity_date[:7], channel,
                40 + order_id % 160, "completed" if completed else "cancelled",
            ))
            rows["campaign_daily"].append((
                activity_date, channel, (20, 30, 60, 120)[channel_index],
                int(completed), (0, 0, 60, 180)[channel_index],
            ))
        ticket_id = day + 1
        resolved = ticket_id % 5 != 0
        rows["support_tickets"].append((
            ticket_id, day * 4 + 1, ("low", "normal", "high")[day % 3],
            "resolved" if resolved else "open", (4, 8, 16)[day % 3] if resolved else None,
        ))
    return rows


def _literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def seed_sql(catalog: str, schema: str) -> str:
    """Render rerunnable Delta table DDL and the exact packaged function bodies."""
    validate_namespace(catalog, schema)
    namespace = f"`{catalog}`.`{schema}`"
    statements = [f"CREATE SCHEMA IF NOT EXISTS {namespace};"]
    for table, rows in sample_rows().items():
        values = ",\n".join("(" + ", ".join(_literal(value) for value in row) + ")" for row in rows)
        statements.append(f"CREATE OR REPLACE TABLE {namespace}.`{table}` ({TABLE_COLUMNS[table]}) USING DELTA;\n"
                          f"INSERT INTO {namespace}.`{table}` VALUES\n{values};")
    functions = (Path(__file__).resolve().parents[1] / "sql/functions.sql").read_text(encoding="utf-8")
    functions = functions.replace("{{ catalog }}", catalog).replace("{{ schema }}", schema)
    return "\n\n".join([*statements, functions]) + "\n"
