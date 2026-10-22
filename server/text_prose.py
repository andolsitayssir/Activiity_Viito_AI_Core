"""Shared prose helpers for the write-then-inject flow.

When a turn asks Viito to put text into a document, the text itself is produced
by the general (redaction) domain and injected by the office domain. Both the
routing layer (``server.webapp.api``) and the office executor need the same two
judgements to agree on it:

* ``is_topic_fragment``: the classifier surfaced the request's SUBJECT instead of
  a text to insert -> the turn needs the redaction domain (or must fail honestly).
* ``clean_prose``: the redaction model framed its answer for a chat ("Voici ...")
  -> that framing must not land in the document.
"""

from __future__ import annotations

import re

#: Longer texts are assumed to be real content, not a subject line.
FRAGMENT_MAX_CHARS = 160

#: Framing the redaction model adds around its answer.
_PREAMBLE_RE = re.compile(r"^\s*(?:voici|here is|here's)[^\n:]{0,200}:\s*", re.IGNORECASE)
_WRAPPED_RE = re.compile(r"^[«\"']+(?P<body>.+?)[»\"']+$", re.DOTALL)

#: Quotation marks a user may use to dictate the exact text to insert.
_QUOTE_CHARS = " \t«»\"'“”‘’"
_QUOTE_PAIRS = (("«", "»"), ("“", "”"), ("'", "'"), ('"', '"'))


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _is_quoted_span(text: str, intent: str) -> bool:
    """True when the user delimited this exact text with quotes in the request.

    "ajoute le texte «Bonjour l'équipe»" is a literal to insert verbatim: it is
    echoed by the classifier like a topic is, but the quotes mark it as dictated
    content rather than a subject to expand.
    """
    t = (text or "").strip()
    if not t or not intent:
        return False
    norm_t = _norm(t)
    for opener, closer in _QUOTE_PAIRS:
        start = 0
        while True:
            i = intent.find(opener, start)
            if i < 0:
                break
            j = intent.find(closer, i + len(opener))
            if j < 0:
                break
            span = intent[i + len(opener) : j].strip(_QUOTE_CHARS)
            if span and _norm(span) == norm_t:
                return True
            start = i + len(opener)
    return False


def clean_prose(text: str) -> str:
    """Strip the model's framing from generated prose before injecting it.

    Keeps the document readable: the redaction answer is prose to insert, not a
    chat reply, so its preamble and enclosing quotes are removed.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    cleaned = _PREAMBLE_RE.sub("", cleaned, count=1).strip()
    match = _WRAPPED_RE.match(cleaned)
    if match and len(match.group("body").strip()) >= 40:
        cleaned = match.group("body").strip()
    return cleaned


def is_topic_fragment(text: str, intent: str = "") -> bool:
    """True when ``text`` is the request's subject rather than content to insert.

    The attribute classifier often returns the topic of the request (e.g. "les
    bienfaits de l'utilisation de l'IA dans les entreprises") instead of a written
    paragraph; inserting that verbatim would drop a stub in the document.
    """
    t = (text or "").strip()
    if len(t) < 12:
        return True
    if len(t) > FRAGMENT_MAX_CHARS:
        return False
    # Quoted text is a literal to insert verbatim ("ajoute le texte \"Bonjour\"").
    if any(q in t for q in "\"\u00ab\u00bb\u201c\u201d"):
        return False
    if _is_quoted_span(t, intent or ""):
        return False  # the user dictated this exact text
    flat = _norm(t)
    haystack = _norm(intent or "")
    if flat and flat in haystack:
        return True  # the classifier echoed the request itself
    # No sentence terminator and few words -> a topic label rather than prose.
    return not re.search(r"[.!?;:]", t) and len(t.split()) < 25
