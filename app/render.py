"""Shared markdown -> safe HTML rendering for anything an LLM wrote (scenario
prompts, tutorial bodies). LLM output is never trusted into the DOM directly —
docs §10 Security, 'XSS from LLM output'."""

import re

import markdown as md
import nh3

_ALLOWED_TAGS = {
    "p", "br", "strong", "em", "ul", "ol", "li", "code", "pre", "blockquote",
    "h1", "h2", "h3", "h4", "a", "table", "thead", "tbody", "tr", "th", "td",
    "hr", "span", "del",
}
_ALLOWED_ATTRS = {"a": {"href", "title"}, "code": {"class"}, "span": {"class"}}

_RESOURCE_MARKER = re.compile(r"\[\[res:([a-z0-9][a-z0-9\-]*)\]\]")


def render_markdown(text, resolve_resource_marker=None):
    """resolve_resource_marker(resource_id) -> html_snippet | None. Returning
    None (unknown or uncited id) drops the marker to plain text rather than a
    broken link — see docs §7.4 / §14 'Resource-citation tests'."""
    text = text or ""

    if resolve_resource_marker:
        def _sub(match):
            resolved = resolve_resource_marker(match.group(1))
            return resolved if resolved is not None else ""

        text = _RESOURCE_MARKER.sub(_sub, text)
    else:
        text = _RESOURCE_MARKER.sub("", text)

    html = md.markdown(text, extensions=["fenced_code", "tables"])
    return nh3.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, link_rel="noopener noreferrer")
