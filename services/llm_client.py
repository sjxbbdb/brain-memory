"""LLM Client — Unified multi-provider LLM + Embedding backend for Brain Memory.

Providers:
  - DeepSeek V3: primary LLM (cheap, fast, good Chinese)
  - GLM-4: fallback LLM
  - DashScope Qwen: LLM fallback + text-embedding-v3

Usage:
    from services.llm_client import LLMClient
    llm = LLMClient()
    result = await llm.chat_json(system="你是记忆摄入Agent", user="分析这段文本...")
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("brain-memory.llm")


def _load_dotenv():
    """Load .env file from project root (no extra dependency)."""
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip(chr(34)+chr(39))
            if key and val and key not in os.environ:
                os.environ[key] = val

_load_dotenv()

DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"  # V3

GLM_KEY = os.environ.get("GLM_API_KEY", os.environ.get("ZHIPU_API_KEY", ""))
GLM_BASE = "https://open.bigmodel.cn/api/paas/v4"
GLM_MODEL = "glm-4-flash"  # cheap + fast

DASHSCOPE_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_MODEL = "qwen-turbo"
EMBEDDING_MODEL = "text-embedding-v3"

# ── Single model (no fallback chain — keep it simple) ──
LLM_PRIMARY = {"provider": "deepseek", "model": DEEPSEEK_MODEL, "base": DEEPSEEK_BASE, "key": DEEPSEEK_KEY}


class LLMClient:
    """LLM client — single model, no chain."""

    def __init__(self):
        self.cfg = LLM_PRIMARY
        self._embedding_cache: dict[str, list[float]] = {}
        self._call_count = 0
        self._total_tokens = 0

    # ── LLM Chat ──

    async def chat_json(self, system: str, user: str, temperature: float = 0.1, max_tokens: int = 1024) -> dict[str, Any]:
        """Call LLM and parse JSON response."""
        result = await self._call_llm(self.cfg, system, user, temperature, max_tokens)
        return self._parse_json(result, self.cfg["provider"])

    async def chat_text(self, system: str, user: str, temperature: float = 0.3, max_tokens: int = 2048) -> str:
        """Call LLM and return raw text response."""
        return await self._call_llm(self.cfg, system, user, temperature, max_tokens)

    async def _call_llm(
        self, cfg: dict, system: str, user: str,
        temperature: float, max_tokens: int,
    ) -> str:
        import aiohttp

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        url = f"{cfg['base']}/chat/completions"

        headers = {
            "Authorization": f"Bearer {cfg['key']}",
            "Content-Type": "application/json",
        }

        body = {
            "model": cfg["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # DeepSeek + DashScope support response_format json_object
        if cfg["provider"] in ("deepseek", "dashscope"):
            body["response_format"] = {"type": "json_object"}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"{cfg['provider']} HTTP {resp.status}: {text[:200]}")
                data = await resp.json()
                content = data["choices"][0]["message"]["content"]
                self._call_count += 1
                self._total_tokens += data.get("usage", {}).get("total_tokens", 0)
                return content

    def _parse_json(self, text: str, provider: str) -> dict:
        """Extract JSON from LLM response (handles markdown fences)."""
        text = text.strip()
        # Remove markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON object in text
            import re
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                return json.loads(match.group())
            raise ValueError(f"{provider}: unable to parse JSON from response: {text[:200]}")

    # ── Embedding ──

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings via DashScope text-embedding-v3."""
        import aiohttp

        # Deduplicate
        unique = []
        seen = set()
        for t in texts:
            key = t[:200]
            if key in seen:
                continue
            seen.add(key)
            unique.append(t)

        # Check cache
        cached = {}
        uncached = []
        for i, t in enumerate(unique):
            if t in self._embedding_cache:
                cached[i] = self._embedding_cache[t]
            else:
                uncached.append((i, t))

        if uncached:
            url = f"{DASHSCOPE_BASE}/embeddings"
            headers = {
                "Authorization": f"Bearer {DASHSCOPE_KEY}",
                "Content-Type": "application/json",
            }
            body = {
                "model": EMBEDDING_MODEL,
                "input": [t for _, t in uncached],
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=body, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise RuntimeError(f"embedding HTTP {resp.status}: {text[:200]}")
                    data = await resp.json()

            for j, item in enumerate(data["data"]):
                idx, text = uncached[j]
                vec = item["embedding"]
                self._embedding_cache[text] = vec
                cached[idx] = vec

        # Reconstruct in original order
        result_map = {}
        seen2 = set()
        for t in texts:
            key = t[:200]
            if key not in seen2:
                seen2.add(key)
            for i, u in enumerate(unique):
                if u[:200] == key:
                    result_map.setdefault(i, self._embedding_cache.get(u) or cached.get(i))
                    break

        results = []
        for t in texts:
            key = t[:200]
            for u in unique:
                if u[:200] == key:
                    results.append(self._embedding_cache.get(u) or [0.0])
                    break
            else:
                results.append([0.0])

        return results

    async def embed_single(self, text: str) -> list[float]:
        results = await self.embed([text])
        return results[0]

    @property
    def stats(self) -> dict:
        return {
            "calls": self._call_count,
            "total_tokens": self._total_tokens,
            "cached_embeddings": len(self._embedding_cache),
        }

    def flush_embedding_cache(self):
        self._embedding_cache.clear()


# Global singleton
_llm_instance: LLMClient | None = None


def get_llm() -> LLMClient:
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = LLMClient()
    return _llm_instance
