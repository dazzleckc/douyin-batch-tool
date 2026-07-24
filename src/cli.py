"""CLI 入口与参数解析模块。

负责 argparse 参数解析、配置加载、合规声明展示、用户确认
以及主流程调度。通过 asyncio.run() 调用流水线并输出摘要。

用法：
    from src.cli import main, build_parser
    sys.exit(main())
"""

import argparse
import asyncio
import os
import sys

from src.config import load_config, ConfigurationError
from src.disclaimer import show_disclaimer
from src.pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    """构建并返回 argparse 参数解析器。"""
    parser = argparse.ArgumentParser(
        description="抖音博主视频批量采集与AI提纲解析工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py --user https://www.douyin.com/user/MS4wLjABAAAAxxxx
  python main.py --user https://www.douyin.com/user/xxxx --limit 10
  python main.py --user https://www.douyin.com/user/xxxx --no-skip --output ./my_output
        """,
    )
    parser.add_argument(
        "--user", required=True,
        help="抖音博主主页 URL",
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
        2. 加载配置（.env）
        3. CLI 参数覆盖配置
        4. 展示合规声明
        5. 用户确认
        6. 运行流水线
        7. 打印结果摘要

    Returns:
        int: 退出码。0 表示正常完成（即使部分视频失败），
             1 表示配置错误或运行时异常。
    """
    parser = build_parser()
    args = parser.parse_args()

    # 参数校验
    if args.limit is not None and args.limit <= 0:
        print("错误: --limit 必须为正整数", file=sys.stderr)
        return 1
    if args.interval_min > args.interval_max:
        print("错误: --interval-min 不能大于 --interval-max", file=sys.stderr)
        return 1
    if args.interval_min < 0 or args.interval_max < 0:
        print("错误: 间隔值不能为负数", file=sys.stderr)
        return 1

    # --output 路径遍历防护：确保输出目录在项目目录内
    project_root = os.path.realpath(
        "/Users/chenkaichen/WorkBuddy/抖音批量采集批处理工具/"
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

    # 3. 合规声明
    show_disclaimer()

    # 4. 用户确认
    if args.limit:
        prompt = f"\n将处理最多 {args.limit} 个视频。继续？[y/N] "
    else:
        prompt = "\n将处理该博主全部视频（可能耗时较长）。继续？[y/N] "

    answer = input(prompt).strip().lower()
    if answer not in ("y", "yes"):
        print("已取消。")
        return 0

    # 5. 运行流水线
    try:
        result = asyncio.run(run_pipeline(
            user_url=args.user,
            limit=args.limit,
            output_dir=args.output,
            no_skip=args.no_skip,
            interval_range=(args.interval_min, args.interval_max),
            config=config,
        ))
    except Exception as e:
        print(f"运行失败: {e}", file=sys.stderr)
        return 1

    # 6. 输出摘要
    print(f"\n{'=' * 50}")
    print("处理完成！")
    print(f"  总计视频: {result.total}")
    print(f"  成功处理: {result.processed}")
    print(f"  跳过(已处理): {result.skipped}")
    print(f"  失败: {result.failed}")
    if result.output_file:
        print(f"  输出文件: {result.output_file}")
    if result.errors:
        print(f"\n失败详情:")
        for err in result.errors[:10]:  # 只展示前 10 个
            print(f"  - {err.get('aweme_id', err.get('title', 'Unknown'))}: {err.get('error', 'Unknown error')}")
    print(f"{'=' * 50}")

    return 0  # 部分失败仍返回 0（非致命）
