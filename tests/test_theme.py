"""The light and the dark theme, and the rule that keeps the views theme-proof.

A view that draws with ``ft.Colors.GREY_400`` is readable on the dark theme and
nearly blank on the light one, and nothing fails until somebody looks. So a fixed
shade in a view is a test failure; what views use are the roles of the colour
scheme (``views/theme.py``), which the client resolves for the theme that is on.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import flet as ft
import pytest

from state.settings import THEME_DARK, THEME_LIGHT, THEMES
from views import theme

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    ROOT / "main.py",
    *(path for path in (ROOT / "views").glob("*.py") if path.name != "theme.py"),
]
# What the colour scheme itself has a role for, spelled as ``ft.Colors`` names it.
ROLES = {field.name.upper() for field in dataclasses.fields(ft.ColorScheme)}
# Not a shade that suits one background: nothing and the text of a snackbar.
ALLOWED = {"TRANSPARENT", "WHITE"}
COLOUR = re.compile(r"\bColors\.([A-Z][A-Z0-9_]*)")


@pytest.mark.parametrize(
    "path", SOURCES, ids=lambda path: path.relative_to(ROOT).as_posix()
)
def test_a_view_draws_only_with_the_roles_of_the_theme(path):
    used = set(COLOUR.findall(path.read_text(encoding="utf-8")))

    fixed = sorted(used - ROLES - ALLOWED)

    assert not fixed, (
        f"{path.name} uses the fixed shades {fixed}: take a name from views/theme.py "
        "(a fixed shade is made for one background and disappears on the other)"
    )


def test_every_name_of_the_theme_module_is_a_role_or_a_toast_shade():
    names = {
        name: value
        for name, value in vars(theme).items()
        if name.isupper() and isinstance(value, ft.Colors)
    }

    # The two toast shades and the plain green carry their own background or are
    # one shade for both backgrounds; everything else must be a role.
    shades = {"OK", "TOAST_OK", "TOAST_PROBLEM", "SEED"}
    assert names
    for name, value in names.items():
        assert name in shades or value.name in ROLES, (name, value)


@pytest.mark.parametrize("make", [theme.dark_theme, theme.light_theme])
def test_both_themes_grow_from_the_same_seed(make):
    assert make().color_scheme_seed == theme.SEED


def test_lili_and_the_attention_colour_are_paler_in_the_dark_theme():
    dark, light = theme.dark_theme().color_scheme, theme.light_theme().color_scheme

    assert dark.secondary != light.secondary
    assert dark.tertiary != light.tertiary
    # Pale on a dark surface, saturated on a light one.
    assert (dark.secondary, light.secondary) == (ft.Colors.PINK_200, ft.Colors.PINK_800)
    assert (dark.tertiary, light.tertiary) == (
        ft.Colors.ORANGE_300,
        ft.Colors.ORANGE_900,
    )


@pytest.mark.parametrize(
    ("name", "mode"),
    [(THEME_DARK, ft.ThemeMode.DARK), (THEME_LIGHT, ft.ThemeMode.LIGHT)],
)
def test_apply_theme_puts_both_themes_and_the_mode_on_the_page(name, mode):
    page = SimpleNamespace()

    theme.apply_theme(page, name)

    assert page.theme_mode == mode
    assert page.theme == theme.light_theme()
    assert page.dark_theme == theme.dark_theme()


def test_an_unknown_name_gives_the_dark_theme():
    page = SimpleNamespace()

    theme.apply_theme(page, "blue")

    assert page.theme_mode == ft.ThemeMode.DARK


def test_every_theme_of_the_settings_is_one_apply_theme_knows():
    page = SimpleNamespace()
    modes = set()
    for name in THEMES:
        theme.apply_theme(page, name)
        modes.add(page.theme_mode)

    assert modes == {ft.ThemeMode.DARK, ft.ThemeMode.LIGHT}


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        (SimpleNamespace(theme_mode=ft.ThemeMode.LIGHT), "ATOM_ONE_LIGHT"),
        (SimpleNamespace(theme_mode=ft.ThemeMode.DARK), "ATOM_ONE_DARK"),
        (SimpleNamespace(theme_mode=ft.ThemeMode.SYSTEM), "ATOM_ONE_DARK"),
        (None, "ATOM_ONE_DARK"),
        (MagicMock(spec=ft.Page), "ATOM_ONE_DARK"),
    ],
    ids=["light", "dark", "system", "no-page", "mock-page"],
)
def test_a_code_block_gets_the_theme_that_fits(page, expected):
    assert theme.code_theme(page).name == expected
