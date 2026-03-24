"""SQLite 状态管理：记录镜像版本、流水线任务、性能测试结果。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator


DDL = """
CREATE TABLE IF NOT EXISTS image_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     TEXT    NOT NULL UNIQUE,
    name        TEXT,
    download_url TEXT,
    os_type     TEXT,
    first_seen  TEXT    NOT NULL,
    processed   INTEGER NOT NULL DEFAULT 0  -- 0=未处理 1=已处理
);

CREATE TABLE IF NOT EXISTS pipeline_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT    NOT NULL UNIQUE,  -- UUID
    version      TEXT    NOT NULL,
    stage        TEXT    NOT NULL,         -- monitor/download/modify/upload/import/launch/benchmark/report
    status       TEXT    NOT NULL DEFAULT 'pending',  -- pending/running/done/failed
    started_at   TEXT,
    finished_at  TEXT,
    error_msg    TEXT,
    meta         TEXT                      -- JSON 附加信息（文件路径、资源ID等）
);

CREATE TABLE IF NOT EXISTS benchmark_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT    NOT NULL,
    version      TEXT    NOT NULL,
    instance_id  TEXT,
    tested_at    TEXT    NOT NULL,
    cpu_score    REAL,
    mem_score    REAL,
    disk_read_mb REAL,
    disk_write_mb REAL,
    net_bandwidth_mb REAL,
    raw_json     TEXT                      -- 完整测试结果 JSON
);
"""


class StateDB:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(DDL)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- image_versions ----

    def add_version(self, version: str, name: str, download_url: str, os_type: str = "linux") -> bool:
        """添加新版本记录，已存在则忽略。返回是否新增。"""
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO image_versions (version, name, download_url, os_type, first_seen) VALUES (?,?,?,?,?)",
                    (version, name, download_url, os_type, _now()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def get_known_versions(self) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT version FROM image_versions").fetchall()
        return {r["version"] for r in rows}

    def mark_version_processed(self, version: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE image_versions SET processed=1 WHERE version=?", (version,))

    def get_version_info(self, version: str) -> dict | None:
        """根据版本号查询版本信息（含 download_url）。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM image_versions WHERE version=?", (version,)
            ).fetchone()
        return dict(row) if row else None

    def get_unprocessed_versions(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM image_versions WHERE processed=0 ORDER BY first_seen ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- pipeline_tasks ----

    def upsert_task(self, task_id: str, version: str, stage: str, status: str,
                    error_msg: str = "", meta: dict | None = None) -> None:
        meta_str = json.dumps(meta or {}, ensure_ascii=False)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO pipeline_tasks (task_id, version, stage, status, started_at, meta)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                       status=excluded.status,
                       finished_at=CASE WHEN excluded.status IN ('done','failed') THEN ? ELSE finished_at END,
                       error_msg=excluded.error_msg,
                       meta=excluded.meta
                """,
                (task_id, version, stage, status, _now(), meta_str, _now()),
            )
            if error_msg:
                conn.execute(
                    "UPDATE pipeline_tasks SET error_msg=? WHERE task_id=?",
                    (error_msg, task_id),
                )

    def get_task(self, task_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM pipeline_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_tasks_by_version(self, version: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM pipeline_tasks WHERE version=? ORDER BY started_at ASC", (version,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_last_task_meta(self, version: str, stage: str) -> dict:
        """获取某版本某阶段最后一次成功任务的 meta 信息。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT meta FROM pipeline_tasks WHERE version=? AND stage=? AND status='done' ORDER BY finished_at DESC LIMIT 1",
                (version, stage),
            ).fetchone()
        if row and row["meta"]:
            return json.loads(row["meta"])
        return {}

    # ---- benchmark_results ----

    def save_benchmark(self, task_id: str, version: str, instance_id: str, result: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO benchmark_results
                   (task_id, version, instance_id, tested_at, cpu_score, mem_score,
                    disk_read_mb, disk_write_mb, net_bandwidth_mb, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task_id, version, instance_id, _now(),
                    result.get("cpu_score"),
                    result.get("mem_score"),
                    result.get("disk_read_mb"),
                    result.get("disk_write_mb"),
                    result.get("net_bandwidth_mb"),
                    json.dumps(result, ensure_ascii=False),
                ),
            )

    def get_all_benchmarks(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM benchmark_results ORDER BY tested_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_benchmark_by_version(self, version: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM benchmark_results WHERE version=? ORDER BY tested_at DESC LIMIT 1",
                (version,),
            ).fetchone()
        return dict(row) if row else None


def _now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
