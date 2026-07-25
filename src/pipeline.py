"""流水线编排模块：采集 → checkpoint → AI 解析 → 输出。

两个原子性事务：
    事务1: run_scrape_phase  → 采集视频 + 保存 checkpoint + 更新 DB
    事务2: run_ai_phase      → 读取 checkpoint → 豆包生成提纲 → 输出结果

事务2 失败时不需要重新采集（可从 checkpoint 恢复）。

用法：
    import asyncio
    from src.config import load_config
    from src.pipeline import run_pipeline, run_scrape_phase, run_ai_phase

    config = load_config()

    # 完整流程（两个事务顺序执行）
    result = asyncio.run(run_pipeline(user_url="...", ..., config=config))

    # 仅采集
    checkpoint = asyncio.run(run_scrape_phase(user_url="...", ..., config=config))

    # 从 checkpoint 执行 AI 阶段
    result = run_ai_phase("output/xxx_checkpoint.json", ..., config=config)
"""

import asyncio
import json as _json
import os
import random
import re
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

from src.config import Config
from src.models import (
    VideoInfo, OutlineResult, OutputRecord, PipelineResult, ScrapeCheckpoint,
)
from src.db import ProcessDB
from src.scraper import VideoScraper
from src.ai_client import DoubaoClient, ReviewBlockedError
from src.output import write_results, generate_output_filename


# =========================================================================
# 事务1：采集阶段（原子性——完成则保存 checkpoint）
# =========================================================================

async def run_scrape_phase(
    user_url: str,
    limit: int | None,
    output_dir: str,
    config: Config,
) -> ScrapeCheckpoint:
    """采集视频列表并保存 checkpoint。

    这是一个原子性事务：
    - 成功：采集的视频列表保存到 checkpoint JSON 文件
    - 失败：不保存 checkpoint（DB 已有记录不受影响）

    Args:
        user_url:   抖音博主主页 URL。
        limit:      最大采集视频数，None 表示不限制。
        output_dir: 输出目录。
        config:     应用运行时配置。

    Returns:
        ScrapeCheckpoint: 包含视频列表、统计信息的检查点对象。

    Raises:
        ScraperError 等采集异常。
    """
    db = ProcessDB()
    # 增量模式：传入 DB 以便采集时识别已处理视频
    scraper = VideoScraper(config, process_db=db)

    try:
        # 1) 获取已知 ID（已采集过的视频）
        known_ids = db.get_known_ids()

        # 2) 采集视频列表（增量模式——遇到已知视频时提前停止滚动）
        videos = await scraper.scrape_user_videos(user_url, limit)

        # 3) 统计新视频 vs 已跳过
        new_videos = [v for v in videos if v.aweme_id not in known_ids]
        skipped_count = len(videos) - len(new_videos)

        # 3.5) 将新视频 ID 写入 DB（status='known'），确保重复采集时自动跳过
        if new_videos:
            db.mark_known_batch([v.aweme_id for v in new_videos])

        # 4) 保存采集清单（供人工核对）
        user_id = _extract_user_id(user_url)
        os.makedirs(output_dir, exist_ok=True)
        scrape_output = os.path.join(output_dir, f"{user_id}_scrape.json")
        scrape_data = [
            {"aweme_id": v.aweme_id, "url": v.url, "title": v.title,
             "description": v.description, "is_pinned": v.is_pinned}
            for v in videos
        ]

        total_known = len(known_ids) + len(new_videos)
        if new_videos:
            with open(scrape_output, "w", encoding="utf-8") as f:
                _json.dump(scrape_data, f, ensure_ascii=False, indent=2)
            print(f"\n📋 采集清单已保存: {scrape_output}")
            print(f"   视频池总量: {total_known}  本次新增: {len(new_videos)}  已跳过: {skipped_count}")
        else:
            print(f"\n📋 视频池总量: {total_known}  本次新增: 0  已跳过: {skipped_count}")

        if not new_videos:
            print("\n✅ 无新增视频，跳过采集。")

        # 5) 创建或更新 checkpoint
        checkpoint_path = os.path.join(output_dir, f"{user_id}_checkpoint.json")
        if new_videos:
            # 有新视频：合并到已有 checkpoint（存在则合并，不存在则新建）
            existing_videos = []
            if os.path.exists(checkpoint_path):
                existing = _load_checkpoint(checkpoint_path)
                existing_videos = existing.videos
                print(f"   📋 合并已有 checkpoint ({len(existing_videos)} 个历史视频)")
            merged_videos = existing_videos + scrape_data
            checkpoint = ScrapeCheckpoint(
                user_url=user_url, user_id=user_id,
                created_at=datetime.now().isoformat(),
                videos=merged_videos,
                total=len(merged_videos),
                new_count=len(new_videos),
                skipped_count=skipped_count,
            )
            _save_checkpoint(checkpoint, checkpoint_path)
        elif os.path.exists(checkpoint_path):
            # 无新视频，现有 checkpoint 仍有效
            checkpoint = _load_checkpoint(checkpoint_path)
        else:
            # 无 checkpoint 且无新视频（首次运行但无视频可采）
            checkpoint = ScrapeCheckpoint(
                user_url=user_url, user_id=user_id,
                created_at=datetime.now().isoformat(),
                videos=[], total=0, new_count=0, skipped_count=0,
            )

        return checkpoint

    finally:
        try:
            await scraper.close()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass


