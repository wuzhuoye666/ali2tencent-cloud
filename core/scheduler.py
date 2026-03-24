"""定时调度模块，基于 schedule 库，按配置间隔触发流水线。"""
from __future__ import annotations

import time
from typing import Callable

import schedule

from core.logger import get_logger

logger = get_logger("scheduler")


def start_daemon(job_fn: Callable[[], None], interval_hours: int = 6) -> None:
    """
    启动定时调度守护进程。

    :param job_fn: 每次触发时执行的回调函数（无参数）
    :param interval_hours: 检测间隔（小时）
    """
    logger.info("调度器启动，检测间隔：%d 小时", interval_hours)

    # 立即执行一次
    logger.info("首次立即执行...")
    _safe_run(job_fn)

    schedule.every(interval_hours).hours.do(_safe_run, job_fn)

    while True:
        schedule.run_pending()
        time.sleep(60)


def _safe_run(fn: Callable[[], None]) -> None:
    try:
        fn()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        logger.error("调度任务执行异常: %s", exc, exc_info=True)
