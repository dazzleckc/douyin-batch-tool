"""抖音博主视频批量采集与AI提纲解析工具 — CLI 入口。

用法:
    python main.py --user https://www.douyin.com/user/MS4wLjABAAAAxxxx
    python main.py --user https://www.douyin.com/user/xxxx --limit 10 --no-skip
"""

import sys

from src.cli import main

if __name__ == "__main__":
    sys.exit(main())
