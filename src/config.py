"""配置管理模块：从环境变量加载配置。

用法：
    from src.config import load_config, Config, ConfigurationError, load_targets, Target

    try:
        config = load_config()
        targets = load_targets()
    except ConfigurationError as e:
        print(f"配置错误: {e}")
        exit(1)
"""

import json
import os
from dataclasses import dataclass


class ConfigurationError(Exception):
    """配置缺失或无效时抛出。"""
    pass


@dataclass
class Target:
    """抖音目标博主配置。"""
    name: str
    url: str


@dataclass
class Config:
    """应用运行时配置。

    抖音登录态优先级: douyin_state.json > DOUYIN_COOKIE 环境变量
    豆包登录态优先级: doubao_state.json > DOUBAO_COOKIE 环境变量
    """
    douyin_cookie: str = ""
    douyin_state_path: str = "douyin_state.json"
    doubao_cookie: str = ""
    doubao_state_path: str = "doubao_state.json"
    output_dir: str = "./output"
    request_interval_min: float = 2.0
    request_interval_max: float = 5.0
    headless: bool = True
    targets_path: str = "targets.json"


def load_targets(path: str = "targets.json") -> list[Target]:
    """加载目标博主配置。

    Args:
        path: targets.json 文件路径。

    Returns:
        list[Target]: 目标博主列表。

    Raises:
        ConfigurationError: 文件不存在或格式错误。
    """
    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise ConfigurationError(f"targets.json 格式错误: {e}")

    targets = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "").strip()
        url = item.get("url", "").strip()
        if name and url:
            targets.append(Target(name=name, url=url))
    return targets


def load_config() -> Config:
    """从环境变量加载配置。

    所有配置项均有默认值，无需创建任何文件。
    登录态通过弹窗自动生成 *_state.json，无需手动配置。
    """
    # 登录态（优先使用弹窗生成的 *_state.json）
    douyin_state_path = os.getenv("DOUYIN_STATE_PATH", "douyin_state.json").strip()
    doubao_state_path = os.getenv("DOUBAO_STATE_PATH", "doubao_state.json").strip()
    douyin_cookie = os.getenv("DOUYIN_COOKIE", "").strip()
    doubao_cookie = os.getenv("DOUBAO_COOKIE", "").strip()

    # 读取可选配置（使用默认值）
    output_dir = os.getenv("OUTPUT_DIR", "./output").strip()
    request_interval_min = _parse_float(
        os.getenv("REQUEST_INTERVAL_MIN"), 2.0
    )
    request_interval_max = _parse_float(
        os.getenv("REQUEST_INTERVAL_MAX"), 5.0
    )
    headless = os.getenv("HEADLESS", "true").strip().lower() not in (
        "false", "0", "no", "off"
    )

    return Config(
        douyin_cookie=douyin_cookie,
        douyin_state_path=douyin_state_path,
        doubao_cookie=doubao_cookie,
        doubao_state_path=doubao_state_path,
        output_dir=output_dir,
        request_interval_min=request_interval_min,
        request_interval_max=request_interval_max,
        headless=headless,
    )


def _parse_float(raw: str | None, default: float) -> float:
    """安全地将字符串转为 float，失败时返回默认值。"""
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except (ValueError, TypeError):
        return default
