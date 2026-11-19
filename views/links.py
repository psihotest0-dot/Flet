"""Where a link in an answer goes, said before it is opened (P20).

What the model writes can come from a web page or a book, and the text of a link is
free: ``[your bank](https://evil.example/login)`` shows the first and opens the
second. So a click on a web link first names the host it will reach, and only a yes
opens it (``ChatMessage._open_link``). Pure functions, no Flet.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# The longest address shown in full; a longer one is cut, its host is named anyway.
SHOWN_URL_CHARS = 300

WEB_PREFIXES = ("http://", "https://")


def link_host(url: str) -> str | None:
    """The host a web link reaches, as the browser reads it; ``None`` when it has none."""
    try:
        return urlsplit(url.strip()).hostname or None
    except ValueError:
        # An address that cannot be read (``http://[::1``) is not opened.
        return None


def ascii_host(host: str) -> str:
    """The host as the network knows it: ``xn--...`` for a name with other letters."""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def shown_url(url: str) -> str:
    """The address as it is shown in the question: cut if it is very long."""
    url = url.strip()
    if len(url) <= SHOWN_URL_CHARS:
        return url
    return url[: SHOWN_URL_CHARS - 1] + "…"


def link_question(url: str) -> str:
    """The text of the question "open this link?" for a web link that has a host.

    Names the host first - it is what decides where the click goes. Two ways to
    make an address look like another site get a line of their own: a ``name@``
    in front of the host (the name is not the site) and a host with letters that
    look like Latin ones (a Cyrillic "а" in ``аpple.com``).
    """
    host = link_host(url)
    if host is None:
        raise ValueError("a link without a host has nothing to ask about")
    lines = [f"Връзката води към {host}.", "", shown_url(url)]
    try:
        has_user = urlsplit(url.strip()).username is not None
    except ValueError:
        has_user = False
    if has_user:
        lines += [
            "",
            f"В адреса има „@“: каквото стои пред него не е сайтът. Сайтът е {host}.",
        ]
    if not host.isascii():
        lines += [
            "",
            "В името има необичайни букви (може да е подобие на друг сайт). "
            f"За браузъра то е {ascii_host(host)}.",
        ]
    return "\n".join(lines)
