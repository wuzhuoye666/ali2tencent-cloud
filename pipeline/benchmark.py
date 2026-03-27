"""
阶段7：SSH 性能基准测试
- 连接 CVM 实例（密钥或密码）
- 上传 benchmark.sh 并执行
- 解析 JSON 结果写入 ctx.benchmark_result
- 测试完成后（非 keep_instance 模式）销毁实例
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

import paramiko

from core.config import Config
from core.logger import get_logger
from core.state import StateDB
from pipeline.context import PipelineContext

logger = get_logger("benchmark")

_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "benchmark.sh"
_REMOTE_SCRIPT = "/tmp/ali2tencent_bench.sh"
_RETRY_SSH = 10          # SSH 连接重试次数
_SSH_TIMEOUT = 30        # SSH 超时（秒）


def run(ctx: PipelineContext, config: Config, db: StateDB) -> None:
    """流水线阶段入口：执行性能测试，填充 ctx.benchmark_result，可选销毁实例。"""
    if not ctx.instance_ip:
        raise ValueError("ctx.instance_ip 为空，请先运行 launch 阶段")

    logger.info("连接实例 %s (%s)，执行性能测试...", ctx.instance_id, ctx.instance_ip)

    result = run_benchmark(
        host=ctx.instance_ip,
        user=config.ssh_user,
        password=config.cvm_login_password,
        private_key_path=config.ssh_private_key_path or None,
        timeout=config.benchmark_timeout,
    )
    ctx.benchmark_result = result

    # 保存到数据库
    db.save_benchmark(ctx.task_id, ctx.version, ctx.instance_id, result)
    logger.info("性能测试完成: cpu=%.1f mem=%.1f disk_r=%.1f disk_w=%.1f net=%.1f",
                result.get("cpu_score", 0), result.get("mem_score", 0),
                result.get("disk_read_mb", 0), result.get("disk_write_mb", 0),
                result.get("net_bandwidth_mb", 0))

    # 销毁实例（除非 keep_instance=true）
    if not config.keep_instance and ctx.instance_id:
        from pipeline.cvm_launcher import terminate_instance
        logger.info("销毁测试实例: %s", ctx.instance_id)
        terminate_instance(config, ctx.instance_id)


def run_benchmark(
    host: str,
    user: str,
    password: str = "",
    private_key_path: str | None = None,
    timeout: int = 300,
) -> dict:
    """
    SSH 连接主机，执行 benchmark 脚本，返回结构化结果 dict。
    """
    client = _connect_ssh(host, user, password, private_key_path)
    try:
        # 上传脚本
        _upload_script(client, str(_SCRIPT_PATH), _REMOTE_SCRIPT)
        # 执行脚本
        stdout, stderr, exit_code = _exec(client, f"bash {_REMOTE_SCRIPT}", timeout=timeout)

        if exit_code != 0:
            logger.warning("benchmark 脚本退出码: %d  stderr: %s", exit_code, stderr[:300])

        # 尝试解析 JSON 输出
        return _parse_output(stdout)
    finally:
        client.close()


def _connect_ssh(host: str, user: str, password: str, private_key_path: str | None) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    pkey = None
    if private_key_path and Path(private_key_path).exists():
        pkey = paramiko.RSAKey.from_private_key_file(private_key_path)

    for attempt in range(_RETRY_SSH):
        try:
            kwargs = dict(
                hostname=host, port=22, username=user,
                timeout=_SSH_TIMEOUT, banner_timeout=60,
            )
            if pkey:
                kwargs["pkey"] = pkey
            else:
                kwargs["password"] = password
                kwargs["look_for_keys"] = False

            client.connect(**kwargs)
            logger.info("SSH 连接成功: %s@%s", user, host)
            return client
        except Exception as e:
            logger.warning("SSH 连接失败 (attempt %d/%d): %s", attempt + 1, _RETRY_SSH, e)
            if attempt < _RETRY_SSH - 1:
                time.sleep(15)

    raise ConnectionError(f"SSH 连接 {host} 失败，已重试 {_RETRY_SSH} 次")


def _upload_script(client: paramiko.SSHClient, local: str, remote: str) -> None:
    sftp = client.open_sftp()
    try:
        sftp.put(local, remote)
        sftp.chmod(remote, 0o755)
    finally:
        sftp.close()


def _exec(client: paramiko.SSHClient, cmd: str, timeout: int = 300) -> tuple[str, str, int]:
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    return stdout.read().decode("utf-8", errors="replace"), \
           stderr.read().decode("utf-8", errors="replace"), \
           exit_code


def _parse_output(stdout: str) -> dict:
    """从脚本输出中提取 JSON 块。"""
    # 脚本输出最后一行应为 JSON
    lines = stdout.strip().splitlines()
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass

    # 找不到 JSON，返回原始文本
    logger.warning("无法解析 benchmark JSON 输出，返回原始文本")
    return {"raw_output": stdout, "cpu_score": 0, "mem_score": 0,
            "disk_read_mb": 0, "disk_write_mb": 0, "net_bandwidth_mb": 0}
