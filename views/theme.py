"""The look of the app: two themes, and the names the views use for colours.

Every colour a view draws with is a *role* of the theme (``ON_SURFACE_VARIANT``,
``ERROR``, ...), never a fixed shade such as ``GREY_400``. A role is resolved by
the client against the theme that is on, so the whole window follows a change of
theme at once, with nothing to redraw. A fixed shade is made for one background:
the pale greys and ambers that read well on the dark theme are close to invisible
on the light one. ``tests/test_theme.py`` fails on a fixed shade in a view.

The colour scheme has no role for "attention" or for the pink of Lili, so two roles
are given those colours in both themes (nothing else in the app draws with them):
``tertiary`` is the attention colour (a warning, a note, a file that is waiting)
and ``secondary`` is Lili's. The plain green (``OK``) is one shade that reads on
both backgrounds.
"""

from __future__ import annotations

import flet as ft

from state.settings import THEME_LIGHT

C = ft.Colors

# The colour every other colour of the scheme is derived from.
SEED = C.BLUE

# ---------------------------------------------------------------------- text
MUTED = C.ON_SURFACE_VARIANT  # hints, counters, the name over a message
FAINT = C.OUTLINE  # what must not draw the eye: a time, an empty-state icon
ACCENT = C.PRIMARY  # you; a model in the cloud; work in progress
LILI = C.SECONDARY  # Lili
ATTENTION = C.TERTIARY  # a warning, a note, something that waits
PROBLEM = C.ERROR  # an error, a refusal, a connection that is gone
OK = C.GREEN_700  # it worked; reads on a dark and on a light background

# ------------------------------------------------------------------ surfaces
YOU_BUBBLE = C.SECONDARY_CONTAINER
LILI_BUBBLE = C.SURFACE_CONTAINER_HIGH
CARD = C.SURFACE_CONTAINER  # a card or a panel inside a view
FLOATING = C.SURFACE_CONTAINER_HIGHEST  # a button over the content
BORDER = C.OUTLINE_VARIANT
PROBLEM_BACKGROUND = C.ERROR_CONTAINER
ON_PROBLEM_BACKGROUND = C.ON_ERROR_CONTAINER
# A snackbar brings its own background and white text, so it needs no role.
TOAST_OK = C.GREEN_700
TOAST_PROBLEM = C.RED_700


def _theme(*, lili: str, attention: str) -> ft.Theme:
    return ft.Theme(
        color_scheme_seed=SEED,
        color_scheme=ft.ColorScheme(secondary=lili, tertiary=attention),
    )


def dark_theme() -> ft.Theme:
    return _theme(lili=C.PINK_200, attention=C.ORANGE_300)


def light_theme() -> ft.Theme:
    return _theme(lili=C.PINK_800, attention=C.ORANGE_900)


def code_theme(page: ft.Page | None) -> ft.MarkdownCodeTheme:
    """The colours of a code block that fit the theme that is on.

    The Markdown control keeps the code theme it was built with and cannot follow
    the colour scheme, so it is chosen here, and set again by ``ChatMessage.restyle``
    when the theme changes. Light text in a dark block (or the other way round)
    is what a block of the wrong theme looks like.
    """
    light = getattr(page, "theme_mode", None) == ft.ThemeMode.LIGHT
    return (
        ft.MarkdownCodeTheme.ATOM_ONE_LIGHT
        if light
        else ft.MarkdownCodeTheme.ATOM_ONE_DARK
    )


def apply_theme(page: ft.Page, name: str) -> None:
    """Put the theme called ``name`` (``THEMES`` of the settings) on ``page``.

    Sends nothing: before the first update the page goes out as it is, and later
    the caller sends it with ``safe_update``.
    """
    page.theme = light_theme()
    page.dark_theme = dark_theme()
    page.theme_mode = ft.ThemeMode.LIGHT if name == THEME_LIGHT else ft.ThemeMode.DARK
