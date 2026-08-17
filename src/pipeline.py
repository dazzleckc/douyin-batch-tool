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
    result = asyncio.run(run_ai_phase("output/xxx_checkpoint.json", ..., config=config))
"""

import asyncio
import json as _json
import os
import random
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

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
        # 1) checkpoint 定义当前博主的已知集合。
        # processed.db 是跨博主共享的状态库，不能直接拿全库 ID 做单博主采集统计。
        user_id = _extract_user_id(user_url)
        checkpoint_path = os.path.join(output_dir, f"{user_id}_checkpoint.json")
        existing_checkpoint = (
            _load_checkpoint(checkpoint_path)
            if os.path.exists(checkpoint_path)
            else None
        )
        checkpoint_known_ids = {
            vd.get("aweme_id", "") for vd in existing_checkpoint.videos
            if vd.get("aweme_id", "")
        } if existing_checkpoint else set()

        # 2) 只传当前博主 checkpoint 的 ID，避免其他目标污染增量判定。
        videos = await scraper.scrape_user_videos(
            user_url, limit, processed_ids=checkpoint_known_ids,
        )

        # 3) 统计当前博主的新视频与本轮实际遇到的已知视频。
        durable_known_ids = checkpoint_known_ids
        new_videos = [v for v in videos if v.aweme_id not in durable_known_ids]
        skipped_count = scraper.last_known_seen_count

        # 4) 保存采集清单（供人工核对）
        os.makedirs(output_dir, exist_ok=True)
        scrape_output = os.path.join(output_dir, f"{user_id}_scrape.json")
        batch_id = _generate_batch_id() if new_videos else ""
        new_aweme_ids = {v.aweme_id for v in new_videos}
        scrape_data = [
            {
                "aweme_id": v.aweme_id,
                "url": v.url,
                "title": v.title,
                "description": v.description,
                "is_pinned": v.is_pinned,
                **({"batch_id": batch_id} if v.aweme_id in new_aweme_ids else {}),
            }
            for v in videos
        ]

        total_known = len(durable_known_ids) + len(new_videos)
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
        if new_videos:
            # 有新视频：合并到已有 checkpoint（存在则合并，不存在则新建）
            existing_videos = existing_checkpoint.videos if existing_checkpoint else []
            if existing_checkpoint:
                print(f"   📋 合并已有 checkpoint ({len(existing_videos)} 个历史视频)")
            merged_videos = existing_videos + [
                vd for vd in scrape_data if vd.get("batch_id") == batch_id
            ]
            checkpoint = ScrapeCheckpoint(
                user_url=user_url, user_id=user_id,
                created_at=datetime.now().isoformat(),
                videos=merged_videos,
                total=len(merged_videos),
                new_count=len(new_videos),
                skipped_count=skipped_count,
                batch_id=batch_id,
                batch_aweme_ids=[v.aweme_id for v in new_videos],
                batch_completed=False,
            )
            _save_checkpoint(checkpoint, checkpoint_path)
        elif existing_checkpoint:
            # 无新视频，现有 checkpoint 仍有效
            checkpoint = ScrapeCheckpoint(
                user_url=existing_checkpoint.user_url,
                user_id=existing_checkpoint.user_id,
                created_at=existing_checkpoint.created_at,
                videos=existing_checkpoint.videos,
                total=existing_checkpoint.total,
                new_count=0,
                skipped_count=skipped_count,
                batch_id="",
                batch_aweme_ids=[],
                batch_completed=True,
            )
        else:
            # 无 checkpoint 且无新视频（首次运行但无视频可采）
            checkpoint = ScrapeCheckpoint(
                user_url=user_url, user_id=user_id,
                created_at=datetime.now().isoformat(),
                videos=[], total=0, new_count=0, skipped_count=0,
                batch_id="", batch_aweme_ids=[], batch_completed=True,
            )

        # 6) checkpoint 已安全落盘后再同步 DB；INSERT OR IGNORE 会保留 parsed。
        checkpoint_aweme_ids = [
            vd.get("aweme_id", "") for vd in checkpoint.videos
            if vd.get("aweme_id", "")
        ]
        if checkpoint_aweme_ids:
            db.mark_known_batch(checkpoint_aweme_ids)

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

    videos_to_process = checkpoint.videos
    if not videos_to_process:
        await doubao.close(); db.close()
        return PipelineResult(total=0, processed=0, skipped=0, failed=0, errors=[], output_file="")

    total = len(videos_to_process)

    # JSONL 路径（追加写入，不清空——中断后重启时保留已有成果）
    jsonl_path = os.path.join(output_dir, f"{checkpoint.user_id}_outline.jsonl")
    os.makedirs(output_dir, exist_ok=True)

    # 从中断的 partial 存档恢复上次已处理的记录
    records, errors, processed, skipped, failed, recovered_increment_files = _recover_from_partial(
        checkpoint.user_id, output_dir, videos_to_process, db, no_skip, jsonl_path
    )
    touched_increment_files = set(recovered_increment_files)
    # 同一 checkpoint 批次恢复时，增量文件可能已在中断前完整或部分落盘。
    # 幂等追加会正确返回“未新增写入”，但 CLI 仍应报告当前批次已有的数据集。
    if (
        checkpoint.batch_id
        and checkpoint.batch_aweme_ids
        and not checkpoint.batch_completed
    ):
        current_increment_path = _increment_jsonl_path(
            checkpoint.user_id, output_dir, checkpoint.batch_id
        )
        if os.path.exists(current_increment_path):
            touched_increment_files.add(current_increment_path)

    # 从 JSONL 加载历史提纲索引，用于恢复已解析视频的提纲内容
    jsonl_outlines = _load_jsonl_outlines(jsonl_path)

    # 先补齐旧 partial 中的空提纲 skipped，再计算恢复集合。
    # 否则这些记录会先被 DB+JSONL 再恢复一次，随后原 skipped 又被补成
    # success，造成同一 aweme_id 重复和 processed 超过 total。
    delta_p, delta_s = _fill_skipped_from_jsonl(records, jsonl_outlines)
    processed += delta_p
    skipped += delta_s

    # 过滤已解析（考虑恢复后的 DB 状态）
    recovered_aweme_ids = {
        _record_aweme_id(r) for r in records
        if r.status == "success" and bool(r.outline)
    }
    pending = []
    for vd in videos_to_process:
        aweme_id = vd["aweme_id"]
        if aweme_id in recovered_aweme_ids:
            # 已从 partial 恢复，跳过
            continue
        if not no_skip and db.is_parsed(aweme_id):
            # 已解析：从 JSONL 恢复提纲，标记为 success
            existing_outline = jsonl_outlines.get(aweme_id, "")
            if existing_outline:
                records.append(OutputRecord(
                    url=vd.get("url", ""), title=vd.get("title", ""),
                    outline=existing_outline,
                    timestamp=datetime.now().isoformat(), status="success",
                    aweme_id=aweme_id,
                ))
                processed += 1
            else:
                # 极端情况：DB 有 parsed 但 JSONL 无记录，保持 skipped
                records.append(OutputRecord(
                    url=vd.get("url", ""), title=vd.get("title", ""),
                    outline="", timestamp=datetime.now().isoformat(), status="skipped",
                    aweme_id=aweme_id,
                ))
                skipped += 1
        else:
            pending.append(vd)

    if not pending:
        print(f"所有视频均已解析（总数 {total}，恢复 {len(recovered_aweme_ids)} 条）。")
        output_path = os.path.join(output_dir, generate_output_filename(checkpoint.user_id))
        write_results(records, output_path)
        _mark_checkpoint_batch_completed(checkpoint, records, checkpoint_path)
        await doubao.close(); db.close()
        increment_count, increment_files = _increment_summary(touched_increment_files)
        return PipelineResult(total=total, processed=processed, skipped=skipped,
                              failed=failed, errors=errors,
                              output_file=os.path.abspath(output_path),
                              increment_count=increment_count,
                              increment_files=increment_files)

    print(f"\n  总数 {total} | 已恢复 {len(recovered_aweme_ids)} | 已解析 {processed} | 待处理 {len(pending)} | 并发 {concurrency}路")
    pbar = tqdm(total=len(pending), desc="AI 解析")
    auto_save_interval = 20
    processed_since_save = 0
    handled_aweme_ids: set[str] = set()

    async def _process_one(vd):
        aid = vd["aweme_id"]
        t = vd.get("title", "")
        u = vd.get("url", "")
        d = vd.get("description", "")
        r = await doubao.generate_outline(t, u, d)
        if r.success:
            # 注意：先返回结果（外层写 JSONL 后才会写 DB），
            # 避免崩溃时 DB 已标记 parsed 但提纲未落盘的不一致。
            return (aid, OutputRecord(
                url=u, title=t, outline=r.outline_markdown,
                timestamp=datetime.now().isoformat(), status="success",
                aweme_id=aid, batch_id=vd.get("batch_id", ""),
            ), None)
        else:
            em = r.error_message or "回复为空"
            db.mark_parse_failed(aid, em)
            return (aid, OutputRecord(
                    url=u, title=t, outline="",
                    timestamp=datetime.now().isoformat(), status="failed",
                    aweme_id=aid, batch_id=vd.get("batch_id", "")),
                    {"aweme_id": aid, "url": u, "error": em})

    async def _drain_and_save(
        task_map: dict, _records: list, _errors: list,
        _checkpoint, _output_dir: str, _jsonl_path: str,
        _pbar, _doubao, _db,
        reason: str,
    ) -> None:
        """优雅关闭：排干飞行中的任务，收集结果，保存并关闭资源。

        修复前：终止时直接抛弃同批次未完成任务，丢失豆包已生成的回复。
        修复后：先 await 所有未完成的任务收集结果，再保存退出。

        防御：gather 有 30s 超时；再次 Ctrl+C 时跳过等待直接保存。
        """
        nonlocal processed, skipped, failed
        _pbar.close()

        all_tasks = list(task_map)
        remaining_count = sum(1 for task in all_tasks if not task.done())
        if all_tasks:
            if remaining_count:
                print(f"\n  ⏳ 等待 {remaining_count} 个进行中的任务完成"
                  f"（再次 Ctrl+C 跳过等待）...")
            results: list = []
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*all_tasks, return_exceptions=True),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                print(f"  ⚠️ 等待超时 (30s)，跳过未完成任务")
            except KeyboardInterrupt:
                print(f"  ⚠️ 再次中断，跳过等待")
            except BaseException as exc:
                print(f"  ⚠️ 等待异常 ({type(exc).__name__})，跳过未完成任务")

            for r in results:
                if isinstance(r, Exception):
                    if not isinstance(r, ReviewBlockedError):
                        failed += 1
                        _errors.append({"error": f"关闭时任务异常: {r}"})
                elif r is not None:
                    aid, rec, err = r
                    if aid in handled_aweme_ids:
                        continue
                    previous_status = _upsert_runtime_record(_records, rec)
                    if err:
                        _errors.append(err)
                        if previous_status not in ("failed", "success"):
                            failed += 1
                    else:
                        increment_path = _persist_success_record(
                            _checkpoint.user_id,
                            _output_dir,
                            _jsonl_path,
                            rec,
                            _db,
                        )
                        if increment_path:
                            touched_increment_files.add(increment_path)
                        if previous_status != "success":
                            processed += 1
                        if previous_status == "failed":
                            failed -= 1
                    handled_aweme_ids.add(aid)
            if results:
                print(f"  ✅ 飞行任务收集完成")

        # 无论如何都要保存——即使 drain 被跳过
        print(f"\n  {reason}")
        print(f"  处理记录已存入 DB，下次运行从此处继续。")
        _save_partial(_checkpoint.user_id, _output_dir, _records,
                      processed, skipped, failed, _errors)
        try:
            await _doubao.close()
        except Exception:
            pass
        try:
            _db.close()
        except Exception:
            pass

    try:
        current_task_map: dict = {}
        for i in range(0, len(pending), concurrency):
            batch = pending[i:i + concurrency]
            task_map = {asyncio.ensure_future(_process_one(v)): idx for idx, v in enumerate(batch)}
            current_task_map = task_map

            for coro in asyncio.as_completed(task_map):
                try:
                    r = await coro
                except ReviewBlockedError:
                    # 人审拦截：先排干同批次剩余任务，再保存退出
                    await _drain_and_save(
                        task_map, records, errors,
                        checkpoint, output_dir, jsonl_path,
                        pbar, doubao, db,
                        reason="🛑 检测到人审拦截，停止处理！",
                    )
                    raise
                except Exception as exc:
                    failed += 1
                    errors.append({"error": f"协程异常: {exc}"})
                    pbar.update(1)
                    continue
                aid, rec, err = r
                previous_status = _upsert_runtime_record(records, rec)
                if err:
                    errors.append(err)
                    if previous_status not in ("failed", "success"):
                        failed += 1
                else:
                    increment_path = _persist_success_record(
                        checkpoint.user_id,
                        output_dir,
                        jsonl_path,
                        rec,
                        db,
                    )
                    if increment_path:
                        touched_increment_files.add(increment_path)
                    if previous_status != "success":
                        processed += 1
                    if previous_status == "failed":
                        failed -= 1
                    processed_since_save += 1
                handled_aweme_ids.add(aid)
                pbar.update(1)

                # 定期存档：每 N 个视频保存一次中间结果
                if processed_since_save >= auto_save_interval:
                    _save_partial(checkpoint.user_id, output_dir, records,
                                  processed, skipped, failed, errors)
                    processed_since_save = 0

            if i + concurrency < len(pending) and interval_range[0] > 0:
                await asyncio.sleep(random.uniform(*interval_range))

    except KeyboardInterrupt:
        try:
            await _drain_and_save(
                current_task_map, records, errors,
                checkpoint, output_dir, jsonl_path,
                pbar, doubao, db,
                reason=f"⚠️ 用户中断！已完成 {processed} 成功 + {failed} 失败, 剩余 {len(pending) - processed - failed}",
            )
        except Exception:
            _save_partial(
                checkpoint.user_id, output_dir, records,
                processed, skipped, failed, errors,
            )
            try:
                await doubao.close()
            finally:
                db.close()
            raise
        increment_count, increment_files = _increment_summary(touched_increment_files)
        return PipelineResult(
            total=len(videos_to_process), processed=processed,
            skipped=skipped, failed=failed, errors=errors,
            increment_count=increment_count,
            increment_files=increment_files,
            interrupted=True,
        )
    except ReviewBlockedError as exc:
        errors.append({"error": f"人审拦截: {exc}"})
        increment_count, increment_files = _increment_summary(touched_increment_files)
        return PipelineResult(
            total=len(videos_to_process), processed=processed,
            skipped=skipped, failed=failed, errors=errors,
            increment_count=increment_count,
            increment_files=increment_files,
            interrupted=True,
        )
    except Exception:
        pbar.close()
        for task in current_task_map:
            if not task.done():
                task.cancel()
        if current_task_map:
            await asyncio.gather(*current_task_map, return_exceptions=True)
        _save_partial(
            checkpoint.user_id, output_dir, records,
            processed, skipped, failed, errors,
        )
        try:
            await doubao.close()
        finally:
            db.close()
        raise

    pbar.close()
    user_id = checkpoint.user_id
    output_path = os.path.join(output_dir, generate_output_filename(user_id))
    write_results(records, output_path)
    _mark_checkpoint_batch_completed(checkpoint, records, checkpoint_path)

    await doubao.close(); db.close()
    increment_count, increment_files = _increment_summary(touched_increment_files)
    return PipelineResult(total=len(videos_to_process), processed=processed,
                          skipped=skipped, failed=failed, errors=errors,
                          output_file=os.path.abspath(output_path),
                          increment_count=increment_count,
                          increment_files=increment_files)


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
            "errors": errors,
            "records": [
                {
                    "aweme_id": _record_aweme_id(r),
                    "url": r.url,
                    "title": r.title,
                    "outline": r.outline,
                    "status": r.status,
                    "timestamp": r.timestamp,
                    **({"batch_id": r.batch_id} if r.batch_id else {}),
                }
                for r in records
            ],
        }, f, ensure_ascii=False, indent=2)
    print(f"  💾 部分结果已保存: {path}")


def _recover_from_partial(
    user_id: str, output_dir: str, videos: list[dict],
    db: "ProcessDB", no_skip: bool, jsonl_path: str | None = None,
) -> tuple[list["OutputRecord"], list[dict], int, int, int, set[str]]:
    """从所有 partial 存档合并恢复已处理的记录，并同步 DB 状态。

    合并策略：对于同一视频 URL，优先保留「success + 有提纲」的记录；
    如果所有 partial 中都是 skipped，则保持 skipped。
    这样可以从中断前的 partial（含有效提纲）恢复，即使后续运行
    将其标记为 skipped。

    Returns:
        (records, errors, processed, skipped, failed, touched_increment_files)
    """
    partial_paths = _find_all_partials(user_id, output_dir)
    if not partial_paths:
        return [], [], 0, 0, 0, set()

    if jsonl_path is None:
        jsonl_path = os.path.join(output_dir, f"{user_id}_outline.jsonl")

    # 构建 checkpoint 中的视频索引（aweme_id → video dict）
    video_index: dict[str, dict] = {}
    for vd in videos:
        aid = vd.get("aweme_id", "")
        if aid:
            video_index[aid] = vd

    # 按 URL 合并所有 partial 记录，success+有提纲 优先
    merged: dict[str, dict] = {}       # url → best record
    merged_errors: list[dict] = []      # 收集所有 errors
    seen_errors: set[str] = set()

    for pp in partial_paths:
        try:
            with open(pp, "r", encoding="utf-8") as f:
                partial = _json.load(f)
        except Exception:
            continue

        for err in partial.get("errors", []):
            err_key = str(err.get("aweme_id", "")) + str(err.get("error", ""))
            if err_key not in seen_errors:
                seen_errors.add(err_key)
                merged_errors.append(err)

        for raw in partial.get("records", []):
            url = raw.get("url", "")
            aid = _extract_aweme_from_url(url)
            if not aid or aid not in video_index:
                continue
            status = raw.get("status", "")
            outline = raw.get("outline", "")
            is_success = (status == "success" and bool(outline))

            if url not in merged:
                merged[url] = raw
            else:
                existing = merged[url]
                existing_is_success = (existing.get("status") == "success" and bool(existing.get("outline")))
                # 只有当前更好时才替换：success优先，title更长的优先
                if is_success and not existing_is_success:
                    merged[url] = raw
                elif is_success and existing_is_success and len(outline) > len(existing.get("outline", "")):
                    merged[url] = raw

    # 转换为 OutputRecord 并统计
    records: list[OutputRecord] = []
    processed = 0
    skipped = 0
    failed = 0
    touched_increment_files: set[str] = set()

    for raw in merged.values():
        url = raw.get("url", "")
        aid = _extract_aweme_from_url(url)
        status = raw.get("status", "")
        outline = raw.get("outline", "")
        title = raw.get("title", "")
        timestamp = raw.get("timestamp") or datetime.now().isoformat()
        batch_id = raw.get("batch_id", "")

        if status == "success" and outline:
            record = OutputRecord(
                url=url, title=title,
                outline=outline, timestamp=timestamp, status="success",
                aweme_id=aid, batch_id=batch_id,
            )
            records.append(record)
            increment_path = _persist_success_record(
                user_id, output_dir, jsonl_path, record, db,
            )
            if increment_path:
                touched_increment_files.add(increment_path)
            processed += 1
        elif status == "failed":
            records.append(OutputRecord(
                url=url, title=title, outline="", timestamp=timestamp,
                status="failed", aweme_id=aid, batch_id=batch_id,
            ))
            failed += 1
        else:
            records.append(OutputRecord(
                url=url, title=title, outline="", timestamp=timestamp,
                status="skipped", aweme_id=aid, batch_id=batch_id,
            ))
            skipped += 1

    if processed or skipped or failed:
        print(f"  📦 从 {len(partial_paths)} 个 partial 合并恢复:"
              f" {processed} 成功 + {skipped} 跳过 + {failed} 失败")

    return records, merged_errors, processed, skipped, failed, touched_increment_files


def _find_all_partials(user_id: str, output_dir: str) -> list[str]:
    """找到 output_dir 中所有 partial 存档文件，按时间升序排列（旧的在前）。"""
    import glob as _glob
    pattern = os.path.join(output_dir, f"{user_id}_partial_*.json")
    return sorted(_glob.glob(pattern))


def _extract_aweme_from_url(url: str) -> str:
    """从抖音视频 URL 中提取 aweme_id。"""
    match = re.search(r"/video/(\d+)", url)
    return match.group(1) if match else ""


def _load_jsonl_outlines(jsonl_path: str) -> dict[str, str]:
    """从 JSONL 加载 aweme_id → outline 映射，用于恢复已解析视频的提纲。

    JSONL 为 append-only 事实源，即使 DB 已标记 parsed 但本次未从 partial
    恢复，仍可从 JSONL 取回历史上已生成的提纲内容。
    """
    index: dict[str, str] = {}
    if not os.path.exists(jsonl_path):
        return index
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except Exception:
                    continue
                aid = rec.get("aweme_id", "") or _extract_aweme_from_url(rec.get("url", ""))
                outline = rec.get("outline", "")
                if aid and outline:
                    # 保留最后一次出现的 outline（JSONL 按时间追加，后者更完整）
                    index[aid] = outline
    except Exception:
        pass
    return index


def _fill_skipped_from_jsonl(
    records: list["OutputRecord"],
    jsonl_outlines: dict[str, str],
) -> tuple[int, int]:
    """对 records 中 status=skipped 的记录，从 JSONL 恢复提纲。

    用于修复旧版 partial 文件中残留的空提纲 skipped 记录。
    当 JSONL 中存在对应提纲时，将 status 修正为 success 并填充提纲。

    Returns:
        (delta_processed, delta_skipped): 修正计数差量（processed 增加、skipped 减少）。
    """
    delta_p = 0
    delta_s = 0
    for i, rec in enumerate(records):
        if rec.status != "skipped" or rec.outline:
            continue
        aid = _extract_aweme_from_url(rec.url)
        existing = jsonl_outlines.get(aid, "")
        if existing:
            records[i] = OutputRecord(
                url=rec.url, title=rec.title,
                outline=existing,
                timestamp=rec.timestamp, status="success",
                aweme_id=aid, batch_id=rec.batch_id,
            )
            delta_p += 1
            delta_s -= 1
    if delta_p:
        print(f"  🔧 从 JSONL 补齐 {delta_p} 条 skipped 记录的提纲 → success")
    return delta_p, delta_s


def _generate_batch_id() -> str:
    """生成可排序且低碰撞的批次标识；保存进 checkpoint 后保持稳定。"""
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    return f"{timestamp}_{uuid4().hex[:8]}"


def _increment_jsonl_path(user_id: str, output_dir: str, batch_id: str) -> str:
    """返回批次独立 JSONL 的绝对路径。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", batch_id):
        raise ValueError(f"无效 batch_id: {batch_id!r}")
    return os.path.abspath(
        os.path.join(output_dir, f"{user_id}_increment_{batch_id}.jsonl")
    )


