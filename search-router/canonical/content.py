"""Stable content fingerprints and inexpensive near-duplicate detection."""

import hashlib
import html
import re
import unicodedata
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}
_IGNORED_TAGS = {"noscript", "script", "style", "template"}
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_TOKEN_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)


class _TextExtractor(HTMLParser):
    """Extract visible text while retaining boundaries between block nodes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.casefold()
        if tag in _IGNORED_TAGS:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if not self._ignored_depth and tag.casefold() in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _IGNORED_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif not self._ignored_depth and tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _normalized_content(value: str) -> str:
    if not isinstance(value, str) or not value:
        return ""

    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except (AssertionError, ValueError):
        # HTMLParser is deliberately forgiving, but malformed input should
        # still produce a stable plain-text fingerprint if parsing fails.
        text = re.sub(r"<[^>]*>", " ", value)

    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    return " ".join(text.split())


def content_fingerprint(html_or_text: str) -> str:
    """Return the SHA-256 digest of normalized visible text.

    Markup, comments, scripts, styles, entity encoding, Unicode compatibility
    forms, and whitespace differences do not affect the digest.
    """

    normalized = _normalized_content(html_or_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def near_duplicate(f1: str, f2: str, threshold: float = 0.9) -> bool:
    """Return whether two fingerprints or text values are near-duplicates.

    Equal SHA-256 fingerprints are exact duplicates. Raw text is compared with
    token-set Jaccard overlap after the same HTML/text normalization used by
    :func:`content_fingerprint`. Different SHA-256 values cannot be compared
    approximately because a cryptographic digest does not retain token data.
    """

    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")
    if not isinstance(f1, str) or not isinstance(f2, str):
        return False

    first_is_hash = bool(_SHA256_RE.fullmatch(f1.strip()))
    second_is_hash = bool(_SHA256_RE.fullmatch(f2.strip()))
    if first_is_hash or second_is_hash:
        return first_is_hash and second_is_hash and f1.casefold() == f2.casefold()

    first = _normalized_content(f1)
    second = _normalized_content(f2)
    if first == second:
        return True
    if not first or not second:
        return False

    first_tokens = {token.casefold() for token in _TOKEN_RE.findall(first)}
    second_tokens = {token.casefold() for token in _TOKEN_RE.findall(second)}
    if not first_tokens or not second_tokens:
        return False

    overlap = len(first_tokens & second_tokens) / len(first_tokens | second_tokens)
    return overlap >= threshold
