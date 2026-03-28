"""
阶段3：cloud-init 配置注入
注入腾讯云兼容的 datasource_list 配置到镜像中。

支持两种注入策略（自动检测可用工具）：
  1. guestfish (libguestfs)  — Linux 首选，需安装 libguestfs-tools
  2. qemu-nbd + mount        — Linux 备选，需内核模块 nbd
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from core.config import Config
from core.logger import get_logger
from core.state import StateDB
from pipeline.context import PipelineContext

logger = get_logger("image_modifier")

# 腾讯云导入镜像要求 datasource_list 必须包含 ConfigDrive + TencentCloud
# 参考：https://cloud.tencent.com/document/product/213/17339
_QCLOUD_DATASOURCE_CFG = """\
# 腾讯云导入镜像专用：覆盖 datasource 配置（99_ 前缀确保最后加载，优先级最高）
datasource_list: [ ConfigDrive, TencentCloud ]
datasource:
  ConfigDrive:
    dsmode: local
  TencentCloud:
    metadata_urls: ['http://169.254.0.23', 'http://metadata.tencentyun.com']
"""

# 完整的腾讯云 cloud.cfg（替换镜像内 /etc/cloud/cloud.cfg）
_QCLOUD_CLOUD_CFG = """\
# The top level settings are used as module
# and system configuration.

users:
   - default

disable_root: true
preserve_hostname: false

datasource_list: [ ConfigDrive, TencentCloud ]
datasource:
  ConfigDrive:
    dsmode: local
  TencentCloud:
    metadata_urls: ['http://169.254.0.23', 'http://metadata.tencentyun.com']

cloud_init_modules:
 - migrator
 - seed_random
 - bootcmd
 - write-files
 - growpart
 - resizefs
 - disk_setup
 - mounts
 - set_hostname
 - update_hostname
 - update_etc_hosts
 - ca-certs
 - rsyslog
 - users-groups
 - ssh

cloud_config_modules:
 - ssh-import-id
 - locale
 - set-passwords
 - ntp
 - timezone
 - disable-ec2-metadata
 - runcmd

cloud_final_modules:
 - package-update-upgrade-install
 - scripts-vendor
 - scripts-per-once
 - scripts-per-boot
 - scripts-per-instance
 - scripts-user
 - ssh-authkey-fingerprints
 - keys-to-console
 - final-message
 - power-state-change

system_info:
   distro: alinux
   paths:
      cloud_dir: /var/lib/cloud/
      templates_dir: /etc/cloud/templates/
   ssh_svcname: sshd
"""


def run(ctx: PipelineContext, config: Config, db: StateDB) -> None:
    """流水线阶段入口：修改镜像 cloud-init 配置，更新 ctx.modified_file_path。

    注入策略优先级：
      1. guestfish             — Linux 首选
      2. qemu-nbd + mount      — Linux 备选
    """
    src = ctx.local_file_path
    if not src or not Path(src).exists():
        raise FileNotFoundError(f"源镜像文件不存在: {src}")

    stem = Path(src).stem
    suffix = Path(src).suffix
    dst = str(Path(src).parent / f"{stem}_modified{suffix}")

    injected = False

    # 策略1：guestfish（Linux）
    if _has_command("guestfish"):
        injected = _inject_with_guestfish(src, dst)

    # 策略2：qemu-nbd（Linux）
    if not injected and _has_command("qemu-nbd"):
        injected = _inject_with_qemu_nbd(src, dst)

    # 策略3：无工具可用，跳过注入
    if not injected:
        logger.warning(
            "未找到任何可用的镜像修改工具（guestfish/qemu-nbd），跳过注入。\n"
            "腾讯云导入检测要求 datasource_list 包含 ConfigDrive，"
            "请在导入后手动修改实例内 /etc/cloud/cloud.cfg.d/99_qcloud.cfg"
        )
        shutil.copy2(src, dst)

    # 保存配置文件供参考
    _save_cloud_init_files(ctx)

    ctx.modified_file_path = dst
    ctx.cloud_init_injected = injected
    logger.info("cloud-init 处理完成: %s (injected=%s)", dst, injected)


def _inject_with_guestfish(src: str, dst: str) -> bool:
    """使用 guestfish 注入 cloud-init 配置。"""
    shutil.copy2(src, dst)
    script = f"""run
