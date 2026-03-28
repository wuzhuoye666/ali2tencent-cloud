#!/usr/bin/env python3
"""
ali2tencent-cloud  --  阿里云镜像自动迁移到腾讯云流水线

用法：
    python main.py run                  # 检测新版本并执行完整流程
    python main.py run --version 3      # 对指定版本执行完整流程
    python main.py run --stage upload   # 从指定阶段恢复执行
    python main.py daemon               # 守护模式，定时检测新版本
    python main.py status               # 查看流水线任务状态
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 将项目根目录加入 sys.path，兼容直接运行
_ROOT = Path(__file__).parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.config import get_config
from core.logger import setup_logger, get_logger
from core.state import StateDB


def cmd_run(args: argparse.Namespace) -> int:
    config = get_config()
    setup_logger("ali2tencent", config.log_dir)
    logger = get_logger("main")
    db = StateDB(config.state_db)

    from pipeline.pipeline import Pipeline
    pipeline = Pipeline(config=config, db=db)

    if args.version:
        versions = [args.version]
    else:
        # 检测新版本
        from pipeline.monitor import detect_new_versions
        versions = detect_new_versions(config, db)
        if not versions:
            logger.info("没有发现新版本，退出")
            return 0
        logger.info("发现 %d 个新版本: %s", len(versions), [v.version for v in versions])

    stop = getattr(args, "stop_stage", None)
    ok = True
    for v in versions:
        ver_str = v.version if hasattr(v, "version") else v
        if args.stage:
            ok = pipeline.run_from_stage(ver_str, args.stage, stop_stage=stop) and ok
        else:
            ok = pipeline.run(ver_str, stop_stage=stop) and ok

    return 0 if ok else 1


def cmd_daemon(args: argparse.Namespace) -> int:
    config = get_config()
    setup_logger("ali2tencent", config.log_dir)
    logger = get_logger("main")
    logger.info("启动守护模式，间隔 %d 小时", config.check_interval_hours)

    from core.scheduler import start_daemon
    from core.state import StateDB
    from pipeline.pipeline import Pipeline
    from pipeline.monitor import detect_new_versions

    db = StateDB(config.state_db)
    pipeline = Pipeline(config=config, db=db)

    def job():
        versions = detect_new_versions(config, db)
        if not versions:
            logger.info("未发现新版本")
            return
        for v in versions:
            pipeline.run(v.version)

    start_daemon(job, config.check_interval_hours)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = get_config()
    setup_logger("ali2tencent", config.log_dir)
    db = StateDB(config.state_db)

    tasks = db.get_tasks_by_version(args.version) if args.version else []
    if not args.version:
        # 显示所有版本最近状态
        import sqlite3, json
        conn = sqlite3.connect(config.state_db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM pipeline_tasks ORDER BY started_at DESC LIMIT 50"
        ).fetchall()
        conn.close()
        tasks = [dict(r) for r in rows]

    if not tasks:
        print("暂无任务记录")
        return 0

    print(f"{'task_id':<38} {'version':<12} {'stage':<12} {'status':<10} {'started_at':<22} {'error'}")
    print("-" * 120)
    for t in tasks:
        err = (t.get("error_msg") or "")[:60].replace("\n", " ")
        print(f"{t['task_id']:<38} {t['version']:<12} {t['stage']:<12} {t['status']:<10} "
              f"{str(t.get('started_at','')):<22} {err}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ali2tencent",
        description="阿里云镜像自动迁移到腾讯云流水线",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = sub.add_parser("run", help="执行流水线")
    p_run.add_argument("--version", "-v", help="指定版本号（如 3），不指定则自动检测新版本")
    p_run.add_argument("--stage", "-s", help="从指定阶段开始（monitor/download/modify/upload/import）")
    p_run.add_argument("--stop-stage", help="在指定阶段结束（含）")
    p_run.set_defaults(func=cmd_run)

    # daemon
    p_daemon = sub.add_parser("daemon", help="守护模式，定时检测并执行")
    p_daemon.set_defaults(func=cmd_daemon)

    # status
    p_status = sub.add_parser("status", help="查看任务状态")
    p_status.add_argument("--version", "-v", help="按版本过滤")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
