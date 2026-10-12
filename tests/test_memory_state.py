from api.client import Memory
from state.memory import MemoryState


def test_memory_state_set_memories():
    state = MemoryState()
    memories = [
        Memory(id="1", content="test 1", status="active"),
        Memory(id="2", content="test 2", status="candidate")
    ]

    callbacks = []
    def callback():
        callbacks.append(1)

    state.add_listener(callback)
    state.set_memories(memories)

    assert len(state.memories) == 2
    assert state.memories[0].id == "1"
    assert len(callbacks) == 1

def test_memory_state_update_memory():
    state = MemoryState()
    memories = [
        Memory(id="1", content="test 1", status="active"),
    ]
    state.set_memories(memories)

    callbacks = []
    def callback():
        callbacks.append(1)

    state.add_listener(callback)
    updated_memory = Memory(id="1", content="updated test 1", status="active")
    state.update_memory(updated_memory)

    assert state.memories[0].content == "updated test 1"
    assert len(callbacks) == 1

def test_memory_state_remove_memory():
    state = MemoryState()
    memories = [
        Memory(id="1", content="test 1", status="active"),
        Memory(id="2", content="test 2", status="candidate")
    ]
    state.set_memories(memories)

    callbacks = []
    def callback():
        callbacks.append(1)

    state.add_listener(callback)
    state.remove_memory("1")

    assert len(state.memories) == 1
    assert state.memories[0].id == "2"
    assert len(callbacks) == 1

def test_memory_state_listeners():
    state = MemoryState()

    callbacks1 = []
    callbacks2 = []

    def cb1():
        callbacks1.append(1)

    def cb2():
        callbacks2.append(1)

    state.add_listener(cb1)
    state.add_listener(cb2)

    state.notify_listeners()

    assert len(callbacks1) == 1
    assert len(callbacks2) == 1

    state.remove_listener(cb1)
    state.notify_listeners()

    assert len(callbacks1) == 1
    assert len(callbacks2) == 2
