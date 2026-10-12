import hashlib
import os
import threading
from pathlib import Path

import pytest
from helpers import finishes

from ifm_advisor import material as material_module
from ifm_advisor.material import (
    INSTRUCTION_PLACEHOLDER,
    TaskLayout,
    TaskMaterial,
    _open_regular,
)


def _paths(material: TaskMaterial) -> list[str]:
    return [entry.relative_path for entry in material.files]


def test_includes_readme_solution_and_tests_in_order(task_dir):
    material = TaskMaterial.from_task_dir(task_dir)

    assert _paths(material) == [
        "README.md",
        "solution/files/gc.py",
        "solution/solve.sh",
        "tests/baseline/types.py",
        "tests/test_outputs.py",
    ]
    assert material.name == "session-window-debug"
    assert material.instruction == "Fix the session window processor.\n"


def test_excludes_cheat_public_and_environment_files(task_dir):
    material = TaskMaterial.from_task_dir(task_dir)

    paths = _paths(material)
    assert not [path for path in paths if path.startswith("cheat")]
    assert "instruction.md" not in paths
    assert "task.toml" not in paths
    assert "environment/Dockerfile" not in paths
    assert "CHEAT_MARKER" not in material.render()


def test_excludes_bytecode_and_cache_directories(task_dir):
    cache = task_dir / "tests" / "__pycache__"
    cache.mkdir()
    (cache / "test_outputs.cpython-312.pyc").write_bytes(b"\x00\x01")
    (task_dir / "solution" / "stale.pyc").write_bytes(b"\x00\x01")

    paths = _paths(TaskMaterial.from_task_dir(task_dir))

    assert not [path for path in paths if "__pycache__" in path or path.endswith(".pyc")]


def test_ordering_is_deterministic(task_dir):
    assert _paths(TaskMaterial.from_task_dir(task_dir)) == _paths(
        TaskMaterial.from_task_dir(task_dir)
    )


def test_per_file_limit_truncates_and_records_truncation(task_dir):
    (task_dir / "tests" / "test_outputs.py").write_text("A" * 5_000)

    material = TaskMaterial.from_task_dir(task_dir, max_file_bytes=100, max_total_bytes=4_000_000)

    entry = next(item for item in material.files if item.relative_path == "tests/test_outputs.py")
    assert entry.truncated is True
    assert entry.text == "A" * 100
    assert entry.size_bytes == 5_000
    assert entry.sha256 == hashlib.sha256(b"A" * 5_000).hexdigest()
    assert "tests/test_outputs.py" in material.truncated_paths
    assert "TRUNCATED" in material.render()
    assert material.manifest()["truncated_paths"] == list(material.truncated_paths)


def test_truncation_inside_a_multibyte_character_keeps_the_text(task_dir):
    """A byte cut through 'é' is an artifact of the limit, not a binary file."""

    (task_dir / "README.md").write_text("é" * 100, encoding="utf-8")  # 200 bytes

    material = TaskMaterial.from_task_dir(task_dir, max_file_bytes=101, max_total_bytes=4_000_000)

    entry = material.files[0]
    assert entry.relative_path == "README.md"
    assert entry.text == "é" * 50
    assert entry.truncated is True
    assert entry.skipped_reason is None


def test_total_limit_omits_later_files_and_records_them(task_dir):
    (task_dir / "README.md").write_text("R" * 400)

    material = TaskMaterial.from_task_dir(task_dir, max_file_bytes=400, max_total_bytes=400)

    assert material.total_content_bytes == 400
    assert material.omitted_paths
    assert "size budget was exhausted" in material.render()
    assert [entry.relative_path for entry in material.files if entry.text is not None] == [
        "README.md"
    ]


