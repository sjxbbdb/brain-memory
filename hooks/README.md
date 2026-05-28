# Brain Memory Hook — Agent Framework Integration

Add one import, two lines, and zero user operations.

## Quick Start

```python
from hooks.agent_hook import BrainMemoryHook

hook = BrainMemoryHook(
    base_url="http://127.0.0.1:8000",
    project_dir="/path/to/brain-memory",
)

# In your framework's on_turn_end callback:
async def on_turn_end(speaker, text, goal=None):
    await hook.append(speaker, text, goal)

# In your framework's on_session_start callback:
async def on_session_start(goal=None):
    ctx = await hook.session_start(goal)
    # ctx["memories"], ctx["alerts"], ctx["narratives"]
```

## Three-Mode Auto-Fallback

The hook tries modes in order, no config needed:

| Mode | When | Requires |
|---|---|---|
| HTTP | Service running | `aiohttp` or `httpx` |
| File | HTTP down | `sessions/` dir writable |
| Feed | File down | `ingest_feed.jsonl` writable |

## Framework Integration Examples

### Hermes / Async Framework

```python
class HermesAgent:
    def __init__(self):
        self.brain = BrainMemoryHook(
            base_url="http://127.0.0.1:8000",
            project_dir="/path/to/brain-memory",
        )

    async def on_turn_end(self, speaker: str, text: str):
        await self.brain.append(speaker, text, goal=self.current_goal)

    async def on_session_start(self):
        ctx = await self.brain.session_start(self.current_goal)
        if ctx.get("alerts"):
            self.show_alerts(ctx["alerts"])
```

### Claude Code / MCP Agent

```json
{
  "mcpServers": {
    "brain-memory": {
      "command": "python",
      "args": ["/path/to/brain-memory/mcp_server.py"]
    }
  }
}
```

Then call `session_append` after each turn and `session_start` at session open.
No hook file needed — MCP handles it natively.

### Sync Framework (no async)

```python
from hooks.agent_hook import SyncBrainMemoryHook

hook = SyncBrainMemoryHook(
    base_url="http://127.0.0.1:8000",
    project_dir="/path/to/brain-memory",
)

def on_turn_end(speaker, text, goal=None):
    hook.append(speaker, text, goal)  # sync, no await
```

## Requirements

For HTTP mode, one of: `pip install aiohttp` or `pip install httpx`

For file/feed mode: no dependencies beyond stdlib.
