"""CLI 入口与参数解析模块。

负责 argparse 参数解析、配置加载、合规声明展示、用户确认
以及主流程调度。

用法：
    # 完整流程（采集 + AI）
    python main.py --user https://www.douyin.com/user/MS4wLjABAAAAxxxx

    # 仅采集（保存 checkpoint）
    python main.py --user https://www.douyin.com/user/xxxx --phase scrape

    # 从 checkpoint 执行 AI 阶段
    python main.py --from-checkpoint output/xxxx_checkpoint.json --phase ai
"""

import argparse
import asyncio
import os
import sys

from src.config import load_config, ConfigurationError, load_targets
from src.disclaimer import show_disclaimer
from src.login import ensure_douyin_login, ensure_doubao_login
from src.pipeline import run_pipeline, run_scrape_phase, run_ai_phase, _load_checkpoint


def _resolve_target(args) -> tuple[str | None, str | None]:
    """解析目标博主。优先级: --user > --target > targets.json 第一条。

    Returns:
        (url, name): url 和 name，无可用目标时返回 (None, None)。
    """
    targets = load_targets()

    # CLI 直接提供 URL
    if args.user:
        # 查 targets 中有无匹配名字
        for t in targets:
            if t.url == args.user:
                return t.url, t.name
        return args.user, None

    # 按名称匹配
    if args.target:
        for t in targets:
            if t.name == args.target:
                return t.url, t.name
        print(f"错误: 未找到名为 '{args.target}' 的目标博主", file=sys.stderr)
        print("  可用目标:", file=sys.stderr)
        for t in targets:
            print(f"    {t.name}: {t.url}", file=sys.stderr)
        return None, None

    # 默认取第一个
    if targets:
        return targets[0].url, targets[0].name

    return None, None


