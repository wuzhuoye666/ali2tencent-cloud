"""
阶段3：cloud-init 配置注入
将 SSH 公钥、用户名、网络配置等写入镜像的 cloud-init user-data / meta-data。

支持两种注入策略（自动检测可用工具）：
  1. guestfish (libguestfs)  — Linux 首选，需安装 libguestfs-tools
  2. qemu-nbd + mount        — Linux 备选，需内核模块 nbd
  3. seed ISO                — 跨平台，生成 NoCloud seed.iso 附加给镜像（推荐用于腾讯云）
     腾讯云导入镜像时只需 qcow2 本体，cloud-init 通过 ConfigDrive/NoCloud 元数据服务获取配置，
     因此注入方式为在同目录生成 seed.iso（测试用）或直接修改镜像内部文件。
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

# cloud-init user-data 模板（腾讯云兼容）
_USER_DATA_TMPL = """\
#cloud-config
users:
  - name: {ssh_user}
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: false
    passwd: {hashed_password}
    ssh_authorized_keys:
      - {ssh_pubkey}

# 禁用 SELinux（兼容性）
runcmd:
  - setenforce 0 || true
  - systemctl enable sshd || systemctl enable ssh || true
  - systemctl start sshd || systemctl start ssh || true

# 允许密码登录
ssh_pwauth: true
chpasswd:
  expire: false
"""

_META_DATA_TMPL = """\
instance-id: ali2tencent-{version}
local-hostname: ali2tencent-{version}
"""


def run(ctx: PipelineContext, config: Config, db: StateDB) -> None:
    """流水线阶段入口：修改镜像 cloud-init 配置，更新 ctx.modified_file_path。

    在 Windows 平台或工具不可用时，直接跳过修改，使用原始镜像文件。
    腾讯云导入后通过控制台密码或密钥登录即可。
    """
    src = ctx.local_file_path
    if not src or not Path(src).exists():
        raise FileNotFoundError(f"源镜像文件不存在: {src}")

    # Windows 平台：无法挂载镜像，直接跳过
    if os.name == "nt":
        logger.info("Windows 平台不支持镜像挂载，跳过 cloud-init 注入，使用原始镜像")
        ctx.modified_file_path = src
        _save_cloud_init_files(ctx, config)
        return

    # 目标文件：在原文件名基础上加 _modified
    stem = Path(src).stem
    suffix = Path(src).suffix
    dst = str(Path(src).parent / f"{stem}_modified{suffix}")

    logger.info("修改 cloud-init 配置: %s -> %s", src, dst)

    # 生成配置内容
    ssh_pubkey = _get_ssh_pubkey(config)
    hashed_pw = _hash_password(config.cvm_login_password)
    user_data = _USER_DATA_TMPL.format(
        ssh_user=config.ssh_user,
        hashed_password=hashed_pw,
        ssh_pubkey=ssh_pubkey,
    )
    meta_data = _META_DATA_TMPL.format(version=ctx.version.replace(".", "-"))

    injected = False

    # 策略1：guestfish
    if _has_command("guestfish") and not injected:
        injected = _inject_with_guestfish(src, dst, user_data, meta_data)

    # 策略2：qemu-nbd
    if not injected and _has_command("qemu-nbd"):
        injected = _inject_with_qemu_nbd(src, dst, user_data, meta_data)

    # 策略3：seed ISO（Linux 有工具时生成）
    if not injected:
        logger.info("镜像挂载工具不可用，使用 NoCloud seed ISO 方式注入 cloud-init")
        shutil.copy2(src, dst)
        seed_iso = _create_seed_iso(Path(src).parent, user_data, meta_data, ctx.version)
        ctx.extra["seed_iso_path"] = seed_iso
        if seed_iso:
            logger.info("Seed ISO 已生成: %s", seed_iso)
        else:
            logger.info("Seed ISO 生成跳过（无工具），镜像将直接上传，使用原始 cloud-init")

    ctx.modified_file_path = dst
    logger.info("cloud-init 处理完成: %s", dst)


def _save_cloud_init_files(ctx: PipelineContext, config: Config) -> None:
    """在 Windows 下将 cloud-init 配置保存到 tmp/ 目录供参考。"""
    try:
        tmp_dir = Path(ctx.local_file_path).parent
        ssh_pubkey = _get_ssh_pubkey(config)
        user_data = _USER_DATA_TMPL.format(
            ssh_user=config.ssh_user,
            hashed_password=config.cvm_login_password,
            ssh_pubkey=ssh_pubkey,
        )
        meta_data = _META_DATA_TMPL.format(version=ctx.version.replace(".", "-"))
        _write(str(tmp_dir / "user-data.txt"), user_data)
        _write(str(tmp_dir / "meta-data.txt"), meta_data)
        logger.info("cloud-init 配置已保存到 %s (仅供参考，未注入镜像)", tmp_dir)
    except Exception as e:
        logger.debug("保存 cloud-init 配置文件失败（非关键）: %s", e)


# ---- 注入策略实现 ----

def _inject_with_guestfish(src: str, dst: str, user_data: str, meta_data: str) -> bool:
    shutil.copy2(src, dst)
    script = f"""run
