"""
阶段1：阿里云镜像版本检测
- 抓取阿里云镜像发布页面
- 解析可用版本列表（含下载 URL）
- 与 state.db 已知版本比对，返回新版本列表

支持的页面类型：
  1. https://mirrors.aliyun.com/alinux/image/  — ALinux 2 HTML 表格页（href 为绝对 OSS URL）
  2. Apache/Nginx 目录列表页（href 为相对路径 .qcow2/.vhd 等）
  3. 版本目录列表页（自动进入子目录探测）
"""
from __future__ import annotations

import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from core.config import Config
from core.logger import get_logger
from core.state import StateDB
from pipeline.context import ImageVersion, PipelineContext

logger = get_logger("monitor")

# 请求超时与重试
_TIMEOUT = 30
_MAX_RETRY = 3

# 支持的镜像文件扩展名（优先级：qcow2 > vhd > raw > img）
_IMAGE_EXTS = (".qcow2", ".vhd", ".raw", ".img")
_PREFER_EXT = ".qcow2"

# 版本号正则1：纯点号分隔，如 3.2, 2.1903, 3.2.0
_VERSION_RE = re.compile(r"\b(\d+\.\d+(?:\.\d+)*)\b")

# 版本号正则2：阿里云文件名格式
# 格式1: aliyun_2_1903_x64 -> 2.1903 (ALinux 2)
# 格式2: aliyun_3_x64_20G_nocloud -> 3 (ALinux 3)
_ALINUX_FNAME_RE = re.compile(
    r"(?:aliyun|alinux)[_-](\d+)(?:[_-](\d+))?[_-]",
    re.IGNORECASE,
)

# 已知的阿里云 ALinux 镜像 OSS 域名前缀
_KNOWN_OSS_PREFIXES = (
    "https://alinux2.oss-cn-hangzhou.aliyuncs.com/",
    "https://alinux3.oss-cn-hangzhou.aliyuncs.com/",
)


def run(ctx: PipelineContext, config: Config, db: StateDB) -> None:
    """流水线阶段入口：检测新版本，填充 ctx.image_info。
    
    若 ctx.version 已指定（来自 --version 参数），则从 DB 恢复已知版本信息，
    不强制要求是"新版本"。
    """
    # 若 ctx.version 已指定，先尝试从 DB 恢复
    if ctx.version:
        ver_info = db.get_version_info(ctx.version) if hasattr(db, "get_version_info") else None
        if ver_info and ver_info.get("download_url"):
            ctx.download_url = ver_info["download_url"]
            ctx.image_info = ImageVersion(
                version=ctx.version,
                name=ver_info.get("name", ""),
                download_url=ver_info["download_url"],
                os_type=ver_info.get("os_type", "linux"),
            )
            logger.info("使用已知版本 %s，下载地址: %s", ctx.version, ctx.download_url)
            return

    # 否则检测新版本
    versions = detect_new_versions(config, db)
    if not versions:
        # 尝试从 DB 里找未处理版本
        unprocessed = db.get_unprocessed_versions()
        if unprocessed:
            v = unprocessed[0]
            ctx.version = v["version"]
            ctx.download_url = v["download_url"] or ""
            ctx.image_info = ImageVersion(
                version=v["version"],
                name=v.get("name", ""),
                download_url=v.get("download_url", ""),
                os_type=v.get("os_type", "linux"),
            )
            logger.info("使用未处理版本 %s，下载地址: %s", ctx.version, ctx.download_url)
            return
        raise RuntimeError("未发现新版本，流水线终止（可用 --version 指定版本强制执行）")

    # 取第一个（最新）版本
    ctx.image_info = versions[0]
    ctx.version = versions[0].version
    ctx.download_url = versions[0].download_url
    logger.info("选取版本 %s，下载地址: %s", ctx.version, ctx.download_url)


def detect_new_versions(config: Config, db: StateDB) -> list[ImageVersion]:
    """
    检测阿里云镜像页面，返回未处理过的新版本列表。
    """
    url = config.ali_image_doc_url
    logger.info("检测阿里云镜像文档: %s", url)

    html = _fetch(url)
    if not html:
        logger.error("无法获取页面内容: %s", url)
        return []

    candidates = _parse_versions(html, base_url=url)
    if not candidates:
        logger.warning("未解析到任何镜像版本，尝试从 /alinux/image/ 探测...")
        # 尝试已知的固定镜像页
        fallback_urls = [
            "https://mirrors.aliyun.com/alinux/image/",
        ]
        for fb_url in fallback_urls:
            if fb_url == url:
                continue
            fb_html = _fetch(fb_url)
            if fb_html:
                candidates = _parse_versions(fb_html, base_url=fb_url)
                if candidates:
                    logger.info("通过 %s 解析到 %d 个版本", fb_url, len(candidates))
                    break

    if not candidates:
        logger.warning("所有页面均未解析到镜像版本，请检查 ALI_IMAGE_DOC_URL 配置")
        return []

    known = db.get_known_versions()
    new_versions: list[ImageVersion] = []

    for img in candidates:
        if img.version not in known:
            is_new = db.add_version(img.version, img.name, img.download_url, img.os_type)
            if is_new:
                new_versions.append(img)
                logger.info("新版本: %s  url=%s", img.version, img.download_url)
        else:
            logger.debug("已知版本跳过: %s", img.version)

    logger.info("共发现 %d 个新版本", len(new_versions))
    return new_versions


