"""豆包 AI 客户端：通过火山引擎 ARK OpenAI 兼容接口调用豆包大模型生成视频文字提纲。

用法：
    from src.config import Config
    from src.ai_client import AIClient

    config = Config(...)
    client = AIClient(config)
    result = client.generate_outline("视频标题", description="视频描述")
    if result.success:
        print(result.outline_markdown)
"""

from openai import OpenAI

from src.config import Config
from src.models import OutlineResult


class AIError(Exception):
    """AI 客户端通用异常。"""


class AuthError(AIError):
    """鉴权失败异常（401/403），不重试。"""


class TimeoutError(AIError):
    """请求超时异常。SDK 自带 max_retries 已处理重试。"""


class AIClient:
    """豆包大模型客户端，通过火山引擎 ARK 的 OpenAI 兼容接口调用。"""

    def __init__(self, config: Config) -> None:
        """初始化 OpenAI 兼容客户端，指向 ARK 端点。

        Args:
            config: 包含 ark_api_key 和 ark_base_url 的配置对象。
        """
        self._config = config
        self._client = OpenAI(
            api_key=config.ark_api_key,
            base_url=config.ark_base_url,
        )

    def generate_outline(self, title: str, description: str = "") -> OutlineResult:
        """对单个视频标题+描述调用豆包 API，返回结构化提纲。

        Args:
            title: 视频标题。
            description: 视频描述（可选）。

        Returns:
            OutlineResult: 包含提纲 Markdown 和原始响应。失败时 success=False。
        """
        prompt = self._build_prompt(title, description)

        try:
            response = self._client.chat.completions.create(
                model=self._config.ark_model,
                messages=[{"role": "user", "content": prompt}],
                timeout=60,
                max_retries=2,
            )
            outline_md = response.choices[0].message.content or ""
            return OutlineResult(
                aweme_id="",
                outline_markdown=outline_md,
                raw_response=outline_md,
                success=True,
            )

        except Exception as exc:
            return self._handle_error(exc)

    def _build_prompt(self, title: str, description: str) -> str:
        """构建发送给豆包模型的 Prompt。

        Args:
            title: 视频标题。
            description: 视频描述。

        Returns:
            完整的 Prompt 字符串。
        """
        prompt = (
            "你是一个视频内容分析助手。请根据以下视频标题，推测视频内容并生成一份结构化的文字提纲。\n"
            "提纲应包含两级层级（一级要点和二级子要点），以 Markdown 格式输出。\n"
            f"视频标题：{title}\n"
        )
        if description:
            prompt += f"视频描述：{description}\n"
        return prompt

    def _handle_error(self, exc: Exception) -> OutlineResult:
        """将 openai SDK 异常映射为项目异常并返回失败结果。

        Args:
            exc: openai SDK 抛出的原始异常。

        Returns:
            包含错误信息的 OutlineResult（success=False）。
        """
        error_message = str(exc)

        # 检查 openai SDK 异常类型（兼容 v1 和 v2）
        exc_type_name = type(exc).__name__
        module_name = type(exc).__module__

        # 401 / 403 → AuthError（不应该重试）
        if exc_type_name in ("AuthenticationError", "PermissionDeniedError"):
            raise AuthError(error_message) from exc

        # 超时或连接错误 → TimeoutError
        if exc_type_name in ("APITimeoutError", "APIConnectionError", "Timeout"):
            raise TimeoutError(error_message) from exc

        # 其他错误 → 返回失败结果（不抛出，由调用方检查 success 字段）
        return OutlineResult(
            aweme_id="",
            outline_markdown="",
            raw_response="",
            success=False,
            error_message=f"AIError: {error_message}",
        )
