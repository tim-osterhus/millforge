"""Parser boundary contracts and deterministic front ends for harness sources."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBytes,
    StrictStr,
    ValidationError,
    field_validator,
)

from millforge.compiler.diagnostics import (
    bound_diagnostics,
    CompilerDiagnostic,
    CompilerPhase,
    DiagnosticField,
    DiagnosticSeverity,
    SourceLocation,
    SourceReference,
)
from millforge.compiler.source import HarnessSource
from millforge.compiler.validators import validate_sha256, validate_utf8_size

MAX_SOURCE_SIZE_BYTES = 1_048_576
MAX_NESTING_DEPTH = 32
MAX_TOTAL_ENTRIES = 10_000
MAX_SCALAR_UTF8_SIZE = 65_536
MAX_INTEGER_LEXEME_ASCII = 128
MAX_FLOAT_LEXEME_ASCII = 128
_ZERO_SHA256 = "0" * 64
_FORBIDDEN_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_JSON_INTEGER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_JSON_FLOAT_RE = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)\Z|"
    r"-?(?:0|[1-9][0-9]*)\.[0-9]+(?:[eE][+-]?[0-9]+)?\Z"
)
_YAML_TAG_ANCHOR_ALIAS_RE = re.compile(r"(^|[\s\[{,])(?:[!&*][A-Za-z0-9_.:/-]*|!)")

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ParserError(ValueError):
    """Bounded parser failure that maps to a compiler diagnostic."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        line: int | None = None,
        column: int | None = None,
        field_path: str = "/",
        fields: tuple[DiagnosticField, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line
        self.column = column
        self.field_path = field_path
        self.fields = fields


@dataclass(frozen=True)
class _YamlLine:
    number: int
    indent: int
    text: str


@dataclass
class _ParseState:
    entries: int = 0

    def note_entry(self) -> None:
        self.entries += 1
        if self.entries > MAX_TOTAL_ENTRIES:
            raise ParserError(
                "MF-S010", "Total mapping/list entries exceed parser limit."
            )


class SourceDocument(BaseModel):
    """Immutable parser input document; content is excluded from repr."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_path: StrictStr
    format: StrictStr
    content: StrictBytes = Field(repr=False)

    @field_validator("logical_path")
    @classmethod
    def _logical_path_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("logical_path must be nonblank")
        return value

    @field_validator("format")
    @classmethod
    def _format_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("format must be nonblank")
        return validate_utf8_size(value, "format", 64)


class ParsedHarnessSource(BaseModel):
    """Immutable parser output boundary for a harness source document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: HarnessSource | None
    source_document_sha256: StrictStr
    diagnostics: tuple[CompilerDiagnostic, ...] = Field(default_factory=tuple)
    location_index: tuple[SourceReference, ...] = Field(default_factory=tuple)

    @field_validator("source_document_sha256")
    @classmethod
    def _source_document_sha256_valid(cls, value: str) -> str:
        return validate_sha256(value, "source_document_sha256")


class HarnessSourceParserProtocol(Protocol):
    """Protocol for deterministic harness source parsers."""

    def parse(self, document: SourceDocument) -> ParsedHarnessSource:
        """Parse a source document into the shared immutable source contract."""
        ...


class HarnessSourceParser:
    """Deterministic YAML/JSON parser front end for harness source documents."""

    def parse(self, document: SourceDocument) -> ParsedHarnessSource:
        try:
            normalized_bytes, digest = _normalize_and_hash(document.content)
            if document.format == "json":
                payload = _parse_json(normalized_bytes)
            elif document.format == "yaml":
                payload = _parse_yaml(normalized_bytes)
            else:
                raise ParserError("MF-S005", "Unsupported source format.")
            _validate_json_compatible(payload)
            if not isinstance(payload, dict):
                raise ParserError(
                    "MF-S012", "Top-level source document must be an object."
                )
            location_index = _build_location_index(document, normalized_bytes, payload)
            source = HarnessSource.model_validate(payload)
            return ParsedHarnessSource(
                source=source,
                source_document_sha256=digest,
                diagnostics=(),
                location_index=location_index,
            )
        except ParserError as exc:
            return ParsedHarnessSource(
                source=None,
                source_document_sha256=(
                    digest if "digest" in locals() else _ZERO_SHA256
                ),
                diagnostics=(_diagnostic(document, exc),),
                location_index=(),
            )
        except ValidationError as exc:
            return ParsedHarnessSource(
                source=None,
                source_document_sha256=digest if "digest" in locals() else _ZERO_SHA256,
                diagnostics=_schema_validation_diagnostics(
                    document,
                    exc,
                    location_index if "location_index" in locals() else (),
                ),
                location_index=location_index if "location_index" in locals() else (),
            )


def _normalize_and_hash(content: bytes) -> tuple[bytes, str]:
    if len(content) > MAX_SOURCE_SIZE_BYTES:
        raise ParserError(
            "MF-S003",
            "Source document exceeds the maximum size.",
            fields=(DiagnosticField(key="source_size", value=len(content)),),
        )
    if content.startswith(b"\xef\xbb\xbf"):
        content = content[3:]
    try:
        text = content.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ParserError(
            "MF-S004",
            "Source document must be strict UTF-8.",
            line=1,
            column=exc.start + 1,
        ) from exc
    _reject_forbidden_text(text)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return normalized, hashlib.sha256(normalized).hexdigest()


def _reject_forbidden_text(text: str) -> None:
    for index, char in enumerate(text):
        codepoint = ord(char)
        if 0xD800 <= codepoint <= 0xDFFF:
            line, column = _line_column(text, index)
            raise ParserError(
                "MF-S011",
                "Source document contains an unpaired Unicode surrogate.",
                line=line,
                column=column,
            )
    match = _FORBIDDEN_CONTROL_RE.search(text)
    if match is not None:
        line, column = _line_column(text, match.start())
        raise ParserError(
            "MF-S011",
            "Source document contains a forbidden control character.",
            line=line,
            column=column,
        )


def _parse_json(content: bytes) -> JsonValue:
    return _parse_json_text(content.decode("utf-8"))


def _parse_json_text(
    text: str, *, state: _ParseState | None = None, depth: int = 1
) -> JsonValue:
    state = state if state is not None else _ParseState()

    def skip_ws(index: int) -> int:
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        return index

    def parse_value(index: int, depth: int) -> tuple[JsonValue, int]:
        index = skip_ws(index)
        if index >= len(text):
            raise ParserError("MF-S011", "Malformed JSON source document.")
        if text.startswith(("NaN", "Infinity", "+Infinity", "-Infinity"), index):
            line, column = _line_column(text, index)
            raise ParserError(
                "MF-S011",
                "Non-finite number is not allowed.",
                line=line,
                column=column,
            )
        char = text[index]
        if char == "{":
            return parse_object(index, depth)
        if char == "[":
            return parse_array(index, depth)
        if char == '"':
            value, end = _decode_json_value(text, index)
            if not isinstance(value, str):
                raise ParserError("MF-S011", "Malformed JSON source document.")
            _validate_scalar_text(value)
            return value, end
        if char in "-0123456789":
            return parse_number(index)
        if text.startswith("true", index):
            return True, index + 4
        if text.startswith("false", index):
            return False, index + 5
        if text.startswith("null", index):
            return None, index + 4
        line, column = _line_column(text, index)
        raise ParserError(
            "MF-S011",
            "Malformed JSON source document.",
            line=line,
            column=column,
        )

    def parse_object(index: int, depth: int) -> tuple[dict[str, JsonValue], int]:
        _check_nesting_depth(depth)
        index += 1
        index = skip_ws(index)
        result: dict[str, JsonValue] = {}
        if index < len(text) and text[index] == "}":
            return result, index + 1
        while True:
            key_start = skip_ws(index)
            if key_start >= len(text) or text[key_start] != '"':
                line, column = _line_column(
                    text, key_start if key_start < len(text) else index
                )
                raise ParserError(
                    "MF-S011",
                    "Malformed JSON source document.",
                    line=line,
                    column=column,
                )
            key, key_end = _decode_json_value(text, key_start)
            if not isinstance(key, str):
                line, column = _line_column(text, key_start)
                raise ParserError(
                    "MF-S011",
                    "Malformed JSON source document.",
                    line=line,
                    column=column,
                )
            _validate_scalar_text(key)
            if key in result:
                raise ParserError("MF-S006", "Duplicate object key.")
            index = skip_ws(key_end)
            if index >= len(text) or text[index] != ":":
                line, column = _line_column(
                    text, index if index < len(text) else key_end
                )
                raise ParserError(
                    "MF-S011",
                    "Malformed JSON source document.",
                    line=line,
                    column=column,
                )
            state.note_entry()
            value, index = parse_value(index + 1, depth + 1)
            result[key] = value
            index = skip_ws(index)
            if index < len(text) and text[index] == ",":
                index += 1
                continue
            if index < len(text) and text[index] == "}":
                return result, index + 1
            line, column = _line_column(text, index if index < len(text) else len(text))
            raise ParserError(
                "MF-S011",
                "Malformed JSON source document.",
                line=line,
                column=column,
            )

    def parse_array(index: int, depth: int) -> tuple[list[JsonValue], int]:
        _check_nesting_depth(depth)
        index += 1
        index = skip_ws(index)
        result: list[JsonValue] = []
        if index < len(text) and text[index] == "]":
            return result, index + 1
        while True:
            item_start = skip_ws(index)
            state.note_entry()
            value, index = parse_value(item_start, depth + 1)
            result.append(value)
            index = skip_ws(index)
            if index < len(text) and text[index] == ",":
                index += 1
                continue
            if index < len(text) and text[index] == "]":
                return result, index + 1
            line, column = _line_column(text, index if index < len(text) else len(text))
            raise ParserError(
                "MF-S011",
                "Malformed JSON source document.",
                line=line,
                column=column,
            )

    def parse_number(index: int) -> tuple[int | float, int]:
        start = index
        if text[index] == "-":
            index += 1
            if index >= len(text):
                line, column = _line_column(text, start)
                raise ParserError(
                    "MF-S011",
                    "Malformed JSON source document.",
                    line=line,
                    column=column,
                )
        if index >= len(text) or not text[index].isdigit():
            line, column = _line_column(text, start)
            raise ParserError(
                "MF-S011",
                "Malformed JSON source document.",
                line=line,
                column=column,
            )
        if text[index] == "0":
            index += 1
            if index < len(text) and text[index].isdigit():
                line, column = _line_column(text, start)
                raise ParserError(
                    "MF-S011",
                    "Malformed JSON source document.",
                    line=line,
                    column=column,
                )
        else:
            while index < len(text) and text[index].isdigit():
                index += 1
        is_float = False
        if index < len(text) and text[index] == ".":
            is_float = True
            index += 1
            if index >= len(text) or not text[index].isdigit():
                line, column = _line_column(text, start)
                raise ParserError(
                    "MF-S011",
                    "Malformed JSON source document.",
                    line=line,
                    column=column,
                )
            while index < len(text) and text[index].isdigit():
                index += 1
        if index < len(text) and text[index] in "eE":
            is_float = True
            index += 1
            if index < len(text) and text[index] in "+-":
                index += 1
            if index >= len(text) or not text[index].isdigit():
                line, column = _line_column(text, start)
                raise ParserError(
                    "MF-S011",
                    "Malformed JSON source document.",
                    line=line,
                    column=column,
                )
            while index < len(text) and text[index].isdigit():
                index += 1
        lexeme = text[start:index]
        maximum = MAX_FLOAT_LEXEME_ASCII if is_float else MAX_INTEGER_LEXEME_ASCII
        if len(lexeme.encode("ascii")) > maximum:
            raise ParserError(
                "MF-S010",
                "Floating-point lexeme exceeds parser limit."
                if is_float
                else "Integer lexeme exceeds parser limit.",
            )
        return (float(lexeme) if is_float else int(lexeme)), index

    value, end = parse_value(0, depth)
    end = skip_ws(end)
    if end != len(text):
        line, column = _line_column(text, end)
        raise ParserError(
            "MF-S011",
            "JSON source document has trailing non-whitespace content.",
            line=line,
            column=column,
        )
    return value


def _decode_json_value(text: str, index: int = 0) -> tuple[JsonValue, int]:
    decoder = json.JSONDecoder(
        object_pairs_hook=_json_object_pairs,
        parse_int=_parse_json_int,
        parse_float=_parse_json_float,
        parse_constant=_reject_json_constant,
    )
    try:
        value, end = decoder.raw_decode(text, index)
    except json.JSONDecodeError as exc:
        raise ParserError(
            "MF-S011",
            "Malformed JSON source document.",
            line=exc.lineno,
            column=exc.colno,
        ) from exc
    return cast(JsonValue, value), end


def _json_object_pairs(pairs: Sequence[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        _validate_scalar_text(key)
        if key in result:
            raise ParserError("MF-S006", "Duplicate object key.")
        result[key] = value
    return result


def _parse_json_int(value: str) -> int:
    if len(value.encode("ascii")) > MAX_INTEGER_LEXEME_ASCII:
        raise ParserError("MF-S010", "Integer lexeme exceeds parser limit.")
    return int(value)


def _parse_json_float(value: str) -> float:
    if len(value.encode("ascii")) > MAX_FLOAT_LEXEME_ASCII:
        raise ParserError("MF-S010", "Floating-point lexeme exceeds parser limit.")
    return float(value)


def _reject_json_constant(value: str) -> None:
    raise ParserError("MF-S011", "Non-finite number is not allowed.")


def _check_nesting_depth(depth: int) -> None:
    if depth > MAX_NESTING_DEPTH:
        raise ParserError("MF-S010", "Nesting depth exceeds parser limit.")


def _parse_yaml(content: bytes) -> JsonValue:
    text = content.decode("utf-8")
    lines = _yaml_lines(text)
    if not lines:
        raise ParserError(
            "MF-S012", "Top-level YAML source document must be an object."
        )
    state = _ParseState()
    value, index = _parse_yaml_block(lines, 0, lines[0].indent, state, 1)
    if index != len(lines):
        line = lines[index]
        raise ParserError(
            "MF-S011",
            "Malformed YAML indentation.",
            line=line.number,
            column=line.indent + 1,
        )
    if not isinstance(value, dict):
        raise ParserError(
            "MF-S012", "Top-level YAML source document must be an object."
        )
    return value


def _yaml_lines(text: str) -> list[_YamlLine]:
    result: list[_YamlLine] = []
    document_markers = 0
    for line_number, raw_line in enumerate(text.split("\n"), start=1):
        if "\t" in raw_line:
            raise ParserError(
                "MF-S011",
                "YAML tabs are not accepted for indentation.",
                line=line_number,
                column=raw_line.index("\t") + 1,
            )
        stripped = _strip_yaml_comment(raw_line).rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        if indent == 0 and stripped.strip() in {"---", "..."}:
            document_markers += 1
            if document_markers > 1 or result:
                raise ParserError(
                    "MF-S009",
                    "YAML source must contain exactly one document.",
                    line=line_number,
                    column=raw_line.index(stripped.strip()) + 1,
                )
            continue
        if _YAML_TAG_ANCHOR_ALIAS_RE.search(stripped) is not None:
            code = (
                "MF-S007" if any(mark in stripped for mark in ("&", "*")) else "MF-S008"
            )
            raise ParserError(
                code,
                "YAML anchors, aliases, and tags are not supported.",
                line=line_number,
                column=1,
            )
        if indent % 2:
            raise ParserError(
                "MF-S011",
                "YAML indentation must use two-space levels.",
                line=line_number,
                column=indent + 1,
            )
        result.append(
            _YamlLine(number=line_number, indent=indent, text=stripped[indent:])
        )
    return result


def _parse_yaml_block(
    lines: Sequence[_YamlLine],
    index: int,
    indent: int,
    state: _ParseState,
    depth: int,
) -> tuple[JsonValue, int]:
    _check_nesting_depth(depth)
    if index >= len(lines):
        raise ParserError("MF-S011", "Expected YAML block.")
    line = lines[index]
    if line.indent != indent:
        raise ParserError(
            "MF-S011",
            "Malformed YAML indentation.",
            line=line.number,
            column=line.indent + 1,
        )
    if line.text.startswith("- "):
        return _parse_yaml_sequence(lines, index, indent, state, depth)
    return _parse_yaml_mapping(lines, index, indent, state, depth)


def _parse_yaml_mapping(
    lines: Sequence[_YamlLine],
    index: int,
    indent: int,
    state: _ParseState,
    depth: int,
) -> tuple[dict[str, JsonValue], int]:
    result: dict[str, JsonValue] = {}
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent != indent or line.text.startswith("- "):
            break
        key_text, value_text = _split_yaml_key_value(line)
        key = _parse_yaml_key(key_text, line)
        if key == "<<":
            raise ParserError(
                "MF-S008",
                "YAML merge keys are not supported.",
                line=line.number,
                column=indent + 1,
            )
        if key in result:
            raise ParserError(
                "MF-S006",
                "Duplicate mapping key.",
                line=line.number,
                column=indent + 1,
            )
        state.note_entry()
        if value_text == "":
            if index + 1 >= len(lines) or lines[index + 1].indent <= indent:
                result[key] = {}
                index += 1
            else:
                result[key], index = _parse_yaml_block(
                    lines, index + 1, lines[index + 1].indent, state, depth + 1
                )
        elif value_text == "|":
            result[key], index = _parse_yaml_block_scalar(lines, index, indent, line)
        elif value_text.startswith("|"):
            raise ParserError(
                "MF-S011",
                "Unsupported YAML block scalar indicator.",
                line=line.number,
                column=line.indent + 1,
            )
        else:
            result[key] = _parse_yaml_scalar(value_text, line, state, depth)
            index += 1
    return result, index


def _parse_yaml_sequence(
    lines: Sequence[_YamlLine],
    index: int,
    indent: int,
    state: _ParseState,
    depth: int,
) -> tuple[list[JsonValue], int]:
    result: list[JsonValue] = []
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent != indent or not line.text.startswith("- "):
            break
        item_text = line.text[2:].strip()
        state.note_entry()
        if item_text == "":
            if index + 1 >= len(lines) or lines[index + 1].indent <= indent:
                result.append({})
                index += 1
            else:
                block_item, index = _parse_yaml_block(
                    lines, index + 1, lines[index + 1].indent, state, depth + 1
                )
                result.append(block_item)
            continue
        if _looks_like_yaml_key_value(item_text):
            key_text, value_text = _split_yaml_key_value(
                _YamlLine(line.number, line.indent + 2, item_text)
            )
            key = _parse_yaml_key(key_text, line)
            state.note_entry()
            mapping_item: dict[str, JsonValue] = {
                key: (
                    _parse_yaml_scalar(value_text, line, state, depth)
                    if value_text
                    else {}
                )
            }
            index += 1
            if index < len(lines) and lines[index].indent > indent:
                extra, index = _parse_yaml_mapping(
                    lines, index, lines[index].indent, state, depth + 1
                )
                for extra_key, extra_value in extra.items():
                    if extra_key in mapping_item:
                        raise ParserError(
                            "MF-S006",
                            "Duplicate mapping key.",
                            line=line.number,
                            column=line.indent + 1,
                        )
                    mapping_item[extra_key] = extra_value
            result.append(mapping_item)
            continue
        if item_text == "|":
            block_value, index = _parse_yaml_block_scalar(lines, index, indent, line)
            result.append(block_value)
            continue
        if item_text.startswith("|"):
            raise ParserError(
                "MF-S011",
                "Unsupported YAML block scalar indicator.",
                line=line.number,
                column=line.indent + 1,
            )
        result.append(_parse_yaml_scalar(item_text, line, state, depth))
        index += 1
    return result, index


def _split_yaml_key_value(line: _YamlLine) -> tuple[str, str]:
    separator = _find_yaml_separator(line.text)
    if separator < 0:
        raise ParserError(
            "MF-S011",
            "YAML mapping entries must contain a colon.",
            line=line.number,
            column=line.indent + 1,
        )
    key = line.text[:separator].strip()
    value = line.text[separator + 1 :].strip()
    if not key:
        raise ParserError(
            "MF-S011",
            "YAML mapping key must be non-empty.",
            line=line.number,
            column=line.indent + 1,
        )
    return key, value


def _find_yaml_separator(text: str) -> int:
    quote: str | None = None
    for index, char in enumerate(text):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == ":" and (index + 1 == len(text) or text[index + 1].isspace()):
            return index
    return -1


def _looks_like_yaml_key_value(text: str) -> bool:
    return _find_yaml_separator(text) >= 0


def _parse_yaml_key(key_text: str, line: _YamlLine) -> str:
    if key_text.startswith(("'", '"')):
        value = _parse_yaml_scalar(key_text, line)
        if not isinstance(value, str):
            raise ParserError("MF-S008", "YAML mapping keys must be strings.")
        return value
    if key_text.startswith(("[", "{", "?")) or key_text in {"true", "false", "null"}:
        raise ParserError(
            "MF-S008",
            "YAML mapping keys must be strings.",
            line=line.number,
            column=line.indent + 1,
        )
    if _JSON_INTEGER_RE.fullmatch(key_text) or _JSON_FLOAT_RE.fullmatch(key_text):
        raise ParserError(
            "MF-S008",
            "YAML mapping keys must be strings.",
            line=line.number,
            column=line.indent + 1,
        )
    _validate_scalar_text(key_text)
    return key_text


def _parse_yaml_block_scalar(
    lines: Sequence[_YamlLine], index: int, indent: int, line: _YamlLine
) -> tuple[str, int]:
    block_lines: list[_YamlLine] = []
    end = index + 1
    while end < len(lines):
        next_line = lines[end]
        if next_line.indent <= indent:
            break
        block_lines.append(next_line)
        end += 1
    if not block_lines:
        raise ParserError(
            "MF-S011",
            "YAML block scalar must contain indented content.",
            line=line.number,
            column=line.indent + 1,
        )
    content_indent = min(item.indent for item in block_lines)
    pieces: list[str] = []
    for block_line in block_lines:
        if block_line.indent < content_indent:
            raise ParserError(
                "MF-S011",
                "Malformed YAML block scalar indentation.",
                line=block_line.number,
                column=block_line.indent + 1,
            )
        pieces.append(" " * (block_line.indent - content_indent) + block_line.text)
    return "\n".join(pieces) + "\n", end


def _parse_yaml_scalar(
    text: str,
    line: _YamlLine,
    state: _ParseState | None = None,
    depth: int = 1,
) -> JsonValue:
    state = state if state is not None else _ParseState()
    if text in {"null", "~"}:
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    lowered = text.lower()
    if lowered in {".nan", ".inf", "+.inf", "-.inf", "nan", "inf", "+inf", "-inf"}:
        raise ParserError(
            "MF-S011",
            "Non-finite YAML numbers are not allowed.",
            line=line.number,
            column=line.indent + 1,
        )
    if _looks_like_timestamp(text):
        raise ParserError(
            "MF-S008",
            "YAML timestamps are not supported.",
            line=line.number,
            column=line.indent + 1,
        )
    if text.startswith(("[", "{")):
        try:
            return _parse_json_text(text, state=state, depth=depth + 1)
        except ParserError as exc:
            if exc.line is not None and exc.column is not None:
                raise ParserError(
                    exc.code,
                    exc.message,
                    line=line.number + exc.line - 1,
                    column=line.indent + exc.column,
                ) from exc
            raise
    if text.startswith('"'):
        try:
            value = _parse_json_text(text, state=state, depth=depth + 1)
        except ParserError as exc:
            if exc.line is not None and exc.column is not None:
                raise ParserError(
                    exc.code,
                    exc.message,
                    line=line.number + exc.line - 1,
                    column=line.indent + exc.column,
                ) from exc
            raise
        if not isinstance(value, str):
            raise ParserError(
                "MF-S011",
                "Malformed YAML quoted scalar.",
                line=line.number,
                column=line.indent + 1,
            )
        return value
    if text.startswith("'") and text.endswith("'"):
        value = text[1:-1].replace("''", "'")
        _validate_scalar_text(value)
        return value
    if _JSON_INTEGER_RE.fullmatch(text):
        if len(text.encode("ascii")) > MAX_INTEGER_LEXEME_ASCII:
            raise ParserError("MF-S010", "Integer lexeme exceeds parser limit.")
        return int(text)
    if _JSON_FLOAT_RE.fullmatch(text):
        if len(text.encode("ascii")) > MAX_FLOAT_LEXEME_ASCII:
            raise ParserError("MF-S010", "Floating-point lexeme exceeds parser limit.")
        return float(text)
    _validate_scalar_text(text)
    return text


def _strip_yaml_comment(line: str) -> str:
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
        index += 1
    return line


def _looks_like_timestamp(text: str) -> bool:
    return re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[Tt ].*)?", text) is not None


def _validate_json_compatible(value: JsonValue) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_scalar_text(key)
            _validate_json_compatible(item)
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_compatible(item)
        return
    if isinstance(value, str):
        _validate_scalar_text(value)
        return
    if isinstance(value, float) and not (float("-inf") < value < float("inf")):
        raise ParserError("MF-S011", "Non-finite number is not allowed.")
    if not isinstance(value, (int, bool, type(None), float)):
        raise ParserError("MF-S008", "Only JSON-native YAML values are supported.")


def _validate_scalar_text(value: str) -> None:
    _reject_forbidden_text(value)
    if len(value.encode("utf-8")) > MAX_SCALAR_UTF8_SIZE:
        raise ParserError("MF-S010", "Scalar exceeds parser UTF-8 size limit.")


def _enforce_limits(value: JsonValue) -> None:
    entries = 0

    def walk(item: JsonValue, depth: int) -> None:
        nonlocal entries
        if depth > MAX_NESTING_DEPTH:
            raise ParserError("MF-S010", "Nesting depth exceeds parser limit.")
        if isinstance(item, Mapping):
            entries += len(item)
            if entries > MAX_TOTAL_ENTRIES:
                raise ParserError(
                    "MF-S010", "Total mapping/list entries exceed parser limit."
                )
            for child in item.values():
                walk(child, depth + 1)
        elif isinstance(item, list):
            entries += len(item)
            if entries > MAX_TOTAL_ENTRIES:
                raise ParserError(
                    "MF-S010", "Total mapping/list entries exceed parser limit."
                )
            for child in item:
                walk(child, depth + 1)

    walk(value, 1)


def _build_location_index(
    document: SourceDocument, content: bytes, payload: JsonValue
) -> tuple[SourceReference, ...]:
    if not isinstance(payload, Mapping):
        return ()
    text = content.decode("utf-8")
    if document.format == "json":
        references = _json_location_references(document.logical_path, text)
    else:
        references = _yaml_location_references(document.logical_path, text)
    path_aliases = _source_path_aliases(payload)
    known_payload_paths = {
        _translate_source_path(path, path_aliases) for path in _payload_paths(payload)
    }
    translated: list[SourceReference] = []
    for reference in references:
        translated_path = _translate_source_path(reference.field_path, path_aliases)
        if translated_path in known_payload_paths:
            translated.append(
                SourceReference(
                    logical_path=reference.logical_path,
                    field_path=translated_path,
                    location=reference.location,
                )
            )
    return tuple(translated)


def _payload_paths(value: JsonValue, path: str = "/") -> tuple[str, ...]:
    result = [path]
    if isinstance(value, Mapping):
        for key, item in value.items():
            result.extend(_payload_paths(item, _join_pointer(path, key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.extend(_payload_paths(item, _join_pointer(path, str(index))))
    return tuple(result)


def _source_path_aliases(
    value: JsonValue,
    *,
    raw_path: str = "/",
    model_path: str = "/",
    aliases: dict[str, str] | None = None,
) -> dict[str, str]:
    aliases = {} if aliases is None else aliases
    aliases[raw_path] = model_path
    if isinstance(value, Mapping):
        converted = raw_path.endswith(
            ("/nodes", "/argument_matches", "/required_by_terminal")
        )
        for index, (key, item) in enumerate(value.items()):
            child_raw_path = _join_pointer(raw_path, key)
            child_model_path = (
                _join_pointer(model_path, str(index))
                if converted
                else _join_pointer(model_path, key)
            )
            aliases[child_raw_path] = child_model_path
            _source_path_aliases(
                item,
                raw_path=child_raw_path,
                model_path=child_model_path,
                aliases=aliases,
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            child_raw_path = _join_pointer(raw_path, str(index))
            child_model_path = _join_pointer(model_path, str(index))
            _source_path_aliases(
                item,
                raw_path=child_raw_path,
                model_path=child_model_path,
                aliases=aliases,
            )
    return aliases


def _translate_source_path(path: str, aliases: Mapping[str, str]) -> str:
    candidate = path
    while candidate not in aliases:
        if candidate == "/":
            return path
        candidate = candidate.rsplit("/", 1)[0] or "/"
    translated = aliases[candidate]
    if candidate == path:
        return translated
    suffix = path[len(candidate) :]
    return translated + suffix if suffix else translated


def _json_location_references(
    logical_path: str, text: str
) -> tuple[SourceReference, ...]:
    references: list[SourceReference] = []

    def skip_ws(index: int) -> int:
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        return index

    def parse_value(index: int, path: str) -> int:
        index = skip_ws(index)
        if index >= len(text):
            return index
        if text[index] == "{":
            return parse_object(index, path)
        if text[index] == "[":
            return parse_array(index, path)
        _, end = _decode_json_value(text, index)
        return end

    def parse_object(index: int, path: str) -> int:
        index += 1
        index = skip_ws(index)
        if index < len(text) and text[index] == "}":
            return index + 1
        while index < len(text):
            key_start = skip_ws(index)
            key, key_end = _decode_json_value(text, key_start)
            if not isinstance(key, str):
                return key_end
            line, column = _line_column(text, key_start)
            key_path = _join_pointer(path, key)
            references.append(
                SourceReference(
                    logical_path=logical_path,
                    field_path=key_path,
                    location=SourceLocation(line=line, column=column),
                )
            )
            index = skip_ws(key_end)
            if index >= len(text) or text[index] != ":":
                return index
            index = parse_value(index + 1, key_path)
            index = skip_ws(index)
            if index < len(text) and text[index] == ",":
                index += 1
                continue
            if index < len(text) and text[index] == "}":
                return index + 1
            return index
        return index

    def parse_array(index: int, path: str) -> int:
        index += 1
        item_index = 0
        index = skip_ws(index)
        if index < len(text) and text[index] == "]":
            return index + 1
        while index < len(text):
            item_start = skip_ws(index)
            item_path = _join_pointer(path, str(item_index))
            line, column = _line_column(text, item_start)
            references.append(
                SourceReference(
                    logical_path=logical_path,
                    field_path=item_path,
                    location=SourceLocation(line=line, column=column),
                )
            )
            index = parse_value(item_start, item_path)
            item_index += 1
            index = skip_ws(index)
            if index < len(text) and text[index] == ",":
                index += 1
                continue
            if index < len(text) and text[index] == "]":
                return index + 1
            return index
        return index

    parse_value(0, "/")
    return tuple(references)


def _yaml_location_references(
    logical_path: str, text: str
) -> tuple[SourceReference, ...]:
    references: list[SourceReference] = []

    lines = _yaml_lines(text)
    if not lines:
        return ()

    def record(field_path: str, line: int, column: int) -> None:
        references.append(
            SourceReference(
                logical_path=logical_path,
                field_path=field_path,
                location=SourceLocation(line=line, column=column),
            )
        )

    def consume_block_scalar(index: int, indent: int) -> int:
        end = index + 1
        while end < len(lines):
            next_line = lines[end]
            if next_line.indent <= indent:
                break
            end += 1
        return end

    def walk_block(index: int, indent: int, path: str) -> int:
        if index >= len(lines):
            return index
        line = lines[index]
        if line.indent != indent:
            return index
        if line.text.startswith("- "):
            return walk_sequence(index, indent, path)
        return walk_mapping(index, indent, path)

    def walk_mapping(index: int, indent: int, path: str) -> int:
        while index < len(lines):
            line = lines[index]
            if line.indent < indent:
                break
            if line.indent != indent or line.text.startswith("- "):
                break
            key_text, value_text = _split_yaml_key_value(line)
            key = _parse_yaml_key(key_text, line)
            key_path = _join_pointer(path, key)
            key_column = line.indent + line.text.index(key_text) + 1
            record(key_path, line.number, key_column)
            if value_text == "":
                index += 1
                if index < len(lines) and lines[index].indent > indent:
                    index = walk_block(index, lines[index].indent, key_path)
                continue
            if value_text == "|":
                index = consume_block_scalar(index, indent)
                continue
            index += 1
        return index

    def walk_sequence(index: int, indent: int, path: str) -> int:
        item_index = 0
        while index < len(lines):
            line = lines[index]
            if line.indent < indent:
                break
            if line.indent != indent or not line.text.startswith("- "):
                break
            item_path = _join_pointer(path, str(item_index))
            record(item_path, line.number, line.indent + 1)
            item_text = line.text[2:].strip()
            if item_text == "":
                index += 1
                if index < len(lines) and lines[index].indent > indent:
                    index = walk_block(index, lines[index].indent, item_path)
                item_index += 1
                continue
            if _looks_like_yaml_key_value(item_text):
                item_line = _YamlLine(line.number, line.indent + 2, item_text)
                key_text, _ = _split_yaml_key_value(item_line)
                key = _parse_yaml_key(key_text, line)
                key_path = _join_pointer(item_path, key)
                key_column = item_line.indent + item_line.text.index(key_text) + 1
                record(key_path, line.number, key_column)
                index += 1
                if index < len(lines) and lines[index].indent > indent:
                    index = walk_mapping(index, lines[index].indent, item_path)
                item_index += 1
                continue
            if item_text == "|":
                index = consume_block_scalar(index, indent)
                item_index += 1
                continue
            index += 1
            item_index += 1
        return index

    walk_block(0, lines[0].indent, "/")
    return tuple(references)


def _nearest_source_reference(
    document: SourceDocument,
    field_path: str,
    location_index: tuple[SourceReference, ...],
) -> SourceReference:
    by_path = {reference.field_path: reference for reference in location_index}
    candidate = field_path
    while candidate:
        if candidate in by_path:
            return by_path[candidate]
        if candidate == "/":
            break
        candidate = candidate.rsplit("/", 1)[0] or "/"
    return SourceReference(logical_path=document.logical_path, field_path=field_path)


def _join_pointer(parent: str, token: str) -> str:
    escaped = token.replace("~", "~0").replace("/", "~1")
    return f"/{escaped}" if parent == "/" else f"{parent}/{escaped}"


def _diagnostic(
    document: SourceDocument,
    error: ParserError,
    location_index: tuple[SourceReference, ...] = (),
) -> CompilerDiagnostic:
    phase, severity = (
        CompilerPhase.SCHEMA
        if error.code.startswith("MF-S02")
        else CompilerPhase.PARSE,
        DiagnosticSeverity.ERROR,
    )
    source_reference = _nearest_source_reference(
        document, error.field_path, location_index
    )
    if error.line is not None and error.column is not None:
        source_reference = SourceReference(
            logical_path=document.logical_path,
            field_path=error.field_path,
            location=SourceLocation(line=error.line, column=error.column),
        )
    return CompilerDiagnostic(
        code=error.code,
        phase=phase,
        severity=severity,
        message=error.message,
        source_reference=source_reference,
        fields=error.fields,
    )


def _schema_validation_diagnostics(
    document: SourceDocument,
    exc: ValidationError,
    location_index: tuple[SourceReference, ...],
) -> tuple[CompilerDiagnostic, ...]:
    diagnostics = []
    for error in exc.errors():
        code = _schema_validation_code(error)
        field_path = _validation_error_pointer(error)
        diagnostics.append(
            _diagnostic(
                document,
                ParserError(
                    code,
                    _schema_validation_message(code),
                    field_path=field_path,
                    fields=(DiagnosticField(key="field_path", value=field_path),),
                ),
                location_index,
            )
        )
    if not diagnostics:
        diagnostics.append(
            _diagnostic(
                document,
                ParserError(
                    "MF-S020",
                    _schema_validation_message("MF-S020"),
                    field_path="/",
                    fields=(DiagnosticField(key="field_path", value="/"),),
                ),
                location_index,
            )
        )
    return bound_diagnostics(diagnostics)


def _schema_validation_code(error: Mapping[str, Any]) -> str:
    error_type = error.get("type")
    loc = error.get("loc")
    path = tuple(str(part) for part in loc) if isinstance(loc, tuple) else ()
    message = str(error.get("msg", ""))

    if error_type == "extra_forbidden":
        return "MF-S021"
    if "tool_ref" in path or "tool_ref" in message:
        return "MF-S023"
    if path and path[0] == "budgets":
        return "MF-S024"
    if path and path[0] == "context":
        return "MF-S025"
    if _is_identifier_validation_error(path, message):
        return "MF-S022"
    return "MF-S020"


def _is_identifier_validation_error(path: tuple[str, ...], message: str) -> bool:
    identifier_names = {
        "argument_name",
        "artifact_id",
        "current_argument",
        "declared_artifact_ids",
        "expected_harness_id",
        "harness_id",
        "model_profile_id",
        "node_id",
        "policy_id",
        "prior_argument",
        "produces",
        "profile_id",
        "required_by_terminal",
        "stage_kind_id",
        "stage_kind_ids",
        "terminal_result",
    }
    identifier_message_markers = (
        "argument_name ",
        "artifact_id ",
        "harness_id ",
        "node_id ",
        "policy_id ",
        "profile_id ",
        "stage_kind_id ",
        "terminal_result ",
    )
    return any(part in identifier_names for part in path) or any(
        marker in message for marker in identifier_message_markers
    )


def _schema_validation_message(code: str) -> str:
    return {
        "MF-S021": "Source schema contains an unknown field.",
        "MF-S022": "Source identifier is invalid.",
        "MF-S023": "Source tool_ref must be an exact-version tool reference.",
        "MF-S024": "Source budget value is invalid.",
        "MF-S025": "Source context policy value is invalid.",
    }.get(code, "Source schema validation failed.")


def _validation_pointer(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "/"
    return _validation_error_pointer(errors[0])


def _validation_error_pointer(error: Mapping[str, Any]) -> str:
    loc = error.get("loc")
    if not isinstance(loc, tuple) or not loc:
        return "/"
    parts = [str(part).replace("~", "~0").replace("/", "~1") for part in loc]
    return "/" + "/".join(parts)


def _line_column(text: str, index: int) -> tuple[int, int]:
    line = text.count("\n", 0, index) + 1
    line_start = text.rfind("\n", 0, index) + 1
    return line, index - line_start + 1


DefaultHarnessSourceParser = HarnessSourceParser
