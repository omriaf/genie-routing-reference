"""Render demo SQL, installer config, and Genie create-space requests offline."""

import argparse
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import yaml
from jinja2 import Environment, StrictUndefined

from demo.generators.generate import seed_sql, validate_namespace
from setup.models import DefinitionsConfig, WorkspaceConfig

_DEMO = Path(__file__).resolve().parents[1]
_DOMAINS = {
    "retail": ("orders", "Compare completed orders by channel",
               "SELECT orders.channel, COUNT(*) AS order_count FROM {table} AS orders WHERE orders.status = 'completed' GROUP BY orders.channel ORDER BY orders.channel"),
    "support": ("support_tickets", "Describe ticket volume by priority",
                "SELECT tickets.priority, COUNT(*) AS ticket_count FROM {table} AS tickets GROUP BY tickets.priority ORDER BY tickets.priority"),
    "marketing": ("campaign_daily", "Compare sessions and spend by channel",
                  "SELECT campaigns.channel, SUM(campaigns.sessions) AS sessions, SUM(campaigns.spend) AS spend FROM {table} AS campaigns GROUP BY campaigns.channel ORDER BY campaigns.channel"),
}


def prepare(output: Path, *, parent_path: str, **settings) -> None:
    """Build import requests without credentials, SDK clients, or workspace IDs."""
    validate_namespace(settings["catalog"], settings["schema"])
    if not parent_path.startswith("/Workspace/") or any(part in {"", ".", ".."} for part in parent_path[1:].split("/")):
        raise ValueError("Use an absolute workspace folder without traversal")
    environment = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)
    config = environment.from_string((_DEMO / "config/demo.yml").read_text(encoding="utf-8")).render(**settings)
    document = yaml.safe_load(config)
    # Validate real installer settings now. Genie IDs deliberately remain invalid
    # until the installer imports agents and copies their returned IDs.
    WorkspaceConfig.model_validate(document["workspace"])
    DefinitionsConfig.model_validate(document["definitions"])
    payload = {"demo.yml": config, "seed.sql": seed_sql(settings["catalog"], settings["schema"])}
    for domain, (table, question, query) in _DOMAINS.items():
        def identifier(kind, domain=domain):
            return uuid5(NAMESPACE_URL, f"sample-analytics/{domain}/{kind}").hex

        full_table = f"`{settings['catalog']}`.`{settings['schema']}`.`{table}`"
        space = {
            "version": 2,
            "data_sources": {"tables": [{"identifier": f"{settings['catalog']}.{settings['schema']}.{table}"}]},
            "config": {"sample_questions": [{"id": identifier("question"), "question": [question]}]},
            "instructions": {
                "text_instructions": [{"id": identifier("instructions"), "content": [
                    "All data is synthetic and covers January through March 2025. Use that period, not today's date.",
                    "Describe the observed data. Do not invent undefined metrics or cross-domain facts.",
                ]}],
                "example_question_sqls": [{"id": identifier("example"), "question": [question], "sql": [query.format(table=full_table)]}],
            },
        }
        request = {
            "title": f"Sample {domain.title()} Analytics",
            "description": f"Synthetic {domain} demonstration data",
            "warehouse_id": settings["warehouse_id"],
            "parent_path": parent_path,
            "serialized_space": json.dumps(space, sort_keys=True),
        }
        payload[f"{domain}.create.json"] = json.dumps(request, indent=2, sort_keys=True) + "\n"
    output.mkdir(parents=True, exist_ok=True)
    for name, content in payload.items():
        (output / name).write_text(content, encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    for flag in ("catalog", "schema", "warehouse-id", "classifier-endpoint", "experiment-path",
                 "repository-url", "secret-scope", "secret-key", "parent-path"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--output", type=Path, required=True)
    arguments = vars(parser.parse_args(argv))
    output = arguments.pop("output")
    try:
        prepare(output, **arguments)
    except (ValueError, OSError):
        print("Demo preparation failed; check settings and output directory.")
        return 1
    print("Prepared demo.yml, seed.sql, and three Genie create requests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
