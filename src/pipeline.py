"""流水线编排模块：串联采集、去重、AI 解析、输出全流程。

用法：
    import asyncio
    from src.config import load_config
    from src.pipeline import run_pipeline

    config = load_config()
    result = asyncio.run(run_pipeline(
        user_url="https://www.douyin.com/user/MS4w...",
        limit=None,
        output_dir="./output",
        no_skip=False,
        interval_range=(2.0, 5.0),
        config=config,
    ))
    print(f"完成: {result.processed}/{result.total}")
"""

import asyncio
import os
import random
import re
from datetime import datetime

from tqdm import tqdm

from src.config import Config
from src.models import VideoInfo, OutlineResult, OutputRecord, PipelineResult
from src.db import ProcessDB
from src.scraper import VideoScraper
from src.ai_client import AIClient
from src.output import write_results, generate_output_filename


async def run_pipeline(
    user_url: str,
    limit: int | None,
    output_dir: str,
    no_skip: bool,
    interval_range: tuple[float, float],
    config: Config,
) -> PipelineResult:
    """完整流水线：采集 → 去重 → AI 解析 → 输出。

    1. 创建 VideoScraper → 采集视频列表
    2. 创建 ProcessDB → 逐视频检查去重（no_skip 时跳过检查）
    3. 创建 AIClient → 对未处理视频生成提纲（try/except）
    4. 收集 OutputRecord → 调用 write_results
    5. 返回 PipelineResult（含统计）

    Args:
        user_url:      抖音博主主页 URL。
        limit:         最大采集视频数，None 表示不限制。
        output_dir:    结果输出目录。
        no_skip:       为 True 时跳过已处理视频的去重检查。
        interval_range: 视频间请求间隔随机范围 (min, max) 秒。
        config:        应用运行时配置。

    Returns:
        PipelineResult: 包含 total/processed/skipped/failed/errors/output_file。
    """
    scraper = VideoScraper(config)
    db = ProcessDB()
    ai = AIClient(config)

    try:
        # ---- 1) 采集视频列表 ----
        videos = await scraper.scrape_user_videos(user_url, limit)

        # ---- 2) 逐视频处理 ----
        records: list[OutputRecord] = []
        errors: list[dict] = []
        processed = 0
        skipped = 0
        failed = 0

        for video in tqdm(videos, desc="处理视频"):
            # 2a) 去重检查
            if not no_skip and db.is_processed(video.aweme_id):
                records.append(OutputRecord(
                    url=video.url,
                    title=video.title,
                    outline="",
                    timestamp=datetime.now().isoformat(),
                    status="skipped",
                ))
                skipped += 1
                continue

            # 2b) AI 解析
            try:
                result = ai.generate_outline(video.title, video.description)
            except Exception as exc:
                # AuthError / TimeoutError 等不可恢复异常
                records.append(OutputRecord(
                    url=video.url,
                    title=video.title,
                    outline="",
                    timestamp=datetime.now().isoformat(),
                    status="failed",
                ))
                errors.append({
                    "aweme_id": video.aweme_id,
                    "url": video.url,
                    "error": str(exc),
                })
                failed += 1
                db.mark_failed(video.aweme_id, str(exc))
                continue

            if result.success:
                records.append(OutputRecord(
                    url=video.url,
                    title=video.title,
                    outline=result.outline_markdown,
                    timestamp=datetime.now().isoformat(),
                    status="success",
                ))
                processed += 1
                db.mark_processed(video.aweme_id, video.title)
            else:
                records.append(OutputRecord(
                    url=video.url,
                    title=video.title,
                    outline="",
                    timestamp=datetime.now().isoformat(),
                    status="failed",
                ))
                errors.append({
                    "aweme_id": video.aweme_id,
                    "url": video.url,
                    "error": result.error_message,
                })
                failed += 1
                db.mark_failed(video.aweme_id, result.error_message)

            # 2c) 请求间隔（保护 API 不被限流）
            if interval_range[0] > 0:
                await asyncio.sleep(random.uniform(*interval_range))

        # ---- 3) 写入结果文件 ----
        user_id = _extract_user_id(user_url)
        filename = generate_output_filename(user_id)
        output_path = os.path.join(output_dir, filename)
        write_results(records, output_path)

        # ---- 4) 返回统计 ----
        return PipelineResult(
            total=len(videos),
            processed=processed,
            skipped=skipped,
            failed=failed,
            errors=errors,
            output_file=os.path.abspath(output_path),
        )

    finally:
        await scraper.close()
        db.close()


def _extract_user_id(user_url: str) -> str:
    """从抖音用户 URL 中提取用于生成文件名的用户标识。

    优先提取 sec_uid；失败时回退到 URL 整体交由
    generate_output_filename 进行安全化处理。
    """
    match = re.search(r"douyin\.com/user/([A-Za-z0-9_-]+)", user_url)
    return match.group(1) if match else user_url
