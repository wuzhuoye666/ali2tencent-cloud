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

# A set of users which may be applied and/or used by various modules
# when a 'default' entry is found it will reference the 'default_user'
# from the distro configuration specified below
users:
   - default

# If this is set, 'root' will not be able to ssh in and they 
# will get a message to login instead as the above $user (ubuntu)
disable_root: true

# This will cause the set+update hostname module to not operate (if true)
preserve_hostname: false

datasource_list: [ ConfigDrive, TencentCloud ]
datasource:
  ConfigDrive:
    dsmode: local
  TencentCloud:
    metadata_urls: ['http://169.254.0.23', 'http://metadata.tencentyun.com']

# The modules that run in the 'init' stage
cloud_init_modules:
 - migrator
 - ubuntu-init-switch
 - seed_random
 - bootcmd
 - write-files
 - growpart
 - resizefs
 - disk_setup
 - mounts
 - set_hostname
 - update_hostname
 - ['update_etc_hosts', 'once-per-instance'] 
 - ca-certs
 - rsyslog
 - users-groups
 - ssh

# The modules that run in the 'config' stage
cloud_config_modules:
# Emit the cloud config ready event
# this can be used by upstart jobs for 'start on cloud-config'.
 - emit_upstart
 - snap_config
 - ssh-import-id
 - locale
 - set-passwords
 - grub-dpkg
 - apt-pipelining
 - apt-configure
 - ntp
 - resolv_conf
 - timezone
 - disable-ec2-metadata
 - runcmd
 - byobu

# The modules that run in the 'final' stage
cloud_final_modules:
 - snappy
 - package-update-upgrade-install
 - fan
 - landscape
 - lxd
 - puppet
 - chef
 - salt-minion
 - mcollective
 - rightscale_userdata
 - scripts-vendor
 - scripts-per-once
 - scripts-per-boot
 - scripts-per-instance
 - scripts-user
 - ssh-authkey-fingerprints
 - keys-to-console
 - phone-home
 - final-message
 - power-state-change

# System and/or distro specific settings
# (not accessible to handlers/transforms)
system_info:
   # This will affect which distro class gets used
   distro: ubuntu
   # Default user name + that default users groups (if added/used)
   default_user:
     name: ubuntu
     lock_passwd: false
     gecos: Ubuntu
     groups: [adm, audio, cdrom, dialout, dip, floppy, lxd, netdev, plugdev, sudo, video]
     sudo: ["ALL=(ALL) NOPASSWD:ALL"]
     shell: /bin/bash
   # Other config here will be given to the distro class and/or path classes
   paths:
      cloud_dir: /var/lib/cloud/
      templates_dir: /etc/cloud/templates/
      upstart_dir: /etc/init/
   package_mirrors:
     - arches: [i386, amd64]
       failsafe:
         primary: http://archive.ubuntu.com/ubuntu
         security: http://security.ubuntu.com/ubuntu
       search:
         primary:
           - http://%(ec2_region)s.ec2.archive.ubuntu.com/ubuntu/
           - http://%(availability_zone)s.clouds.archive.ubuntu.com/ubuntu/
           - http://%(region)s.clouds.archive.ubuntu.com/ubuntu/
         security: []
     - arches: [armhf, armel, default]
       failsafe:
         primary: http://ports.ubuntu.com/ubuntu-ports
         security: http://ports.ubuntu.com/ubuntu-ports
   ssh_svcname: ssh
apt:
  preserve_sources_list: true
