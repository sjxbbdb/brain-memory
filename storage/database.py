"""Brain Memory v5.0 — storage layer with shared connections for concurrency."""

import json, logging, sqlite3, uuid
from datetime import datetime, timezone

from config import DB_PATH

logger = logging.getLogger("brain-v5.storage")
DB_TIMEOUT = 10.0
STATE_SNAPSHOT_RETENTION = 200


def _connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: str = DB_PATH):
    """Create the database schema at ``db_path`` if it does not exist.

    The original implementation always initialized ``DB_PATH`` even when a
    ``Brain`` instance was configured with another database.  Keeping the
    path explicit is important for isolated tests, backups, and running more
    than one brain instance on the same machine.
    """
    conn = _connect(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY, type TEXT NOT NULL, title TEXT, content TEXT,
            entities TEXT DEFAULT '[]', emotion_tags TEXT DEFAULT '[]',
            importance REAL DEFAULT 0.5, summary TEXT, source TEXT,
            emotion_label TEXT DEFAULT 'neutral', emotion_vector TEXT DEFAULT '{}',
            created TEXT NOT NULL, access_count INTEGER DEFAULT 0,
            last_accessed TEXT, decay_rate REAL DEFAULT 0.05,
            llm_encoded INTEGER DEFAULT 0, archived INTEGER DEFAULT 0,
            embedding TEXT DEFAULT NULL, is_identity_forming INTEGER DEFAULT 0,
            consolidated INTEGER DEFAULT 0, last_consolidated TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS brain_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, snapshot TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS output_feed (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL, data TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS habits (
            id TEXT PRIMARY KEY, trigger TEXT, pattern TEXT, response TEXT,
            confidence REAL DEFAULT 0.6
        )""")
        for col, ct in [
            ("embedding", "TEXT DEFAULT NULL"),
            ("is_identity_forming", "INTEGER DEFAULT 0"),
            ("consolidated", "INTEGER DEFAULT 0"),
            ("last_consolidated", "TEXT"),
        ]:
            try:
                conn.execute("ALTER TABLE memories ADD COLUMN {} {}".format(col, ct))
            except sqlite3.OperationalError:
                pass
        conn.commit()
        logger.info("storage: database initialized")
    finally:
        conn.close()


class StateStore:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _connect(self.db_path)
        return self._conn

    def save(self, snapshot: dict) -> bool:
        if not isinstance(snapshot, dict) or not snapshot.get("timestamp"):
            logger.error("state-store snapshot is missing a timestamp")
            return False
        try:
            payload = json.dumps(snapshot, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            logger.error("state-store snapshot is not serializable: %s", str(exc)[:120])
            return False
        # Retry once after reconnecting.  Recursive retry used to make a
        # persistent disk/lock error recurse until the process crashed.
        for attempt in range(2):
            try:
                self.conn.execute(
                    "INSERT INTO brain_state (timestamp, snapshot) VALUES (?, ?)",
                    (snapshot["timestamp"], payload),
                )
                # Keep the journal bounded so an always-on consciousness does
                # not eventually exhaust the local disk.  The newest 200
                # snapshots still provide ample crash/restart history.
                self.conn.execute(
                    "DELETE FROM brain_state WHERE id NOT IN "
                    "(SELECT id FROM brain_state ORDER BY id DESC LIMIT ?)",
                    (STATE_SNAPSHOT_RETENTION,),
                )
                self.conn.commit()
                return True
            except sqlite3.Error as e:
                logger.warning("state-store save failed (attempt %d): %s", attempt + 1, str(e)[:80])
                self._reset_connection()
        logger.error("state-store save abandoned after reconnect retry")
        return False

    def _reset_connection(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def close(self):
        """Close the persistent connection, if it is open."""
        self._reset_connection()

    def load_latest(self) -> dict | None:
        try:
            row = self.conn.execute(
                "SELECT snapshot FROM brain_state ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return json.loads(row["snapshot"]) if row else None
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            logger.warning("state-store load failed: %s", str(exc)[:100])
            self._reset_connection()
            return None


class MemoryStore:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _connect(self.db_path)
        return self._conn

    def _reconnect(self):
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass
        self._conn = _connect(self.db_path)

    def close(self):
        """Close the persistent connection, if it is open."""
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def save(self, memory: dict) -> str:
        mem_id = memory.get("id") or "mem-" + uuid.uuid4().hex[:12]
        vals = (
            mem_id, memory.get("type", "episodic"),
            memory.get("title", ""), memory.get("content", ""),
            json.dumps(memory.get("entities", []), ensure_ascii=False),
            json.dumps(memory.get("emotion_tags", []), ensure_ascii=False),
            memory.get("importance", 0.5),
            json.dumps(memory.get("embedding", []), ensure_ascii=False) if memory.get("embedding") else None,
            memory.get("summary", ""), memory.get("source", "unknown"),
            memory.get("emotion_label", "neutral"),
            json.dumps(memory.get("emotion_vector", {}), ensure_ascii=False),
            memory.get("created", datetime.now(timezone.utc).isoformat()),
            1 if memory.get("llm_encoded") else 0,
            1 if memory.get("is_identity_forming") else 0,
        )
        sql = """INSERT OR REPLACE INTO memories (id, type, title, content, entities, emotion_tags,
            importance, embedding, summary, source, emotion_label, emotion_vector, created, llm_encoded, is_identity_forming)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        try:
            self.conn.execute(sql, vals)
            self.conn.commit()
        except sqlite3.Error as e:
            logger.warning("memory-store save failed: %s", str(e)[:80])
            self._reconnect()
            self.conn.execute(sql, vals)
            self.conn.commit()
        return mem_id

    def search(self, query: str, limit: int = 10) -> list[dict]:
        try:
            keywords = [query]
            parts = query.split()
            if parts:
                kw = []
                for part in parts:
                    if len(part) <= 2:
                        kw.append(part)
                    else:
                        for i in range(len(part) - 1):
                            bigram = part[i:i+2]
                            if not all(ord(c) < 128 and not c.isalnum() for c in bigram):
                                kw.append(bigram)
                keywords = list(set(kw))[:15] or [query]
            conds = " OR ".join("(content LIKE ? OR title LIKE ? OR summary LIKE ?)" for _ in keywords)
            params = []
            for kw in keywords:
                params.extend(["%" + kw + "%"] * 3)
            params.append(limit)
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE archived=0 AND (" + conds + ") ORDER BY created DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            self._reconnect()
            return []

    def get_identity_memories(self, limit: int = 20) -> list[dict]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE archived=0 AND is_identity_forming=1 ORDER BY importance DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            self._reconnect()
            return []

    def decay_all(self, decay_rate: float = 0.01, archive_threshold: float = 0.15) -> dict:
        """Decay all active memories. Archives those below threshold."""
        try:
            # Reduce importance
            self.conn.execute(
                "UPDATE memories SET importance = MAX(0.0, importance - ?) WHERE archived = 0",
                (decay_rate,),
            )
            # Archive low-importance memories (but not identity-forming ones)
            cursor = self.conn.execute(
                "SELECT COUNT(*) FROM memories WHERE archived = 0 AND importance < ? AND is_identity_forming = 0",
                (archive_threshold,),
            )
            to_archive = cursor.fetchone()[0]
            if to_archive > 0:
                self.conn.execute(
                    "UPDATE memories SET archived = 1 WHERE archived = 0 AND importance < ? AND is_identity_forming = 0",
                    (archive_threshold,),
                )
            self.conn.commit()
            return {"decayed": True, "archived": to_archive}
        except sqlite3.Error:
            self._reconnect()
            return {"decayed": False, "archived": 0}

    def boost_on_access(self, mem_id: str, boost: float = 0.05):
        """Boost a memory's importance when it's retrieved."""
        try:
            self.conn.execute(
                "UPDATE memories SET importance = MIN(1.0, importance + ?), access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                (boost, __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(), mem_id),
            )
            self.conn.commit()
        except sqlite3.Error:
            self._reconnect()

    def total_archived(self) -> int:
        """Count archived memories."""
        try:
            row = self.conn.execute("SELECT COUNT(*) FROM memories WHERE archived = 1").fetchone()
            return row[0] if row else 0
        except sqlite3.Error:
            self._reconnect()
            return 0

    def count(self) -> int:
        try:
            row = self.conn.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()
            return row[0] if row else 0
        except sqlite3.Error:
            self._reconnect()
            return 0
