"""
.github/scripts/llm_client.py

Optimized async LLM client with reasonable timeouts for ARM64 inference.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx


class LLMError(Exception):
    """Raised when LLM server returns an error."""


class LLMClient:
    """
    Optimized async LLM client for llama-server on ARM64.
    
    ARM64 inference is slower (4-8 tok/s), so timeouts need to be
    generous enough to allow completion but not so long that we hang.
    """
    
    # Timeouts for ARM64 inference
    # - Connect: quick, server should be ready
    # - Read: needs to be long enough for slow ARM64 generation
    # - Write: sending prompt is fast
    CONNECT_TIMEOUT = 10.0
    READ_TIMEOUT = 180.0      # 3 minutes - enough for ~1000 tokens at 5 tok/s
    WRITE_TIMEOUT = 10.0
    
    def __init__(self, base_url: str | None = None):
        self.endpoints: list[str] = []
        
        # Parse endpoints
        urls_env = os.environ.get("LLAMA_SERVER_URLS", "")
        if urls_env:
            self.endpoints = [u.strip().rstrip("/") for u in urls_env.split(",") if u.strip()]
        elif base_url:
            self.endpoints = [base_url.rstrip("/")]
        else:
            default = os.environ.get("LLAMA_SERVER_URL", "http://127.0.0.1:8080")
            self.endpoints = [default.rstrip("/")]
        
        print(f"LLMClient: endpoints={self.endpoints}, read_timeout={self.READ_TIMEOUT}s")
        
        # Initialize HTTP client
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=self.CONNECT_TIMEOUT,
                read=self.READ_TIMEOUT,
                write=self.WRITE_TIMEOUT,
                pool=5.0,
            ),
            limits=httpx.Limits(max_connections=3, max_keepalive_connections=2),
        )
    
    async def _health_check(self, endpoint: str) -> bool:
        """Quick health check before making request."""
        try:
            response = await self._client.get(
                f"{endpoint}/health",
                timeout=5.0
            )
            return response.status_code == 200
        except Exception:
            return False
    
    async def chat(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int = 512,  # Reduced default for faster generation
        temperature: float = 0.1,
        top_p: float = 0.8,
        top_k: int = 20,
        repeat_penalty: float = 1.1,
        max_retries: int = 1,  # Reduced retries - if it fails once, likely to fail again
    ) -> dict[str, Any]:
        """
        Send chat request with timeout appropriate for ARM64.
        """
        # Estimate required time: ~5 tok/s on ARM64
        # Add 30s overhead for prompt processing
        estimated_time = 30.0 + (max_tokens / 5.0)
        timeout = min(estimated_time, self.READ_TIMEOUT)
        
        payload = {
            "model": "local",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "output",
                    "strict": True,
                    "schema": schema,
                },
            },
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "repeat_penalty": repeat_penalty,
            "max_tokens": max_tokens,
            "cache_prompt": True,
            "stream": False,
        }
        
        last_error = None
        endpoint = self.endpoints[0]
        
        for attempt in range(max_retries + 1):
            # Health check on first attempt
            if attempt == 0:
                healthy = await self._health_check(endpoint)
                print(f"  Health check: {'OK' if healthy else 'UNHEALTHY'}")
            
            print(f"  Request (attempt {attempt + 1}/{max_retries + 1}, timeout={timeout:.0f}s, max_tokens={max_tokens})...", end=" ", flush=True)
            t0 = time.monotonic()
            
            try:
                response = await self._client.post(
                    f"{endpoint}/v1/chat/completions",
                    json=payload,
                    timeout=timeout,
                )
                
                elapsed = time.monotonic() - t0
                
                if response.status_code != 200:
                    print(f"FAILED HTTP {response.status_code}")
                    error_text = response.text[:200]
                    raise LLMError(f"HTTP {response.status_code}: {error_text}")
                
                data = response.json()
                choice = data["choices"][0]
                finish_reason = choice.get("finish_reason", "")
                content = choice["message"]["content"]
                
                if finish_reason == "length":
                    print(f"TRUNCATED")
                    raise LLMError(f"Response truncated at max_tokens={max_tokens}")
                
                # Log performance
                usage = data.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                tok_per_sec = completion_tokens / elapsed if elapsed > 0 else 0
                
                print(f"OK [{elapsed:.1f}s | {completion_tokens} tok | {tok_per_sec:.1f} tok/s]")
                
                try:
                    return json.loads(content)
                except json.JSONDecodeError as exc:
                    raise LLMError(f"Non-JSON response: {content[:200]}") from exc
                    
            except httpx.ReadTimeout:
                elapsed = time.monotonic() - t0
                print(f"TIMEOUT after {elapsed:.1f}s")
                last_error = LLMError(f"Request timed out after {elapsed:.1f}s (timeout={timeout}s)")
                if attempt < max_retries:
                    # Increase timeout for retry
                    timeout = min(timeout * 1.5, self.READ_TIMEOUT)
                    print(f"  Retrying with timeout={timeout:.0f}s...")
                    await asyncio.sleep(1)
                continue
                
            except httpx.ConnectError as e:
                print(f"CONNECT ERROR: {e}")
                last_error = LLMError(f"Cannot connect to {endpoint}: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2)
                continue
                
            except Exception as e:
                print(f"ERROR: {e}")
                last_error = e if isinstance(e, LLMError) else LLMError(str(e))
                if attempt < max_retries:
                    await asyncio.sleep(1)
                continue
        
        raise last_error or LLMError("All retries failed")
    
    async def chat_with_fallback(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        fallback_value: dict[str, Any],
        max_tokens: int = 512,
        temperature: float = 0.1,
        timeout_seconds: float = 180.0,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], bool]:
        """
        Like chat(), but returns fallback on failure.
        Returns (result, used_fallback).
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
            print(f"  WARNING: Pass failed ({exc}), using fallback")
            return fallback_value, True
    
    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
    
    async def __aenter__(self) -> "LLMClient":
        return self
    
    async def __aexit__(self, *_: Any) -> None:
        await self.close()
