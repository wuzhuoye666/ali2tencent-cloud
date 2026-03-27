"""
阶段4：上传镜像到腾讯云 COS
- 使用 cos-python-sdk-v5 分片上传（SDK 内置自动分片与断点续传）
- 返回对象 URL 供后续阶段使用
"""
from __future__ import annotations

import os
from pathlib import Path

from qcloud_cos import CosConfig, CosS3Client

from core.config import Config
from core.logger import get_logger
from core.state import StateDB
from pipeline.context import PipelineContext

logger = get_logger("cos_uploader")


def run(ctx: PipelineContext, config: Config, db: StateDB) -> None:
    """流水线阶段入口：上传镜像到 COS，填充 ctx.cos_object_key 和 ctx.cos_object_url。
    
    上传完成后生成预签名 URL（有效期 24h），供 ImportImage API 使用。
    """
    src = ctx.modified_file_path or ctx.local_file_path
    if not src or not Path(src).exists():
        raise FileNotFoundError(f"待上传镜像文件不存在: {src}")

    object_key = f"images/{ctx.version}/{Path(src).name}"
    logger.info("上传到 COS: %s -> %s/%s", src, config.tencent_cos_bucket, object_key)

    cos_config = CosConfig(
        Region=config.tencent_region,
        SecretId=config.tencent_secret_id,
        SecretKey=config.tencent_secret_key,
        Scheme="https",
        Timeout=1200,
    )
    client = CosS3Client(cos_config)

    # 检查文件是否已上传（断点续传跳过）
    already_uploaded = False
    try:
        head = client.head_object(Bucket=config.tencent_cos_bucket, Key=object_key)
        remote_size = int(head.get("Content-Length", 0))
        local_size = Path(src).stat().st_size
        if remote_size == local_size:
            logger.info("COS 上已有同名同大小文件，跳过上传")
            already_uploaded = True
    except Exception:
        pass

    if not already_uploaded:
        upload_file(
            src=src,
            bucket=config.tencent_cos_bucket,
            object_key=object_key,
            region=config.tencent_region,
            secret_id=config.tencent_secret_id,
            secret_key=config.tencent_secret_key,
        )

    # 生成预签名 URL（有效期 86400 秒 = 24 小时），ImportImage 需要有签名的 URL
    signed_url = client.get_presigned_download_url(
        Bucket=config.tencent_cos_bucket,
        Key=object_key,
        Expired=86400,
    )

    ctx.cos_object_key = object_key
    ctx.cos_object_url = signed_url
    logger.info("COS 上传完成，预签名 URL: %s...（有效期24h）", signed_url[:80])


def upload_file(
    src: str,
    bucket: str,
    object_key: str,
    region: str,
    secret_id: str,
    secret_key: str,
) -> str:
    """
    分片上传文件到 COS，返回对象 URL。
    SDK 内置 upload_file 方法自动处理分片上传与断点续传。
    """
    cos_config = CosConfig(
        Region=region,
        SecretId=secret_id,
        SecretKey=secret_key,
        Scheme="https",
        Timeout=1200,         # 每次请求超时 1200 秒（大分片上传需要更长时间）
    )
    client = CosS3Client(cos_config)

    file_size = os.path.getsize(src)
    logger.info("文件大小: %.2f GB", file_size / (1024 ** 3))

    # SDK 的 upload_file 会自动根据文件大小选择普通上传或分片上传（默认阈值 20MB）
    response = client.upload_file(
        Bucket=bucket,
        LocalFilePath=src,
        Key=object_key,
        PartSize=20,          # 每个分片 20 MB（减小分片，降低单次超时风险）
        MAXThread=2,          # 并发线程数（降低并发减少连接压力）
        EnableMD5=False,      # 禁用 MD5 校验
    )

    logger.debug("COS upload response: %s", response)

    # 构造对象访问 URL（私有读，后续通过 ImportImage 使用）
    cos_url = f"https://{bucket}.cos.{region}.myqcloud.com/{object_key}"
    return cos_url


def delete_object(bucket: str, object_key: str, region: str,
                  secret_id: str, secret_key: str) -> None:
    """清理 COS 上的临时镜像文件（可选）。"""
    cos_config = CosConfig(Region=region, SecretId=secret_id, SecretKey=secret_key)
    client = CosS3Client(cos_config)
    client.delete_object(Bucket=bucket, Key=object_key)
    logger.info("已删除 COS 对象: %s", object_key)
