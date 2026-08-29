"""集中管理主模型与幕后模型调用。

所有 HTTP、模型选择、超时和 JSON 解析都从这里经过，避免插件各自维护
一套实现。幕后模型未配置时会正确回退到当前主模型。
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from typing import Any

import httpx
from nonebot import get_driver
from nonebot.log import logger

driver = get_driver()
config = driver.config

AI_PROVIDER = str(getattr(config, "ai_provider", "claude")).lower()
CLAUDE_API_KEY = str(getattr(config, "claude_api_key", ""))
CLAUDE_MODEL = str(getattr(config, "claude_model", "claude-sonnet-4-20250514"))
DEEPSEEK_API_KEY = str(getattr(config, "deepseek_api_key", ""))
DEEPSEEK_MODEL = str(getattr(config, "deepseek_model", "deepseek-chat"))
LMSTUDIO_API_URL = str(
    getattr(config, "lmstudio_api_url", "http://127.0.0.1:1234/v1/chat/completions")
)
LMSTUDIO_MODEL = str(
    getattr(config, "lmstudio_model", "qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive")
)
BACKGROUND_API_URL = str(getattr(config, "background_api_url", ""))
BACKGROUND_API_KEY = str(getattr(config, "background_api_key", ""))
BACKGROUND_MODEL = str(getattr(config, "background_model", ""))
DEFAULT_MAX_TOKENS = int(getattr(config, "max_tokens", 1024))
MAX_CONCURRENCY = int(getattr(config, "llm_max_concurrency", 4))

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()
_request_semaphore = asyncio.Semaphore(max(1, MAX_CONCURRENCY))
_background_auth_disabled = False


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    timeout=httpx.Timeout(60.0, connect=15.0),
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                )
    return _client


@driver.on_shutdown
async def _close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _clean_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {"role": str(item["role"]), "content": str(item["content"])}
        for item in copy.deepcopy(messages)
        if item.get("role") and item.get("content") is not None
    ]


async def chat(
    messages: list[dict[str, str]],
    *,
    system: str = "",
    max_tokens: int | None = None,
    background: bool = False,
    timeout: float | None = None,
    json_mode: bool = False,
) -> str:
    """调用主模型或幕后模型并返回纯文本。"""
    global _background_auth_disabled
    safe_messages = _clean_messages(messages)
    token_limit = max_tokens or DEFAULT_MAX_TOKENS

    if (
        background
        and BACKGROUND_API_URL
        and BACKGROUND_MODEL
        and not _background_auth_disabled
    ):
        try:
            return await _call_openai_compatible(
                BACKGROUND_API_URL,
                BACKGROUND_API_KEY,
                BACKGROUND_MODEL,
                safe_messages,
                system,
                token_limit,
                timeout or 60,
                no_think=True,
                json_mode=json_mode,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {401, 403}:
                raise
            _background_auth_disabled = True
            logger.warning(
                "幕后模型鉴权失败；本进程后续意图识别、摘要和影子评价"
                "将自动回退到主模型"
            )

    if AI_PROVIDER == "deepseek":
        if not DEEPSEEK_API_KEY:
            raise RuntimeError("DEEPSEEK_API_KEY 未配置")
        return await _call_openai_compatible(
            "https://api.deepseek.com/chat/completions",
            DEEPSEEK_API_KEY,
            DEEPSEEK_MODEL,
            safe_messages,
            system,
            token_limit,
            timeout or 45,
            no_think=background,
            json_mode=json_mode,
        )

    if AI_PROVIDER == "lmstudio":
        return await _call_openai_compatible(
            LMSTUDIO_API_URL,
            "",
            LMSTUDIO_MODEL,
            safe_messages,
            system,
            token_limit,
            timeout or 120,
            no_think=True,
            thinking=False,
            json_mode=json_mode,
        )

    if AI_PROVIDER != "claude":
        raise RuntimeError(f"不支持的 AI_PROVIDER: {AI_PROVIDER}")
    if not CLAUDE_API_KEY:
        raise RuntimeError("CLAUDE_API_KEY 未配置")
    return await _call_claude(
        safe_messages,
        system,
        token_limit,
        timeout or 45,
    )


async def complete(
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 300,
    background: bool = True,
    timeout: float | None = None,
) -> str:
    return await chat(
        [{"role": "user", "content": prompt}],
        system=system,
        max_tokens=max_tokens,
        background=background,
        timeout=timeout,
    )


async def complete_json(
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 400,
    background: bool = True,
    timeout: float | None = None,
) -> dict[str, Any] | list[Any]:
    text = await chat(
        [{"role": "user", "content": prompt}],
        system=system,
        max_tokens=max_tokens,
        background=background,
        timeout=timeout,
        json_mode=True,
    )
    try:
        return parse_json(text)
    except ValueError as first_error:
        # 如果第一次返回的是整段分析文字，“修复这段文字”会让模型继续讨论题目，
        # 而不是完成原任务。重新执行原任务既保留原始事实约束，也更容易得到结构化结果。
        logger.warning("模型未返回合法 JSON，正在严格重试原任务")
        strict_prompt = (
            prompt
            + "\n\n你上一次没有按格式回答。现在重新完成上面的原任务："
            "第一字符必须是 { 或 [，最后一字符必须是 } 或 ]；只输出合法JSON，不要解释。"
        )
        strict_system = "\n".join(
            part
            for part in (
                system,
                "最高优先级格式要求：只输出严格合法的JSON对象或数组，不要分析、解释或Markdown。",
            )
            if part
        )
        retried = await chat(
            [{"role": "user", "content": strict_prompt}],
            system=strict_system,
            max_tokens=max_tokens,
            background=background,
            timeout=timeout,
            json_mode=True,
        )
        try:
            return parse_json(retried)
        except ValueError as retry_error:
            raise ValueError(
                f"模型连续两次没有返回合法 JSON；首次：{str(first_error)[:180]}；"
                f"重试：{str(retry_error)[:180]}"
            ) from retry_error


def parse_json(text: str) -> dict[str, Any] | list[Any]:
    """容忍 Markdown 包裹和少量前后说明，但拒绝非 JSON 内容。"""
    cleaned = text.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as original_error:
        repaired = _repair_common_json_errors(cleaned)
        if repaired != cleaned:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass
        decoder = json.JSONDecoder()
        candidates: list[tuple[int, int, dict[str, Any] | list[Any]]] = []
        # 模型有时会先写一句说明或一个错误示例，再给真正结果。检查所有可能
        # 的 JSON 起点，并优先选择覆盖文本最多的完整对象，而不是只看第一个括号。
        starts = [index for index, char in enumerate(repaired) if char in "{["]
        for start in starts:
            try:
                value, consumed = decoder.raw_decode(repaired[start:])
                if isinstance(value, (dict, list)):
                    candidates.append((consumed, start, value))
            except json.JSONDecodeError:
                continue
        if candidates:
            return max(candidates, key=lambda item: (item[0], item[1]))[2]
        raise ValueError(f"模型没有返回合法 JSON: {cleaned[:200]}") from original_error


def _repair_common_json_errors(text: str) -> str:
    """修复模型常见的尾逗号、行注释和正号，不猜测缺失字段。"""
    without_comments = _strip_line_comments(text)
    without_trailing_commas = re.sub(r",\s*([}\]])", r"\1", without_comments)
    return re.sub(r"(:\s*)\+(?=\d)", r"\1", without_trailing_commas)


def _strip_line_comments(text: str) -> str:
    result: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < len(text) and text[index + 1] == "/":
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        result.append(char)
        index += 1
    return "".join(result)


async def _call_claude(
    messages: list[dict[str, str]],
    system: str,
    max_tokens: int,
    timeout: float,
) -> str:
    client = await _get_client()
    response = await _post_with_retry(
        client,
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        },
        timeout=timeout,
    )
    _raise_for_status(response, "Claude")
    data = response.json()
    blocks = data.get("content", [])
    text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
    if not text.strip():
        raise RuntimeError("Claude 返回了空内容")
    return text.strip()


async def _call_openai_compatible(
    url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    system: str,
    max_tokens: int,
    timeout: float,
    *,
    no_think: bool = False,
    thinking: bool | dict[str, str] | None = None,
    json_mode: bool = False,
) -> str:
    payload_messages = []
    if system:
        payload_messages.append({"role": "system", "content": system})
    payload_messages.extend(messages)
    is_deepseek = "deepseek" in url.lower() or model.lower().startswith("deepseek")
    if (
        no_think
        and not is_deepseek
        and payload_messages
        and payload_messages[-1]["role"] == "user"
    ):
        content = payload_messages[-1]["content"]
        if "/no_think" not in content:
            payload_messages[-1]["content"] = f"/no_think {content}"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload: dict[str, Any] = {
        "model": model,
        "messages": payload_messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if thinking is not None:
        payload["thinking"] = thinking
    elif no_think and is_deepseek:
        # DeepSeek 推理模型不会识别文本形式的 /no_think。若不使用结构化
        # 开关，推理 token 可能耗尽 max_tokens，使 content 只剩下一个“{”。
        payload["thinking"] = {"type": "disabled"}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    client = await _get_client()
    response = await _post_with_retry(
        client,
        url,
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    _raise_for_status(response, model)
    message = response.json().get("choices", [{}])[0].get("message", {})
    text = message.get("content") or message.get("reasoning_content") or ""
    if isinstance(text, list):
        text = "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in text
        )
    if not str(text).strip():
        raise RuntimeError(f"模型 {model} 返回了空内容")
    return str(text).strip()


def _raise_for_status(response: httpx.Response, label: str) -> None:
    if response.is_error:
        logger.error(f"{label} 请求失败: HTTP {response.status_code}, {response.text[:300]}")
    response.raise_for_status()


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    """只重试超时、网络错误、限流和服务端错误，避免重复业务错误。"""
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            async with _request_semaphore:
                response = await client.post(url, **kwargs)
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 1:
                return response
            logger.warning(f"模型服务暂时不可用（HTTP {response.status_code}），准备重试")
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt == 1:
                raise
            logger.warning(f"模型请求发生 {type(exc).__name__}，准备重试")
        await asyncio.sleep(0.8 * (attempt + 1))
    if last_error:
        raise last_error
    raise RuntimeError("模型请求重试后仍未返回")


def is_background_fallback_active() -> bool:
    """供未来状态页显示幕后模型是否因鉴权失败而回退。"""
    return _background_auth_disabled
