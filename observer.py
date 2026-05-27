#!/usr/bin/env python3
"""Brain Memory Observer — 轮询摄入 feed 文件，自动推送至记忆系统。

两种运行模式：
  1. 周期模式（配合 cron）：python observer.py --once
  2. 守护模式：python observer.py --daemon

Feed 文件格式（JSONL）：
  {"text": "...", "source": "hermes", "explicit_mark": false, "metadata": {}}

任何智能体只需向 feed 文件追加一行 JSON 即可触发自动记忆摄入。
"""
import json
import os
import sys
import time
import requests
from datetime import datetime
from pathlib import Path

FEED_PATH = Path(__file__).parent / "ingest_feed.jsonl"
CURSOR_PATH = Path(__file__).parent / ".observer_cursor"
INGEST_URL = "http://127.0.0.1:8765/api/v1/ingest"
POLL_INTERVAL = 30
BATCH_SIZE = 20
NL = chr(10)


def read_cursor():
    if CURSOR_PATH.exists():
        return int(CURSOR_PATH.read_text().strip())
    return 0


def write_cursor(pos):
    CURSOR_PATH.write_text(str(pos))


def ensure_feed():
    if not FEED_PATH.exists():
        FEED_PATH.touch()


def process_feed():
    ensure_feed()
    cursor = read_cursor()
    file_size = FEED_PATH.stat().st_size
    
    if file_size <= cursor:
        return {"processed": 0, "accepted": 0, "rejected": 0, "errors": 0}
    
    with open(FEED_PATH, "r", encoding="utf-8") as f:
        f.seek(cursor)
        new_content = f.read()
    
    raw_lines = new_content.strip().split(NL)
    entries = [l.strip() for l in raw_lines if l.strip()]
    
    if not entries:
        return {"processed": 0, "accepted": 0, "rejected": 0, "errors": 0}
    
    processed = 0
    accepted = 0
    rejected = 0
    errors = 0
    
    for line in entries[-BATCH_SIZE:]:
        processed += 1
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            errors += 1
            continue
        
        if "text" not in entry:
            errors += 1
            continue
        
        try:
            resp = requests.post(
                INGEST_URL,
                json={
                    "text": entry["text"],
                    "source": entry.get("source", "unknown"),
                    "explicit_mark": entry.get("explicit_mark", False),
                    "force": entry.get("force", False),
                    "metadata": entry.get("metadata"),
                },
                timeout=10,
            )
            if resp.status_code == 200:
                result = resp.json()
                if result.get("accepted"):
                    accepted += 1
                else:
                    rejected += 1
            else:
                errors += 1
        except requests.RequestException:
            errors += 1
    
    write_cursor(FEED_PATH.stat().st_size)
    
    return {
        "processed": processed,
        "accepted": accepted,
        "rejected": rejected,
        "errors": errors,
    }


def run_once():
    result = process_feed()
    if result["processed"] > 0:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] observer: {result['processed']} processed, "
              f"{result['accepted']} accepted, {result['rejected']} rejected, "
              f"{result['errors']} errors")
    return result


def run_daemon():
    print(f"[observer] daemon started, watching {FEED_PATH}")
    print(f"[observer] ingest URL: {INGEST_URL}")
    print(f"[observer] poll interval: {POLL_INTERVAL}s")
    while True:
        try:
            result = run_once()
        except Exception as e:
            print(f"[observer] error: {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


def append_to_feed(text, source="unknown", explicit_mark=False, metadata=None):
    ensure_feed()
    entry = {
        "text": text,
        "source": source,
        "explicit_mark": explicit_mark,
        "metadata": metadata or {},
        "timestamp": datetime.now().isoformat(),
    }
    with open(FEED_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + NL)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Brain Memory Observer")
    p.add_argument("--once", action="store_true")
    p.add_argument("--daemon", action="store_true")
    p.add_argument("--append", type=str)
    p.add_argument("--source", type=str, default="cli")
    p.add_argument("--feed-only", action="store_true")
    args = p.parse_args()
    
    if args.append:
        append_to_feed(args.append, source=args.source)
        print(f"Appended to feed (source={args.source})")
        if not args.feed_only:
            run_once()
    elif args.daemon:
        run_daemon()
    else:
        run_once()
