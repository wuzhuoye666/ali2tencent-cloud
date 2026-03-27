"""测试 StateDB 基本读写。"""
import os
import tempfile
import pytest
from core.state import StateDB


@pytest.fixture
def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield StateDB(path)
    os.unlink(path)


def test_add_version_new(db):
    assert db.add_version("3.2", "alinux-3.2", "http://example.com/3.2.qcow2") is True


def test_add_version_duplicate(db):
    db.add_version("3.2", "alinux-3.2", "http://example.com/3.2.qcow2")
    assert db.add_version("3.2", "alinux-3.2", "http://example.com/3.2.qcow2") is False


def test_known_versions(db):
    db.add_version("3.2", "v32", "http://a.com/1.qcow2")
    db.add_version("2.1903", "v2", "http://a.com/2.qcow2")
    known = db.get_known_versions()
    assert "3.2" in known
    assert "2.1903" in known


def test_unprocessed_versions(db):
    db.add_version("3.2", "v32", "http://a.com/1.qcow2")
    unprocessed = db.get_unprocessed_versions()
    assert len(unprocessed) == 1
    assert unprocessed[0]["version"] == "3.2"


def test_mark_processed(db):
    db.add_version("3.2", "v32", "http://a.com/1.qcow2")
    db.mark_version_processed("3.2")
    assert db.get_unprocessed_versions() == []


def test_upsert_task(db):
    db.upsert_task("task-1", "3.2", "download", "running")
    task = db.get_task("task-1")
    assert task is not None
    assert task["status"] == "running"

    db.upsert_task("task-1", "3.2", "download", "done", meta={"file": "/tmp/x.qcow2"})
    task = db.get_task("task-1")
    assert task["status"] == "done"


def test_save_and_get_benchmark(db):
    result = {
        "cpu_score": 1234.5,
        "mem_score": 5678.0,
        "disk_read_mb": 300.0,
        "disk_write_mb": 150.0,
        "net_bandwidth_mb": 0.0,
    }
    db.save_benchmark("task-1", "3.2", "ins-abc123", result)
    row = db.get_benchmark_by_version("3.2")
    assert row is not None
    assert abs(row["cpu_score"] - 1234.5) < 0.01
