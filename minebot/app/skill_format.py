"""Strict, deterministic parsing for MineBot ``SKILL.md`` documents."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Iterable

import yaml
from yaml.constructor import ConstructorError
from yaml.tokens import AliasToken, AnchorToken, TagToken


SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SKILL_NAME_CHARS = 64
MAX_SKILL_DESCRIPTION_CHARS = 320
MAX_SKILL_BODY_CHARS = 8_000
MAX_SKILL_BODY_LINES = 200
REQUIRED_SKILL_SECTIONS = (
    "Use When",
    "Do Not Use When",
    "Method",
    "Evidence Of Success",
    "Failure And Adaptation",
    "Boundaries",
)

_FRONTMATTER_FIELDS = frozenset({"name", "description", "tools"})
_GENERIC_DESCRIPTIONS = frozenset(
    {
        "helps with tasks",
        "help with tasks",
        "useful skill",
        "does things",
        "general methodology",
    }
)
_MARKDOWN_LINK = re.compile(r"!?\[[^\]\n]*\]\([^\)\n]+\)")
_RAW_SERVER_COMMAND = re.compile(
    r"(?:^|\s)`?/(?:player|execute|give|setblock|fill|summon|kill|tp|gamemode|script)\b",
    flags=re.IGNORECASE | re.MULTILINE,
)
_RAW_SCARPET_ENTRYPOINT = re.compile(
    r"\bminebot_(?:action|perceive|state|reset|interrupt)\s*\(",
    flags=re.IGNORECASE,
)
_UNSAFE_DIRECTIVE = re.compile(
    r"\b(?:bypass|disable|ignore|override)\s+(?:the\s+)?"
    r"(?:governance|block protection|permissions?|tool registry|terminal truth)\b",
    flags=re.IGNORECASE,
)
_FALSE_SUCCESS_DIRECTIVE = re.compile(
    r"\b(?:assume|claim|report|treat)\b.{0,40}\bsuccess(?:ful(?:ly)?)?\b",
    flags=re.IGNORECASE,
)
_SECRET_PATTERN = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{20,}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bAIza[A-Za-z0-9_-]{30,}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


class SkillFormatError(ValueError):
    """A Skill document violates the canonical, non-executable format."""

    def __init__(self, message: str, *, code: str = "skill_format_invalid") -> None:
        super().__init__(message)
        self.code = code


class _StrictSkillLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _StrictSkillLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key == "<<":
            raise ConstructorError(None, None, "YAML merge keys are not allowed", key_node.start_mark)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                None,
                None,
                "YAML mapping keys must be scalar strings",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(None, None, f"duplicate YAML key: {key!r}", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSkillLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class ParsedSkill:
    name: str
    description: str
    tools: tuple[str, ...]
    body: str
    source: str
    version_digest: str


def parse_skill_markdown(
    source: str,
    *,
    registered_tools: Iterable[str] | None = None,
) -> ParsedSkill:
    """Parse and canonicalize one complete ``SKILL.md`` document."""

    text = str(source or "").replace("\r\n", "\n").replace("\r", "\n")
    frontmatter, body = _split_frontmatter(text)
    metadata = _load_frontmatter(frontmatter)
    name = _validate_name(metadata.get("name"))
    description = _validate_description(metadata.get("description"))
    tools = _validate_tools(metadata.get("tools", ()), registered_tools=registered_tools)
    clean_body = _validate_body(body)
    canonical = canonical_skill_markdown(
        name=name,
        description=description,
        tools=tools,
        body=clean_body,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ParsedSkill(
        name=name,
        description=description,
        tools=tools,
        body=clean_body,
        source=canonical,
        version_digest=f"sha256:{digest}",
    )


def build_skill_markdown(
    *,
    name: str,
    description: str,
    tools: Iterable[str],
    body: str,
    registered_tools: Iterable[str] | None = None,
) -> ParsedSkill:
    """Validate model-supplied fields through the same portable representation."""

    source = canonical_skill_markdown(
        name=str(name or "").strip(),
        description=" ".join(str(description or "").split()),
        tools=tuple(str(tool or "").strip() for tool in tools),
        body=str(body or "").strip(),
    )
    return parse_skill_markdown(source, registered_tools=registered_tools)


def canonical_skill_markdown(
    *,
    name: str,
    description: str,
    tools: Iterable[str],
    body: str,
) -> str:
    clean_tools = tuple(sorted(dict.fromkeys(str(tool).strip() for tool in tools if str(tool).strip())))
    lines = [
        "---",
        f"name: {name}",
        f"description: {json.dumps(description, ensure_ascii=False)}",
    ]
    if clean_tools:
        lines.append("tools:")
        lines.extend(f"  - {tool}" for tool in clean_tools)
    lines.extend(("---", "", body.strip(), ""))
    return "\n".join(lines)


def _split_frontmatter(source: str) -> tuple[str, str]:
    if not source.startswith("---\n"):
        raise SkillFormatError("SKILL.md must start with YAML frontmatter")
    end = source.find("\n---\n", 4)
    if end < 0:
        raise SkillFormatError("SKILL.md frontmatter is not closed")
    frontmatter = source[4:end]
    body = source[end + 5 :].strip()
    if not body:
        raise SkillFormatError("SKILL.md body must not be empty")
    return frontmatter, body


def _load_frontmatter(frontmatter: str) -> dict[str, object]:
    try:
        tokens = tuple(yaml.scan(frontmatter, Loader=_StrictSkillLoader))
    except yaml.YAMLError as exc:
        raise SkillFormatError(f"invalid Skill frontmatter: {exc}") from exc
    if any(isinstance(token, (AliasToken, AnchorToken, TagToken)) for token in tokens):
        raise SkillFormatError("YAML aliases, anchors, and custom tags are not allowed")
    try:
        loaded = yaml.load(frontmatter, Loader=_StrictSkillLoader)
    except yaml.YAMLError as exc:
        raise SkillFormatError(f"invalid Skill frontmatter: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SkillFormatError("Skill frontmatter must be a mapping")
    if not all(isinstance(key, str) for key in loaded):
        raise SkillFormatError("Skill frontmatter keys must be strings")
    unknown = sorted(set(loaded) - _FRONTMATTER_FIELDS)
    if unknown:
        raise SkillFormatError(f"unknown Skill frontmatter fields: {', '.join(unknown)}")
    missing = sorted({"name", "description"} - set(loaded))
    if missing:
        raise SkillFormatError(f"missing Skill frontmatter fields: {', '.join(missing)}")
    return loaded


def _validate_name(value: object) -> str:
    if not isinstance(value, str):
        raise SkillFormatError("Skill name must be a string")
    clean = value.strip()
    if len(clean) > MAX_SKILL_NAME_CHARS or not SKILL_NAME_PATTERN.fullmatch(clean):
        raise SkillFormatError("Skill name must be lowercase hyphen-case and at most 64 characters")
    return clean


def _validate_description(value: object) -> str:
    if not isinstance(value, str):
        raise SkillFormatError("Skill description must be a string")
    clean = " ".join(value.split())
    if not clean or len(clean) > MAX_SKILL_DESCRIPTION_CHARS:
        raise SkillFormatError("Skill description must contain 1 to 320 characters")
    if len(clean) < 24 or clean.casefold().rstrip(".!?") in _GENERIC_DESCRIPTIONS:
        raise SkillFormatError("Skill description must state specific selection guidance")
    if _SECRET_PATTERN.search(clean):
        raise SkillFormatError("Skill description appears to contain a secret")
    return clean


def _validate_tools(
    value: object,
    *,
    registered_tools: Iterable[str] | None,
) -> tuple[str, ...]:
    if value is None:
        raw: list[object] = []
    elif isinstance(value, list):
        raw = value
    else:
        raise SkillFormatError("Skill tools must be a flat YAML list")
    if not all(isinstance(item, str) for item in raw):
        raise SkillFormatError("Skill tools must contain only tool-name strings")
    clean = tuple(str(item).strip() for item in raw)
    if any(not item or len(item) > 128 for item in clean):
        raise SkillFormatError("Skill tool names must contain 1 to 128 characters")
    if len(set(clean)) != len(clean):
        raise SkillFormatError("Skill tools must be unique")
    ordered = tuple(sorted(clean))
    if registered_tools is not None:
        available = frozenset(str(item) for item in registered_tools)
        missing = tuple(tool for tool in ordered if tool not in available)
        if missing:
            raise SkillFormatError(
                f"unknown Skill tools: {', '.join(missing)}",
                code="skill_dependency_unavailable",
            )
    return ordered


def _validate_body(body: str) -> str:
    clean = body.strip()
    if len(clean) > MAX_SKILL_BODY_CHARS:
        raise SkillFormatError(f"Skill body exceeds {MAX_SKILL_BODY_CHARS} characters")
    lines = clean.splitlines()
    if len(lines) > MAX_SKILL_BODY_LINES:
        raise SkillFormatError(f"Skill body exceeds {MAX_SKILL_BODY_LINES} lines")
    if any(ord(character) < 32 and character not in "\n\t" for character in clean):
        raise SkillFormatError("Skill body contains control characters")
    h1 = [line for line in lines if re.fullmatch(r"#\s+\S.*", line)]
    if len(h1) != 1:
        raise SkillFormatError("Skill body must contain exactly one H1 heading")
    h2_entries = [
        (index, match.group(1).strip())
        for index, line in enumerate(lines)
        if (match := re.fullmatch(r"##\s+(.+)", line))
    ]
    h2 = [name for _, name in h2_entries]
    if tuple(h2) != REQUIRED_SKILL_SECTIONS:
        raise SkillFormatError(
            "Skill H2 sections must appear exactly once in canonical order: "
            + ", ".join(REQUIRED_SKILL_SECTIONS)
        )
    for section in REQUIRED_SKILL_SECTIONS:
        start = next(index for index, name in h2_entries if name == section) + 1
        content: list[str] = []
        for line in lines[start:]:
            if line.startswith("## "):
                break
            content.append(line)
        if not any(line.strip() and not line.startswith("### ") for line in content):
            raise SkillFormatError(f"Skill section is empty: {section}")
    if _MARKDOWN_LINK.search(clean) or "../" in clean or "~/" in clean:
        raise SkillFormatError("Skill bodies cannot contain links or filesystem paths")
    if _SECRET_PATTERN.search(clean):
        raise SkillFormatError("Skill body appears to contain a secret")
    if _RAW_SERVER_COMMAND.search(clean) or _RAW_SCARPET_ENTRYPOINT.search(clean):
        raise SkillFormatError("Skill bodies cannot contain raw server commands")
    for pattern in (_UNSAFE_DIRECTIVE, _FALSE_SUCCESS_DIRECTIVE):
        for match in pattern.finditer(clean):
            prefix = clean[max(0, match.start() - 120) : match.start()].casefold()
            if any(
                denial in prefix
                for denial in ("do not", "never", "must not", "cannot", "can't")
            ):
                continue
            raise SkillFormatError("Skill body attempts to bypass governance or terminal truth")
    return clean


__all__ = [
    "MAX_SKILL_BODY_CHARS",
    "MAX_SKILL_BODY_LINES",
    "MAX_SKILL_DESCRIPTION_CHARS",
    "MAX_SKILL_NAME_CHARS",
    "ParsedSkill",
    "REQUIRED_SKILL_SECTIONS",
    "SKILL_NAME_PATTERN",
    "SkillFormatError",
    "build_skill_markdown",
    "canonical_skill_markdown",
    "parse_skill_markdown",
]
