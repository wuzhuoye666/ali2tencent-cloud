"""流水线上下文数据类，贯穿各阶段共享元数据。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ImageVersion:
    """从阿里云文档解析出的镜像版本信息。"""
    version: str
    name: str
    download_url: str
    checksum_url: str = ""
    os_type: str = "linux"
    arch: str = "x86_64"


@dataclass
class PipelineContext:
    """流水线单次运行的完整上下文，各阶段读写该对象传递状态。"""

    # 基础标识
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    version: str = ""

    # 阶段1：版本检测
    image_info: ImageVersion | None = None

    # 阶段2：下载
    download_url: str = ""
    local_file_path: str = ""           # 本地镜像文件路径
    file_sha256: str = ""

    # 阶段3：cloud-init 修改
    modified_file_path: str = ""        # 修改后的镜像路径
    cloud_init_injected: bool = False   # cloud-init 配置是否成功注入镜像

    # 阶段4：COS 上传
    cos_object_key: str = ""
    cos_object_url: str = ""            # https://<bucket>.cos.<region>.myqcloud.com/<key>

    # 阶段5：镜像导入
    image_id: str = ""                  # 腾讯云自定义镜像 ID

    # 通用附加信息
    extra: dict = field(default_factory=dict)

    def to_meta(self) -> dict[str, Any]:
        """序列化为可存入 SQLite meta 列的字典。"""
        return {
            "task_id": self.task_id,
            "version": self.version,
            "download_url": self.download_url,
            "local_file_path": self.local_file_path,
            "modified_file_path": self.modified_file_path,
            "cloud_init_injected": self.cloud_init_injected,
            "cos_object_key": self.cos_object_key,
            "cos_object_url": self.cos_object_url,
            "image_id": self.image_id,
        }