mount /dev/sda3 /
mkdir-p /etc/cloud/cloud.cfg.d
write /etc/cloud/cloud.cfg "{_escape(_QCLOUD_CLOUD_CFG)}"
write /etc/cloud/cloud.cfg.d/99_qcloud.cfg "{_escape(_QCLOUD_DATASOURCE_CFG)}"
umount /
exit
"""
    try:
        # 设置 LIBGUESTFS_BACKEND=direct 以在容器/云主机环境正常工作
        env = os.environ.copy()
        env["LIBGUESTFS_BACKEND"] = "direct"
        result = subprocess.run(
            ["guestfish", "-a", dst],
            input=script,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        if result.returncode != 0:
            logger.warning("guestfish 失败: %s", result.stderr[:500])
            return False
        logger.info("guestfish 注入成功")
        return True
    except Exception as e:
        logger.warning("guestfish 异常: %s", e)
        return False


def _inject_with_qemu_nbd(src: str, dst: str) -> bool:
    """使用 qemu-nbd 注入 cloud-init 配置。"""
    shutil.copy2(src, dst)
    nbd_dev = "/dev/nbd0"
    mount_point = tempfile.mkdtemp(prefix="ali2tencent_nbd_")
    try:
        subprocess.run(["modprobe", "nbd", "max_part=8"], check=True, timeout=10)
        subprocess.run(["qemu-nbd", "-c", nbd_dev, dst], check=True, timeout=30)
        # 尝试挂载第一个分区
        mounted = False
        for part in ["p3", "3", "p1", "1"]:
            try:
                subprocess.run(["mount", f"{nbd_dev}{part}", mount_point], check=True, timeout=10)
                mounted = True
                break
            except subprocess.CalledProcessError:
                continue

        if not mounted:
            raise RuntimeError("无法挂载镜像分区")

        cloud_cfg_d = os.path.join(mount_point, "etc/cloud/cloud.cfg.d")
        os.makedirs(cloud_cfg_d, exist_ok=True)
        _write(os.path.join(mount_point, "etc/cloud/cloud.cfg"), _QCLOUD_CLOUD_CFG)
        _write(os.path.join(cloud_cfg_d, "99_qcloud.cfg"), _QCLOUD_DATASOURCE_CFG)
        logger.info("qemu-nbd 注入成功")
        return True
    except Exception as e:
        logger.warning("qemu-nbd 注入失败: %s", e)
        return False
    finally:
        try:
            subprocess.run(["umount", mount_point], timeout=10)
        except Exception:
            pass
        try:
            subprocess.run(["qemu-nbd", "-d", nbd_dev], timeout=10)
        except Exception:
            pass
        shutil.rmtree(mount_point, ignore_errors=True)


def _save_cloud_init_files(ctx: PipelineContext) -> None:
    """将 cloud-init 配置保存到 tmp/ 目录供参考。"""
    try:
        tmp_dir = Path(ctx.local_file_path).parent
        _write(str(tmp_dir / "99_qcloud.cfg"), _QCLOUD_DATASOURCE_CFG)
        _write(str(tmp_dir / "cloud.cfg"), _QCLOUD_CLOUD_CFG)
        logger.info("cloud-init 配置已保存到 %s", tmp_dir)
    except Exception as e:
        logger.debug("保存 cloud-init 配置文件失败（非关键）: %s", e)


# ---- 工具函数 ----

def _has_command(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _escape(s: str) -> str:
    return s.replace('"', '\\"').replace("\n", "\\n")


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
