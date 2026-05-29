"""SQLite 数据库连接、建表、初始化。"""
import sqlite3
import aiosqlite
from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL CHECK(type IN ("episodic","semantic","procedural","narrative","global")),
    layer           TEXT NOT NULL CHECK(layer IN ("episodic","semantic","procedural","narrative","global")),
    title           TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT "",
    importance      REAL NOT NULL DEFAULT 0.5,
    novelty         REAL NOT NULL DEFAULT 0.5,
    failure_cost    REAL NOT NULL DEFAULT 0.5,
    goal_relevance  REAL NOT NULL DEFAULT 0.5,
    emotion_weight  REAL NOT NULL DEFAULT 0.5,
    surprise_score  REAL NOT NULL DEFAULT 0.3,
    confidence      REAL NOT NULL DEFAULT 0.8,
    source          TEXT NOT NULL DEFAULT "direct_experience",
    corroboration_count INTEGER NOT NULL DEFAULT 0,
    access_count    INTEGER NOT NULL DEFAULT 0,
    last_accessed   TEXT,
    decay_rate      REAL NOT NULL DEFAULT 0.05,
    half_life_days  REAL NOT NULL DEFAULT 30,
    created         TEXT NOT NULL,
    consolidated    INTEGER NOT NULL DEFAULT 0,
    consolidated_to TEXT DEFAULT "[]",
    last_consolidated TEXT,
    tags            TEXT DEFAULT "[]",
    entities        TEXT DEFAULT "[]",
    relations       TEXT DEFAULT "[]",
    contradicted_by TEXT DEFAULT "[]",
    supersedes      TEXT DEFAULT "[]",
    review_needed   INTEGER NOT NULL DEFAULT 0,
    archived        INTEGER NOT NULL DEFAULT 0,
    -- v3.0: 类脑覆写权值体系
    overwrite_weight REAL NOT NULL DEFAULT 0.5,
    compressed_to    TEXT DEFAULT "[]",
    parent_id        TEXT,
    region_trace     TEXT DEFAULT "[]",
    version          INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_type ON memories(type);
CREATE INDEX IF NOT EXISTS idx_layer ON memories(layer);
CREATE INDEX IF NOT EXISTS idx_created ON memories(created);
CREATE INDEX IF NOT EXISTS idx_emotion_weight ON memories(emotion_weight);
CREATE INDEX IF NOT EXISTS idx_confidence ON memories(confidence);
CREATE INDEX IF NOT EXISTS idx_archived ON memories(archived);
CREATE INDEX IF NOT EXISTS idx_review_needed ON memories(review_needed);
CREATE INDEX IF NOT EXISTS idx_overwrite_weight ON memories(overwrite_weight);
CREATE INDEX IF NOT EXISTS idx_parent_id ON memories(parent_id);

CREATE TABLE IF NOT EXISTS consolidation_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    phase           TEXT NOT NULL,
    action          TEXT NOT NULL,
    details         TEXT,
    affected_count  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS retrieval_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    query           TEXT NOT NULL,
    result_count    INTEGER DEFAULT 0,
    top_score       REAL DEFAULT 0,
    feedback        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS knowledge_gaps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    query           TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    count           INTEGER DEFAULT 1
);

-- v3.0: 记忆覆写版本链（冷归档，不参与检索）
CREATE TABLE IF NOT EXISTS memory_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id       TEXT NOT NULL REFERENCES memories(id),
    version         INTEGER NOT NULL,
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    overwrite_weight REAL NOT NULL,
    overwrite_reason TEXT,
    created         TEXT NOT NULL,
    snapshot        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mv_memory_id ON memory_versions(memory_id);
CREATE INDEX IF NOT EXISTS idx_mv_version ON memory_versions(memory_id, version);
"""


async def get_db():
    """异步上下文管理器，返回 aiosqlite 连接（row_factory = sqlite3.Row）。"""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    """启动时执行，创建所有表 + 迁移旧数据。"""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    # 逐条执行，避免 executescript 在大 schema 上阻塞
    for stmt in SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt and not stmt.startswith("--"):
            await db.execute(stmt)
    await db.commit()

    # v3.0 迁移：为已有记录回填 overwrite_weight
    await _migrate_v3(db)

    await db.close()


async def _migrate_v3(db):
    """v3.0 数据迁移：回填覆写权值字段。"""
    import json, math
    from datetime import datetime, timezone

    cursor = await db.execute(
        "SELECT id, overwrite_weight, version, parent_id, compressed_to, region_trace FROM memories WHERE overwrite_weight = 0.5 AND version = 1 LIMIT 1"
    )
    test = await cursor.fetchone()
    if test is None:
        # 全部已迁移，跳过
        return

    cursor = await db.execute("SELECT * FROM memories")
    rows = await cursor.fetchall()
    now = datetime.now(timezone.utc).isoformat()

    for row in rows:
        r = dict(row)
        ow = _compute_migration_weight(r)
        await db.execute(
            "UPDATE memories SET overwrite_weight = ? WHERE id = ?",
            (round(ow, 4), r["id"]),
        )

    await db.commit()


def _compute_migration_weight(row: dict) -> float:
    """为已有记录回填覆写权值。"""
    importance = row.get("importance", 0.5)
    corroboration = min(1.0, row.get("corroboration_count", 0) / 10)
    specificity = _estimate_specificity(row.get("content", ""))
    access = min(1.0, row.get("access_count", 0) / 20)
    return (
        importance * 0.25
        + corroboration * 0.20
        + (min(1.0, row.get("corroboration_count", 0) / 10)) * 0.20
        + specificity * 0.20
        + access * 0.15
    )


def _estimate_specificity(text: str) -> float:
    """估算文本特异度：检测路径/数字/命令/配置值的密度。"""
    import re
    if not text:
        return 0.3
    patterns = [
        r"[A-Za-z]:[\\/][\w\\/\-\.]+",  # Windows path
        r"~?/[\w/\-\.]+",                # Unix path
        r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+",  # IP:port
        r"\b(\.exe|\.py|\.md|\.yaml|\.json|\.toml)\b",  # 文件扩展名
        r"\b(v\d+\.\d+|port\s*\d+|token|api.?key)\b",  # 版本/端口/密钥
        r"\b(pip\s+install|npm\s+i|git\s+clone)\b",   # 命令
    ]
    hits = sum(1 for p in patterns if re.search(p, text, re.IGNORECASE))
    return min(1.0, 0.15 + hits * 0.12)
