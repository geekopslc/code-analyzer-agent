import os
from typing import Optional

from app.utils.logger import get_logger

log = get_logger("settings")


def use_ollama() -> bool:
	return os.getenv("USE_OLLAMA", "1") == "1"


def ollama_model() -> str:
	return os.getenv("OLLAMA_MODEL", "llama3.2:latest")


def ollama_embedding_model() -> str:
	return os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text:latest")


def ollama_host() -> str:
	return os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")


def enable_verification() -> bool:
	"""Whether to enable optional test generation and execution (bonus feature)."""
	return os.getenv("ENABLE_VERIFICATION", "0") == "1"


def verification_timeout() -> int:
	"""Timeout in seconds for test execution."""
	return int(os.getenv("VERIFICATION_TIMEOUT", "30"))


# ============ Anthropic Configuration (Kimi) ============
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.moonshot.cn/anthropic")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "sk-78S1Iv6s7uUdkQS1avjsG2cOsVS6b2xxB9deYhJ1TGLJu2UC")
AGENT_MODEL = os.environ.get("ANTHROPIC_MODEL", "kimi-k2-0905-preview")

_ANTHROPIC_CLIENT = None


def anthropic_model() -> str:
    return AGENT_MODEL


def get_anthropic_client():

    global _ANTHROPIC_CLIENT
    if _ANTHROPIC_CLIENT:
        return _ANTHROPIC_CLIENT
    try:
        from anthropic import Anthropic
        _ANTHROPIC_CLIENT = Anthropic(api_key=ANTHROPIC_API_KEY, base_url=ANTHROPIC_BASE_URL)
        return _ANTHROPIC_CLIENT
    except Exception as e:
        log.warning("Anthropic init failed: %s", e)
        return None


# ============ DashScope (Ali Qwen Coder) Configuration ============
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "sk-a532607f37824c35bc1f1d26e1caa2bf")
DASHSCOPE_BASE_URL = os.environ.get("DASHSCOPE_BASE_URL", "")  # e.g. https://dashscope-intl.aliyuncs.com/api/v1
DASHSCOPE_MODEL = os.environ.get("DASHSCOPE_MODEL", "qwen3-coder-plus")

_DASHSCOPE_CLIENT = None


def dashscope_model() -> str:
    return DASHSCOPE_MODEL


class _DashscopeMessageBlock:
    def __init__(self, text: str):
        self.text = text


class _DashscopeResponse:
    def __init__(self, text: str):
        # mimic Anthropic response: .content is a list of blocks with .text
        self.content = [_DashscopeMessageBlock(text or "")]


class _DashscopeMessagesAdapter:
    def create(self, model: str, system: str, messages, max_tokens: int = 4000):
        try:
            import dashscope
            # configure base url if provided
            if DASHSCOPE_BASE_URL:
                dashscope.base_http_api_url = DASHSCOPE_BASE_URL
            # build messages in dashscope format
            ds_messages = []
            if system:
                ds_messages.append({"role": "system", "content": system})
            for m in messages or []:
                # expect {role, content}
                if isinstance(m, dict) and "role" in m and "content" in m:
                    ds_messages.append({"role": m["role"], "content": m["content"]})
            resp = dashscope.Generation.call(
                api_key=DASHSCOPE_API_KEY,
                model=model or DASHSCOPE_MODEL,
                messages=ds_messages,
                result_format="message",
            )
            # extract first choice text
            text = ""
            try:
                text = resp.output.choices[0].message.content  # type: ignore[attr-defined]
            except Exception:
                text = ""
            return _DashscopeResponse(text)
        except Exception as e:
            # re-raise to match Anthropic error flow and let callers handle
            raise e


class _DashscopeClientAdapter:
    def __init__(self):
        self.messages = _DashscopeMessagesAdapter()


def get_dashscope_client():
    """Return a client adapter that mimics Anthropic's messages.create interface using DashScope."""
    global _DASHSCOPE_CLIENT
    if _DASHSCOPE_CLIENT:
        return _DASHSCOPE_CLIENT
    try:
        # validate import early
        import dashscope  # noqa: F401
        _DASHSCOPE_CLIENT = _DashscopeClientAdapter()
        return _DASHSCOPE_CLIENT
    except Exception as e:
        log.warning("DashScope init failed: %s", e)
        return None
