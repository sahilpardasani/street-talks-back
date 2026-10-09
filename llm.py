"""Weights & Biases serverless inference (OpenAI-compatible) client, stdlib only."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

import telemetry

BASE = os.environ.get("WANDB_INFERENCE_URL", "https://api.inference.wandb.ai/v1")
FAST_MODEL = os.environ.get("LLM_FAST_MODEL", "openai/gpt-oss-20b")
SMART_MODEL = os.environ.get("LLM_SMART_MODEL", "meta-llama/Llama-3.3-70B-Instruct")


def _headers() -> dict[str, str]:
    key = os.environ.get("WANDB_API_KEY", "")
    if not key:
        raise RuntimeError("WANDB_API_KEY is not set")
    return {
        "Authorization": "Bearer " + key,
        "OpenAI-Project": f"{os.environ.get('WANDB_TEAM', '')}/{os.environ.get('WANDB_PROJECT', '')}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 street-talks-back",
    }


@telemetry.op("wandb.chat_completion")
def chat(messages: list[dict], model: str = FAST_MODEL, json_mode: bool = False, temperature: float = 0.2,
         max_tokens: int = 900, retries: int = 3) -> str:
    body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if model.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = "low"
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(BASE + "/chat/completions", data=json.dumps(body).encode(), headers=_headers())
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.load(r)
            usage = data.get("usage") or {}
            telemetry.bump("wandb.calls")
            telemetry.bump("wandb.calls." + model)
            telemetry.bump("wandb.prompt_tokens", int(usage.get("prompt_tokens") or 0))
            telemetry.bump("wandb.completion_tokens", int(usage.get("completion_tokens") or 0))
            return data["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception as e:  # network hiccup
            last = e
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"LLM call failed: {last}")


def chat_json(messages: list[dict], model: str = FAST_MODEL, **kw: Any) -> dict:
    text = chat(messages, model=model, json_mode=True, **kw)
    return parse_json(text)


def parse_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {"_raw": text}