def test_binary_and_invalid_utf8_files_record_identity_only(task_dir):
    (task_dir / "tests" / "fixture.bin").write_bytes(b"\xff\xfe\x00binary")
    (task_dir / "solution" / "broken.txt").write_bytes(b"valid \xff\xfe invalid")

    material = TaskMaterial.from_task_dir(task_dir)

    for name in ("tests/fixture.bin", "solution/broken.txt"):
        entry = next(item for item in material.files if item.relative_path == name)
        assert entry.text is None
        assert entry.skipped_reason == "not valid utf-8"
        assert len(entry.sha256) == 64
        assert entry.manifest_entry()["text"] is False
    assert "binary or undecodable content omitted" in material.render()


def test_manifest_holds_hashes_and_the_task_name_but_no_contents_or_paths(task_dir):
    material = TaskMaterial.from_task_dir(task_dir)

    manifest = material.manifest()
    serialized = repr(manifest)

    assert manifest["task"] == "session-window-debug"
    assert manifest["file_count"] == len(material.files)
    assert all(len(entry["sha256"]) == 64 for entry in manifest["files"])
    assert manifest["excluded_directory_names"] == sorted(manifest["excluded_directory_names"])
    for secret in (
        "REFERENCE_GC_IMPLEMENTATION",
        "test_unfired_survives_gc",
        "Five targeted fixes",
    ):
        assert secret not in serialized
    assert str(task_dir) not in serialized


def test_rendered_material_delimits_each_file(task_dir):
    rendered = TaskMaterial.from_task_dir(task_dir).render()

    assert rendered.count("----- BEGIN PRIVILEGED FILE") == 5
    assert rendered.count("----- END PRIVILEGED FILE") == 5
    assert "REFERENCE_GC_IMPLEMENTATION" in rendered


def test_missing_privileged_directories_are_tolerated(tmp_path):
    empty = tmp_path / "no-privileged-material"
    empty.mkdir()

    material = TaskMaterial.from_task_dir(empty)

    assert material.files == ()
    assert material.render() == "(no privileged material available)"
    assert material.instruction == INSTRUCTION_PLACEHOLDER


def test_missing_task_directory_raises(tmp_path):
    """A typo must not produce an advisor that reviews blind."""

    with pytest.raises(FileNotFoundError, match="task directory not found"):
        TaskMaterial.from_task_dir(tmp_path / "does-not-exist")


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        ({"max_file_bytes": 0}, "positive"),
        ({"max_total_bytes": -1}, "positive"),
        ({"max_file_bytes": 10, "max_total_bytes": 5}, "must not be below"),
    ],
)
def test_bad_limits_are_rejected(task_dir, limits, message):
    with pytest.raises(ValueError, match=message):
        TaskMaterial.from_task_dir(task_dir, **limits)


def test_custom_layout(tmp_path):
    root = tmp_path / "custom"
    (root / "reference").mkdir(parents=True)
    (root / "TASK.md").write_text("Do the thing.")
    (root / "reference" / "answer.py").write_text("ANSWER = 42\n")
    (root / "tests").mkdir()
    (root / "tests" / "hidden.py").write_text("NOT_A_ROOT_HERE\n")
    layout = TaskLayout(instruction_file="TASK.md", privileged_roots=("reference",))

    material = TaskMaterial.from_task_dir(root, layout)

    assert material.instruction == "Do the thing."
    assert _paths(material) == ["reference/answer.py"]
    assert material.manifest()["privileged_roots"] == ["reference"]


def test_from_texts_builds_material_without_a_directory():
    material = TaskMaterial.from_texts(
        "Fix median().",
        {"solution.patch": "PATCH", "tests/test_median.py": "def test_even(): ..."},
        name="toy",
    )

    assert material.instruction == "Fix median()."
    assert _paths(material) == ["solution.patch", "tests/test_median.py"]
    assert material.files[0].sha256 == hashlib.sha256(b"PATCH").hexdigest()
    assert material.manifest()["task"] == "toy"
    assert material.manifest()["privileged_roots"] is None
    assert "PATCH" in material.privileged_text()


