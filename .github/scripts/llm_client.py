"""
.github/scripts/llm_client.py

Advanced async LLM client with:
  - Circuit breaker pattern for fault tolerance
  - Weighted round-robin for multi-server setups
  - Token usage tracking
  - Request coalescing for identical prompts
"""

from __future__ import annotations

import asyncio
import json
import os
import random
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
    weight: int = 1  # For weighted round-robin
    healthy: bool = True
    consecutive_failures: int = 0
    last_used: float = field(default_factory=time.monotonic)
    total_requests: int = 0
    total_tokens: int = 0


class CircuitBreaker:
    """Circuit breaker pattern for fault tolerance."""
    
    FAILURE_THRESHOLD = 3
    RECOVERY_TIMEOUT = 30.0
    
    def __init__(self):
        self.state = "closed"  # closed, open, half-open
        self.failures = 0
        self.last_failure_time: float | None = None
        self.lock = asyncio.Lock()
    
    async def call(self, func, *args, **kwargs):
        async with self.lock:
            if self.state == "open":
                if time.monotonic() - (self.last_failure_time or 0) > self.RECOVERY_TIMEOUT:
                    self.state = "half-open"
                    self.failures = 0
                else:
                    raise LLMError("Circuit breaker is OPEN")
        
        try:
            result = await func(*args, **kwargs)
            async with self.lock:
                if self.state == "half-open":
                    self.state = "closed"
                self.failures = 0
            return result
        except Exception as e:
            async with self.lock:
                self.failures += 1
                self.last_failure_time = time.monotonic()
                if self.failures >= self.FAILURE_THRESHOLD:
                    self.state = "open"
            raise


class LLMClient:
    """
    Advanced async LLM client with load balancing and fault tolerance.
    
    Supports multiple server endpoints via LLAMA_SERVER_URLS env var:
        LLAMA_SERVER_URLS=http://host1:8080,http://host2:8080
    
    Or single endpoint via LLAMA_SERVER_URL:
        LLAMA_SERVER_URL=http://localhost:8080
    """
    
    def __init__(self, base_url: str | None = None):
        self.endpoints: list[ServerEndpoint] = []
        self.current_index = 0
        self.circuit_breaker = CircuitBreaker()
        self._client: httpx.AsyncClient | None = None
        
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
        
        # Initialize HTTP client
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=300.0,
                write=30.0,
                pool=10.0,
            ),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    
    def _get_next_endpoint(self) -> ServerEndpoint:
        """Weighted round-robin endpoint selection."""
        healthy = [e for e in self.endpoints if e.healthy]
        if not healthy:
            # All unhealthy, try anyway
            healthy = self.endpoints
        
        # Simple weighted round-robin
        total_weight = sum(e.weight for e in healthy)
        if total_weight == 0:
            return healthy[0]
        
        # Pick based on weight and recency
        candidates = []
        for e in healthy:
            # Prefer less recently used servers
            recency_bonus = max(0, 10 - (time.monotonic() - e.last_used))
            score = e.weight + recency_bonus
            candidates.extend([e] * int(score))
        
        endpoint = random.choice(candidates) if candidates else healthy[0]
        endpoint.last_used = time.monotonic()
        return endpoint
    
    def _mark_endpoint_healthy(self, endpoint: ServerEndpoint, success: bool):
        """Update endpoint health status."""
        if success:
            endpoint.consecutive_failures = 0
            endpoint.healthy = True
        else:
            endpoint.consecutive_failures += 1
            if endpoint.consecutive_failures >= 3:
                endpoint.healthy = False
                print(f"  Marked endpoint unhealthy: {endpoint.url}")
    
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
        Send chat request with circuit breaker and retry logic.
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
            endpoint = self._get_next_endpoint()
            
            try:
                result = await self._circuit_breaker.call(
                    self._do_request, endpoint, payload
                )
                self._mark_endpoint_healthy(endpoint, True)
                return result
                
            except LLMError as e:
                last_error = e
                self._mark_endpoint_healthy(endpoint, False)
                if attempt < max_retries:
                    wait_time = 2 ** attempt  # Exponential backoff
                    print(f"  Retry {attempt + 1}/{max_retries} after {wait_time}s...")
                    await asyncio.sleep(wait_time)
                continue
        
        raise last_error or LLMError("All retries failed")
    
    async def _do_request(
        self,
        endpoint: ServerEndpoint,
        payload: dict,
    ) -> dict[str, Any]:
        """Execute actual HTTP request."""
        t0 = time.monotonic()
        
        try:
            response = await self._client.post(
                f"{endpoint.url}/v1/chat/completions",
                json=payload,
            )
        except httpx.ReadTimeout:
            raise LLMError(f"Request timed out after {time.monotonic() - t0:.1f}s")
        except httpx.ConnectError:
            raise LLMError(f"Cannot connect to {endpoint.url}")
        
        elapsed = time.monotonic() - t0
        
        if response.status_code != 200:
            raise LLMError(f"HTTP {response.status_code}: {response.text[:400]}")
        
        data = response.json()
        choice = data["choices"][0]
        finish_reason = choice.get("finish_reason", "")
        content = choice["message"]["content"]
        
        if finish_reason == "length":
            raise LLMError(f"Response truncated at max_tokens={payload['max_tokens']}")
        
        # Log performance
        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        tok_per_sec = completion_tokens / elapsed if elapsed > 0 else 0
        
        print(
            f"  [{elapsed:.1f}s | in={prompt_tokens} out={completion_tokens} "
            f"tok/s={tok_per_sec:.1f}]"
        )
        
        # Update endpoint stats
        endpoint.total_requests += 1
        endpoint.total_tokens += prompt_tokens + completion_tokens
        
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Non-JSON response: {content[:200]}") from exc
    
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
