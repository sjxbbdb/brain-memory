import tempfile
import unittest
from pathlib import Path

from storage.database import MemoryStore, init_db


class MemoryStoreSaveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str((Path(self._tmp.name).resolve() / "memory.sqlite3").resolve())
        init_db(self.db_path)
        self.store = MemoryStore(self.db_path)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _row(self, memory_id="mem-1"):
        return self.store.conn.execute(
            "SELECT id, title, content, created, is_identity_forming, "
            "access_count, last_accessed, decay_rate, archived, consolidated, "
            "last_consolidated FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()

    def test_first_insert_keeps_original_content_and_defaults(self):
        created = "2026-09-19T00:00:00+00:00"
        self.assertEqual(
            self.store.save(
                {
                    "id": "mem-first",
                    "title": "first",
                    "content": "initial",
                    "created": created,
                }
            ),
            "mem-first",
        )

        row = self._row("mem-first")
        self.assertEqual(row["title"], "first")
        self.assertEqual(row["content"], "initial")
        self.assertEqual(row["created"], created)
        self.assertEqual(row["access_count"], 0)
        self.assertIsNone(row["last_accessed"])
        self.assertAlmostEqual(row["decay_rate"], 0.05)
        self.assertEqual(row["archived"], 0)
        self.assertEqual(row["consolidated"], 0)
        self.assertIsNone(row["last_consolidated"])

    def test_resave_updates_memory_fields_without_replacing_history(self):
        created = "2026-09-19T00:00:00+00:00"
        self.store.save(
            {
                "id": "mem-1",
                "title": "before",
                "content": "old content",
                "created": created,
                "importance": 0.4,
            }
        )
        self.store.boost_on_access("mem-1", boost=0.1)
        accessed = self._row()["last_accessed"]
        self.assertIsNotNone(accessed)
        self.store.conn.execute(
            "UPDATE memories SET decay_rate = ?, archived = ?, consolidated = ?, "
            "last_consolidated = ? WHERE id = ?",
            (0.17, 1, 1, "2026-09-19T00:01:00+00:00", "mem-1"),
        )
        self.store.conn.commit()

        self.store.save(
            {
                "id": "mem-1",
                "title": "after",
                "content": "new content",
                "created": "2099-01-01T00:00:00+00:00",
                "importance": 0.9,
                "is_identity_forming": True,
            }
        )

        self.store.close()
        self.store = MemoryStore(self.db_path)
        row = self._row()
        self.assertEqual(row["title"], "after")
        self.assertEqual(row["content"], "new content")
        self.assertEqual(row["created"], created)
        self.assertEqual(row["is_identity_forming"], 1)
        self.assertEqual(row["access_count"], 1)
        self.assertEqual(row["last_accessed"], accessed)
        self.assertAlmostEqual(row["decay_rate"], 0.17)
        self.assertEqual(row["archived"], 1)
        self.assertEqual(row["consolidated"], 1)
        self.assertEqual(row["last_consolidated"], "2026-09-19T00:01:00+00:00")
        count = self.store.conn.execute(
            "SELECT COUNT(*) AS row_count FROM memories WHERE id = ?", ("mem-1",)
        ).fetchone()["row_count"]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
