"""SQLite 数据库模块 —— 已处理视频记录持久化"""

import sqlite3
from datetime import datetime


class ProcessDB:
    """管理已处理视频的 SQLite 记录，支持增量跳过和失败追踪。"""

    def __init__(self, db_path: str = "processed.db") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_videos (
                aweme_id TEXT PRIMARY KEY,
                title TEXT,
                status TEXT DEFAULT 'success',
                error TEXT,
                processed_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        self._conn.commit()

    def is_processed(self, aweme_id: str) -> bool:
        """查询 aweme_id 是否已成功处理（status='success'）。"""
        row = self._conn.execute(
            "SELECT 1 FROM processed_videos WHERE aweme_id = ? AND status = 'success'",
            (aweme_id,),
        ).fetchone()
        return row is not None

    def mark_processed(self, aweme_id: str, title: str = "") -> None:
        """标记视频为已成功处理。"""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO processed_videos (aweme_id, title, status, error, processed_at)
            VALUES (?, ?, 'success', NULL, ?)
            """,
            (aweme_id, title, datetime.now().isoformat()),
        )
        self._conn.commit()

    def mark_failed(self, aweme_id: str, error: str) -> None:
        """标记视频处理失败并记录错误信息。"""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO processed_videos (aweme_id, title, status, error, processed_at)
            VALUES (?, '', 'failed', ?, ?)
            """,
            (aweme_id, error, datetime.now().isoformat()),
        )
        self._conn.commit()

    def processed_count(self) -> int:
        """返回已成功处理的视频数量。"""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM processed_videos WHERE status = 'success'"
        ).fetchone()
        return row[0]

    def get_failed_items(self) -> list[tuple[str, str]]:
        """返回所有处理失败的 (aweme_id, error) 列表。"""
        rows = self._conn.execute(
            "SELECT aweme_id, error FROM processed_videos WHERE status = 'failed'"
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def close(self) -> None:
        """关闭数据库连接。"""
        self._conn.close()