def _parse_versions(html: str, base_url: str) -> list[ImageVersion]:
    """解析 HTML，提取镜像版本与下载链接。支持多种页面格式。"""
    soup = BeautifulSoup(html, "html.parser")
    results: list[ImageVersion] = []

    # ---- 策略1：任意 <a href> 指向镜像文件（含绝对 URL 和相对路径）----
    for a in soup.find_all("a", href=True):
        href: str = a["href"].strip()

        # 跳过锚点、父目录、非文件链接
        if href in ("", ".", "..") or href.startswith("#"):
            continue

        # 忽略非镜像文件的纯文件名（如 seed.img 仅作为 seed，不是系统镜像）
        lower_href = href.lower()
        if "seed.img" in lower_href:
            continue

        # 判断是否为镜像文件链接
        if not any(lower_href.endswith(ext) for ext in _IMAGE_EXTS):
            continue

        # 构造完整 URL
        if href.startswith("http://") or href.startswith("https://"):
            full_url = href
        else:
            full_url = urljoin(base_url, href)

        # 提取版本号：从文件名中解析
        filename = full_url.split("/")[-1].split("?")[0]
        version = _extract_version(filename)
        if not version:
            version = _extract_version(full_url)
        if not version:
            logger.debug("无法从链接提取版本号，跳过: %s", full_url)
            continue

        name = filename
        arch = "aarch64" if "aarch64" in lower_href else "x86_64"

        # 尝试找对应的 checksum 文件链接
        checksum_url = _find_checksum_url(soup, base_url, filename)

        results.append(ImageVersion(
            version=version,
            name=name,
            download_url=full_url,
            checksum_url=checksum_url,
            os_type="linux",
            arch=arch,
        ))

    if results:
        logger.info("策略1（直接文件链接）解析到 %d 个条目", len(results))
        return _deduplicate(results)

    # ---- 策略2：目录列表页，href 指向版本子目录，递归进入 ----
    version_dirs: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].rstrip("/")
        if href in (".", "..") or not href:
            continue
        # 跳过外部链接
        if href.startswith("http") and not href.startswith(base_url.rstrip("/")):
            continue
        m = _VERSION_RE.search(href)
        if m:
            dir_url = urljoin(base_url, a["href"])
            if not dir_url.endswith("/"):
                dir_url += "/"
            version_dirs.append((m.group(1), dir_url))

    if version_dirs:
        logger.info("策略2：发现 %d 个版本目录，进入探测...", len(version_dirs))
        for ver, dir_url in version_dirs[:20]:
            sub_html = _fetch(dir_url)
            if not sub_html:
                continue
            sub_results = _parse_versions(sub_html, base_url=dir_url)
            # 覆盖子目录中解析到的版本（以顶层目录版本号为准）
            for r in sub_results:
                r.version = ver
            results.extend(sub_results)

    return _deduplicate(results)


def _find_checksum_url(soup: BeautifulSoup, base_url: str, filename: str) -> str:
    """在页面中查找 SHA256SUM / checksum 文件链接。"""
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        if "sha256" in href or "checksum" in href or "md5" in href:
            full = a["href"] if a["href"].startswith("http") else urljoin(base_url, a["href"])
            return full
    return ""


def _fetch(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ali2tencent-monitor/1.0)",
        "Accept": "text/html,application/xhtml+xml,*/*",
    }
    for attempt in range(_MAX_RETRY):
        try:
            resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
            resp.raise_for_status()
            # 强制用 utf-8 解码，避免 apparent_encoding 检测错误
            try:
                return resp.content.decode("utf-8")
            except UnicodeDecodeError:
                return resp.content.decode("latin-1")
        except requests.RequestException as e:
            logger.warning("请求失败 (attempt %d/%d): %s - %s", attempt + 1, _MAX_RETRY, url, e)
            if attempt < _MAX_RETRY - 1:
                time.sleep(2 ** attempt)
    return ""


def _extract_version(text: str) -> str:
    """从文件名或 URL 中提取版本号。
    
    支持格式：
    - 3.2, 2.1903, 3.2.0（点号分隔）
    - aliyun_2_1903_x64... -> "2.1903"（ALinux 2 格式）
    - aliyun_3_x64_20G_nocloud... -> "3"（ALinux 3 格式）
    """
    # 优先匹配阿里云文件名格式：aliyun_2_1903_ 或 aliyun_3_x64_
    m = _ALINUX_FNAME_RE.search(text)
    if m:
        major = m.group(1)
        minor = m.group(2)
        if minor:
            return f"{major}.{minor}"
        return major
    # 其次匹配点号版本号
    m = _VERSION_RE.search(text)
    return m.group(1) if m else ""


def _deduplicate(versions: list[ImageVersion]) -> list[ImageVersion]:
    """按版本号去重，优先保留 qcow2 格式，同格式保留第一个。"""
    seen: dict[str, ImageVersion] = {}
    for v in versions:
        key = v.version
        if key not in seen:
            seen[key] = v
        else:
            existing = seen[key]
            # qcow2 优先于 vhd/raw/img
            if (v.download_url.lower().endswith(".qcow2")
                    and not existing.download_url.lower().endswith(".qcow2")):
                seen[key] = v
    # 按版本倒序（最新在前）
    return sorted(seen.values(), key=lambda x: x.version, reverse=True)
