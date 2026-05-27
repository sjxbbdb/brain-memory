"""SQLite 数据库连接、建表、初始化。"""
import sqlite3
import aiosqlite
from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL CHECK(type IN ('episodic','semantic','procedural','narrative','global')),
    layer           TEXT NOT NULL CHECK(layer IN ('episodic','semantic','procedural','narrative','global')),
    title           TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    importance      REAL NOT NULL DEFAULT 0.5,
    novelty         REAL NOT NULL DEFAULT 0.5,
    failure_cost    REAL NOT NULL DEFAULT 0.5,
    goal_relevance  REAL NOT NULL DEFAULT 0.5,
    emotion_weight  REAL NOT NULL DEFAULT 0.5,
    surprise_score  REAL NOT NULL DEFAULT 0.3,
    confidence      REAL NOT NULL DEFAULT 0.8,
    source          TEXT NOT NULL DEFAULT 'direct_experience',
    corroboration_count INTEGER NOT NULL DEFAULT 0,
    access_count    INTEGER NOT NULL DEFAULT 0,
    last_accessed   TEXT,
    decay_rate      REAL NOT NULL DEFAULT 0.05,
    half_life_days  REAL NOT NULL DEFAULT 30,
    created         TEXT NOT NULL,
    consolidated    INTEGER NOT NULL DEFAULT 0,
    consolidated_to TEXT DEFAULT '[]',
    last_consolidated TEXT,
    tags            TEXT DEFAULT '[]',
    entities        TEXT DEFAULT '[]',
    relations       TEXT DEFAULT '[]',
    contradicted_by TEXT DEFAULT '[]',
    supersedes      TEXT DEFAULT '[]',
    review_needed   INTEGER NOT NULL DEFAULT 0,
    archived        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_type ON memories(type);
CREATE INDEX IF NOT EXISTS idx_layer ON memories(layer);
CREATE INDEX IF NOT EXISTS idx_created ON memories(created);
CREATE INDEX IF NOT EXISTS idx_emotion_weight ON memories(emotion_weight);
CREATE INDEX IF NOT EXISTS idx_confidence ON memories(confidence);
CREATE INDEX IF NOT EXISTS idx_archived ON memories(archived);
CREATE INDEX IF NOT EXISTS idx_review_needed ON memories(review_needed);

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
"""


async def get_db():
    """异步上下文管理器，返回 aiosqlite 连接（row_factory = sqlite3.Row）。"""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    """启动时执行，创建所有表。"""
    db = await aiosqlite.connect(DB_PATH)
    await db.executescript(SCHEMA)
    await db.commit()
    await db.close()
