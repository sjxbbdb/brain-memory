"""Hippocampus — 海马体。记忆编码、embedding检索、链式联想（LLM + 规则）。

职责:
  1. LLM编码: 将摄入文本编码为结构化记忆
  2. embedding检索: 基于语义相似度搜索
  3. 链式联想: 检索到的记忆触发更多关联记忆
  4. 模式分离: 新建/覆写/合并判定
"""

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

from services.llm_client import get_llm

logger = logging.getLogger("brain-v5.hippocampus")

HIPPOCAMPUS_ENCODE_PROMPT = """[SYSTEM CONSTRAINT]
You are the HIPPOCAMPUS of a brain. You encode information into memory.
Do NOT add opinions, interpretations, or suggestions. Only record facts.

[TASK] Encode this text into a structured memory entry.

Output JSON only:
{
  "type": "episodic|semantic|procedural|narrative",
  "title": "factual title, max 12 chars, use original wording",
  "entities": ["only entities explicitly mentioned"],
  "emotion_tags": ["tags derived from text"],
  "importance": 0.0-1.0,
  "summary": "verbatim excerpt, max 80 chars, NO rewriting"
}"""

HIPPOCAMPUS_RETRIEVE_PROMPT = """[SYSTEM CONSTRAINT]
You are the HIPPOCAMPUS of a brain. You retrieve relevant memories.
Do NOT add opinions or commentary. Only return ranked memory IDs.

[TASK] Given a query and candidate memories, rank by relevance.

Output JSON only:
{
  "ranked_ids": ["id1", "id2"],
  "best_match_id": "id1",
  "relevance_reason": "short reason, 20 chars max"
}

Query and candidates follow."""


