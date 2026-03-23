import os
import time
import logging
import traceback
import requests
import re
import tempfile
import shlex
import argparse
from pathlib import Path
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

try:
    import guestfs
except ImportError:
    logging.warning("guestfs module not found. Please install python3-guestfs.")

try:
    from qcloud_cos import CosConfig
    from qcloud_cos import CosS3Client
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
    from tencentcloud.cvm.v20170312 import cvm_client, models as cvm_models
except ImportError:
    logging.warning("Tencent Cloud SDKs not found. Please install tencentcloud-sdk-python and cos-python-sdk-v5.")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("Alios-to-Tencent-Porter")


class ScraperModule:
    """
    负责从阿里云镜像站爬取并下载最新的 Alibaba Cloud Linux 3 镜像。
    """
    def __init__(self, base_url="https://mirrors.aliyun.com/alinux/3/image/"):
        self.base_url = base_url
        self.session = requests.Session()

    def find_latest_image(self) -> str:
        logger.info(f"Scraping image list from {self.base_url}")
        try:
            response = self.session.get(self.base_url, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            links = soup.find_all('a')

            candidates = set()
            for link in links:
                href = (link.get('href') or "").strip()
                text = (link.get_text() or "").strip()
                if href:
                    candidates.add(href)
                if text:
                    candidates.add(text)

            for m in re.findall(r"[A-Za-z0-9._-]+\.qcow2", response.text):
                candidates.add(m)

            image_pattern = re.compile(r"nocloud.*alibase.*\.qcow2$", re.IGNORECASE)
            arch_exclude = re.compile(r"arm64", re.IGNORECASE)
            arch_include = re.compile(r"(x64|x86_64)", re.IGNORECASE)

            candidate_links = []
            for item in candidates:
                name = item.split("/")[-1]
                if not image_pattern.search(name):
                    continue
                if arch_exclude.search(name):
                    continue
                if not arch_include.search(name):
                    continue
                candidate_links.append(name)
            
            if not candidate_links:
                raise Exception("No matching images found on the mirror site.")
                
            def version_key(name: str):
                nums = re.findall(r'\d+', name)
                if not nums:
                    return (0,)
                return tuple(int(n) for n in nums)

            candidate_links.sort(key=lambda s: (version_key(s), s))
            latest_image = candidate_links[-1]
            
            full_url = urljoin(self.base_url, latest_image)
            logger.info(f"Found latest image: {full_url}")
            return full_url
            
        except Exception as e:
            logger.error(f"Failed to find latest image: {e}")
            raise

    def download_image(self, url: str, dest_path: str, max_retries: int = 5):
        logger.info(f"Downloading image from {url} to {dest_path}")
        try:
            dest = Path(dest_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".part")

            attempt = 0
            while True:
                attempt += 1
                existing = tmp.stat().st_size if tmp.exists() else 0
                headers = {}
                mode = "ab" if existing > 0 else "wb"
                if existing > 0:
                    headers["Range"] = f"bytes={existing}-"

                try:
                    with self.session.get(url, stream=True, headers=headers, timeout=(10, 120)) as r:
                        if existing > 0 and r.status_code == 200:
                            tmp.unlink(missing_ok=True)
                            existing = 0
                            mode = "wb"

                        r.raise_for_status()

                        total_length = None
                        if r.headers.get("Content-Range"):
                            m = re.search(r"/(\d+)$", r.headers["Content-Range"])
                            if m:
                                total_length = int(m.group(1))
                        elif r.headers.get("Content-Length"):
                            total_length = existing + int(r.headers["Content-Length"])

                        downloaded = existing
                        chunk_size = 10 * 1024 * 1024
                        last_print_time = time.time()

                        with open(tmp, mode) as f:
                            for data in r.iter_content(chunk_size=chunk_size):
                                if not data:
                                    continue
                                f.write(data)
                                downloaded += len(data)
                                now = time.time()
                                if now - last_print_time > 5:
                                    if total_length:
                                        pct = (downloaded / total_length) * 100
                                        logger.info(f"Download Progress: {pct:.1f}% ({downloaded/(1024*1024):.1f} MB / {total_length/(1024*1024):.1f} MB)")
                                    else:
                                        logger.info(f"Download Progress: {downloaded/(1024*1024):.1f} MB")
                                    last_print_time = now

                    os.replace(tmp, dest)
                    logger.info("Download completed successfully.")
                    return

                except Exception as e:
                    if attempt >= max_retries:
                        raise
                    backoff = min(60, 2 ** attempt)
                    logger.warning(f"Download attempt {attempt} failed: {e}. Retrying in {backoff}s.")
                    time.sleep(backoff)
        except Exception as e:
            logger.error(f"Failed to download image: {e}")
            raise


class SurgeryModule:
    """
    负责使用 libguestfs 对镜像进行离线“手术”适配。
    """
    def __init__(self, image_path: str, backend: str = "direct"):
        if "guestfs" not in globals():
            raise RuntimeError("guestfs is required but not available in this environment.")
        self.image_path = image_path
        self.g = guestfs.GuestFS(python_return_dict=True)
        self.g.set_backend(backend)
        self.mount_root = "/sysroot"
        
    def perform_surgery(self):
        try:
            logger.info(f"Starting surgery on image: {self.image_path}")
            self.g.add_drive_opts(self.image_path, format="qcow2", readonly=0)
            self.g.launch()
            
            roots = self.g.inspect_os()
            if not roots:
                raise Exception("No operating system found in the image")
            root = roots[0]

            logger.info(f"Mounting filesystems for root: {root}")
            self._mount_all(root)

            self._inject_drivers()
            self._adapt_cloud_init()
            self._adapt_network()
            self._fix_repo_sources()
            self._redirect_console()
            
            logger.info("Surgery completed successfully.")
        except Exception as e:
            logger.error(f"Error during surgery: {str(e)}")
            logger.error(traceback.format_exc())
            raise
        finally:
            logger.info("Syncing and unmounting...")
            try:
                self.g.umount_all()
                self.g.sync()
            except Exception as unmount_err:
                logger.warning(f"Warning during unmount: {unmount_err}")
            self.g.close()
            
    def _gp(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        if path == "/":
            return self.mount_root
        return self.mount_root + path

    def _mount_all(self, root: str):
        self.g.mkdir_p(self.mount_root)
        mountpoints = self.g.inspect_get_mountpoints(root)
        items = list(mountpoints.items())
        if items and not str(items[0][0]).startswith("/"):
            items = [(mp, dev) for dev, mp in items]
        items.sort(key=lambda kv: len(kv[0]))
        for mountpoint, device in items:
            target = self.mount_root if mountpoint == "/" else self.mount_root + mountpoint
            self.g.mkdir_p(target)
            self.g.mount(device, target)

    def _prepare_chroot(self):
        for d in ("proc", "sys", "dev", "run"):
            self.g.mkdir_p(self._gp(f"/{d}"))
        self.g.sh(f"mount -t proc proc {self._gp('/proc')} || true")
        self.g.sh(f"mount -t sysfs sys {self._gp('/sys')} || true")
        self.g.sh(f"mount --bind /dev {self._gp('/dev')} || true")
        self.g.sh(f"mount --bind /run {self._gp('/run')} || true")

    def _sh_guest(self, cmd: str) -> str:
        self._prepare_chroot()
        wrapped = f"chroot {self.mount_root} /bin/sh -lc {shlex.quote(cmd)}"
        return self.g.sh(wrapped)

    def _inject_drivers(self):
        logger.info("Injecting Virtio drivers...")
        cmd = 'dracut --force --add-drivers "virtio virtio_pci virtio_ring virtio_net virtio_blk"'
        output = self._sh_guest(cmd)
        logger.debug(f"dracut output: {output}")

    def _adapt_cloud_init(self):
        logger.info("Adapting cloud-init...")
        content = "datasource_list:\n  - ConfigDrive\n  - OpenStack\n"
        self.g.mkdir_p(self._gp("/etc/cloud/cloud.cfg.d"))
        self.g.write(self._gp("/etc/cloud/cloud.cfg.d/99_tencent.cfg"), content)

    def _adapt_network(self):
        logger.info("Adapting network configuration (eth0 to DHCP)...")
        content = """DEVICE=eth0
BOOTPROTO=dhcp
ONBOOT=yes
TYPE=Ethernet
USERCTL=yes
PEERDNS=yes
IPV6INIT=no
PERSISTENT_DHCLIENT=yes
"""
        self.g.mkdir_p(self._gp("/etc/sysconfig/network-scripts"))
        self.g.write(self._gp("/etc/sysconfig/network-scripts/ifcfg-eth0"), content)

    def _fix_repo_sources(self):
        logger.info("Fixing yum/dnf repo sources...")
        try:
            repos_dir = self._gp("/etc/yum.repos.d/")
            if not self.g.is_dir(repos_dir):
                return
            files = self.g.ls(repos_dir)
            for f in files:
                if f.endswith(".repo"):
                    path = f"{repos_dir.rstrip('/')}/{f}"
                    content = self.g.cat(path)
                    if "mirrors.cloud.aliyuncs.com" in content:
                        new_content = content.replace("mirrors.cloud.aliyuncs.com", "mirrors.aliyun.com")
                        self.g.write(path, new_content)
                        logger.info(f"Fixed repo file: {path}")
        except Exception as e:
            logger.warning(f"Could not fix repo sources: {e}")

    def _redirect_console(self):
        logger.info("Redirecting console to ttyS0...")
        grub_path = self._gp("/etc/default/grub")
        if self.g.is_file(grub_path):
            grub_config = self.g.cat(grub_path)
            lines = grub_config.split('\n')
            new_lines = []
            for line in lines:
                if line.startswith("GRUB_CMDLINE_LINUX=") or line.startswith("GRUB_CMDLINE_LINUX_DEFAULT="):
                    if "console=ttyS0" not in line and '"' in line:
                        prefix, rest = line.split("=", 1)
                        rest = rest.strip()
                        if rest.startswith('"') and rest.endswith('"'):
                            inner = rest[1:-1].strip()
                            inner = (inner + " console=ttyS0").strip()
                            line = f'{prefix}="{inner}"'
                new_lines.append(line)
            self.g.write(grub_path, '\n'.join(new_lines))
            
            if self.g.is_file(self._gp("/boot/grub2/grub.cfg")):
                self._sh_guest("grub2-mkconfig -o /boot/grub2/grub.cfg")
            elif self.g.is_file(self._gp("/boot/efi/EFI/centos/grub.cfg")):
                self._sh_guest("grub2-mkconfig -o /boot/efi/EFI/centos/grub.cfg")


class StorageRegistryModule:
    """
    负责将处理后的镜像上传至 COS 并调用腾讯云 CVM ImportImage API 导入镜像。
    """
    def __init__(self, secret_id: str, secret_key: str, region: str, cos_bucket: str):
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.region = region
        self.cos_bucket = cos_bucket
        
        cos_config = CosConfig(Region=self.region, SecretId=self.secret_id, SecretKey=self.secret_key)
        self.cos_client = CosS3Client(cos_config)
        
        cred = credential.Credential(self.secret_id, self.secret_key)
        httpProfile = HttpProfile()
        httpProfile.endpoint = "cvm.tencentcloudapi.com"
        clientProfile = ClientProfile()
        clientProfile.httpProfile = httpProfile
        self.cvm_client = cvm_client.CvmClient(cred, self.region, clientProfile)

    def upload_to_cos(self, local_path: str, cos_path: str, public_read: bool = True, signed_url_ttl: int = 24 * 3600) -> str:
        logger.info(f"Uploading {local_path} to COS bucket {self.cos_bucket} at {cos_path}...")
        try:
            self.cos_client.upload_file(
                Bucket=self.cos_bucket,
                LocalFilePath=local_path,
                Key=cos_path,
                PartSize=10,
                MAXThread=5,
                EnableMD5=False
            )
            logger.info("Upload completed.")
            if public_read:
                try:
                    self.cos_client.put_object_acl(Bucket=self.cos_bucket, Key=cos_path, ACL="public-read")
                except Exception as acl_err:
                    logger.warning(f"Failed to set object ACL to public-read: {acl_err}")

            if public_read:
                public_url = f"https://{self.cos_bucket}.cos.{self.region}.myqcloud.com/{cos_path}"
                try:
                    r = requests.head(public_url, timeout=10, allow_redirects=True)
                    if r.status_code == 200:
                        return public_url
                    logger.warning(f"Public URL not accessible (HTTP {r.status_code}), switching to presigned URL.")
                except Exception as head_err:
                    logger.warning(f"Public URL check failed, switching to presigned URL: {head_err}")

            return self.cos_client.get_presigned_url(
                Method="GET",
                Bucket=self.cos_bucket,
                Key=cos_path,
                Expired=signed_url_ttl
            )
        except Exception as e:
            logger.error(f"COS upload failed: {str(e)}")
            raise

    def import_image(self, image_url: str, architecture: str = "x86_64") -> str:
        logger.info(f"Importing image from {image_url}...")
        try:
            req = cvm_models.ImportImageRequest()
            image_name = f"AliLinux3-Tencent-{int(time.time())}"
            req.Architecture = architecture
            # 必须传 OsType 和 OsVersion，兼容 CentOS 7/8
            req.OsType = "CentOS"
            req.OsVersion = "CentOS 7.6 64bit"
            req.ImageName = image_name
            req.ImageUrl = image_url
            
            resp = self.cvm_client.ImportImage(req)
            image_id = resp.ImageId
            logger.info(f"Image import task submitted. ImageId: {image_id}")
            return image_id
        except TencentCloudSDKException as err:
            logger.error(f"ImportImage API failed: {err}")
            raise

    def get_image_state(self, image_id: str) -> str:
        req = cvm_models.DescribeImagesRequest()
        req.ImageIds = [image_id]
        resp = self.cvm_client.DescribeImages(req)
        if not resp.ImageSet:
            return "UNKNOWN"
        img = resp.ImageSet[0]
        return getattr(img, "ImageState", "UNKNOWN")

    def wait_for_image_ready(self, image_id: str, timeout_sec: int = 6 * 3600, interval_sec: int = 60) -> str:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            state = self.get_image_state(image_id)
            logger.info(f"Image {image_id} state: {state}")
            if state in ("NORMAL", "AVAILABLE"):
                return state
            if state in ("FAILED", "ERROR"):
                raise RuntimeError(f"Import failed with image state: {state}")
            time.sleep(interval_sec)
        raise TimeoutError(f"Timed out waiting for image {image_id} to become ready.")


class AliosToTencentPorter:
    """
    Agent 主类：负责整体流程编排、异常捕获与临时文件清理。
    """
    def __init__(self, config: dict, workdir: str | None = None):
        self.config = config
        base = Path(workdir) if workdir else Path(tempfile.gettempdir())
        self.temp_image_path = str(base / "alilinux3_temp.qcow2")

    def run(self, cos_prefix: str = "wzy/", skip_surgery: bool = False, quick_import: bool = False, wait: bool = False, wait_timeout: int = 6 * 3600, wait_interval: int = 60):
        logger.info("Starting Alios-to-Tencent-Porter Agent...")
        registry = StorageRegistryModule(
            secret_id=self.config['TENCENT_SECRET_ID'],
            secret_key=self.config['TENCENT_SECRET_KEY'],
            region=self.config['TENCENT_REGION'],
            cos_bucket=self.config['TENCENT_COS_BUCKET']
        )
        try:
            scraper = ScraperModule()
            url = scraper.find_latest_image()

            if quick_import:
                image_id = registry.import_image(url)
                logger.info(f"Agent finished. ImageId: {image_id}")
                if wait:
                    registry.wait_for_image_ready(image_id, timeout_sec=wait_timeout, interval_sec=wait_interval)
                return image_id

            scraper.download_image(url, self.temp_image_path)
            if not os.path.exists(self.temp_image_path):
                raise FileNotFoundError(f"Image not found at {self.temp_image_path}")

            if not skip_surgery:
                surgery = SurgeryModule(self.temp_image_path)
                surgery.perform_surgery()

            cos_prefix = cos_prefix.lstrip("/")
            if cos_prefix and not cos_prefix.endswith("/"):
                cos_prefix += "/"

            cos_path = f"{cos_prefix}alilinux3-{int(time.time())}.qcow2"
            image_url = registry.upload_to_cos(self.temp_image_path, cos_path)
            image_id = registry.import_image(image_url)
            logger.info(f"Agent finished. ImageId: {image_id}")
            if wait:
                registry.wait_for_image_ready(image_id, timeout_sec=wait_timeout, interval_sec=wait_interval)
            return image_id
            
        except Exception as e:
            logger.error(f"Agent workflow failed: {str(e)}")
            logger.error(traceback.format_exc())
            raise
        finally:
            if os.path.exists(self.temp_image_path):
                logger.info(f"Cleaning up temporary file: {self.temp_image_path}")
                try:
                    os.remove(self.temp_image_path)
                except Exception as cleanup_err:
                    logger.error(f"Failed to clean up: {cleanup_err}")

def load_config_from_env() -> dict:
    return {
        "TENCENT_SECRET_ID": os.environ.get("TENCENT_SECRET_ID", ""),
        "TENCENT_SECRET_KEY": os.environ.get("TENCENT_SECRET_KEY", ""),
        "TENCENT_REGION": os.environ.get("TENCENT_REGION", "ap-guangzhou"),
        "TENCENT_COS_BUCKET": os.environ.get("TENCENT_COS_BUCKET", ""),
    }


def main():
    parser = argparse.ArgumentParser(prog="Alios-to-Tencent-Porter")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scrape_p = sub.add_parser("scrape")
    scrape_p.add_argument("--base-url", default="https://mirrors.aliyun.com/alinux/3/image/")

    run_p = sub.add_parser("run")
    run_p.add_argument("--workdir", default=None)
    run_p.add_argument("--cos-prefix", default="wzy/")
    run_p.add_argument("--skip-surgery", action="store_true")
    run_p.add_argument("--quick-import", action="store_true")
    run_p.add_argument("--wait", action="store_true")
    run_p.add_argument("--wait-timeout", type=int, default=6 * 3600)
    run_p.add_argument("--wait-interval", type=int, default=60)

    args = parser.parse_args()
    if args.cmd == "scrape":
        scraper = ScraperModule(base_url=args.base_url)
        print(scraper.find_latest_image())
        return

    config = load_config_from_env()
    if not all([config["TENCENT_SECRET_ID"], config["TENCENT_SECRET_KEY"], config["TENCENT_COS_BUCKET"]]):
        raise SystemExit("Missing required environment variables. Please check your .env file.")

    porter = AliosToTencentPorter(config, workdir=args.workdir)
    porter.run(
        cos_prefix=args.cos_prefix,
        skip_surgery=args.skip_surgery,
        quick_import=args.quick_import,
        wait=args.wait,
        wait_timeout=args.wait_timeout,
        wait_interval=args.wait_interval,
    )


if __name__ == "__main__":
    main()
