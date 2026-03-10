"""
.github/scripts/llm_client.py

Optimized async LLM client with:
  - Shorter timeouts to prevent hanging
  - Health check before requests
  - Request/response logging for debugging
  - Circuit breaker pattern for fault tolerance
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


class LLMError(Exception):
    """Raised when LLM server returns an error."""


@dataclass
class ServerEndpoint:
    """Configuration for a single llama-server endpoint."""
    url: str
    weight: int = 1
    healthy: bool = True
    consecutive_failures: int = 0
    last_used: float = field(default_factory=time.monotonic)
    total_requests: int = 0
    total_tokens: int = 0


class LLMClient:
    """
    Optimized async LLM client for llama-server.
    
    Uses shorter timeouts to prevent hanging and adds health checks.
    """
    
    # Timeouts - shorter to prevent hanging
    CONNECT_TIMEOUT = 10.0
    READ_TIMEOUT = 60.0      # Reduced from 300s - max time per request
    WRITE_TIMEOUT = 10.0
    
    def __init__(self, base_url: str | None = None):
        self.endpoints: list[ServerEndpoint] = []
        
        # Parse endpoints
        urls_env = os.environ.get("LLAMA_SERVER_URLS", "")
        if urls_env:
            urls = [u.strip() for u in urls_env.split(",") if u.strip()]
            for url in urls:
                self.endpoints.append(ServerEndpoint(url=url.rstrip("/")))
        elif base_url:
            self.endpoints.append(ServerEndpoint(url=base_url.rstrip("/")))
        else:
            default = os.environ.get("LLAMA_SERVER_URL", "http://127.0.0.1:8080")
            self.endpoints.append(ServerEndpoint(url=default.rstrip("/")))
        
        print(f"LLMClient initialized with endpoints: {[e.url for e in self.endpoints]}")
        
        # Initialize HTTP client with shorter timeouts
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=self.CONNECT_TIMEOUT,
                read=self.READ_TIMEOUT,
                write=self.WRITE_TIMEOUT,
                pool=5.0,
            ),
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
        )
    
    async def _health_check(self, endpoint: ServerEndpoint) -> bool:
        """Quick health check before making request."""
        try:
            response = await self._client.get(
                f"{endpoint.url}/health",
                timeout=5.0
            )
            return response.status_code == 200
        except Exception as e:
            print(f"  Health check failed for {endpoint.url}: {e}")
            return False
    
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
        max_retries: int = 2,
    ) -> dict[str, Any]:
        """
        Send chat request with timeout and retry logic.
        """
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
        
        for attempt in range(max_retries + 1):
            # Use first healthy endpoint or default to first
            endpoint = self.endpoints[0]
            
            # Health check before request (on first attempt only)
            if attempt == 0:
                print(f"  Checking server health...", end=" ")
                if not await self._health_check(endpoint):
                    print("UNHEALTHY")
                    # Try to find healthy endpoint
                    for ep in self.endpoints[1:]:
                        if await self._health_check(ep):
                            endpoint = ep
                            print(f"Using alternative {endpoint.url}")
                            break
                    else:
                        print("No healthy endpoints found, trying anyway...")
                else:
                    print("OK")
            
            print(f"  Sending request (attempt {attempt + 1}/{max_retries + 1})...", end=" ")
            t0 = time.monotonic()
            
            try:
                response = await self._client.post(
                    f"{endpoint.url}/v1/chat/completions",
                    json=payload,
                )
                
                elapsed = time.monotonic() - t0
                
                if response.status_code != 200:
                    error_text = response.text[:200]
                    print(f"FAILED ({response.status_code})")
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
                
                print(f"OK [{elapsed:.1f}s | {prompt_tokens}→{completion_tokens} tok | {tok_per_sec:.1f} tok/s]")
                
                try:
                    return json.loads(content)
                except json.JSONDecodeError as exc:
                    raise LLMError(f"Non-JSON response: {content[:200]}") from exc
                    
            except httpx.ReadTimeout:
                elapsed = time.monotonic() - t0
                print(f"TIMEOUT after {elapsed:.1f}s")
                last_error = LLMError(f"Request timed out after {elapsed:.1f}s")
                if attempt < max_retries:
                    wait_time = 2 ** attempt
                    print(f"  Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                continue
                
            except httpx.ConnectError as e:
                print(f"CONNECT ERROR: {e}")
                last_error = LLMError(f"Cannot connect to {endpoint.url}: {e}")
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
        max_tokens: int = 1024,
        temperature: float = 0.1,
        timeout_seconds: float = 120.0,
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
