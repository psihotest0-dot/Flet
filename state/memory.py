from typing import Callable

from pydantic import BaseModel, Field, PrivateAttr

from api.client import Memory


class MemoryState(BaseModel):
    memories: list[Memory] = Field(default_factory=list)
    _on_change_callbacks: list[Callable[[], None]] = PrivateAttr(default_factory=list)

    def set_memories(self, new_memories: list[Memory]) -> None:
        self.memories = new_memories
        self.notify_listeners()

    def update_memory(self, updated_memory: Memory) -> None:
        for i, memory in enumerate(self.memories):
            if memory.id == updated_memory.id:
                self.memories[i] = updated_memory
                self.notify_listeners()
                return

    def remove_memory(self, memory_id: str) -> None:
        self.memories = [m for m in self.memories if m.id != memory_id]
        self.notify_listeners()

    def add_listener(self, callback: Callable[[], None]) -> None:
        if callback not in self._on_change_callbacks:
            self._on_change_callbacks.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        if callback in self._on_change_callbacks:
            self._on_change_callbacks.remove(callback)

    def notify_listeners(self) -> None:
        for callback in self._on_change_callbacks:
            callback()
