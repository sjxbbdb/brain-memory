"""后台自动调度器 — Brain Memory 全自动引擎。

定时任务层级：
  Layer 0 — 每 30 秒：Feed 轮询（消费外部 Agent 推送）
  Layer 1 — 每 30 分钟：评分刷新 + 话题聚合 + 自我感知
  Layer 2 — 每 1 小时：轻量巩固
  Layer 3 — 每 2 小时：完整巩固
  Layer 4 — 每 6 小时：压缩 + 知识缺口分析

所有任务静默运行，失败不中断服务。
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("brain-memory.scheduler")

# ── 调度间隔（秒） ──
INTERVAL_FEED = 30              # 30 秒：轮询 feed
INTERVAL_MID = 30 * 60          # 30 分钟：评分 + 自我感知
INTERVAL_LIGHT = 60 * 60        # 1 小时：轻量巩固
INTERVAL_FULL = 2 * 60 * 60     # 2 小时：完整巩固
INTERVAL_DEEP = 6 * 60 * 60     # 6 小时：压缩 + 缺口

# ── Feed 轮询 ──
FEED_PATH = Path(__file__).parent.parent / "ingest_feed.jsonl"
CURSOR_PATH = Path(__file__).parent.parent / ".observer_cursor"
BATCH_SIZE = 20


def _read_cursor() -> int:
    try:
        if CURSOR_PATH.exists():
            return int(CURSOR_PATH.read_text().strip())
    except (ValueError, OSError):
        pass
    return 0


def _write_cursor(pos: int):
    CURSOR_PATH.write_text(str(pos))


async def _poll_feed():
    """轮询 ingest_feed.jsonl，有新条目直接走脑区管线摄入。

    替代原来 observer.py 守护进程，内嵌到服务生命周期。
    """
    try:
        if not FEED_PATH.exists():
            return

        cursor_pos = _read_cursor()
        file_size = FEED_PATH.stat().st_size
        if file_size <= cursor_pos:
            return

        with open(FEED_PATH, "r", encoding="utf-8") as f:
            f.seek(cursor_pos)
            new_content = f.read()

        raw_lines = new_content.strip().split("\n")
        entries = [l.strip() for l in raw_lines if l.strip()]
        if not entries:
            return

        from services.pipeline import pipeline_ingest

        processed = 0
        accepted = 0
        for line in entries[-BATCH_SIZE:]:
            processed += 1
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "text" not in entry:
                continue

            try:
                result = await pipeline_ingest(
                    text=entry["text"],
                    source=entry.get("source", "observer"),
                    metadata=entry.get("metadata"),
                    bypass_gate=entry.get("explicit_mark", False),
                )
                if result.get("accepted"):
                    accepted += 1
            except Exception:
                logger.exception("scheduler: feed ingest failed")

        _write_cursor(FEED_PATH.stat().st_size)

        if processed > 0:
            logger.info(
                "feed_poll: %d processed, %d accepted",
                processed, accepted,
            )
    except Exception:
        logger.exception("scheduler: feed_poll failed")


# ── 巩固 ──

async def _run_phase(name: str, phases: list[int]):
    try:
        from services import consolidation as cons_svc
        report = await cons_svc.run_consolidation(phases)
        logger.info(
            "consolidation: %s — %d changes across %s",
            name, report.memory_changes, report.phases_executed,
        )
    except Exception:
        logger.exception("consolidation: %s failed", name)


async def _run_compression():
    try:
        from services.compression import compress_episodic_clusters
        from models.database import get_db
        db = await get_db()
        try:
            result = await compress_episodic_clusters(db)
            if result.get("summaries_generated", 0) > 0:
                logger.info(
                    "compression: %d clusters, %d compressed, %d summaries",
                    result["clusters_found"], result["compressed"],
                    result["summaries_generated"],
                )
        finally:
            await db.close()
    except Exception:
        logger.exception("compression: failed")


async def _analyze_knowledge_gaps():
    try:
        from models.database import get_db
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT query, COUNT(*) as cnt FROM knowledge_gaps "
                "GROUP BY query ORDER BY cnt DESC LIMIT 10"
            )
            rows = await cursor.fetchall()
            if rows:
                gaps = [(r[0][:60], r[1]) for r in rows]
                logger.info("gaps: top queries — %s", gaps)
        finally:
            await db.close()
    except Exception:
        logger.exception("gaps: analysis failed")


async def _run_self_awareness():
    """自我感知：扫描系统状态，自动摄入元认知记忆。"""
    try:
        from services.self_awareness import auto_ingest_self_events
        ingested = await auto_ingest_self_events()
        if ingested > 0:
            logger.info("self-awareness: %d events auto-ingested", ingested)
    except Exception:
        logger.exception("self-awareness: failed")


# ── 主调度循环 ──

async def run_scheduler(stop_event: asyncio.Event):
    """五层定时调度器。"""
    logger.info(
        "scheduler: started — feed=30s, mid=30m, light=1h, full=2h, deep=6h"
    )

    last_feed = 0.0
    last_mid = 0.0
    last_light = 0.0
    last_full = 0.0
    last_deep = 0.0

    while not stop_event.is_set():
        now_ts = datetime.now(timezone.utc).timestamp()

        # Layer 0: Feed 轮询（每 30 秒）
        if now_ts - last_feed >= INTERVAL_FEED:
            last_feed = now_ts
            await _poll_feed()

        # Layer 1: 评分刷新 + 自我感知（每 30 分钟）
        if now_ts - last_mid >= INTERVAL_MID:
            last_mid = now_ts
            await _run_phase("scoring_refresh", [1, 28])
            await _run_self_awareness()

        # Layer 2: 轻量巩固（每 1 小时）
        if now_ts - last_light >= INTERVAL_LIGHT:
            last_light = now_ts
            await _run_phase("light_consolidation", [0, 1])

        # Layer 3: 完整巩固（每 2 小时）
        if now_ts - last_full >= INTERVAL_FULL:
            last_full = now_ts
            await _run_phase("full_consolidation", [0, 1, 2, 25, 28, 3])

        # Layer 4: 压缩 + 缺口（每 6 小时）
        if now_ts - last_deep >= INTERVAL_DEEP:
            last_deep = now_ts
            await _run_compression()
            await _run_narrative()
            await _analyze_knowledge_gaps()

        # 非忙轮询：每 10 秒醒来检查一次
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass

    logger.info("scheduler: stopped")