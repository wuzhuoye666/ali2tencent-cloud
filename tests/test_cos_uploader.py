"""测试 COS 上传接口（mock SDK）。"""
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest
from pipeline.cos_uploader import upload_file


def test_upload_file_mock():
    # 创建临时文件
    with tempfile.NamedTemporaryFile(suffix=".qcow2", delete=False) as f:
        f.write(b"fake image data")
        local_path = f.name

    try:
        mock_client = MagicMock()
        mock_client.upload_file.return_value = {}

        with patch("pipeline.cos_uploader.CosS3Client", return_value=mock_client):
            url = upload_file(
                src=local_path,
                bucket="test-bucket-123456",
                object_key="images/3.2/test.qcow2",
                region="ap-guangzhou",
                secret_id="fake_id",
                secret_key="fake_key",
            )

        assert "test-bucket-123456" in url
        assert "images/3.2/test.qcow2" in url
        mock_client.upload_file.assert_called_once()
    finally:
        os.unlink(local_path)
