"""
阶段5：导入自定义镜像到腾讯云
- 调用 CVM ImportImage API，以 COS URL 为镜像源
- 轮询 DescribeImages 直到导入完成或失败
- 最长等待 2 小时，每 60 秒轮询一次
"""
from __future__ import annotations

import time

from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.cvm.v20170312 import cvm_client, models

from core.config import Config
from core.logger import get_logger
from core.state import StateDB
from pipeline.context import PipelineContext

logger = get_logger("cvm_importer")

_POLL_INTERVAL = 60      # 轮询间隔（秒）
_MAX_WAIT = 7200         # 最长等待时间（秒，2 小时）


def run(ctx: PipelineContext, config: Config, db: StateDB) -> None:
    """流水线阶段入口：导入 COS 镜像为腾讯云自定义镜像，填充 ctx.image_id。"""
    if not ctx.cos_object_url:
        raise ValueError("ctx.cos_object_url 为空，请先运行 upload 阶段")

    client = _make_client(config)

    image_id = import_image(
        client=client,
        cos_url=ctx.cos_object_url,
        image_name=f"ali-{ctx.version}",
        image_desc=f"自动从阿里云迁移 version={ctx.version}",
        os_type=_detect_os_type(ctx),
        architecture="x86_64",
    )

    ctx.image_id = image_id
    logger.info("镜像导入完成: %s", image_id)


def import_image(
    client: cvm_client.CvmClient,
    cos_url: str,
    image_name: str,
    image_desc: str = "",
    os_type: str = "CentOS",
    architecture: str = "x86_64",
) -> str:
    """
    发起镜像导入请求，轮询等待完成，返回镜像 ID。
    """
    logger.info("发起 ImportImage 请求: %s", cos_url)

    req = models.ImportImageRequest()
    req.Architecture = architecture
    req.OsType = os_type
    req.OsVersion = "7"   # 通用版本号，ALinux2 基于 CentOS 7 系列
    req.ImageUrl = cos_url
    req.ImageName = image_name
    req.ImageDescription = image_desc
    req.DryRun = False
    req.Force = False

    try:
        resp = client.ImportImage(req)
        logger.info("ImportImage 提交成功，ImageId: %s", resp.ImageId)
        image_id = resp.ImageId
    except TencentCloudSDKException as e:
        raise RuntimeError(f"ImportImage API 调用失败: {e}") from e

    # 轮询等待导入完成
    _wait_for_image(client, image_id)
    return image_id


def _wait_for_image(client: cvm_client.CvmClient, image_id: str) -> None:
    """轮询 DescribeImages，直到镜像状态变为 NORMAL 或失败。"""
    waited = 0
    while waited < _MAX_WAIT:
        state = _get_image_state(client, image_id)
        logger.info("镜像 %s 状态: %s (已等待 %ds)", image_id, state, waited)

        if state == "NORMAL":
            return
        if state in ("ERROR", "DELETED"):
            raise RuntimeError(f"镜像导入失败，状态: {state}")

        time.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL

    raise TimeoutError(f"镜像导入超时（已等待 {_MAX_WAIT}s），镜像 ID: {image_id}")


def _get_image_state(client: cvm_client.CvmClient, image_id: str) -> str:
    req = models.DescribeImagesRequest()
    req.ImageIds = [image_id]
    try:
        resp = client.DescribeImages(req)
        if resp.ImageSet:
            return resp.ImageSet[0].ImageState
        return "UNKNOWN"
    except TencentCloudSDKException as e:
        logger.warning("DescribeImages 失败: %s", e)
        return "UNKNOWN"


def _make_client(config: Config) -> cvm_client.CvmClient:
    cred = credential.Credential(config.tencent_secret_id, config.tencent_secret_key)
    return cvm_client.CvmClient(cred, config.tencent_region)


def _detect_os_type(ctx: PipelineContext) -> str:
    """根据镜像名称猜测 OS 类型（腾讯云 ImportImage 需要该参数）。
    
    腾讯云支持的 OsType 枚举值：
    CentOS, Ubuntu, Debian, Windows, OpenSUSE, SUSE, CoreOS, FreeBSD, Other Linux
    """
    name = (ctx.image_info.name if ctx.image_info else "").lower()
    if "centos" in name:
        return "CentOS"
    if "ubuntu" in name:
        return "Ubuntu"
    if "debian" in name:
        return "Debian"
    if "alinux" in name or "alibaba" in name or "alios" in name or "aliyun" in name:
        return "CentOS"   # AliOS/ALinux 基于 RHEL/CentOS，选 CentOS 兼容性最好
    if "opensuse" in name:
        return "OpenSUSE"
    if "suse" in name:
        return "SUSE"
    return "Other Linux"   # 默认用 "Other Linux" 而非 "Linux"
