import json, logging, os, sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("brain-memory.hook")

class BrainMemoryHook:
    def __init__(self, base_url="http://127.0.0.1:8000", project_dir=None):
        self.base_url = base_url.rstrip("/")
        self.project_dir = Path(project_dir) if project_dir else None
        self._http_ok = None
        self._sessions_dir = self.project_dir / "sessions" if self.project_dir else None

    async def append(self, speaker, text, goal=None):
        if await self._try_http(speaker, text, goal):
            return True
        if self._try_file(speaker, text, goal):
            return True
        return self._try_feed(speaker, text, goal)

    async def _try_http(self, speaker, text, goal):
        if self._http_ok is False:
            return False
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/api/v1/session/append",
                    json={"speaker": speaker, "text": text, "goal": goal},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    self._http_ok = resp.status == 200
                    return self._http_ok
        except Exception:
            self._http_ok = False
            return False

    def _try_file(self, speaker, text, goal):
        if not self._sessions_dir:
            return False
        try:
            self._sessions_dir.mkdir(parents=True, exist_ok=True)
            entry = {
                "speaker": speaker, "text": text, "goal": goal,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            NL = chr(10)
            with open(self._sessions_dir / "current.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + NL)
            return True
        except Exception:
            return False

    def _try_feed(self, speaker, text, goal):
        if not self.project_dir:
            return False
        try:
            entry = {
                "text": f"[{speaker}]: {text}",
                "source": f"hook:{speaker}",
                "explicit_mark": any(
                    kw in text.lower()
                    for kw in ["bug", "error", "fixed", "solved", "conflict"]
                ),
                "metadata": {"goal": goal} if goal else {},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            NL = chr(10)
            with open(self.project_dir / "ingest_feed.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + NL)
            return True
        except Exception:
            return False

    async def session_start(self, goal=None):
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/api/v1/context/auto-trigger",
                    json={"query": goal or "current task", "top_k": 5},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return await resp.json() if resp.status == 200 else {}
        except Exception:
            return {}


class SyncBrainMemoryHook:
    def __init__(self, base_url="http://127.0.0.1:8000", project_dir=None):
        self.base_url = base_url.rstrip("/")
        self.project_dir = Path(project_dir) if project_dir else None
        self._sessions_dir = self.project_dir / "sessions" if self.project_dir else None
        self._http_ok = None

    def append(self, speaker, text, goal=None):
        if self._http_ok is not False:
            try:
                import urllib.request
                data = json.dumps({
                    "speaker": speaker, "text": text, "goal": goal,
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"{self.base_url}/api/v1/session/append",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5)
                self._http_ok = True
                return True
            except Exception:
                self._http_ok = False
        if self._sessions_dir:
            try:
                self._sessions_dir.mkdir(parents=True, exist_ok=True)
                entry = {
                    "speaker": speaker, "text": text, "goal": goal,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                NL = chr(10)
                with open(self._sessions_dir / "current.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + NL)
                return True
            except Exception:
                pass
        if self.project_dir:
            try:
                entry = {
                    "text": f"[{speaker}]: {text}",
                    "source": f"hook:{speaker}",
                    "metadata": {"goal": goal} if goal else {},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                NL = chr(10)
                with open(self.project_dir / "ingest_feed.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + NL)
                return True
            except Exception:
                pass
        return False