# =========================================================================
# 事务2：AI 阶段（从 checkpoint 读取）
# =========================================================================

async def run_ai_phase(
    checkpoint_path: str,
    output_dir: str,
    no_skip: bool,
    interval_range: tuple[float, float],
    config: Config,
    concurrency: int = 3,
) -> PipelineResult:
    """从 checkpoint 读取视频列表，并发调用豆包生成提纲。

    支持 Ctrl+C 中断存档：已处理视频记录在 DB 中，下次运行跳过。
    并发 3 路：3 个视频同时调用豆包，无需逐个等待。

    Args:
        checkpoint_path: checkpoint JSON 文件路径。
        output_dir:      结果输出目录。
        no_skip:         True 时忽略已处理状态，强制处理所有视频。
        interval_range:  批次间请求间隔随机范围 (min, max) 秒。
        config:          应用运行时配置。
        concurrency:     并发数（默认 3）。
    """
    checkpoint = _load_checkpoint(checkpoint_path)
    db = ProcessDB()
    doubao_cookies = _parse_cookies(config.doubao_cookie)
    doubao = DoubaoClient(headless=False, cookies=doubao_cookies)

    # 预初始化浏览器上下文，避免并发竞争
    await doubao._ensure_context()

    records: list[OutputRecord] = []
    errors: list[dict] = []
    processed = 0
    skipped = 0
    failed = 0

    videos_to_process = checkpoint.videos
    if not videos_to_process:
        await doubao.close(); db.close()
        return PipelineResult(total=0, processed=0, skipped=0, failed=0, errors=[], output_file="")

    total = len(videos_to_process)

    # 过滤已解析
    pending = []
    for vd in videos_to_process:
        aweme_id = vd["aweme_id"]
        if not no_skip and db.is_parsed(aweme_id):
            records.append(OutputRecord(url=vd.get("url", ""), title=vd.get("title", ""),
                          outline="", timestamp=datetime.now().isoformat(), status="skipped"))
            skipped += 1
        else:
            pending.append(vd)

    if not pending:
        print(f"所有视频均已解析（总数 {total}）。")
        await doubao.close(); db.close()
        return PipelineResult(total=total, processed=0, skipped=skipped, failed=0, errors=[], output_file="")

    print(f"\n  总数 {total} | 已解析 {skipped} | 待处理 {len(pending)} | 并发 {concurrency}路")
    pbar = tqdm(total=len(pending), desc="AI 解析")
    auto_save_interval = 20
    processed_since_save = 0
    # 实时追加写入，防丢
    jsonl_path = os.path.join(output_dir, f"{checkpoint.user_id}_outline.jsonl")
    _init_jsonl(jsonl_path)

    async def _process_one(vd):
        aid = vd["aweme_id"]
        t = vd.get("title", "")
        u = vd.get("url", "")
        d = vd.get("description", "")
        r = await doubao.generate_outline(t, u, d)
        if r.success:
            db.mark_parsed(aid, t)
            return (aid, OutputRecord(url=u, title=t, outline=r.outline_markdown,
                    timestamp=datetime.now().isoformat(), status="success"), None)
        else:
            em = r.error_message or "回复为空"
            db.mark_parse_failed(aid, em)
            return (aid, OutputRecord(url=u, title=t, outline="",
                    timestamp=datetime.now().isoformat(), status="failed"),
                    {"aweme_id": aid, "url": u, "error": em})

    try:
        for i in range(0, len(pending), concurrency):
            batch = pending[i:i + concurrency]
            task_map = {asyncio.ensure_future(_process_one(v)): idx for idx, v in enumerate(batch)}

            for coro in asyncio.as_completed(task_map):
                try:
                    r = await coro
                except ReviewBlockedError:
                    # 人审拦截：立即停止全部，保存已有结果
                    pbar.close()
                    print(f"\n  🛑 检测到人审拦截，停止处理！")
                    _save_partial(checkpoint.user_id, output_dir, records,
                                  processed, skipped, failed, errors)
                    await doubao.close(); db.close()
                    raise
                except Exception:
                    failed += 1
                    pbar.update(1)
                    continue
                _, rec, err = r
                records.append(rec)
                if err:
                    errors.append(err)
                    failed += 1
                else:
                    processed += 1
                    processed_since_save += 1
                    # 实时写入：每条解析结果立即落盘
                    _append_to_jsonl(jsonl_path, rec)
                pbar.update(1)

                # 定期存档：每 N 个视频保存一次中间结果
                if processed_since_save >= auto_save_interval:
                    _save_partial(checkpoint.user_id, output_dir, records,
                                  processed, skipped, failed, errors)
                    processed_since_save = 0

            if i + concurrency < len(pending) and interval_range[0] > 0:
                await asyncio.sleep(random.uniform(*interval_range))

    except KeyboardInterrupt:
        pbar.close()
        print(f"\n  ⚠️ 用户中断！已完成 {processed} 成功 + {failed} 失败, 剩余 {len(pending) - processed - failed}")
        print(f"  处理记录已存入 DB，下次运行从此处继续。")
        _save_partial(checkpoint.user_id, output_dir, records, processed, skipped, failed, errors)
        await doubao.close(); db.close()
        raise

    pbar.close()
    user_id = checkpoint.user_id
    output_path = os.path.join(output_dir, generate_output_filename(user_id))
    write_results(records, output_path)

    await doubao.close(); db.close()
    return PipelineResult(total=len(videos_to_process), processed=processed,
                          skipped=skipped, failed=failed, errors=errors,
                          output_file=os.path.abspath(output_path))


