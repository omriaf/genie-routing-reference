"""Governed Markdown is strict metadata plus inert, nonempty business prose."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml


FIXTURE = Path(__file__).parent / "fixtures/definitions/monthly_revenue.md"


def document(**changes):
    metadata = {
        "name": "monthly_revenue", "domain": "sales",
        "aliases": ["monthly sales", "revenue by month"],
        "certified_function": "monthly_revenue",
    }
    metadata.update(changes)
    return "---\n" + yaml.safe_dump(metadata) + "---\n\n# Revenue\n\nBusiness meaning.\n"


def test_parses_fixture_into_immutable_definition(modules):
    definition = modules.markdown_definitions.parse_definition(FIXTURE.read_text(), "definitions/monthly_revenue.md")
    assert definition.name == "monthly_revenue"
    assert definition.domain == "sales"
    assert definition.aliases == ("monthly sales", "revenue by month")
    assert definition.certified_function == "monthly_revenue"
    assert definition.source_path == "definitions/monthly_revenue.md"
    assert definition.body == "# Monthly revenue\n\nRecognized revenue for a calendar month, excluding refunds."
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        definition.domain = "support"


@pytest.mark.parametrize("field", ["name", "domain", "aliases", "certified_function"])
def test_requires_all_front_matter_fields(modules, field):
    metadata = {
        "name": "monthly_revenue", "domain": "sales",
        "aliases": ["monthly sales"], "certified_function": "monthly_revenue",
    }
    del metadata[field]
    text = "---\n" + yaml.safe_dump(metadata) + "---\n# Revenue\n"
    with pytest.raises(ValueError):
        modules.markdown_definitions.parse_definition(text, "metric.md")


@pytest.mark.parametrize("changes", [
    {"name": ""}, {"name": "a.b.metric"}, {"domain": "Unknown Domain"},
    {"aliases": "revenue"}, {"aliases": [1]}, {"aliases": [""]},
    {"aliases": [" revenue ", "REVENUE"]}, {"aliases": ["MONTHLY_REVENUE"]},
    {"certified_function": "catalog.schema.monthly_revenue"},
    {"certified_function": "metric(); SELECT 1"}, {"certified_function": 1},
    {"sql": "SELECT unapproved"},
])
def test_rejects_invalid_or_ambiguous_metadata(modules, changes):
    with pytest.raises(ValueError):
        modules.markdown_definitions.parse_definition(document(**changes), "metric.md")


def test_noncertified_term_preserves_markdown_as_inert_text(modules):
    text = document(certified_function=None, aliases=[]) + "\n```sql\nSELECT unapproved();\n```\n"
    definition = modules.markdown_definitions.parse_definition(text.replace("\n", "\r\n"), "metric.md")
    assert definition.certified_function is None
    assert definition.aliases == ()
    assert "SELECT unapproved();" in definition.body


@pytest.mark.parametrize("text", [
    "", "# Missing front matter", "---\nname: metric", "---\n[]\n---\nBody",
    "---\nname: [\n---\nBody", "---\n!!python/object:module.Class {}\n---\nBody",
    document().replace("domain: sales", "domain: sales\ndomain: support"),
    document().split("---\n", 2)[0] + "---\n{}\n---\n",
    document().replace("# Revenue\n\nBusiness meaning.", "   "),
    document() + "\x00", document() + "\ud800",
])
def test_malformed_envelope_or_encoding_fails_without_echoing_input(modules, text):
    with pytest.raises(ValueError) as error:
        modules.markdown_definitions.parse_definition(text, "metric.md")
    assert "module.Class" not in str(error.value)


@pytest.mark.parametrize("path", [
    "/metric.md", "../metric.md", "definitions/../metric.md", "definitions//metric.md",
    "./metric.md", "definitions\\metric.md", "C:/metric.md", "https://other/metric.md",
    "definitions/%2e%2e/metric.md", "definitions/%252e%252e/metric.md",
    "definitions/metric.md?secret", "definitions/metric.md#fragment", "metric.md\n", "metric.txt",
])
def test_source_paths_are_repository_relative_markdown(modules, path):
    with pytest.raises(ValueError):
        modules.markdown_definitions.parse_definition(document(), path)


def test_cross_references_and_indexes_use_runtime_logical_names(modules, runtime_document, install_runtime):
    runtime = install_runtime(runtime_document)
    parse = modules.markdown_definitions.parse_definition
    definitions = [
        parse(document(name="support_metric", domain="support", aliases=["TTR"], certified_function="resolution_time"), "support.md"),
        parse(document(), "revenue.md"),
    ]
    snapshot = modules.markdown_definitions.build_snapshot(definitions, runtime, "a" * 40)
    assert [definition.name for definition in snapshot.definitions] == ["monthly_revenue", "support_metric"]
    assert snapshot.by_name["support_metric"].certified_function == "resolution_time"
    assert snapshot.by_term["revenue by month"].name == "monthly_revenue"
    assert snapshot.by_term["ttr"].name == "support_metric"
    assert modules.markdown_definitions.build_snapshot(reversed(definitions), runtime, "a" * 40) == snapshot
    with pytest.raises(TypeError):
        snapshot.by_name["invented"] = definitions[0]
    with pytest.raises(TypeError):
        snapshot.by_term["invented"] = definitions[0]


@pytest.mark.parametrize("changes", [
    {"domain": "unknown"}, {"certified_function": "unknown"},
    {"certified_function": "resolution_time"},
])
def test_unknown_or_wrong_domain_function_invalidates_candidate(modules, runtime_document, install_runtime, changes):
    runtime = install_runtime(runtime_document)
    definition = modules.markdown_definitions.parse_definition(document(**changes), "metric.md")
    with pytest.raises(ValueError):
        modules.markdown_definitions.build_snapshot([definition], runtime, "a" * 40)


@pytest.mark.parametrize("changes", [
    {}, {"name": "other", "aliases": [" MONTHLY   SALES "]},
    {"name": "other", "aliases": ["Monthly_Revenue"]},
    {"name": "other", "aliases": ["monthly sales", "new term"]},
])
def test_duplicate_names_and_aliases_invalidate_whole_candidate(modules, runtime_document, install_runtime, changes):
    runtime = install_runtime(runtime_document)
    parse = modules.markdown_definitions.parse_definition
    definitions = [parse(document(), "one.md"), parse(document(**changes), "two.md")]
    with pytest.raises(ValueError):
        modules.markdown_definitions.build_snapshot(definitions, runtime, "a" * 40)
