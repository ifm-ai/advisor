import json
import os
import stat

import pytest

from ifm_advisor.artifacts import ArtifactWriter


def test_trace_records_are_valid_jsonl(tmp_path):
    writer = ArtifactWriter(tmp_path / "advisor")

    assert writer.append_trace({"solver_call_index": 1, "intervened": True}) is True
    assert writer.append_trace({"solver_call_index": 2, "intervened": False}) is True

    lines = writer.private_trace_path.read_text().splitlines()
    assert [json.loads(line)["solver_call_index"] for line in lines] == [1, 2]


def test_directories_are_created_lazily(tmp_path):
    writer = ArtifactWriter(tmp_path / "nested" / "advisor")

    assert not writer.directory.exists()
    writer.append_trace({"a": 1})
    assert writer.private_trace_path.is_file()


def test_summary_is_atomically_replaced(tmp_path):
    writer = ArtifactWriter(tmp_path / "advisor")

    writer.write_summary({"enabled": True, "interventions": 0})
    writer.write_summary({"enabled": True, "interventions": 2})

    assert json.loads(writer.summary_path.read_text())["interventions"] == 2
    assert [path.name for path in writer.directory.iterdir() if path.name.endswith(".tmp")] == []


def test_output_failure_does_not_raise(tmp_path):
    blocker = tmp_path / "advisor"
    # A file where the advisor directory should be makes every write fail.
    blocker.write_text("not a directory")
    writer = ArtifactWriter(blocker)

    assert writer.append_trace({"a": 1}) is False
    assert writer.write_summary({"enabled": True}) is False
    assert writer.write_task_material_manifest({"file_count": 0}) is False


def test_unserializable_trace_values_do_not_raise(tmp_path):
    writer = ArtifactWriter(tmp_path / "advisor")

    assert writer.append_trace({"path": tmp_path, "object": object()}) is True
    assert writer.append_trace({"bad": float("nan")}) is False


def test_manifest_file_name(tmp_path):
    writer = ArtifactWriter(tmp_path / "advisor")

    assert writer.write_task_material_manifest({"file_count": 0}) is True
    assert writer.task_material_manifest_path.name == "task_material_manifest.json"


def _private(path) -> bool:
    return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def test_every_artifact_is_readable_by_its_owner_only(tmp_path):
    writer = ArtifactWriter(tmp_path / "advisor")
    previous = os.umask(0o022)  # the usual umask: files would be created 0644
    try:
        writer.append_trace({"hint": "derived from privileged material"})
        writer.write_summary({"enabled": True})
        writer.write_task_material_manifest({"file_count": 0})
    finally:
        os.umask(previous)

    assert _private(writer.private_trace_path)
    assert _private(writer.summary_path)
    assert _private(writer.task_material_manifest_path)


def test_appending_tightens_an_existing_trace_to_owner_only(tmp_path):
    writer = ArtifactWriter(tmp_path / "advisor")
    writer.directory.mkdir()
    writer.private_trace_path.write_text("")
    writer.private_trace_path.chmod(0o644)  # left by an earlier run or another tool

    assert writer.append_trace({"hint": "derived from privileged material"}) is True

    assert _private(writer.private_trace_path)


def test_a_symlinked_trace_is_never_written_through(tmp_path):
    writer = ArtifactWriter(tmp_path / "advisor")
    writer.directory.mkdir()
    target = tmp_path / "elsewhere.txt"
    target.write_text("untouched\n")
    writer.private_trace_path.symlink_to(target)

    assert writer.append_trace({"hint": "derived from privileged material"}) is False

    assert target.read_text() == "untouched\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_trace_is_refused_without_blocking(tmp_path):
    writer = ArtifactWriter(tmp_path / "advisor")
    writer.directory.mkdir()
    os.mkfifo(writer.private_trace_path)

    assert writer.append_trace({"hint": "derived from privileged material"}) is False
