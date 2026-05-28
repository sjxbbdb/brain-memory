"""向 Brain Memory feed 追加记录并立即触发处理。

用法:
  python feed_append.py "文本" --source hermes          # 追加 + 立即处理（带去重）
  python feed_append.py "文本" --source hermes --mark   # 标记重要
  python feed_append.py "文本" --source hermes --force  # 强制摄入（跳过重复检测）
  python feed_append.py "文本" --source hermes --no-process  # 只追加
"""
import sys, json, os, subprocess
from datetime import datetime

FEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ingest_feed.jsonl")
OBSERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "observer.py")
PYTHON = sys.executable
NL = chr(10)


def append(text, source="unknown", explicit_mark=False, force=False, metadata=None):
    entry = {
        "text": text,
        "source": source,
        "explicit_mark": explicit_mark,
        "force": force,
        "metadata": metadata or {},
        "timestamp": datetime.now().isoformat(),
    }
    with open(FEED, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + NL)
    return True


def process():
    try:
        result = subprocess.run(
            [PYTHON, OBSERVER, "--once"],
            capture_output=True, text=True, timeout=30,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        output = result.stdout.strip()
        if output:
            print(output)
        return result.returncode == 0
    except Exception as e:
        print(f"[feed_append] observer error: {e}", file=sys.stderr)
        return False


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Brain Memory Feed Append")
    p.add_argument("text", help="Text to append")
    p.add_argument("--source", default="cli")
    p.add_argument("--mark", action="store_true", help="Explicit mark (bypass gate)")
    p.add_argument("--force", action="store_true", help="Force ingest (skip dedup check)")
    p.add_argument("--no-process", action="store_true", help="Skip immediate processing")
    args = p.parse_args()
    
    append(args.text, args.source, args.mark, args.force)
    
    if not args.no_process:
        process()
    else:
        print(f"Appended to feed (source={args.source}) — waiting for cron")
