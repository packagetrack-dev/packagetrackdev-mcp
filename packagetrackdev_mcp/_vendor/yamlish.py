"""Parse the YAML subset that lock files are written in."""

from __future__ import annotations

_BLOCK_SCALAR = frozenset({"|", ">", "|-", ">-", "|+", ">+"})


class _Line:
    __slots__ = ("indent", "text")

    def __init__(self, indent: int, text: str) -> None:
        self.indent = indent
        self.text = text

    @property
    def is_item(self) -> bool:
        return self.text == "-" or self.text.startswith("- ")


def loads(content: str) -> dict:
    """Parse `content` into nested dicts, lists and strings."""
    lines: list[_Line] = []
    for raw in content.splitlines():
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ValueError("tab in indentation")
        text = _strip_comment(raw).rstrip()
        stripped = text.lstrip(" ")
        if not stripped or stripped in ("---", "..."):
            continue
        if stripped.startswith(("&", "*", "!", "%", "? ")):
            raise ValueError(f"unsupported YAML construct: {stripped[:40]!r}")
        lines.append(_Line(len(text) - len(stripped), stripped))
    if not lines:
        return {}
    value, end = _block(lines, 0, lines[0].indent)
    if end != len(lines):
        raise ValueError(f"unexpected indentation at {lines[end].text[:40]!r}")
    if not isinstance(value, dict):
        raise ValueError("top level is not a mapping")
    return value


def _strip_comment(raw: str) -> str:
    """Return `raw` without a trailing `# comment`, quotes respected."""
    quote = ""
    previous = " "
    i = 0
    while i < len(raw):
        char = raw[i]
        if quote:
            if quote == '"' and char == "\\":
                i += 2
                previous = "x"
                continue
            if char == quote:
                if quote == "'" and raw[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                quote = ""
        elif char in "'\"" and previous in " :-[{,":
            quote = char
        elif char == "#" and previous in " \t":
            return raw[:i]
        previous = char
        i += 1
    return raw


def _block(lines: list[_Line], i: int, indent: int) -> tuple[object, int]:
    if lines[i].is_item:
        return _sequence(lines, i, indent)
    return _mapping(lines, i, indent)


def _mapping(lines: list[_Line], i: int, indent: int) -> tuple[dict, int]:
    result: dict = {}
    while i < len(lines) and lines[i].indent == indent and not lines[i].is_item:
        key, rest = _split_key(lines[i].text)
        i += 1
        if rest == "":
            nested = i < len(lines) and (
                lines[i].indent > indent
                or (lines[i].indent == indent and lines[i].is_item)
            )
            if nested:
                value, i = _block(lines, i, lines[i].indent)
            else:
                value = None
        elif rest in _BLOCK_SCALAR:
            parts = []
            while i < len(lines) and lines[i].indent > indent:
                parts.append(lines[i].text)
                i += 1
            value = "\n".join(parts)
        else:
            value = _scalar(rest)
            if isinstance(value, str) and not rest.startswith(("'", '"')):
                while (
                    i < len(lines)
                    and lines[i].indent > indent
                    and _key_split_point(lines[i].text) is None
                    and not lines[i].is_item
                ):
                    value = f"{value} {lines[i].text}"
                    i += 1
        result[key] = value
    if i < len(lines) and lines[i].indent > indent:
        raise ValueError(f"unexpected indentation at {lines[i].text[:40]!r}")
    return result, i


def _sequence(lines: list[_Line], i: int, indent: int) -> tuple[list, int]:
    items: list = []
    while i < len(lines) and lines[i].indent == indent and lines[i].is_item:
        rest = lines[i].text[1:].lstrip(" ")
        if rest == "":
            i += 1
            if i < len(lines) and lines[i].indent > indent:
                value, i = _block(lines, i, lines[i].indent)
            else:
                value = None
            items.append(value)
            continue
        if not rest.startswith(("'", '"', "[", "{")) and _key_split_point(rest):
            offset = indent + (len(lines[i].text) - len(rest))
            lines[i] = _Line(offset, rest)
            value, i = _mapping(lines, i, offset)
            items.append(value)
            continue
        items.append(_scalar(rest))
        i += 1
    return items, i


def _key_split_point(text: str) -> int | None:
    """Index of the `:` ending a plain key in `text`, or None."""
    at = text.find(": ")
    if at > 0:
        return at
    if text.endswith(":") and len(text) > 1:
        return len(text) - 1
    return None


def _split_key(text: str) -> tuple[str, str]:
    if text[0] in "'\"":
        key, end = _quoted(text, 0)
        rest = text[end:].lstrip(" ")
        if not rest.startswith(":"):
            raise ValueError(f"no ':' after key {key[:40]!r}")
        return key, rest[1:].strip()
    at = _key_split_point(text)
    if at is None:
        raise ValueError(f"not a mapping entry: {text[:40]!r}")
    return text[:at].rstrip(), text[at + 1 :].strip()


def _quoted(text: str, start: int) -> tuple[str, int]:
    """Read the quoted string starting at `start`: (value, index after it)."""
    quote = text[start]
    out: list[str] = []
    i = start + 1
    while i < len(text):
        char = text[i]
        if quote == '"' and char == "\\":
            escaped = text[i + 1 : i + 2]
            out.append(
                {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/"}.get(
                    escaped, escaped
                )
            )
            i += 2
            continue
        if char == quote:
            if quote == "'" and text[i + 1 : i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(char)
        i += 1
    raise ValueError("unterminated quoted string")


def _scalar(text: str) -> object:
    text = text.strip()
    if text[:1] in "'\"":
        value, end = _quoted(text, 0)
        if text[end:].strip():
            raise ValueError(f"text after a quoted scalar: {text[:40]!r}")
        return value
    if text.startswith("{") and text.endswith("}"):
        return _flow_mapping(text[1:-1])
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(part) for part in _flow_parts(text[1:-1])]
    if text in ("~", "null"):
        return None
    return text


def _flow_parts(body: str) -> list[str]:
    """Split a flow collection's body on top-level commas."""
    parts: list[str] = []
    depth = 0
    quote = ""
    current: list[str] = []
    for char in body:
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return [part for part in parts if part]


def _flow_mapping(body: str) -> dict:
    result: dict = {}
    for part in _flow_parts(body):
        if part[0] in "'\"":
            key, rest = _split_key(part)
        else:
            at = part.find(": ")
            if at <= 0:
                key, rest = part.rstrip(":"), ""
            else:
                key, rest = part[:at], part[at + 1 :]
        result[key.strip()] = _scalar(rest) if rest.strip() else None
    return result