"""


def run(ctx: PipelineContext, config: Config, db: StateDB) -> None:
    """流水线阶段入口：修改镜像 cloud-init 配置，更新 ctx.modified_file_path。

    注入策略优先级：
      0. WSL + virt-customize  — Windows 首选（需已安装 Ubuntu WSL）
      1. guestfish             — Linux 首选
      2. qemu-nbd + mount      — Linux 备选
      3. 跳过（仅保存配置文件，人工注入）
    """
    src = ctx.local_file_path
    if not src or not Path(src).exists():
        raise FileNotFoundError(f"源镜像文件不存在: {src}")

    stem = Path(src).stem
    suffix = Path(src).suffix
    dst = str(Path(src).parent / f"{stem}_modified{suffix}")

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

    # 策略0：Windows 下通过 WSL 调用 virt-customize
    if os.name == "nt" and not injected:
        injected = _inject_with_wsl(src, dst, user_data, meta_data)

    # 策略1：guestfish（Linux）
    if not injected and _has_command("guestfish"):
        injected = _inject_with_guestfish(src, dst, user_data, meta_data)

    # 策略2：qemu-nbd（Linux）
    if not injected and _has_command("qemu-nbd"):
        injected = _inject_with_qemu_nbd(src, dst, user_data, meta_data)

    # 策略3：无工具可用，跳过注入
    if not injected:
        logger.warning(
            "未找到任何可用的镜像修改工具（virt-customize/guestfish/qemu-nbd），跳过注入。\n"
            "腾讯云导入检测要求 datasource_list 包含 ConfigDrive，"
            "请在导入后手动修改实例内 /etc/cloud/cloud.cfg.d/99_qcloud.cfg"
        )
        shutil.copy2(src, dst)

    _save_cloud_init_files(ctx, config)
    ctx.modified_file_path = dst
    logger.info("cloud-init 处理完成: %s (injected=%s)", dst, injected)


def _inject_with_wsl(src: str, dst: str, user_data: str, meta_data: str) -> bool:
    """Windows 专用：通过 WSL 使用 qemu-nbd 挂载镜像并注入 cloud-init 配置。

    WSL2 自定义内核不含 /boot/vmlinuz，libguestfs/virt-customize 无法启动 appliance，
    因此改用 qemu-nbd + mount 方案（qemu-utils 随 libguestfs-tools 一并安装）。

    注入内容：
      /etc/cloud/cloud.cfg.d/99_qcloud.cfg  ← datasource_list: [ConfigDrive, None]
      /var/lib/cloud/seed/nocloud-net/user-data
      /var/lib/cloud/seed/nocloud-net/meta-data
    """
    if not shutil.which("wsl"):
        logger.debug("WSL 不可用，跳过 WSL 注入策略")
        return False

    # 检测 WSL 发行版列表
    try:
        raw = subprocess.run(["wsl", "--list", "--quiet"], capture_output=True, timeout=10).stdout
        try:
            distros_text = raw.decode("utf-16-le").strip()
        except Exception:
            distros_text = raw.decode("utf-8", errors="ignore").strip()
        distros = [d.strip("\x00").strip() for d in distros_text.splitlines() if d.strip("\x00").strip()]
    except Exception as e:
        logger.debug("获取 WSL 发行版列表失败: %s", e)
        return False

    if not distros:
        logger.debug("未找到任何 WSL 发行版")
        return False

    wsl_distro = next((d for d in distros if "ubuntu" in d.lower()), distros[0])
    logger.info("WSL 注入：使用发行版 %s", wsl_distro)

    # Windows 路径 -> WSL 路径
    def to_wsl(win_path: str) -> str:
        p = Path(win_path).resolve()
        drive = p.drive.rstrip(":").lower()
        rest = p.as_posix()[2:]
        return f"/mnt/{drive}{rest}"

    wsl_dst = to_wsl(dst)
    shutil.copy2(src, dst)

    # 检测 qemu-nbd 是否可用
    check = subprocess.run(
        ["wsl", "-d", wsl_distro, "--", "bash", "-c", "command -v qemu-nbd"],
        capture_output=True, timeout=10,
    )
    if check.returncode != 0:
        logger.warning(
            "WSL (%s) 中未找到 qemu-nbd，请先执行：\n"
            "  sudo apt-get install -y qemu-utils",
            wsl_distro,
        )
        return False

    # 转义文件内容中的单引号，用于 bash -c 内嵌
    def sh_escape(s: str) -> str:
        return s.replace("'", "'\\''")

    cfg      = sh_escape(_QCLOUD_DATASOURCE_CFG)
    cloud_cfg = sh_escape(_QCLOUD_CLOUD_CFG)
    ud       = sh_escape(user_data)
    md       = sh_escape(meta_data)

    # 一次性脚本：挂载 -> 写文件 -> 卸载
    # 同时写入：
    #   /etc/cloud/cloud.cfg          ← 完整腾讯云 cloud.cfg（含 datasource_list）
    #   /etc/cloud/cloud.cfg.d/99_qcloud.cfg ← 覆盖补丁（双重保险）
    #   /var/lib/cloud/seed/nocloud-net/user-data
    #   /var/lib/cloud/seed/nocloud-net/meta-data
    script = f"""set -e
