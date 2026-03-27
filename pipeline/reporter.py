"""
阶段8：汇总报告生成
- 读取 state.db 中所有版本的 benchmark 结果与任务历史
- 使用 Jinja2 渲染 HTML 对比报告
- 同时输出 JSON 结构化数据
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from core.config import Config
from core.logger import get_logger
from core.state import StateDB
from pipeline.context import PipelineContext

logger = get_logger("reporter")

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_TEMPLATE_FILE = "report.html.j2"


def run(ctx: PipelineContext, config: Config, db: StateDB) -> None:
    """流水线阶段入口：生成 HTML 报告，填充 ctx.report_path。"""
    report_path = generate_report(ctx, config, db)
    ctx.report_path = report_path


def generate_report(ctx: PipelineContext, config: Config, db: StateDB) -> str:
    """读取所有数据并渲染报告，返回报告文件路径。"""
    report_dir = Path(config.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    # 获取 benchmark 结果（所有版本）
    all_benchmarks = db.get_all_benchmarks()

    # 解析 raw_json 获取 os/kernel 等信息
    enriched = []
    for row in all_benchmarks:
        entry = dict(row)
        if entry.get("raw_json"):
            try:
                raw = json.loads(entry["raw_json"])
                entry.setdefault("os_release", raw.get("os_release", ""))
                entry.setdefault("kernel", raw.get("kernel", ""))
            except Exception:
                pass
        enriched.append(entry)

    # 获取任务历史
    tasks = _get_recent_tasks(db, config)

    # 计算各指标最大值（用于高亮显示）
    def _max(field: str) -> float:
        vals = [r.get(field) or 0 for r in enriched]
        return max(vals) if vals else 0

    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # 渲染 HTML
    env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)
    template = env.get_template(_TEMPLATE_FILE)
    html = template.render(
        results=enriched,
        tasks=tasks,
        generated_at=now_str,
        max_cpu=_max("cpu_score"),
        max_mem=_max("mem_score"),
        max_disk_r=_max("disk_read_mb"),
        max_disk_w=_max("disk_write_mb"),
        max_net=_max("net_bandwidth_mb"),
    )

    # 写 HTML 报告
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    html_path = str(report_dir / f"report_{ts}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    # 写 JSON 数据
    json_path = str(report_dir / f"report_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {"generated_at": now_str, "benchmarks": enriched, "tasks": tasks},
            f, ensure_ascii=False, indent=2,
        )

    # 写 latest 快捷方式
    latest_html = str(report_dir / "report_latest.html")
    with open(latest_html, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info("报告已生成: %s", html_path)
    return html_path


def _get_recent_tasks(db: StateDB, config: Config) -> list[dict]:
    import sqlite3
    try:
        conn = sqlite3.connect(config.state_db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM pipeline_tasks ORDER BY started_at DESC LIMIT 200"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []
