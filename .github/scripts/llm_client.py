"""
.github/scripts/llm_client.py

Async HTTP client for the locally-running llama-server instance.
All requests use grammar-constrained JSON via response_format.json_schema.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx


class LLMError(Exception):
    """Raised when llama-server returns an error or produces bad output."""


class LLMClient:
    """
    Thin async wrapper around the llama.cpp OpenAI-compatible API.

    Grammar-constrained JSON is enforced via:
        response_format.type = "json_schema"
        response_format.json_schema.strict = True
        response_format.json_schema.schema = <your schema>

    llama.cpp converts this to a GGML grammar applied at the token sampler.
    Invalid output is structurally impossible to produce.
    """

    def __init__(self, base_url: str | None = None):
        self.base_url = (
            base_url
            or os.environ.get("LLAMA_SERVER_URL", "http://127.0.0.1:8080")
        ).rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                connect=10.0,
                read=300.0,
                write=30.0,
                pool=10.0,
            ),
        )

    async def chat(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int = 1024,
        temperature: float = 0.1,
        top_p: float = 0.8,
        top_k: int = 20,
        repeat_penalty: float = 1.1,
    ) -> dict[str, Any]:
        """
        Send a chat completion request with grammar-constrained JSON output.

        Parameters match the official Qwen2.5-Coder generation_config.json
        except temperature, which is overridden per-task (see prompts.py).

        Returns:
            Parsed JSON dict matching the provided schema.

        Raises:
            LLMError: On HTTP error, truncated response, or JSON parse failure.
        """
        payload = {
            "model": "local",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name":   "output",
                    "strict": True,
                    "schema": schema,
                },
            },
            "temperature":    temperature,
            "top_p":          top_p,
            "top_k":          top_k,
            "repeat_penalty": repeat_penalty,
            "max_tokens":     max_tokens,
            "cache_prompt":   True,   # enables KV prefix reuse across passes
            "stream":         False,
        }

        t0 = time.monotonic()
        try:
            response = await self._client.post(
                "/v1/chat/completions",
                json=payload,
            )
        except httpx.ReadTimeout:
            raise LLMError(
                f"Request timed out after {time.monotonic() - t0:.1f}s. "
                "Consider reducing diff size or increasing timeout."
            )
        except httpx.ConnectError:
            raise LLMError(
                "Cannot connect to llama-server at "
                f"{self.base_url}. Is it running?"
            )

        elapsed = time.monotonic() - t0

        if response.status_code != 200:
            raise LLMError(
                f"llama-server HTTP {response.status_code}: "
                f"{response.text[:400]}"
            )

        data = response.json()
        choice = data["choices"][0]
        finish_reason = choice.get("finish_reason", "")
        content = choice["message"]["content"]

        if finish_reason == "length":
            raise LLMError(
                f"Response was truncated at max_tokens={max_tokens}. "
                "Increase max_tokens in the pass configuration or reduce input size."
            )

        # Log timing for step summary
        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        tok_per_sec = completion_tokens / elapsed if elapsed > 0 else 0
        print(
            f"  [{elapsed:.1f}s | "
            f"in={prompt_tokens} out={completion_tokens} "
            f"tok/s={tok_per_sec:.1f}]"
        )

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"llama-server returned non-JSON content despite "
                f"grammar constraint: {content[:200]}"
            ) from exc

    async def chat_with_fallback(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        fallback_value: dict[str, Any],
        max_tokens: int = 1024,
        temperature: float = 0.1,
        timeout_seconds: float = 120.0,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], bool]:
        """
        Like chat(), but catches LLMError and returns (fallback_value, True)
        instead of raising. Second element is True if fallback was used.

        Callers use this for non-critical passes so the review always posts.
        """
        try:
            result = await asyncio.wait_for(
                self.chat(
                    system=system,
                    user=user,
                    schema=schema,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    **kwargs,
                ),
                timeout=timeout_seconds,
            )
            return result, False
        except (LLMError, asyncio.TimeoutError) as exc:
            print(f"  WARNING: Pass failed ({exc}), using fallback value.")
            return fallback_value, True

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()