"""输出管道 — 系统向外部 Agent 主动推送信息的通道。

三种输出类型：
  - alert: 异常告警（冲突激增、濒危过多、知识缺口）
  - narrative: 新生成的叙事记忆摘要
  - context: 上下文预加载数据

格式：JSONL，外部 Agent 可轮询消费。
文件：output_feed.jsonl（类似 ingest_feed.jsonl 的反向通道）
"""
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("brain-memory.output")

OUTPUT_FEED = Path(__file__).parent.parent / "output_feed.jsonl"


def _append(entry_type: str, data: dict):
    """向输出 feed 追加一条记录。"""
    entry = {
        "type": entry_type,
        "data": data,
        "timestamp": datetime.now().isoformat(),
    }
    try:
        with open(OUTPUT_FEED, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("output_feed: write failed")


def push_alert(severity: str, title: str, detail: str, metadata: dict | None = None):
    """推送一条告警。"""
    _append("alert", {
        "severity": severity,
        "title": title,
        "detail": detail,
        "metadata": metadata or {},
    })
    logger.info("output: alert [%s] %s", severity, title[:60])


def push_narrative(narrative_id: str, title: str, summary: str):
    """推送新生成的叙事。"""
    _append("narrative", {
        "narrative_id": narrative_id,
        "title": title,
        "summary": summary[:200],
    })
    logger.info("output: narrative — %s", title[:60])


def push_context(session_id: str, query: str, result_count: int, top_titles: list[str]):
    """推送上下文预加载结果。"""
    _append("context", {
        "session_id": session_id,
        "query": query,
        "result_count": result_count,
        "top_titles": top_titles[:5],
    })
    logger.info("output: context — %d results for '%s'", result_count, query[:40])


def read_recent(limit: int = 20) -> list[dict]:
    """读取最近的输出（供外部 Agent 查询）。"""
    if not OUTPUT_FEED.exists():
        return []
    try:
        with open(OUTPUT_FEED, "r", encoding="utf-8") as f:
            lines = f.readlines()
        entries = []
        for line in lines[-limit:]:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return entries
    except Exception:
        return []