class Hippocampus:
    """Memory encoding and retrieval engine."""

    def __init__(self, memory_store=None):
        self.llm = get_llm()
        self.store = memory_store  # MemoryStore instance
        self.association_chains: list[list[str]] = []
        self._embedding_cache: dict[str, list[float]] = {}

    # ── Encoding ──

    async def encode(self, text: str, source: str, emotion: dict, goal: str | None = None) -> dict | None:
        """LLM encode raw text → structured memory, then save to store."""
        if not text or len(text.strip()) < 3:
            return None

        try:
            user_prompt = text[:3000]
            if goal:
                user_prompt = "[goal: {0}]\n{1}".format(goal, user_prompt)

            result = await self.llm.chat_json(
                system=HIPPOCAMPUS_ENCODE_PROMPT,
                user=user_prompt,
                temperature=0.1,
                max_tokens=1024,
            )

            raw_entities = result.get("entities", [])
            entities = [e for e in raw_entities if e and e in text]

            # Compute embedding for this memory
            embed_text = "{0} {1}".format(result.get("title", ""), text[:500])
            embedding = None
            try:
                embedding = await self.llm.embed_single(embed_text)
            except Exception:
                pass

            memory = {
                "type": result.get("type", "episodic"),
                "title": result.get("title", text[:80]),
                "content": text[:4000],
                "entities": entities,
                "emotion_tags": result.get("emotion_tags", []) + emotion.get("emotional_tags", []),
                "importance": max(0.0, min(1.0, float(result.get("importance", 0.5)))),
                "embedding": embedding,
                "summary": result.get("summary", text[:80]),
                "source": source,
                "emotion_label": emotion.get("emotion_label", "neutral"),
                "emotion_vector": emotion.get("emotion_vector", {}),
                "created": datetime.now(timezone.utc).isoformat(),
                "llm_encoded": True,
            }

            if self.store:
                mem_id = self.store.save(memory)
                memory["id"] = mem_id
                logger.info("hippocampus: encoded memory %s", mem_id)
            else:
                import uuid
                memory["id"] = "mem-{0}".format(uuid.uuid4().hex[:12])

            return memory
        except Exception as e:
            logger.warning("hippocampus encode failed: %s", str(e)[:80])
            return None

    # ── Retrieval ──

    async def retrieve(self, query: str, top_k: int = 5) -> dict:
        """Semantic retrieval using embedding similarity.

        Returns: {"results": [{id, title, summary, score, ...}], "total_stored": int}
        """
        if not self.store:
            return {"results": [], "total_stored": 0}

        total = self.store.count()
        if total == 0:
            return {"results": [], "total_stored": 0}

        # Step 1: Get candidate memories (keyword pre-filter)
        candidates = self.store.search(query, limit=min(total, 30))
        if not candidates:
            return {"results": [], "total_stored": total}

        # Step 2: Embedding similarity
        try:
            query_vec = await self.llm.embed_single(query)
            
            # Load cached embeddings or compute new ones
            cached_count = 0
            candidate_embeddings = []
            texts_to_embed = []
            indices_to_embed = []
            
            for i, mem in enumerate(candidates):
                cached = mem.get("embedding")
                if cached:
                    try:
                        vec = json.loads(cached) if isinstance(cached, str) else cached
                        if isinstance(vec, list) and len(vec) > 10:
                            candidate_embeddings.append(vec)
                            cached_count += 1
                            continue
                    except (json.JSONDecodeError, TypeError):
                        pass
                texts_to_embed.append("{0} {1}".format(mem.get("title", ""), (mem.get("content", "") or "")[:300]))
                indices_to_embed.append(i)
                candidate_embeddings.append(None)  # placeholder
            
            # Compute embeddings for uncached
            if texts_to_embed:
                try:
                    new_vecs = await self.llm.embed(texts_to_embed)
                    for j, idx in enumerate(indices_to_embed):
                        if j < len(new_vecs):
                            candidate_embeddings[idx] = new_vecs[j]
                except Exception:
                    pass
            
            scored = []
            for i, mem in enumerate(candidates):
                if i < len(candidate_embeddings) and candidate_embeddings[i]:
                    sim = self._cosine_similarity(query_vec, candidate_embeddings[i])
                    scored.append((mem, sim))

            scored.sort(key=lambda x: x[1], reverse=True)
            top = scored[:top_k]

            results = []
            for mem, score in top:
                results.append({
                    "id": mem.get("id"),
                    "title": mem.get("title", ""),
                    "summary": mem.get("summary", "")[:100],
                    "type": mem.get("type", "episodic"),
                    "entities": json.loads(mem.get("entities", "[]")),
                    "importance": mem.get("importance", 0.5),
                    "score": round(score, 3),
                    "created": mem.get("created", ""),
                })
            # ── Boost retrieved memories (access reinforces importance) ──
            if self.store and results:
                for r in results[:3]:  # boost top 3
                    try:
                        self.store.boost_on_access(r["id"], boost=0.05)
                    except Exception:
                        pass

            return {"results": results, "total_stored": total}
        except Exception as e:
            logger.warning("hippocampus embedding search failed: %s", str(e)[:80])
            # Fallback: keyword-based score
            results = []
            query_lower = query.lower()
            for mem in candidates[:top_k]:
                title = (mem.get("title") or "").lower()
                summary = (mem.get("summary") or "").lower()
                score = 0.0
                for word in query_lower.split():
                    if word in title: score += 0.3
                    if word in summary: score += 0.2
                results.append({
                    "id": mem.get("id"),
                    "title": mem.get("title", ""),
                    "summary": mem.get("summary", "")[:100],
                    "type": mem.get("type", "episodic"),
                    "entities": json.loads(mem.get("entities", "[]")),
                    "importance": mem.get("importance", 0.5),
                    "score": min(round(score, 3), 1.0),
                    "created": mem.get("created", ""),
                })
            results.sort(key=lambda x: x["score"], reverse=True)
            # ── Boost retrieved memories (access reinforces importance) ──
            if self.store and results:
                for r in results[:3]:  # boost top 3
                    try:
                        self.store.boost_on_access(r["id"], boost=0.05)
                    except Exception:
                        pass

            return {"results": results, "total_stored": total}

    # ── Chain Association ──

    async def associate(self, query: str, previous_results: list[dict], depth: int = 2) -> list[str]:
        """Chain association: retrieved memory triggers more memories.

        The brain doesn't just retrieve — one memory reminds it of another.
        Returns list of memory IDs in the association chain.
        """
        if not previous_results or depth <= 0:
            return []

        chain = []
        seen_ids = set()

        # Collect entities from previous results
        all_entities = []
        for r in previous_results:
            all_entities.extend(r.get("entities", []))
            seen_ids.add(r.get("id"))

        if not all_entities:
            return []

        # Search for each entity as a query
        import re
        for entity in list(set(all_entities))[:3]:
            if not entity or len(entity) < 2:
                continue
            result = await self.retrieve(query=entity, top_k=2)
            for r in result.get("results", []):
                if r["id"] not in seen_ids:
                    chain.append(r["id"])
                    seen_ids.add(r["id"])
                    if len(chain) >= depth * 2:
                        break
            if len(chain) >= depth * 2:
                break

        if chain:
            self.association_chains.append(chain)
            if len(self.association_chains) > 10:
                self.association_chains = self.association_chains[-10:]

        return chain

    # ── Pattern Separation ──

    def merge_decision(self, new_memory: dict, existing_memories: list[dict]) -> dict:
        """Decide: new / merge / overwrite / conflict."""
        if not existing_memories:
            return {"action": "new", "target_id": None}

        for existing in existing_memories:
            new_ents = set(new_memory.get("entities", []))
            old_ents = set(existing.get("entities", []))
            if not new_ents or not old_ents:
                continue

            overlap = len(new_ents & old_ents) / max(len(new_ents | old_ents), 1)
            new_title = new_memory.get("title", "")
            old_title = existing.get("title", "")

            if overlap > 0.7 and new_title[:30] == old_title[:30]:
                return {"action": "merge", "target_id": existing.get("id")}
            elif overlap > 0.5:
                return {"action": "weighted_merge", "target_id": existing.get("id")}

        return {"action": "new", "target_id": None}

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
