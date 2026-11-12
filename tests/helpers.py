"""What several test modules share to pretend a control is on a page."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import flet as ft
import msgpack
from flet.controls.base_control import BaseControl
from flet.messaging.protocol import MessageAction, configure_encode_object_for_msgpack
from flet.messaging.session import Session
from flet.pubsub.pubsub_hub import PubSubHub


class MountedPage:
    """Stands in for the page of one control that is on screen.

    The views ask two things of a page before they update a control: that the
    control can reach it (``control.page`` does not raise) and that the page
    still knows the control by its id (``get_control``).
    """

    def __init__(self, control: ft.BaseControl):
        self._control = control

    def get_control(self, control_id: int):
        return self._control if control_id == self._control._i else None

    def update(self, *controls) -> None:
        """What ``Control.update()`` ends in; there is no client to send to."""


def mounted(control_class: type):
    """Pretend every control of ``control_class`` is on a page.

    ``Control.page`` is read-only and raises for a control that never reached a
    page, so the property is replaced on the class for the length of the block.
    """
    return patch.object(control_class, "page", property(lambda self: MountedPage(self)))


def tab_view(tabs: ft.Tabs, index: int) -> ft.Control:
    """The content of tab ``index`` as ``main`` builds it (a bar over the views)."""
    _bar, contents = tabs.content.controls
    return contents.controls[index]


def tab_labels(tabs: ft.Tabs) -> list[str]:
    bar, _contents = tabs.content.controls
    return [tab.label for tab in bar.tabs]


class RecordingConnection:
    """A Flet connection without a client: every message is encoded and kept.

    The encoding is not for show. Flet works out the next patch of a control
    against what it serialised last, so a connection that only stored the
    objects would make every later update look empty.
    """

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.pubsubhub = PubSubHub()
        self.sent: list = []
        self._encode = configure_encode_object_for_msgpack(BaseControl)

    def send_message(self, message) -> None:
        msgpack.packb([message.action, message.body], default=self._encode)
        self.sent.append(message)

    def register(self, session: Session) -> None:
        """What the server does when the client connects: send the whole page."""
        msgpack.packb(session.get_page_patch(), default=self._encode)

    def actions(self) -> list[MessageAction]:
        return [message.action for message in self.sent]


def real_session() -> tuple[Session, RecordingConnection]:
    """A real Flet session with its page, as after a client has connected.

    For the few things a mock page cannot answer honestly: what Flet itself does
    with a control that was added or removed, or with a handler that raises.
    Keep the session: the page only holds a weak reference to it. Needs a
    running event loop.
    """
    connection = RecordingConnection()
    session = Session(connection)
    connection.register(session)
    return session, connection
