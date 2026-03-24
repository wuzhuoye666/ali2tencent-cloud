"""
阶段2：流式下载镜像文件
- 支持断点续传（Range 请求）
- SHA256 / MD5 完整性校验
- tqdm 进度显示
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import requests
from tqdm import tqdm

from core.config import Config
from core.logger import get_logger
from core.state import StateDB
from pipeline.context import PipelineContext

logger = get_logger("downloader")

_CHUNK = 1024 * 1024        # 1 MB 分块读取
_MAX_RETRY = 3
_TIMEOUT = (10, 60)          # (connect, read) 超时


def run(ctx: PipelineContext, config: Config, db: StateDB) -> None:
    """流水线阶段入口：下载镜像到 tmp/ 目录，填充 ctx.local_file_path 和 ctx.file_sha256。"""
    if not ctx.download_url:
        raise ValueError("ctx.download_url 为空，请先运行 monitor 阶段")

    tmp_dir = Path(config.tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    file_name = _url_to_filename(ctx.download_url)
    local_path = str(tmp_dir / file_name)

    logger.info("下载镜像: %s -> %s", ctx.download_url, local_path)
    sha256 = download_file(ctx.download_url, local_path)

    ctx.local_file_path = local_path
    ctx.modified_file_path = local_path   # 若无 modify 阶段则直接使用原文件
    ctx.file_sha256 = sha256

    # 尝试下载 checksum 文件并校验
    if ctx.image_info and ctx.image_info.checksum_url:
        _verify_checksum(local_path, sha256, ctx.image_info.checksum_url)

    logger.info("下载完成: %s (sha256=%s...)", local_path, sha256[:16])


def download_file(url: str, dest: str, checksum: str = "") -> str:
    """
    流式下载文件，支持断点续传。
    返回下载文件的 SHA256 摘要。
    """
    dest_path = Path(dest)
    resume_pos = dest_path.stat().st_size if dest_path.exists() else 0

    for attempt in range(_MAX_RETRY):
        try:
            headers = {}
            if resume_pos > 0:
                headers["Range"] = f"bytes={resume_pos}-"
                logger.info("断点续传，从 %d 字节继续", resume_pos)

            resp = requests.get(url, headers=headers, stream=True, timeout=_TIMEOUT)

            # 服务器不支持 Range 时重新下载
            if resume_pos > 0 and resp.status_code == 200:
                logger.warning("服务器不支持断点续传，重新下载")
                resume_pos = 0
                dest_path.unlink(missing_ok=True)

            resp.raise_for_status()

            total = int(resp.headers.get("Content-Length", 0)) + resume_pos
            mode = "ab" if resume_pos > 0 else "wb"

            sha256 = hashlib.sha256()
            # 如果是续传，先对已下载部分做 hash
            if resume_pos > 0:
                with open(dest, "rb") as f:
                    while chunk := f.read(_CHUNK):
                        sha256.update(chunk)

            with open(dest, mode) as f, tqdm(
                total=total,
                initial=resume_pos,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=dest_path.name,
            ) as bar:
                for chunk in resp.iter_content(chunk_size=_CHUNK):
                    if chunk:
                        f.write(chunk)
                        sha256.update(chunk)
                        bar.update(len(chunk))

            digest = sha256.hexdigest()
            if checksum and checksum.lower() != digest:
                raise ValueError(f"SHA256 校验失败：期望 {checksum}，实际 {digest}")
            return digest

        except (requests.RequestException, OSError) as e:
            logger.warning("下载失败 (attempt %d/%d): %s", attempt + 1, _MAX_RETRY, e)
            if attempt < _MAX_RETRY - 1:
                time.sleep(2 ** attempt)
                resume_pos = Path(dest).stat().st_size if Path(dest).exists() else 0
            else:
                raise

    raise RuntimeError("下载失败，已达最大重试次数")


def _verify_checksum(local_path: str, actual_sha256: str, checksum_url: str) -> None:
    """下载并解析 checksum 文件，验证本地文件完整性。"""
    try:
        resp = requests.get(checksum_url, timeout=30)
        resp.raise_for_status()
        text = resp.text.strip()
        file_name = Path(local_path).name
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and file_name in parts[-1]:
                expected = parts[0].lower()
                if expected != actual_sha256.lower():
                    raise ValueError(
                        f"官方 checksum 不匹配！期望={expected}, 实际={actual_sha256[:16]}..."
                    )
                logger.info("Checksum 校验通过")
                return
        logger.warning("checksum 文件中未找到 %s 的记录，跳过校验", file_name)
    except requests.RequestException as e:
        logger.warning("无法下载 checksum 文件: %s", e)


def _url_to_filename(url: str) -> str:
    name = url.split("/")[-1].split("?")[0]
    return name if name else "image.qcow2"