def _record_aweme_id(record: "OutputRecord") -> str:
    """优先使用显式 aweme_id，兼容旧记录从 URL 提取。"""
    return record.aweme_id or _extract_aweme_from_url(record.url)


def _upsert_runtime_record(
    records: list["OutputRecord"], record: "OutputRecord",
) -> str:
    """按 aweme_id 合并运行时记录，success 优先，返回替换前状态。"""
    aweme_id = _record_aweme_id(record)
    for index, existing in enumerate(records):
        if _record_aweme_id(existing) != aweme_id:
            continue
        previous_status = existing.status
        if record.status == "success" or existing.status != "success":
            records[index] = record
        return previous_status
    records.append(record)
    return ""


def _jsonl_payload(record: "OutputRecord", aweme_id: str, batch_id: str = "") -> dict:
    """构建累计/增量 JSONL 共用的记录格式。"""
    payload = {
        "aweme_id": aweme_id,
        "url": record.url,
        "title": record.title,
        "outline": record.outline,
        "status": record.status,
        "timestamp": record.timestamp,
    }
    if batch_id:
        payload["batch_id"] = batch_id
    return payload


def _load_jsonl_aweme_ids(path: str) -> set[str]:
    """读取 JSONL 中已有 aweme_id；损坏的单行不会阻断其余恢复。"""
    aweme_ids: set[str] = set()
    if not os.path.exists(path):
        return aweme_ids
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                raw = _json.loads(line)
            except Exception:
                continue
            aid = raw.get("aweme_id", "") or _extract_aweme_from_url(raw.get("url", ""))
            if aid:
                aweme_ids.add(aid)
    return aweme_ids


