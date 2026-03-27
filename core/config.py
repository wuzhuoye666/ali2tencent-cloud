"""统一配置加载模块，从 .env 文件读取所有配置项。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 自动加载项目根目录下的 .env
_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env", override=False)


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"必要配置项 {key!r} 未在 .env 中设置")
    return val


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


@dataclass
class Config:
    # ---- 腾讯云基础凭证 ----
    tencent_secret_id: str
    tencent_secret_key: str
    tencent_region: str
    tencent_cos_bucket: str

    # ---- 阿里云镜像检测 ----
    ali_image_doc_url: str          # 阿里云 Alibaba Cloud Linux 镜像发布页
    check_interval_hours: int       # 定时检测间隔（小时）

    # ---- CVM 实例参数 ----
    cvm_instance_type: str          # 测试实例规格，如 S5.MEDIUM2
    cvm_vpc_id: str                 # 所用 VPC
    cvm_subnet_id: str              # 所用 Subnet
    cvm_security_group_id: str      # 安全组
    cvm_login_password: str         # 实例登录密码（仅用于 benchmark SSH）
    cvm_disk_size: int              # 系统盘大小 GB

    # ---- SSH / 性能测试 ----
    ssh_user: str                   # SSH 登录用户名，如 root
    ssh_private_key_path: str       # 私钥路径（优先；若为空则用密码）
    benchmark_timeout: int          # 单项测试超时秒数

    # ---- 运行行为 ----
    keep_instance: bool             # 测试后是否保留 CVM 实例
    tmp_dir: str                    # 镜像临时存放目录
    log_dir: str                    # 日志目录
    report_dir: str                 # 报告输出目录
    state_db: str                   # SQLite 状态库路径

    @classmethod
    def from_env(cls) -> "Config":
        root = str(_ROOT)
        return cls(
            tencent_secret_id=_require("TENCENT_SECRET_ID"),
            tencent_secret_key=_require("TENCENT_SECRET_KEY"),
            tencent_region=_optional("TENCENT_REGION", "ap-guangzhou"),
            tencent_cos_bucket=_require("TENCENT_COS_BUCKET"),
            ali_image_doc_url=_optional(
                "ALI_IMAGE_DOC_URL",
                "https://mirrors.aliyun.com/alinux/3/image/",
            ),
            check_interval_hours=int(_optional("CHECK_INTERVAL_HOURS", "6")),
            cvm_instance_type=_optional("CVM_INSTANCE_TYPE", "S5.MEDIUM2"),
            cvm_vpc_id=_optional("CVM_VPC_ID", ""),
            cvm_subnet_id=_optional("CVM_SUBNET_ID", ""),
            cvm_security_group_id=_optional("CVM_SECURITY_GROUP_ID", ""),
            cvm_login_password=_optional("CVM_LOGIN_PASSWORD", "Ali2Tencent@2024"),
            cvm_disk_size=int(_optional("CVM_DISK_SIZE", "50")),
            ssh_user=_optional("SSH_USER", "root"),
            ssh_private_key_path=_optional("SSH_PRIVATE_KEY_PATH", ""),
            benchmark_timeout=int(_optional("BENCHMARK_TIMEOUT", "300")),
            keep_instance=_optional("KEEP_INSTANCE", "false").lower() == "true",
            tmp_dir=_optional("TMP_DIR", os.path.join(root, "tmp")),
            log_dir=_optional("LOG_DIR", os.path.join(root, "logs")),
            report_dir=_optional("REPORT_DIR", os.path.join(root, "reports")),
            state_db=_optional("STATE_DB", os.path.join(root, "state.db")),
        )


# 全局单例（惰性初始化）
_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config
