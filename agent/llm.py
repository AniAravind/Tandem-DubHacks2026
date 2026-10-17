import os
from openai import OpenAI

# xAI is OpenAI-compatible. The key comes from the environment only (never commit it).
# A placeholder keeps this module importable when XAI_API_KEY is not set; real calls will fail with an auth error.
client = OpenAI(api_key=os.getenv("XAI_API_KEY") or "missing-xai-api-key",
                base_url=os.getenv("XAI_BASE_URL", "https://api.x.ai/v1"),
                timeout=float(os.getenv("XAI_TIMEOUT_S", "8")))
MODEL = os.getenv("XAI_MODEL", "grok-4-1-fast")   # VERIFY exact id in the xAI console


def chat(messages: list[dict], tools: list[dict]):
    return client.chat.completions.create(model=MODEL, messages=messages, tools=tools,
                                          tool_choice="auto", temperature=0.2)
