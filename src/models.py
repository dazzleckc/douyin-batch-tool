"""数据模型定义"""

from dataclasses import dataclass, field


@dataclass
class VideoInfo:
    """抖音视频信息"""
    aweme_id: str
    url: str
    title: str
    description: str = ""
    publish_time: str | None = None
    is_pinned: bool = False


@dataclass
class OutlineResult:
    """AI 提纲解析结果"""
    aweme_id: str
    outline_markdown: str
    raw_response: str
    success: bool
    error_message: str = ""


@dataclass
class OutputRecord:
    """输出记录"""
    url: str
    title: str
    outline: str
    timestamp: str
    status: str  # "success" | "failed" | "skipped"


@dataclass
class PipelineResult:
    """流水线批处理结果"""
    total: int
    processed: int
    skipped: int
    failed: int
    errors: list[dict] = field(default_factory=list)
    output_file: str = ""


@dataclass
class ScrapeCheckpoint:
    """采集阶段检查点：采集完成后持久化，供 AI 阶段独立读取。

    用作采集和 AI 调用之间的原子事务边界。
    """
    user_url: str
    user_id: str
    created_at: str
    videos: list[dict] = field(default_factory=list)  # [{"aweme_id":..., "url":..., "title":..., "description":...}]
    total: int = 0
    new_count: int = 0        # 本次新采集的视频数
    skipped_count: int = 0    # 已在DB中跳过的视频数