def build_parser() -> argparse.ArgumentParser:
    """构建并返回 argparse 参数解析器。"""
    parser = argparse.ArgumentParser(
        description="抖音博主视频批量采集与AI提纲解析工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 完整流程（采集 + AI）
  python main.py --user https://www.douyin.com/user/MS4wLjABAAAAxxxx

  # 仅采集视频（不调用豆包）
  python main.py --user https://www.douyin.com/user/xxxx --phase scrape

  # 从已有 checkpoint 执行 AI 阶段（无需重新采集）
  python main.py --from-checkpoint output/xxxx_checkpoint.json --phase ai

  # 只处理前10个视频
  python main.py --user https://www.douyin.com/user/xxxx --limit 10

  # 显示浏览器窗口
  python main.py --user https://www.douyin.com/user/xxxx --show-browser
        """,
    )

    # 输入源（二选一）
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument(
        "--user",
        help="抖音博主主页 URL（采集 + AI 完整流程或仅采集时使用）",
    )
    input_group.add_argument(
        "--from-checkpoint",
        help="从指定 checkpoint JSON 文件执行 AI 阶段",
    )
    input_group.add_argument(
        "--target",
        help="按名称选择 targets.json 中的目标博主",
    )

    parser.add_argument(
        "--phase", choices=["all", "scrape", "ai"], default="all",
        help="执行阶段: all=完整流程, scrape=仅采集, ai=仅AI解析（默认 all）",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="最多处理视频数（默认全部）",
    )
    parser.add_argument(
        "--output", default="./output",
        help="输出目录（默认 ./output）",
    )
    parser.add_argument(
        "--no-skip", action="store_true",
        help="禁用增量跳过，强制全量处理",
    )
    parser.add_argument(
        "--headless", action="store_true", default=True,
        help="Playwright 无头模式（默认开启）",
    )
    parser.add_argument(
        "--show-browser", action="store_false", dest="headless",
        help="显示浏览器窗口",
    )
    parser.add_argument(
        "--interval-min", type=float, default=2.0,
        help="请求最小间隔秒（默认2.0）",
    )
    parser.add_argument(
        "--interval-max", type=float, default=5.0,
        help="请求最大间隔秒（默认5.0）",
    )
    return parser


def main() -> int:
    """CLI 主入口函数。

    执行流程：
        1. 解析命令行参数
        2. 加载环境变量配置
        3. CLI 参数覆盖配置
        4. 展示合规声明
        5. 用户确认
        6. 执行对应阶段
        7. 打印结果摘要

    Returns:
        int: 退出码。0 表示正常完成，1 表示错误。
    """
    parser = build_parser()
    args = parser.parse_args()

    # --- 参数校验 ---
    if args.phase in ("all", "scrape") and args.from_checkpoint:
        print("错误: --phase all/scrape 不能与 --from-checkpoint 同时使用", file=sys.stderr)
        return 1
    if args.phase == "ai" and not args.from_checkpoint:
        print("错误: --phase ai 需要提供 --from-checkpoint 参数", file=sys.stderr)
        return 1
    if args.limit is not None and args.limit <= 0:
        print("错误: --limit 必须为正整数", file=sys.stderr)
        return 1
    if args.interval_min > args.interval_max:
        print("错误: --interval-min 不能大于 --interval-max", file=sys.stderr)
        return 1
    if args.interval_min < 0 or args.interval_max < 0:
        print("错误: 间隔值不能为负数", file=sys.stderr)
        return 1

    # 解析目标博主
    user_url, blogger_name = (None, None)
    if args.phase in ("all", "scrape"):
        user_url, blogger_name = _resolve_target(args)
        if not user_url:
            print("错误: 未指定目标博主。请使用 --user URL、--target 名称或在 targets.json 中配置", file=sys.stderr)
            return 1

    # --output 路径遍历防护
    project_root = os.path.realpath(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    output_real = os.path.realpath(args.output)
    if not output_real.startswith(project_root + os.sep) and output_real != project_root:
        print("错误: --output 路径必须在项目目录内", file=sys.stderr)
        return 1

    # 1. 加载配置
    try:
        config = load_config()
    except ConfigurationError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 1

    # 2. CLI 参数覆盖配置
    config.headless = args.headless

    # 2.5. 需要抖音采集时，确保已登录（缺失则自动弹窗）
    if args.phase in ("all", "scrape"):
        try:
            asyncio.run(ensure_douyin_login(config))
        except Exception as e:
            print(f"抖音登录失败: {e}", file=sys.stderr)
            return 1

    # 2.6. 需要豆包解析时，确保已登录（缺失则自动弹窗）
    if args.phase in ("all", "ai"):
        try:
            asyncio.run(ensure_doubao_login(config))
        except Exception as e:
            print(f"豆包登录失败: {e}", file=sys.stderr)
            return 1

    # 3. 合规声明
    show_disclaimer()

    # 4. 用户确认
    name_label = f"「{blogger_name}」" if blogger_name else "该博主"
    if args.phase == "scrape":
        prompt = f"\n将仅采集 {name_label} 的视频列表（不调用豆包 AI）。继续？[y/N] "
    elif args.phase == "ai":
        prompt = f"\n将从 checkpoint 读取视频并调用豆包 AI 解析。继续？[y/N] "
    else:
        if args.limit:
            prompt = f"\n将处理 {name_label} 的 {args.limit} 个视频（采集 + AI 解析）。继续？[y/N] "
        else:
            prompt = f"\n将处理 {name_label} 的全部视频（采集 + AI 解析，可能耗时较长）。继续？[y/N] "

    answer = input(prompt).strip().lower()
    if answer not in ("y", "yes"):
        print("已取消。")
        return 0

    # 5. 执行对应阶段
    try:
        if args.phase == "scrape":
            return _run_scrape(user_url, args, config)
        elif args.phase == "ai":
            return _run_ai(args, config)
        else:
            return _run_all(user_url, args, config)
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断。")
        return 0
    except Exception as e:
        print(f"运行失败: {e}", file=sys.stderr)
        return 1


def _run_scrape(user_url, args, config) -> int:
    """仅执行采集阶段。"""
    checkpoint = asyncio.run(run_scrape_phase(
        user_url=user_url,
        limit=args.limit,
        output_dir=args.output,
        config=config,
    ))
    print(f"\n{'=' * 50}")
    print("采集完成！")
    print(f"  全部视频: {checkpoint.total}")
    print(f"  新视频: {checkpoint.new_count}")
    print(f"  已跳过: {checkpoint.skipped_count}")
    print(f"  Checkpoint: {args.output}/{checkpoint.user_id}_checkpoint.json")
    print(f"\n下一步: python main.py --from-checkpoint {args.output}/{checkpoint.user_id}_checkpoint.json --phase ai")
    print(f"{'=' * 50}")
    return 0


def _run_ai(args, config) -> int:
    """仅执行 AI 阶段（从 checkpoint）。"""
    result = asyncio.run(run_ai_phase(
        checkpoint_path=args.from_checkpoint,
        output_dir=args.output,
        no_skip=args.no_skip,
        interval_range=(args.interval_min, args.interval_max),
        config=config,
    ))
    _print_pipeline_result(result)
    return 0


def _run_all(user_url, args, config) -> int:
    """执行完整流程：采集 → AI 解析。

    采集阶段无新增时，自动检查是否需要 AI 解析——有未解析视频则直接进入 AI 阶段。
    """
    checkpoint = asyncio.run(run_scrape_phase(
        user_url=user_url,
        limit=args.limit,
        output_dir=args.output,
        config=config,
    ))

    checkpoint_path = f"{args.output}/{checkpoint.user_id}_checkpoint.json"

    if checkpoint.new_count == 0:
        # 无新增视频：检查是否有已采集但未解析的视频需要处理
        existing = _load_checkpoint(checkpoint_path)
        if not existing.videos:
            print("\n✅ 无视频需要处理。")
            return 0
        print(f"\n📋 无新增视频，自动进入 AI 解析阶段（视频池共 {existing.total} 个）")

    result = asyncio.run(run_ai_phase(
        checkpoint_path=checkpoint_path,
        output_dir=args.output,
        no_skip=args.no_skip,
        interval_range=(args.interval_min, args.interval_max),
        config=config,
    ))
    _print_pipeline_result(result)
    return 0


def _print_pipeline_result(result) -> None:
    """打印流水线结果摘要。"""
    print(f"\n{'=' * 50}")
    print("已安全中断，断点已保存。" if result.interrupted else "处理完成！")
    print(f"  总计视频: {result.total}")
    print(f"  成功处理: {result.processed}")
    print(f"  跳过(已处理): {result.skipped}")
    print(f"  失败: {result.failed}")
    if result.output_file:
        print(f"  输出文件: {result.output_file}")
    print(f"  本批增量: {result.increment_count}")
    if result.increment_files:
        for path in result.increment_files:
            print(f"  增量文件: {path}")
    else:
        print("  增量文件: 未生成（本次没有新增成功提纲）")
    if result.errors:
        print(f"\n失败详情:")
        for err in result.errors[:10]:
            print(f"  - {err.get('aweme_id', err.get('title', 'Unknown'))}: "
                  f"{err.get('error', 'Unknown error')}")
    print(f"{'=' * 50}")