mount /dev/sda1 /
write /etc/cloud/cloud.cfg.d/99_ali2tencent.cfg "{_escape(user_data)}"
write /var/lib/cloud/seed/nocloud-net/user-data "{_escape(user_data)}"
write /var/lib/cloud/seed/nocloud-net/meta-data "{_escape(meta_data)}"
umount /
exit
"""
    try:
        result = subprocess.run(
            ["guestfish", "-a", dst],
            input=script,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            logger.warning("guestfish 失败: %s", result.stderr[:500])
            return False
        logger.info("guestfish 注入成功")
        return True
    except Exception as e:
        logger.warning("guestfish 异常: %s", e)
        return False


def _inject_with_qemu_nbd(src: str, dst: str, user_data: str, meta_data: str) -> bool:
    shutil.copy2(src, dst)
    nbd_dev = "/dev/nbd0"
    mount_point = tempfile.mkdtemp(prefix="ali2tencent_nbd_")
    try:
        subprocess.run(["modprobe", "nbd", "max_part=8"], check=True, timeout=10)
        subprocess.run(["qemu-nbd", "-c", nbd_dev, dst], check=True, timeout=30)
        # 尝试挂载第一个分区
        for part in ["p1", "1"]:
            try:
                subprocess.run(["mount", f"{nbd_dev}{part}", mount_point], check=True, timeout=10)
                break
            except subprocess.CalledProcessError:
                continue

        cloud_seed = os.path.join(mount_point, "var/lib/cloud/seed/nocloud-net")
        os.makedirs(cloud_seed, exist_ok=True)
        _write(os.path.join(cloud_seed, "user-data"), user_data)
        _write(os.path.join(cloud_seed, "meta-data"), meta_data)
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


def _create_seed_iso(parent_dir: Path, user_data: str, meta_data: str, version: str) -> str:
    """创建 NoCloud seed ISO，可在 qemu 启动时附加 -cdrom seed.iso。"""
    seed_dir = parent_dir / f"seed_{version}"
    seed_dir.mkdir(exist_ok=True)
    _write(str(seed_dir / "user-data"), user_data)
    _write(str(seed_dir / "meta-data"), meta_data)

    iso_path = str(parent_dir / f"seed_{version}.iso")

    if _has_command("genisoimage"):
        cmd = ["genisoimage", "-output", iso_path, "-volid", "cidata", "-joliet",
               "-rock", str(seed_dir)]
    elif _has_command("mkisofs"):
        cmd = ["mkisofs", "-output", iso_path, "-volid", "cidata", "-joliet",
               "-rock", str(seed_dir)]
    elif _has_command("xorriso"):
        cmd = ["xorriso", "-as", "mkisofs", "-output", iso_path, "-volid", "cidata",
               "-joliet", "-rock", str(seed_dir)]
    else:
        logger.warning("未找到 ISO 制作工具（genisoimage/mkisofs/xorriso），跳过 seed ISO 生成")
        return ""

    try:
        subprocess.run(cmd, check=True, timeout=30, capture_output=True)
        return iso_path
    except Exception as e:
        logger.warning("seed ISO 生成失败: %s", e)
        return ""


# ---- 工具函数 ----

def _has_command(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _escape(s: str) -> str:
    return s.replace('"', '\\"').replace("\n", "\\n")


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _hash_password(password: str) -> str:
    """生成 SHA-512 密码哈希（cloud-init 格式）。"""
    try:
        import crypt
        return crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
    except ImportError:
        # Python 3.13+ 移除了 crypt 模块；返回明文（仅测试环境）
        logger.warning("crypt 模块不可用，密码将以明文存储（仅适用于测试环境）")
        return password


def _get_ssh_pubkey(config: Config) -> str:
    if config.ssh_private_key_path:
        pub_path = config.ssh_private_key_path + ".pub"
        if Path(pub_path).exists():
            return Path(pub_path).read_text().strip()
    return "# 请在 .env 中配置 SSH_PRIVATE_KEY_PATH 或手动填入公钥"
