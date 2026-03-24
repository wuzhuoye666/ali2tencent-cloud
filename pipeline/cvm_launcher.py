"""
阶段6：创建 CVM 实例
- 调用 RunInstances API 购买按量计费实例
- 等待实例进入 RUNNING 状态，获取公网 IP
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

logger = get_logger("cvm_launcher")

_POLL_INTERVAL = 15     # 轮询间隔（秒）
_MAX_WAIT = 600         # 最长等待启动时间（秒）


def run(ctx: PipelineContext, config: Config, db: StateDB) -> None:
    """流水线阶段入口：创建 CVM 实例，填充 ctx.instance_id 和 ctx.instance_ip。"""
    if not ctx.image_id:
        raise ValueError("ctx.image_id 为空，请先运行 import 阶段")

    client = _make_client(config)

    instance_id, instance_ip = create_instance(
        client=client,
        config=config,
        image_id=ctx.image_id,
        instance_name=f"ali2tencent-bench-{ctx.version}",
    )

    ctx.instance_id = instance_id
    ctx.instance_ip = instance_ip
    logger.info("CVM 实例已就绪: %s  IP=%s", instance_id, instance_ip)


def create_instance(
    client: cvm_client.CvmClient,
    config: Config,
    image_id: str,
    instance_name: str = "ali2tencent-bench",
) -> tuple[str, str]:
    """
    创建 CVM 实例，返回 (instance_id, public_ip)。
    按量计费，测试完成后会自动销毁。
    """
    req = models.RunInstancesRequest()
    req.InstanceChargeType = "POSTPAID_BY_HOUR"   # 按量计费

    req.Placement = models.Placement()
    req.Placement.Zone = f"{config.tencent_region}-1"

    req.InstanceType = config.cvm_instance_type
    req.ImageId = image_id
    req.InstanceName = instance_name

    # 系统盘
    req.SystemDisk = models.SystemDisk()
    req.SystemDisk.DiskType = "CLOUD_PREMIUM"
    req.SystemDisk.DiskSize = config.cvm_disk_size

    # 网络：按需分配公网 IP
    req.InternetAccessible = models.InternetAccessible()
    req.InternetAccessible.InternetChargeType = "TRAFFIC_POSTPAID_BY_HOUR"
    req.InternetAccessible.InternetMaxBandwidthOut = 10
    req.InternetAccessible.PublicIpAssigned = True

    # 登录方式
    req.LoginSettings = models.LoginSettings()
    req.LoginSettings.Password = config.cvm_login_password

    # VPC 配置（可选）
    if config.cvm_vpc_id and config.cvm_subnet_id:
        req.VirtualPrivateCloud = models.VirtualPrivateCloud()
        req.VirtualPrivateCloud.VpcId = config.cvm_vpc_id
        req.VirtualPrivateCloud.SubnetId = config.cvm_subnet_id

    if config.cvm_security_group_id:
        req.SecurityGroupIds = [config.cvm_security_group_id]

    req.InstanceCount = 1

    try:
        logger.info("创建 CVM 实例: type=%s image=%s", config.cvm_instance_type, image_id)
        resp = client.RunInstances(req)
        instance_id = resp.InstanceIdSet[0]
        logger.info("实例已创建: %s，等待 RUNNING 状态...", instance_id)
    except TencentCloudSDKException as e:
        raise RuntimeError(f"RunInstances API 调用失败: {e}") from e

    # 轮询等待 RUNNING
    instance_ip = _wait_for_running(client, instance_id)
    return instance_id, instance_ip


def _wait_for_running(client: cvm_client.CvmClient, instance_id: str) -> str:
    waited = 0
    while waited < _MAX_WAIT:
        state, ip = _describe_instance(client, instance_id)
        logger.info("实例 %s 状态: %s (已等待 %ds)", instance_id, state, waited)

        if state == "RUNNING" and ip:
            return ip
        if state in ("LAUNCH_FAILED", "TERMINATING", "TERMINATED"):
            raise RuntimeError(f"实例启动失败，状态: {state}")

        time.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL

    raise TimeoutError(f"实例 {instance_id} 启动超时（已等待 {_MAX_WAIT}s）")


def _describe_instance(client: cvm_client.CvmClient, instance_id: str) -> tuple[str, str]:
    req = models.DescribeInstancesRequest()
    req.InstanceIds = [instance_id]
    try:
        resp = client.DescribeInstances(req)
        if resp.InstanceSet:
            inst = resp.InstanceSet[0]
            ip = inst.PublicIpAddresses[0] if inst.PublicIpAddresses else ""
            return inst.InstanceState, ip
        return "UNKNOWN", ""
    except TencentCloudSDKException as e:
        logger.warning("DescribeInstances 失败: %s", e)
        return "UNKNOWN", ""


def terminate_instance(config: Config, instance_id: str) -> None:
    """销毁 CVM 实例（测试完成后调用）。"""
    client = _make_client(config)
    req = models.TerminateInstancesRequest()
    req.InstanceIds = [instance_id]
    try:
        client.TerminateInstances(req)
        logger.info("实例 %s 已提交销毁请求", instance_id)
    except TencentCloudSDKException as e:
        logger.error("销毁实例失败: %s", e)


def _make_client(config: Config) -> cvm_client.CvmClient:
    cred = credential.Credential(config.tencent_secret_id, config.tencent_secret_key)
    return cvm_client.CvmClient(cred, config.tencent_region)