modprobe nbd max_part=8 2>/dev/null || true
qemu-nbd -d /dev/nbd0 2>/dev/null || true
sleep 1
qemu-nbd -c /dev/nbd0 '{wsl_dst}'
sleep 2
mkdir -p /mnt/_ali2tencent_inject
mount /dev/nbd0p1 /mnt/_ali2tencent_inject 2>/dev/null || mount /dev/nbd01 /mnt/_ali2tencent_inject
mkdir -p /mnt/_ali2tencent_inject/etc/cloud/cloud.cfg.d
printf '%s' '{cloud_cfg}' > /mnt/_ali2tencent_inject/etc/cloud/cloud.cfg
printf '%s' '{cfg}' > /mnt/_ali2tencent_inject/etc/cloud/cloud.cfg.d/99_qcloud.cfg
mkdir -p /mnt/_ali2tencent_inject/var/lib/cloud/seed/nocloud-net
printf '%s' '{ud}' > /mnt/_ali2tencent_inject/var/lib/cloud/seed/nocloud-net/user-data
printf '%s' '{md}' > /mnt/_ali2tencent_inject/var/lib/cloud/seed/nocloud-net/meta-data
sync
umount /mnt/_ali2tencent_inject
qemu-nbd -d /dev/nbd0
echo WSL_INJECT_OK
"""

    logger.info("WSL qemu-nbd 注入 cloud-init 配置...")
    try:
        result = subprocess.run(
            ["wsl", "-d", wsl_distro, "--", "sudo", "bash", "-c", script],
            capture_output=True, timeout=120,
        )
        stdout = result.stdout.decode(errors="ignore")
        stderr = result.stderr.decode(errors="ignore")
        if "WSL_INJECT_OK" in stdout:
            logger.info("WSL qemu-nbd 注入成功")
            return True
        logger.warning("WSL 注入未成功，stdout=%s stderr=%s", stdout[-300:], stderr[-300:])
        return False
    except subprocess.TimeoutExpired:
        logger.warning("WSL 注入超时（120s）")
        return False
    except Exception as e:
        logger.warning("WSL 注入异常: %s", e)
        return False


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
        _write(str(tmp_dir / "99_qcloud.cfg"), _QCLOUD_DATASOURCE_CFG)
        _write(str(tmp_dir / "cloud.cfg"), _QCLOUD_CLOUD_CFG)
        logger.info(
            "cloud-init 配置已保存到 %s (仅供参考，未注入镜像)\n"
            "  重要：99_qcloud.cfg 内容需手动注入镜像的 /etc/cloud/cloud.cfg.d/99_qcloud.cfg",
            tmp_dir,
        )
    except Exception as e:
        logger.debug("保存 cloud-init 配置文件失败（非关键）: %s", e)


# ---- 注入策略实现 ----

def _inject_with_guestfish(src: str, dst: str, user_data: str, meta_data: str) -> bool:
    shutil.copy2(src, dst)
    script = f"""run
mount /dev/sda1 /
write /etc/cloud/cloud.cfg "{_escape(_QCLOUD_CLOUD_CFG)}"
write /etc/cloud/cloud.cfg.d/99_qcloud.cfg "{_escape(_QCLOUD_DATASOURCE_CFG)}"
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

        cloud_cfg_d = os.path.join(mount_point, "etc/cloud/cloud.cfg.d")
        os.makedirs(cloud_cfg_d, exist_ok=True)
        _write(os.path.join(mount_point, "etc/cloud/cloud.cfg"), _QCLOUD_CLOUD_CFG)
        _write(os.path.join(cloud_cfg_d, "99_qcloud.cfg"), _QCLOUD_DATASOURCE_CFG)
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
