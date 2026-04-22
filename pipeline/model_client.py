"""统一 LLM 调用客户端。

支持 DeepSeek、Qwen（通义千问）、OpenAI 三种模型提供商，
均通过 OpenAI 兼容 API 协议调用。使用 httpx 直接发起 HTTP 请求，
不依赖 openai SDK。

环境变量:
    LLM_PROVIDER: 模型提供商，可选 deepseek / qwen / openai，默认 deepseek
    LLM_API_KEY: API 密钥（各提供商通用，也可用提供商专属变量覆盖）
    LLM_MODEL: 模型名称，默认取提供商默认模型
    DEEPSEEK_API_KEY: DeepSeek 专属密钥
    QWEN_API_KEY: Qwen 专属密钥
    OPENAI_API_KEY: OpenAI 专属密钥

用法:
    from pipeline.model_client import quick_chat

    resp = quick_chat("用一句话解释什么是 RAG")
    print(resp.content)
"""

from __future__ import annotations

import logging
import math
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MAX_RETRIES: int = 3
REQUEST_TIMEOUT: float = 60.0

PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "env_key": "DEEPSEEK_API_KEY",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "env_key": "QWEN_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "env_key": "OPENAI_API_KEY",
    },
}

PRICING_PER_MILLION: dict[str, dict[str, float]] = {
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    "qwen-plus": {"input": 0.80, "output": 2.00},
    "qwen-turbo": {"input": 0.30, "output": 0.60},
    "qwen-max": {"input": 2.40, "output": 9.60},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
}


@dataclass
class LLMResponse:
    """LLM 响应数据类。

    Attributes:
        content: 模型生成的文本内容。
        model: 实际使用的模型名称。
        usage: Token 用量统计，包含 prompt_tokens、completion_tokens、total_tokens。
        cost_usd: 本次调用估算成本（美元）。
        elapsed_seconds: 请求耗时（秒）。
    """

    content: str
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    elapsed_seconds: float = 0.0


class LLMProvider(ABC):
    """LLM 提供商抽象基类。

    所有具体提供商必须实现 chat 方法。
    """

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """发送聊天请求。

        Args:
            messages: 消息列表，每条消息为 {"role": "...", "content": "..."} 格式。
            model: 模型名称，为 None 时使用默认模型。
            temperature: 采样温度，0.0 ~ 2.0。
            max_tokens: 最大生成 Token 数，None 表示不限。

        Returns:
            LLMResponse 实例。
        """


