"""SQLite 数据库模块 —— 视频采集与 AI 解析两个维度的记录持久化。

生产者维度（采集）：追踪“这个视频 ID 我们知道吗？”
消费者维度（AI）：追踪“这个视频提纲解析过了吗？”

状态流转：known（已采集） → parsed（已解析）
"""

import sqlite3
from datetime import datetime


class ProcessDB:
    """管理视频采集与 AI 解析两个维度的持久化记录。

    两个维度独立追踪：
        - 生产者（采集）：视频是否已入库（已知 ID）
        - 消费者（AI）： 视频是否已生成提纲（解析完成）
    """

    def __init__(self, db_path: str = "processed.db") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_videos (
                aweme_id TEXT PRIMARY KEY,
                title TEXT,
                status TEXT DEFAULT 'parsed',
                error TEXT,
                processed_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        self._conn.commit()
        self._migrate_statuses()

    def _migrate_statuses(self) -> None:
        """将旧版状态值迁移为新版命名（success→parsed, scraped→known, failed→known）。"""
        self._conn.execute(
            "UPDATE processed_videos SET status = 'parsed' WHERE status = 'success'"
        )
        self._conn.execute(
            "UPDATE processed_videos SET status = 'known' WHERE status IN ('scraped', 'failed')"
        )
        self._conn.commit()

    # =========================================================================
    # 生产者维度：采集去重 —— "这个视频 ID 我知道吗？"
    # =========================================================================

    def get_known_ids(self) -> set[str]:
        """返回所有已知视频 ID（已采集到的全部 ID）。"""
        rows = self._conn.execute(
            "SELECT aweme_id FROM processed_videos WHERE status IN ('known', 'parsed')"
        ).fetchall()
        return {row[0] for row in rows}

    def mark_known_batch(self, aweme_ids: list[str]) -> None:
        """批量标记视频 ID 为「已知」。

        采集阶段成功后调用，后续采集自动跳过这些 ID。
        使用 INSERT OR IGNORE，不会覆盖已有的 parsed 状态。
        """
        now = datetime.now().isoformat()
        self._conn.executemany(
            """
            INSERT OR IGNORE INTO processed_videos (aweme_id, title, status, error, processed_at)
            VALUES (?, '', 'known', NULL, ?)
            """,
            [(aid, now) for aid in aweme_ids],
        )
        self._conn.commit()

    # =========================================================================
    # 消费者维度：AI 去重 —— “这个视频提纲解析过了吗？”
    # =========================================================================

    def is_parsed(self, aweme_id: str) -> bool:
        """查询视频是否已完成 AI 解析（status='parsed'）。"""
        row = self._conn.execute(
            "SELECT 1 FROM processed_videos WHERE aweme_id = ? AND status = 'parsed'",
            (aweme_id,),
        ).fetchone()
        return row is not None

    def mark_parsed(self, aweme_id: str, title: str = "") -> None:
        """标记视频 AI 解析完成（known → parsed）。"""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO processed_videos (aweme_id, title, status, error, processed_at)
            VALUES (?, ?, 'parsed', NULL, ?)
            """,
            (aweme_id, title, datetime.now().isoformat()),
        )
        self._conn.commit()

    def mark_parse_failed(self, aweme_id: str, error: str) -> None:
        """标记 AI 解析失败，下次运行时重新尝试。"""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO processed_videos (aweme_id, title, status, error, processed_at)
            VALUES (?, '', 'known', ?, ?)
            """,
            (aweme_id, error, datetime.now().isoformat()),
        )
        self._conn.commit()

    # =========================================================================
    # 统计与工具
    # =========================================================================

    def parsed_count(self) -> int:
        """返回已解析的视频数量。"""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM processed_videos WHERE status = 'parsed'"
        ).fetchone()
        return row[0]

    def get_failed_items(self) -> list[tuple[str, str]]:
        """返回所有解析失败的 (aweme_id, error) 列表。"""
        rows = self._conn.execute(
            "SELECT aweme_id, error FROM processed_videos WHERE status = 'known' AND error IS NOT NULL"
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def close(self) -> None:
        """关闭数据库连接。"""
        self._conn.close()
