from typing import Callable

from pydantic import BaseModel, Field, PrivateAttr

from api.client import IngestedFile


class FilesState(BaseModel):
    files: list[IngestedFile] = Field(default_factory=list)
    _on_change_callbacks: list[Callable[[], None]] = PrivateAttr(default_factory=list)

    def set_files(self, new_files: list[IngestedFile]) -> None:
        self.files = new_files
        self.notify_listeners()

    def update_file(self, updated_file: IngestedFile) -> None:
        for i, file in enumerate(self.files):
            if file.id == updated_file.id:
                self.files[i] = updated_file
                self.notify_listeners()
                return

    def add_listener(self, callback: Callable[[], None]) -> None:
        if callback not in self._on_change_callbacks:
            self._on_change_callbacks.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        if callback in self._on_change_callbacks:
            self._on_change_callbacks.remove(callback)

    def notify_listeners(self) -> None:
        for callback in self._on_change_callbacks:
            callback()
