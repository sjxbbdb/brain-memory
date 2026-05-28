"""One-time dedup cleanup: archive duplicate memories based on 60-char prefix."""
import sqlite3, os, sys

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'brain_memory.db')

def main():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM memories WHERE archived = 0 ORDER BY created ASC").fetchall()
    
    seen_prefixes = {}
    to_archive = []
    
    for r in rows:
        content = (r['content'] or '').strip()
        prefix = content[:60].lower()
        if prefix and len(prefix) >= 20 and prefix in seen_prefixes:
            to_archive.append((r['id'], seen_prefixes[prefix], r['title']))
            print(f"DUP: {r['id']} -> {seen_prefixes[prefix]} | {r['title'][:60]}")
        else:
            seen_prefixes[prefix] = r['id']
    
    if not to_archive:
        print("No duplicates found.")
    else:
        print(f"\nArchiving {len(to_archive)} duplicates...")
        for dup_id, orig_id, _title in to_archive:
            db.execute("UPDATE memories SET archived = 1 WHERE id = ?", (dup_id,))
        db.commit()
        print(f"Done. {len(to_archive)} duplicates archived.")
    
    remaining = db.execute("SELECT COUNT(*) FROM memories WHERE archived = 0").fetchone()[0]
    print(f"Remaining active memories: {remaining}")
    db.close()

if __name__ == '__main__':
    main()
