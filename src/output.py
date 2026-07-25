"""结果输出模块：将 OutputRecord 列表序列化为 JSON 文件。

用法：
    from src.models import OutputRecord
    from src.output import write_results, generate_output_filename

    records = [OutputRecord(...)]
    fname = generate_output_filename("my_user")
    path = write_results(records, f"./output/{fname}")
"""

import json
import os
import re
from datetime import datetime

from src.models import OutputRecord


def generate_output_filename(user_identifier: str) -> str:
    """生成 {安全标识}_{YYYYMMDD}.json 格式文件名。

    从 user_identifier 中提取安全字符（字母、数字、连字符、下划线），
    移除空格与其他特殊字符，并与当前日期拼接。

    Args:
        user_identifier: 用户标识（博主名、账号 ID 等）。

    Returns:
        str: 形如 "some_user_20250724.json" 的文件名。
    """
    safe_part = re.sub(r"[^\w\-]", "_", user_identifier).strip("_")
    if not safe_part:
        safe_part = "output"
    date_part = datetime.now().strftime("%Y%m%d")
    return f"{safe_part}_{date_part}.json"


def write_results(records: list[OutputRecord], output_path: str, indent: int = 2) -> str:
    """将 OutputRecord 列表序列化为 JSON 写入文件，返回实际文件路径。

    自动创建输出文件的父目录（os.makedirs）。每个 OutputRecord
    实例通过 dataclass 字段转换为 dict 后写入 JSON 数组。

    Args:
        records: OutputRecord 列表。
        output_path: 目标文件路径。
        indent: JSON 缩进空格数，默认 2。

    Returns:
        str: 实际写入的文件绝对路径。
    """
    # 自动创建输出目录
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    # 序列化为 dict 列表
    data = [
        {
            "url": r.url,
            "title": r.title,
            "outline": r.outline,
            "timestamp": r.timestamp,
            "status": r.status,
        }
        for r in records
    ]

    # 写入 JSON（ensure_ascii=False 保证 emoji、中文等字符原样输出）
    content = json.dumps(data, ensure_ascii=False, indent=indent)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    return os.path.abspath(output_path)
