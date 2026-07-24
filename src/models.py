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