def test_from_texts_applies_the_same_limits():
    material = TaskMaterial.from_texts(
        "x", {"a": "A" * 10, "b": "B" * 10}, max_file_bytes=6, max_total_bytes=6
    )

    assert material.files[0].text == "A" * 6
    assert material.truncated_paths == ("a",)
    assert material.omitted_paths == ("b",)


def test_a_symlinked_file_pointing_outside_the_task_is_not_read(tmp_path):
    """A symlink to an external secret is not read."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("HOST-SECRET")

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "README.md").write_text("## Solution\n")
    (task_dir / "solution").mkdir()
    (task_dir / "tests").mkdir()
    # Symlink to external secret
    (task_dir / "tests" / "leak").symlink_to(outside / "secret.txt")

    material = TaskMaterial.from_task_dir(task_dir)

    entry = next(item for item in material.files if item.relative_path == "tests/leak")
    assert entry.text is None
    assert entry.skipped_reason == "symlink"
    assert "HOST-SECRET" not in material.render()
    assert "HOST-SECRET" not in material.privileged_text()


def test_a_symlink_into_an_excluded_directory_is_not_read(task_dir):
    """A symlink into an excluded directory is skipped, not followed."""
    (task_dir / "tests" / "x").symlink_to("../cheat/solve.sh")

    material = TaskMaterial.from_task_dir(task_dir)

    # The symlink itself should be recorded as skipped
    skipped = [item for item in material.files if item.relative_path == "tests/x"]
    assert len(skipped) == 1
    assert skipped[0].skipped_reason == "symlink"
    assert "CHEAT_MARKER" not in material.render()


def test_a_symlinked_privileged_root_is_not_followed(tmp_path):
    """A symlinked privileged root is not descended."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("HOST-SECRET")

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "README.md").write_text("## Solution\n")
    (task_dir / "tests").mkdir()
    # Replace solution with a symlink to outside directory
    (task_dir / "solution").symlink_to(outside)

    material = TaskMaterial.from_task_dir(task_dir)

    # solution should be recorded as skipped
    skipped = [item for item in material.files if item.relative_path == "solution"]
    assert len(skipped) == 1
    assert skipped[0].skipped_reason == "symlink"
    assert "HOST-SECRET" not in material.render()


def test_a_symlinked_directory_inside_a_root_is_not_descended(tmp_path):
    """A symlinked subdirectory is not descended."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("HOST-SECRET")

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "README.md").write_text("## Solution\n")
    (task_dir / "solution").mkdir()
    (task_dir / "tests").mkdir()
    # Create symlinked subdirectory inside tests
    (task_dir / "tests" / "sub").symlink_to(outside)

    material = TaskMaterial.from_task_dir(task_dir)

    # tests/sub should be recorded as skipped
    skipped = [item for item in material.files if item.relative_path == "tests/sub"]
    assert len(skipped) == 1
    assert skipped[0].skipped_reason == "symlink"
    assert "HOST-SECRET" not in material.render()


def test_a_symlinked_instruction_is_not_read(tmp_path):
    """A symlinked instruction file is not read."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "real.md").write_text("OUTSIDER INSTRUCTION")

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "README.md").write_text("## Solution\n")
    (task_dir / "solution").mkdir()
    (task_dir / "tests").mkdir()
    # Replace instruction with a symlink
    (task_dir / "instruction.md").symlink_to(outside / "real.md")

    material = TaskMaterial.from_task_dir(task_dir)

    assert material.instruction == INSTRUCTION_PLACEHOLDER


@pytest.mark.parametrize(
    ("instruction_file", "message"),
    [
        ("/etc/passwd", "stay inside the task directory"),
        ("../x", "stay inside the task directory"),
    ],
)
def test_layout_instruction_file_must_stay_inside_the_task(instruction_file, message):
    with pytest.raises(ValueError, match=message):
        TaskLayout(instruction_file=instruction_file)


@pytest.mark.parametrize(
    ("privileged_roots", "message"),
    [
        (("a/../../b",), "stay inside the task directory"),
        (("",), "stay inside the task directory"),
    ],
)
def test_layout_privileged_roots_must_stay_inside_the_task(privileged_roots, message):
    with pytest.raises(ValueError, match=message):
        TaskLayout(privileged_roots=privileged_roots)


