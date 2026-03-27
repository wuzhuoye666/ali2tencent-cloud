"""测试 monitor.py 版本解析逻辑（mock HTTP 响应）。"""
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest
from core.state import StateDB
from pipeline.monitor import detect_new_versions, _parse_versions, _extract_version


# ---- 单元测试：版本解析 ----

def test_extract_version():
    assert _extract_version("alinux-3.2-x86_64.qcow2") == "3.2"
    assert _extract_version("AliyunOS-2.1903-x86_64.qcow2") == "2.1903"
    assert _extract_version("no-version-here.txt") == ""


def test_parse_versions_directory_listing():
    html = """
    <html><body>
    <a href="alinux-3.2-x86_64.qcow2">3.2</a>
    <a href="alinux-3.1-x86_64.qcow2">3.1</a>
    <a href="alinux-3.2-aarch64.qcow2">3.2 arm</a>
    </body></html>
    """
    results = _parse_versions(html, base_url="http://mirrors.example.com/alinux/")
    versions = {r.version for r in results}
    assert "3.2" in versions
    assert "3.1" in versions


def test_parse_versions_no_images():
    html = "<html><body><p>No images here</p></body></html>"
    results = _parse_versions(html, base_url="http://example.com/")
    assert results == []


def test_parse_versions_dedup_qcow2_preferred():
    html = """
    <html><body>
    <a href="alinux-3.2-x86_64.raw">raw</a>
    <a href="alinux-3.2-x86_64.qcow2">qcow2</a>
    </body></html>
    """
    results = _parse_versions(html, base_url="http://example.com/")
    assert len(results) == 1
    assert results[0].download_url.endswith(".qcow2")


# ---- 集成测试：detect_new_versions（mock HTTP） ----

@pytest.fixture
def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield StateDB(path)
    os.unlink(path)


def test_detect_new_versions(db):
    from core.config import Config

    config = Config(
        tencent_secret_id="x", tencent_secret_key="x",
        tencent_region="ap-guangzhou", tencent_cos_bucket="bucket",
        ali_image_doc_url="http://mock.example.com/alinux/",
        check_interval_hours=6,
        cvm_instance_type="S5.MEDIUM2",
        cvm_vpc_id="", cvm_subnet_id="", cvm_security_group_id="",
        cvm_login_password="test", cvm_disk_size=50,
        ssh_user="root", ssh_private_key_path="",
        benchmark_timeout=30, keep_instance=False,
        tmp_dir="/tmp", log_dir="/tmp", report_dir="/tmp",
        state_db=db._path,
    )

    mock_html = """
    <html><body>
    <a href="alinux-3.2-x86_64.qcow2">3.2</a>
    </body></html>
    """

    with patch("pipeline.monitor._fetch", return_value=mock_html):
        versions = detect_new_versions(config, db)

    assert len(versions) == 1
    assert versions[0].version == "3.2"

    # 第二次检测，不应再出现相同版本
    with patch("pipeline.monitor._fetch", return_value=mock_html):
        versions2 = detect_new_versions(config, db)

    assert len(versions2) == 0
