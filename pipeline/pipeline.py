"""流水线编排核心：按序调用各阶段，管理 PipelineContext 与状态持久化。"""
from __future__ import annotations

import traceback
from typing import Callable

from core.config import Config, get_config
from core.logger import get_logger
from core.state import StateDB
from pipeline.context import PipelineContext

logger = get_logger("pipeline")

# 阶段名称顺序（与实现模块对应）
STAGES = [
    "monitor",
    "download",
    "modify",
    "upload",
    "import",
    "launch",
    "benchmark",
    "report",
]

StageFunc = Callable[[PipelineContext, Config, StateDB], None]


class Pipeline:
    def __init__(self, config: Config | None = None, db: StateDB | None = None):
        self.config = config or get_config()
        self.db = db or StateDB(self.config.state_db)
        self._stages: dict[str, StageFunc] = {}
        self._register_stages()

    def _register_stages(self) -> None:
        """延迟导入各阶段模块，避免循环依赖，同时允许部分阶段缺失时跳过。"""
        from pipeline.monitor import run as monitor_run
        from pipeline.downloader import run as download_run
        from pipeline.image_modifier import run as modify_run
        from pipeline.cos_uploader import run as upload_run
        from pipeline.cvm_importer import run as import_run
        from pipeline.cvm_launcher import run as launch_run
        from pipeline.benchmark import run as benchmark_run
        from pipeline.reporter import run as report_run

        self._stages = {
            "monitor": monitor_run,
            "download": download_run,
            "modify": modify_run,
            "upload": upload_run,
            "import": import_run,
            "launch": launch_run,
            "benchmark": benchmark_run,
            "report": report_run,
        }

    def run(self, version: str, stop_stage: str | None = None) -> bool:
        """从头执行全部阶段。stop_stage 指定后只跑到该阶段（含）为止。"""
        return self._execute(version, start_stage="monitor", stop_stage=stop_stage)

    def run_from_stage(self, version: str, start_stage: str, stop_stage: str | None = None) -> bool:
        """从指定阶段恢复执行（用于断点续跑）。stop_stage 指定后只跑到该阶段（含）为止。"""
        if start_stage not in STAGES:
            raise ValueError(f"无效阶段名 {start_stage!r}，可选：{STAGES}")
        if stop_stage and stop_stage not in STAGES:
            raise ValueError(f"无效终止阶段名 {stop_stage!r}，可选：{STAGES}")
        return self._execute(version, start_stage=start_stage, stop_stage=stop_stage)

    def _execute(self, version: str, start_stage: str, stop_stage: str | None = None) -> bool:
        ctx = PipelineContext(version=version)

        # 恢复已完成阶段的 meta 到 ctx
        self._restore_context(ctx)

        start_idx = STAGES.index(start_stage)
        stop_idx = STAGES.index(stop_stage) if stop_stage else len(STAGES) - 1
        stages_to_run = STAGES[start_idx:stop_idx + 1]

        for stage in stages_to_run:
            fn = self._stages.get(stage)
            if fn is None:
                logger.warning("阶段 %s 未注册，跳过", stage)
                continue

            logger.info("▶ 开始阶段 [%s] version=%s task_id=%s", stage, version, ctx.task_id)
            self.db.upsert_task(ctx.task_id, version, stage, "running", meta=ctx.to_meta())

            try:
                fn(ctx, self.config, self.db)
                self.db.upsert_task(ctx.task_id, version, stage, "done", meta=ctx.to_meta())
                logger.info("✔ 阶段 [%s] 完成", stage)
            except Exception as exc:
                err = traceback.format_exc()
                logger.error("✘ 阶段 [%s] 失败: %s", stage, exc)
                self.db.upsert_task(ctx.task_id, version, stage, "failed",
                                    error_msg=err, meta=ctx.to_meta())
                return False

        self.db.mark_version_processed(version)
        logger.info("🎉 版本 %s 全流程完成", version)
        return True

    def _restore_context(self, ctx: PipelineContext) -> None:
        """从上次任务 meta 恢复 ctx 字段，支持断点续跑。"""
        # 先从 image_versions 表恢复版本基础信息（download_url 等）
        ver_info = self.db.get_version_info(ctx.version)
        if ver_info:
            if not ctx.download_url and ver_info.get("download_url"):
                ctx.download_url = ver_info["download_url"]
            if not ctx.image_info and ver_info.get("download_url"):
                from pipeline.context import ImageVersion
                ctx.image_info = ImageVersion(
                    version=ctx.version,
                    name=ver_info.get("name", ""),
                    download_url=ver_info["download_url"],
                    os_type=ver_info.get("os_type", "linux"),
                )

        # 从所有任务 meta 中聚合非空字段（不限制 status，按时间从旧到新覆盖）
        tasks = self.db.get_tasks_by_version(ctx.version)
        import json
        for task in tasks:
            meta = json.loads(task.get("meta") or "{}")
            for key, val in meta.items():
                if hasattr(ctx, key) and val:
                    setattr(ctx, key, val)
