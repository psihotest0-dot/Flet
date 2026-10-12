from api.client import IngestedFile
from state.files import FilesState


def test_files_state_set_files():
    state = FilesState()
    files = [
        IngestedFile(id="1", file_name="test1.txt", status="uploaded"),
        IngestedFile(id="2", file_name="test2.txt", status="processing"),
    ]

    called = False
    def listener():
        nonlocal called
        called = True

    state.add_listener(listener)
    state.set_files(files)

    assert state.files == files
    assert called is True


def test_files_state_update_file():
    state = FilesState()
    files = [
        IngestedFile(id="1", file_name="test1.txt", status="processing"),
    ]
    state.set_files(files)

    called = False
    def listener():
        nonlocal called
        called = True

    state.add_listener(listener)

    updated_file = IngestedFile(id="1", file_name="test1.txt", status="done")
    state.update_file(updated_file)

    assert state.files[0].status == "done"
    assert called is True


def test_files_state_remove_listener():
    state = FilesState()
    files = [
        IngestedFile(id="1", file_name="test1.txt", status="processing"),
    ]

    called = False
    def listener():
        nonlocal called
        called = True

    state.add_listener(listener)
    state.remove_listener(listener)
    state.set_files(files)

    assert called is False
