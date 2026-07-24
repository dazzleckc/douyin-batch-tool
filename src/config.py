"""配置管理模块：从 .env 文件与环境变量加载配置。

用法：
    from src.config import load_config, Config, ConfigurationError

    try:
        config = load_config()
    except ConfigurationError as e:
        print(f"配置错误: {e}")
        exit(1)
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(Exception):
    """配置缺失或无效时抛出。"""
    pass


@dataclass
class Config:
    """应用运行时配置。

    必填字段通过 .env / 环境变量注入，可选字段提供合理默认值。
    """
    ark_api_key: str
    douyin_cookie: str
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    ark_model: str = "doubao-seed-1-6-251015"
    output_dir: str = "./output"
    request_interval_min: float = 2.0
    request_interval_max: float = 5.0
    headless: bool = True


def load_config() -> Config:
    """从 .env 文件与环境变量加载配置。

    调用 load_dotenv() 后逐项读取 os.getenv()；必填项 ARK_API_KEY
    和 DOUYIN_COOKIE 缺失或为空时抛出 ConfigurationError。

    Returns:
        Config: 已填充的配置对象。

    Raises:
        ConfigurationError: 必填配置项缺失。
    """
    # 尝试加载 .env；未找到时给出明确提示
    env_path = Path(".env")
    if not env_path.exists():
        print(
            "[配置] 未找到 .env 文件（请在项目根目录创建 .env，"
            "可参考 .env.example 填写必填项）"
        )

    loaded = load_dotenv(env_path)
    if not loaded and env_path.exists():
        print("[配置] .env 文件存在但未能加载，请检查文件格式。")

    # 读取必填配置
    ark_api_key = os.getenv("ARK_API_KEY", "").strip()
    douyin_cookie = os.getenv("DOUYIN_COOKIE", "").strip()

    # 校验必填项
    missing: list[str] = []
    if not ark_api_key:
        missing.append("ARK_API_KEY")
    if not douyin_cookie:
        missing.append("DOUYIN_COOKIE")
    if missing:
        raise ConfigurationError(
            f"缺少必填配置项: {', '.join(missing)}。"
            f"请在 .env 文件或环境变量中设置。"
        )

    # 读取可选配置（使用默认值）
    ark_base_url = os.getenv(
        "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
    ).strip()
    ark_model = os.getenv("ARK_MODEL", "doubao-seed-1-6-251015").strip()
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
        ark_api_key=ark_api_key,
        douyin_cookie=douyin_cookie,
        ark_base_url=ark_base_url,
        ark_model=ark_model,
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