def test_skipped_symlink_appears_in_manifest(tmp_path):
    """A skipped symlink appears in the manifest with skipped_reason set."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("HOST-SECRET")

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "README.md").write_text("## Solution\n")
    (task_dir / "solution").mkdir()
    (task_dir / "tests").mkdir()
    (task_dir / "tests" / "leak").symlink_to(outside / "secret.txt")

    material = TaskMaterial.from_task_dir(task_dir)
    manifest = material.manifest()

    skipped_entries = [entry for entry in manifest["files"] if entry["skipped_reason"] == "symlink"]
    assert len(skipped_entries) == 1
    assert skipped_entries[0]["relative_path"] == "tests/leak"
    assert skipped_entries[0]["text"] is False


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo not available on this platform")
def test_a_named_pipe_is_skipped_without_blocking(task_dir):
    """A named pipe (FIFO) is skipped without blocking the loader."""
    # Create a named pipe in the tests directory
    pipe_path = task_dir / "tests" / "pipe"
    os.mkfifo(str(pipe_path))

    # This should not hang; the pipe should be skipped
    material = TaskMaterial.from_task_dir(task_dir)

    # Verify the pipe is recorded as skipped
    skipped_entries = [entry for entry in material.files if entry.relative_path == "tests/pipe"]
    assert len(skipped_entries) == 1
    assert skipped_entries[0].text is None
    assert skipped_entries[0].skipped_reason == "not a regular file"

    # Verify other files still loaded normally
    readme_entries = [entry for entry in material.files if entry.relative_path == "README.md"]
    assert len(readme_entries) == 1
    assert readme_entries[0].text is not None


def test_a_nested_root_through_a_symlinked_directory_is_not_read(tmp_path):
    """A layout entry like "hidden/sub" must not reach outside the task through "hidden"."""
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "secret.txt").write_text("HOST-SECRET\n")

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("Do it.\n")
    (task_dir / "hidden").symlink_to(outside)

    material = TaskMaterial.from_task_dir(task_dir, TaskLayout(privileged_roots=("hidden/sub",)))

    assert [(entry.relative_path, entry.skipped_reason) for entry in material.files] == [
        ("hidden/sub", "symlink")
    ]
    assert "HOST-SECRET" not in material.privileged_text()
    assert "HOST-SECRET" not in material.render()


def test_a_nested_file_root_through_a_symlinked_directory_is_not_read(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.md").write_text("HOST-SECRET\n")

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "private").symlink_to(outside)

    material = TaskMaterial.from_task_dir(
        task_dir, TaskLayout(privileged_roots=("private/notes.md",))
    )

    assert [(entry.relative_path, entry.skipped_reason) for entry in material.files] == [
        ("private/notes.md", "symlink")
    ]
    assert "HOST-SECRET" not in material.render()


def test_a_nested_instruction_through_a_symlinked_directory_is_not_read(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "TASK.md").write_text("OUTSIDER INSTRUCTION")

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "docs").symlink_to(outside)

    material = TaskMaterial.from_task_dir(task_dir, TaskLayout(instruction_file="docs/TASK.md"))

    assert material.instruction == INSTRUCTION_PLACEHOLDER


def test_a_nested_layout_without_symlinks_is_read(tmp_path):
    task_dir = tmp_path / "task"
    (task_dir / "docs").mkdir(parents=True)
    (task_dir / "hidden" / "sub").mkdir(parents=True)
    (task_dir / "docs" / "TASK.md").write_text("Do the thing.")
    (task_dir / "hidden" / "sub" / "answer.py").write_text("ANSWER = 42\n")
    layout = TaskLayout(instruction_file="docs/TASK.md", privileged_roots=("hidden/sub",))

    material = TaskMaterial.from_task_dir(task_dir, layout)

    assert material.instruction == "Do the thing."
    assert _paths(material) == ["hidden/sub/answer.py"]
    assert "ANSWER = 42" in material.privileged_text()


needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")


@needs_fifo
def test_a_fifo_instruction_gives_the_placeholder(task_dir):
    (task_dir / "instruction.md").unlink()
    os.mkfifo(task_dir / "instruction.md")

    material = finishes(lambda: TaskMaterial.from_task_dir(task_dir))

    assert material.instruction == INSTRUCTION_PLACEHOLDER


@needs_fifo
def test_a_fifo_privileged_file_is_skipped(task_dir):
    os.mkfifo(task_dir / "tests" / "pipe")

    material = finishes(lambda: TaskMaterial.from_task_dir(task_dir))

    entry = next(entry for entry in material.files if entry.relative_path == "tests/pipe")
    assert entry.text is None and entry.skipped_reason == "not a regular file"


@needs_fifo
def test_the_open_helper_refuses_a_fifo_without_blocking(tmp_path):
    os.mkfifo(tmp_path / "pipe")

    assert finishes(lambda: _open_regular(tmp_path / "pipe")) is None


def test_the_open_helper_refuses_a_symlink(tmp_path):
    (tmp_path / "real.txt").write_text("text")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")

    assert _open_regular(tmp_path / "link.txt") is None
    with _open_regular(tmp_path / "real.txt") as stream:
        assert stream.read() == b"text"


def test_whitespace_only_material_is_no_material():
    assert not TaskMaterial.from_texts(
        "Fix it.", {"tests/test.py": " \n\t\n"}
    ).has_privileged_text()
    assert TaskMaterial.from_texts("Fix it.", {"tests/test.py": "assert x\n"}).has_privileged_text()


def test_a_binary_byte_near_a_truncation_cut_is_not_text(tmp_path):
    root = tmp_path / "task"
    (root / "tests").mkdir(parents=True)
    # 0xFF is never valid UTF-8: the file is binary even though the cut lands right after it.
    (root / "tests" / "blob").write_bytes(b"a" * 20 + b"\xff" + b"bbbb")

    material = TaskMaterial.from_task_dir(root, max_file_bytes=22, max_total_bytes=1000)

    entry = material.files[0]
    assert entry.text is None and entry.skipped_reason == "not valid utf-8"


@pytest.mark.parametrize("entry", [".", "./"])
def test_layout_entries_may_not_name_the_task_directory_itself(entry):
    with pytest.raises(ValueError):
        TaskLayout(privileged_roots=(entry,))
    with pytest.raises(ValueError):
        TaskLayout(instruction_file=entry)


def test_a_task_loaded_as_dot_is_named_after_its_directory(task_dir, monkeypatch):
    monkeypatch.chdir(task_dir)

    assert TaskMaterial.from_task_dir(".").name == "session-window-debug"
    assert TaskMaterial.from_task_dir(Path(".")).name == "session-window-debug"


@needs_fifo
def test_a_fifo_instruction_is_never_opened_for_reading(task_dir):
    fifo = task_dir / "instruction.md"
    fifo.unlink()
    os.mkfifo(fifo)
    opened = threading.Event()

    def writer():
        descriptor = os.open(fifo, os.O_WRONLY)  # blocks until some reader opens the FIFO
        opened.set()
        os.close(descriptor)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    assert not opened.wait(0.3)

    finishes(lambda: TaskMaterial.from_task_dir(task_dir))

    still_blocked = not opened.wait(0.3)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)  # release the writer
    thread.join(10)
    os.close(reader)
    assert still_blocked, "the loader opened the FIFO instruction for reading"
    assert not thread.is_alive()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_an_instruction_behind_an_unsearchable_directory_gives_the_placeholder(task_dir):
    (task_dir / "docs").mkdir()
    (task_dir / "docs" / "TASK.md").write_text("Fix it.\n")
    (task_dir / "docs").chmod(0o600)
    try:
        material = TaskMaterial.from_task_dir(task_dir, TaskLayout(instruction_file="docs/TASK.md"))
    finally:
        (task_dir / "docs").chmod(0o700)

    assert material.instruction == INSTRUCTION_PLACEHOLDER


def test_a_file_swapped_for_a_special_file_after_the_check_is_skipped(task_dir, monkeypatch):
    real = material_module._open_regular

    def swapped(path):
        return None if path.name == "test_outputs.py" else real(path)

    monkeypatch.setattr(material_module, "_open_regular", swapped)

    material = TaskMaterial.from_task_dir(task_dir)

    entry = next(item for item in material.files if item.relative_path == "tests/test_outputs.py")
    assert entry.text is None and entry.skipped_reason == "not a regular file"


# --- unreadable privileged material is recorded, never dropped ---------------------------------

needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")


def _skipped(material: TaskMaterial) -> dict[str, str | None]:
    return {entry.relative_path: entry.skipped_reason for entry in material.files}


@needs_non_root
def test_an_unreadable_directory_inside_a_root_is_recorded_as_unreadable(task_dir):
    hidden = task_dir / "tests" / "hidden"
    hidden.mkdir()
    (hidden / "test_hidden.py").write_text("def test_hidden(): ...\n")
    hidden.chmod(0)
    try:
        material = TaskMaterial.from_task_dir(task_dir)
    finally:
        hidden.chmod(0o755)

    assert _skipped(material)["tests/hidden"] == "unreadable: PermissionError"
    assert {
        "relative_path": "tests/hidden",
        "size_bytes": 0,
        "sha256": "",
        "text": False,
        "truncated": False,
        "skipped_reason": "unreadable: PermissionError",
    } in material.manifest()["files"]
    # The advisor keeps loading everything else.
    assert "tests/test_outputs.py" in _paths(material)
    assert "REFERENCE_GC_IMPLEMENTATION" in material.privileged_text()


@needs_non_root
def test_a_root_that_cannot_be_listed_is_recorded_as_unreadable(task_dir):
    (task_dir / "tests").chmod(0)
    try:
        material = TaskMaterial.from_task_dir(task_dir)
    finally:
        (task_dir / "tests").chmod(0o755)

    assert _skipped(material)["tests"] == "unreadable: PermissionError"
    assert not [path for path in _paths(material) if path.startswith("tests/")]


@needs_non_root
def test_files_in_a_directory_that_cannot_be_searched_are_recorded_as_unreadable(task_dir):
    # Read but no search permission: the names can be listed, the files cannot be reached.
    locked = task_dir / "tests" / "locked"
    (locked / "deeper").mkdir(parents=True)
    (locked / "test_locked.py").write_text("def test_locked(): ...\n")
    locked.chmod(0o600)
    try:
        material = TaskMaterial.from_task_dir(task_dir)
    finally:
        locked.chmod(0o755)

    skipped = _skipped(material)
    assert skipped["tests/locked/test_locked.py"] == "unreadable: PermissionError"
    assert skipped["tests/locked/deeper"] == "unreadable: PermissionError"


@needs_non_root
def test_a_nested_root_behind_an_unsearchable_directory_is_recorded_as_unreadable(tmp_path):
    task_dir = tmp_path / "task"
    (task_dir / "hidden" / "sub").mkdir(parents=True)
    (task_dir / "hidden" / "sub" / "answer.py").write_text("ANSWER = 42\n")
    (task_dir / "hidden").chmod(0o600)
    try:
        material = TaskMaterial.from_task_dir(
            task_dir, TaskLayout(privileged_roots=("hidden/sub",))
        )
    finally:
        (task_dir / "hidden").chmod(0o755)

    assert _skipped(material) == {"hidden/sub": "unreadable: PermissionError"}


@needs_fifo
def test_a_fifo_root_is_recorded_as_not_a_regular_file(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    os.mkfifo(task_dir / "README.md")

    material = finishes(lambda: TaskMaterial.from_task_dir(task_dir))

    assert _skipped(material) == {"README.md": "not a regular file"}