def _append_jsonl_once(path: str, payload: dict, aweme_id: str) -> bool:
    """按 aweme_id 幂等追加并 fsync；写入失败向上传播。

    Returns:
        True 表示本次实际追加，False 表示文件中已存在该 aweme_id。
    """
    if aweme_id in _load_jsonl_aweme_ids(path):
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    line = _json.dumps(payload, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    return True


def _persist_success_record(
    user_id: str,
    output_dir: str,
    cumulative_path: str,
    record: "OutputRecord",
    db: "ProcessDB",
) -> str:
    """持久化一条成功提纲，严格执行累计 → 增量 → DB 的顺序。

    旧 partial 没有 batch_id 时只补累计事实源，不会生成或混入增量文件。
    返回本次实际追加的增量文件路径；若没有批次或已经存在则返回空串。
    """
    aweme_id = _record_aweme_id(record)
    if record.status != "success" or not record.outline or not aweme_id:
        raise ValueError("仅能持久化带 aweme_id 和非空提纲的 success 记录")

    cumulative_payload = _jsonl_payload(record, aweme_id, record.batch_id)
    _append_jsonl_once(cumulative_path, cumulative_payload, aweme_id)

    increment_path = ""
    if record.batch_id:
        candidate = _increment_jsonl_path(user_id, output_dir, record.batch_id)
        increment_payload = _jsonl_payload(record, aweme_id, record.batch_id)
        if _append_jsonl_once(candidate, increment_payload, aweme_id):
            increment_path = candidate

    db.mark_parsed(aweme_id, record.title)
    return increment_path


def _append_to_jsonl(path: str, record: "OutputRecord") -> None:
    """兼容旧调用点的单文件幂等追加；异常必须向上传播。"""
    aweme_id = _record_aweme_id(record)
    if not aweme_id:
        raise ValueError("JSONL 记录缺少 aweme_id")
    _append_jsonl_once(
        path,
        _jsonl_payload(record, aweme_id, record.batch_id),
        aweme_id,
    )


def _increment_summary(paths: set[str]) -> tuple[int, list[str]]:
    """统计本次实际写入过的批次文件；无新增成功时返回 0 和空路径。"""
    existing_paths = sorted(path for path in paths if os.path.exists(path))
    count = sum(len(_load_jsonl_aweme_ids(path)) for path in existing_paths)
    return count, existing_paths


def _mark_checkpoint_batch_completed(
    checkpoint: "ScrapeCheckpoint",
    records: list["OutputRecord"],
    checkpoint_path: str,
) -> bool:
    """当前批次所有视频均成功落盘后，持久化完成标记。

    视频记录上的 batch_id 保留，供历史失败重试归档；完成标记只用于区分
    “同一批次恢复”与“后续无新增运行”，避免把旧增量显示成新更新。
    """
    if (
        checkpoint.batch_completed
        or not checkpoint.batch_id
        or not checkpoint.batch_aweme_ids
    ):
        return False

    successful_aweme_ids = {
        _record_aweme_id(record)
        for record in records
        if record.status == "success" and bool(record.outline)
    }
    if not set(checkpoint.batch_aweme_ids).issubset(successful_aweme_ids):
        return False

    checkpoint.batch_completed = True
    _save_checkpoint(checkpoint, checkpoint_path)
    return True


def _save_checkpoint(checkpoint: ScrapeCheckpoint, path: str) -> None:
    """原子序列化 checkpoint，避免中断时截断上一个有效版本。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = {
        "user_url": checkpoint.user_url,
        "user_id": checkpoint.user_id,
        "created_at": checkpoint.created_at,
        "videos": checkpoint.videos,
        "total": checkpoint.total,
        "new_count": checkpoint.new_count,
        "skipped_count": checkpoint.skipped_count,
        "batch_id": checkpoint.batch_id,
        "batch_aweme_ids": checkpoint.batch_aweme_ids,
        "batch_completed": checkpoint.batch_completed,
    }
    temp_path = f"{path}.tmp.{os.getpid()}"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
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
        batch_id=data.get("batch_id", ""),
        batch_aweme_ids=data.get("batch_aweme_ids", []),
        batch_completed=data.get("batch_completed", False),
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