class OpenAICompatibleProvider(LLMProvider):
    """OpenAI 兼容 API 提供商实现。

    通过 httpx 直接调用 OpenAI 兼容的 /chat/completions 端点，
    适配 DeepSeek、Qwen、OpenAI 等提供商。

    Args:
        provider_name: 提供商名称，必须在 PROVIDERS 中注册。
        api_key: API 密钥，为 None 时从环境变量读取。
        base_url: API 基础 URL，为 None 时使用提供商默认值。
        default_model: 默认模型名称，为 None 时使用提供商默认值。
    """

    def __init__(
        self,
        provider_name: str,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
    ) -> None:
        if provider_name not in PROVIDERS:
            raise ValueError(
                f"不支持的提供商 '{provider_name}'，可选: {', '.join(PROVIDERS)}"
            )

        cfg = PROVIDERS[provider_name]
        self._provider_name = provider_name
        self._base_url = (base_url or cfg["base_url"]).rstrip("/")
        self._default_model = default_model or cfg["default_model"]
        self._api_key = (
            api_key or os.getenv(cfg["env_key"]) or os.getenv("LLM_API_KEY", "")
        )

        if not self._api_key:
            raise ValueError(
                f"未找到 API 密钥。请设置环境变量 {cfg['env_key']} 或 LLM_API_KEY"
            )

    @property
    def provider_name(self) -> str:
        """当前提供商名称。"""
        return self._provider_name

    @property
    def default_model(self) -> str:
        """默认模型名称。"""
        return self._default_model

    def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """发送聊天请求到 OpenAI 兼容 API。

        Args:
            messages: 消息列表。
            model: 模型名称。
            temperature: 采样温度。
            max_tokens: 最大生成 Token 数。

        Returns:
            LLMResponse 实例。

        Raises:
            httpx.HTTPStatusError: HTTP 状态码非 2xx 时抛出。
            httpx.TimeoutException: 请求超时时抛出。
        """
        model = model or self._default_model
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        start = time.monotonic()
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.post(url, json=body, headers=headers)
            resp.raise_for_status()
        elapsed = time.monotonic() - start

        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        usage_dict = {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
        cost = estimate_cost(
            model, usage_dict["prompt_tokens"], usage_dict["completion_tokens"]
        )

        logger.debug(
            "LLM 请求完成: provider=%s, model=%s, "
            "prompt_tokens=%d, completion_tokens=%d, cost=$%.6f, elapsed=%.2fs",
            self._provider_name,
            model,
            usage_dict["prompt_tokens"],
            usage_dict["completion_tokens"],
            cost,
            elapsed,
        )

        return LLMResponse(
            content=content,
            model=data.get("model", model),
            usage=usage_dict,
            cost_usd=cost,
            elapsed_seconds=round(elapsed, 3),
        )


def create_provider(
    provider_name: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> OpenAICompatibleProvider:
    """根据环境变量或参数创建 LLM 提供商实例。

    Args:
        provider_name: 提供商名称，为 None 时读取环境变量 LLM_PROVIDER。
        api_key: API 密钥，为 None 时从环境变量读取。
        model: 默认模型名称，为 None 时读取环境变量 LLM_MODEL 或使用提供商默认。

    Returns:
        OpenAICompatibleProvider 实例。
    """
    provider_name = provider_name or os.getenv("LLM_PROVIDER", "deepseek")
    resolved_model = model or os.getenv("LLM_MODEL") or None
    return OpenAICompatibleProvider(
        provider_name, api_key=api_key, default_model=resolved_model
    )


def chat_with_retry(
    messages: list[dict[str, str]],
    provider: OpenAICompatibleProvider | None = None,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    max_retries: int = MAX_RETRIES,
) -> LLMResponse:
    """带重试的聊天请求，指数退避策略。

    最多重试 max_retries 次，每次间隔 2^attempt 秒（1s, 2s, 4s），
    仅对可重试的 HTTP 错误（429、5xx）进行重试。

    Args:
        messages: 消息列表。
        provider: 提供商实例，为 None 时自动创建。
        model: 模型名称。
        temperature: 采样温度。
        max_tokens: 最大生成 Token 数。
        max_retries: 最大重试次数，默认 3。

    Returns:
        LLMResponse 实例。

    Raises:
        httpx.HTTPStatusError: 重试耗尽后仍失败时抛出最后一次异常。
    """
    provider = provider or create_provider()
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return provider.chat(
                messages, model=model, temperature=temperature, max_tokens=max_tokens
            )
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            status = exc.response.status_code
            if status == 429 or status >= 500:
                if attempt < max_retries:
                    wait = 2**attempt
                    logger.warning(
                        "请求失败 (HTTP %d)，第 %d/%d 次重试，等待 %ds...",
                        status,
                        attempt + 1,
                        max_retries,
                        wait,
                    )
                    time.sleep(wait)
                    continue
            raise
        except httpx.TimeoutException as exc:
            last_exc = exc
            if attempt < max_retries:
                wait = 2**attempt
                logger.warning(
                    "请求超时，第 %d/%d 次重试，等待 %ds...",
                    attempt + 1,
                    max_retries,
                    wait,
                )
                time.sleep(wait)
                continue
            raise

    if last_exc is not None:
        raise last_exc

    raise RuntimeError("不可达: chat_with_retry 异常路径")


def estimate_tokens(text: str) -> int:
    """估算文本的 Token 数量。

    采用简单的字符比例估算：英文约 4 字符 = 1 Token，
    中文约 1.5 字符 = 1 Token。混合文本取加权平均。
    仅为粗略估算，实际 Token 数以 API 返回为准。

    Args:
        text: 待估算文本。

    Returns:
        估算的 Token 数量。
    """
    if not text:
        return 0

    chinese_chars = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other_chars = len(text) - chinese_chars

    tokens = math.ceil(chinese_chars / 1.5 + other_chars / 4.0)
    return max(tokens, 1)


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """估算 API 调用成本（美元）。

    基于各提供商公开定价，按每百万 Token 计费。

    Args:
        model: 模型名称。
        prompt_tokens: 输入 Token 数。
        completion_tokens: 输出 Token 数。

    Returns:
        估算成本（美元），未收录模型返回 0.0。
    """
    pricing = PRICING_PER_MILLION.get(model)
    if not pricing:
        return 0.0

    input_cost = prompt_tokens * pricing["input"] / 1_000_000
    output_cost = completion_tokens * pricing["output"] / 1_000_000
    return round(input_cost + output_cost, 8)


def quick_chat(
    prompt: str,
    system: str | None = None,
    model: str | None = None,
    temperature: float = 0.7,
    provider: OpenAICompatibleProvider | None = None,
) -> LLMResponse:
    """便捷函数：一句话调用 LLM。

    自动构建消息列表、创建提供商实例、带重试发送请求。

    Args:
        prompt: 用户消息内容。
        system: 系统提示词，为 None 时不添加 system 消息。
        model: 模型名称。
        temperature: 采样温度。
        provider: 提供商实例，为 None 时自动创建。

    Returns:
        LLMResponse 实例。

    Example::

        resp = quick_chat("用一句话解释 RAG")
        print(resp.content)
    """
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    return chat_with_retry(
        messages,
        provider=provider,
        model=model,
        temperature=temperature,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 60)
    print("LLM 统一客户端测试")
    print("=" * 60)

    print("\n--- 1. Token 估算 ---")
    samples = [
        "Hello, world!",
        "这是一段中文测试文本，用于验证 Token 估算功能。",
        "Mixed 混合 text 文本 with 123 numbers",
        "",
    ]
    for s in samples:
        tokens = estimate_tokens(s)
        print(f"  文本: {s!r:50s} → 估算 {tokens:>4d} tokens")

    print("\n--- 2. 成本估算 ---")
    test_cases = [
        ("deepseek-chat", 1000, 500),
        ("qwen-plus", 2000, 1000),
        ("gpt-4o-mini", 500, 200),
        ("unknown-model", 1000, 500),
    ]
    for model_name, pt, ct in test_cases:
        cost = estimate_cost(model_name, pt, ct)
        print(f"  {model_name:20s}: input={pt:>5d}, output={ct:>4d} → ${cost:.6f}")

    print("\n--- 3. 创建提供商（环境变量检测）---")
    provider_name = os.getenv("LLM_PROVIDER", "deepseek")
    api_key_set = bool(
        os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("QWEN_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("LLM_API_KEY")
    )
    print(f"  LLM_PROVIDER = {provider_name}")
    print(f"  API Key 已配置: {api_key_set}")

    if not api_key_set:
        print("\n  [跳过] 未检测到 API Key，跳过在线调用测试")
        print("  设置环境变量后可运行在线测试：")
        print("    export DEEPSEEK_API_KEY=sk-xxx")
        print("    python -m pipeline.model_client")
    else:
        print("\n--- 4. 在线调用测试 ---")
        try:
            prov = create_provider()
            print(f"  提供商: {prov.provider_name}, 默认模型: {prov.default_model}")

            resp = quick_chat(
                "请用一句话回答：1+1等于几？",
                system="你是一个简洁的助手，只回答核心答案。",
                temperature=0.1,
            )
            print(f"  响应: {resp.content}")
            print(f"  模型: {resp.model}")
            print(f"  用量: {resp.usage}")
            print(f"  成本: ${resp.cost_usd:.6f}")
            print(f"  耗时: {resp.elapsed_seconds:.2f}s")
        except Exception as exc:
            print(f"  [失败] {type(exc).__name__}: {exc}")

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)