# =========================================================================
# 完整流水线（事务1 + 事务2 顺序执行）
# =========================================================================

async def run_pipeline(
    user_url: str,
    limit: int | None,
    output_dir: str,
    no_skip: bool,
    interval_range: tuple[float, float],
    config: Config,
) -> PipelineResult:
    """完整流水线：采集（事务1） → AI 解析（事务2）。

    内部调用 run_scrape_phase 和 run_ai_phase，以 checkpoint 为边界。
    事务1 失败时事务2 不会执行；事务2 失败时可从 checkpoint 单独恢复。
    """
    # 事务1：采集
    checkpoint = await run_scrape_phase(
        user_url=user_url, limit=limit,
        output_dir=output_dir, config=config,
    )

    # 事务2：AI 解析
    checkpoint_path = os.path.join(output_dir, f"{checkpoint.user_id}_checkpoint.json")
    return await run_ai_phase(
        checkpoint_path=checkpoint_path,
        output_dir=output_dir,
        no_skip=no_skip,
        interval_range=interval_range,
        config=config,
    )


# =========================================================================
# 辅助函数
# =========================================================================

def _save_partial(user_id, output_dir, records, processed, skipped, failed, errors):
    """中断时保存已完成的部分结果。"""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"{user_id}_partial_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        _json.dump({
            "interrupted": True,
            "processed": processed, "skipped": skipped, "failed": failed,
            "errors": errors, "records": [{"url": r.url, "title": r.title, "outline": r.outline, "status": r.status} for r in records],
        }, f, ensure_ascii=False, indent=2)
    print(f"  💾 部分结果已保存: {path}")


def _init_jsonl(path: str) -> None:
    """初始化 JSONL 文件（目录不存在则创建，已有则清空）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # 每次 AI 阶段重新开始时清空旧的 JSONL
    open(path, "w").close()


def _append_to_jsonl(path: str, record: "OutputRecord") -> None:
    """追加单条解析结果到 JSONL 文件，异常幂等不死。"""
    try:
        line = _json.dumps({
            "url": record.url,
            "title": record.title,
            "outline": record.outline,
            "status": record.status,
            "timestamp": record.timestamp,
        }, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # 文件写入失败不中断主流程


def _save_checkpoint(checkpoint: ScrapeCheckpoint, path: str) -> None:
    """序列化 checkpoint 为 JSON 文件。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = {
        "user_url": checkpoint.user_url,
        "user_id": checkpoint.user_id,
        "created_at": checkpoint.created_at,
        "videos": checkpoint.videos,
        "total": checkpoint.total,
        "new_count": checkpoint.new_count,
        "skipped_count": checkpoint.skipped_count,
    }
    with open(path, "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"💾 Checkpoint 已保存: {path}")


def _load_checkpoint(path: str) -> ScrapeCheckpoint:
    """从 JSON 文件反序列化 checkpoint。"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Checkpoint 文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = _json.load(f)
    return ScrapeCheckpoint(
        user_url=data.get("user_url", ""),
        user_id=data.get("user_id", ""),
        created_at=data.get("created_at", ""),
        videos=data.get("videos", []),
        total=data.get("total", 0),
        new_count=data.get("new_count", 0),
        skipped_count=data.get("skipped_count", 0),
    )


def _parse_cookies(cookie_str: str) -> dict[str, str]:
    """解析 Cookie 字符串为键值对字典。"""
    if not cookie_str:
        return {}
    from urllib.parse import unquote
    result: dict[str, str] = {}
    for part in cookie_str.split(";"):
        if "=" not in part:
            continue
        k, _, v = part.strip().partition("=")
        k = k.strip().lower()
        if k not in ("domain", "path", "expires", "max-age", "secure",
                      "httponly", "samesite"):
            result[k] = unquote(v.strip())
    return result


def _extract_user_id(user_url: str) -> str:
    """从抖音用户 URL 中提取用于生成文件名的用户标识。"""
    match = re.search(r"douyin\.com/user/([A-Za-z0-9_-]+)", user_url)
    return match.group(1) if match else user_url
