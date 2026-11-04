"""Strict governed Markdown and immutable, fully validated definition generations.

Every document requires name, domain, aliases, and certified_function front
matter. A term without a certified function uses YAML null. The nonempty body
is inert Markdown; it is never interpreted as executable metadata or SQL.
"""

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Iterable, Mapping

import yaml

from app.runtime_config import RuntimeConfig
from setup.models import LogicalName, NonBlank, StrictConfigModel, _UniqueKeySafeLoader


MAX_MARKDOWN_BYTES = 1024 * 1024


def repository_path(value: str) -> str:
    """Accept an unencoded relative path, without normalization hiding traversal."""
    if (
        not isinstance(value, str)
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(character in value for character in "\\%:?#")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("Invalid repository-relative path")
    return value


def normalize_term(value: str) -> str:
    return " ".join(value.split()).casefold()


class _FrontMatter(StrictConfigModel):
    name: LogicalName
    domain: LogicalName
    aliases: list[NonBlank]
    certified_function: LogicalName | None


@dataclass(frozen=True)
class GovernedDefinition:
    name: str
    domain: str
    aliases: tuple[str, ...]
    certified_function: str | None
    body: str
    source_path: str


@dataclass(frozen=True)
class DefinitionsSnapshot:
    revision: str
    definitions: tuple[GovernedDefinition, ...]
    by_name: Mapping[str, GovernedDefinition]
    by_term: Mapping[str, GovernedDefinition]


def parse_definition(text: str, source_path: str) -> GovernedDefinition:
    """Parse one UTF-8 Markdown document; diagnostics never echo rejected input."""
    try:
        repository_path(source_path)
        if not source_path.lower().endswith(".md"):
            raise ValueError
        if not isinstance(text, str) or "\x00" in text or len(text.encode("utf-8")) > MAX_MARKDOWN_BYTES:
            raise ValueError
        lines = text.replace("\r\n", "\n").split("\n")
        if lines[0] != "---":
            raise ValueError
        closing = lines.index("---", 1)
        metadata = _FrontMatter.model_validate(yaml.load("\n".join(lines[1:closing]), Loader=_UniqueKeySafeLoader))
        body = "\n".join(lines[closing + 1:]).strip()
        if not body:
            raise ValueError
        aliases = tuple(sorted((" ".join(alias.split()) for alias in metadata.aliases), key=normalize_term))
        terms = [normalize_term(term) for term in (metadata.name, *aliases)]
        if len(set(terms)) != len(terms):
            raise ValueError
        return GovernedDefinition(
            metadata.name, metadata.domain, aliases, metadata.certified_function, body, source_path,
        )
    except (ValueError, TypeError, yaml.YAMLError):
        # SafeLoader/Pydantic diagnostics may include the entire rejected document.
        pass
    raise ValueError("Invalid governed Markdown definition")


def build_snapshot(
    definitions: Iterable[GovernedDefinition], runtime: RuntimeConfig, revision: str,
) -> DefinitionsSnapshot:
    """Validate all cross-references and collisions before exposing any indexes."""
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("Invalid definitions revision")
    ordered = tuple(sorted(definitions, key=lambda definition: definition.name))
    if not ordered:
        raise ValueError("At least one governed Markdown definition is required")
    domains, functions = runtime.agents_by_name, runtime.functions_by_name
    names, terms, sources = {}, {}, set()
    for definition in ordered:
        if definition.domain not in domains:
            raise ValueError("Unknown definition domain")
        if definition.certified_function is not None:
            function = functions.get(definition.certified_function)
            if function is None or function.domain != definition.domain:
                raise ValueError("Unknown or incompatible certified function")
        if definition.name in names or definition.source_path in sources:
            raise ValueError("Duplicate definition name or source")
        names[definition.name] = definition
        sources.add(definition.source_path)
        for term in (definition.name, *definition.aliases):
            normalized = normalize_term(term)
            if normalized in terms:
                raise ValueError("Duplicate definition name or alias")
            terms[normalized] = definition
    return DefinitionsSnapshot(
        revision, ordered, MappingProxyType(names), MappingProxyType(dict(sorted(terms.items()))),
    )
