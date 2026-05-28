import json, os, shutil
from pathlib import Path
from datetime import datetime

# ============================================================
# Brain Memory v3.1 — Self-Installer
# ============================================================
# Detects agent framework and installs auto-ingest hook.
# Agent runs: python hooks/install.py
# Result: session_append called automatically. Zero user action.

PROJECT_DIR = Path(__file__).parent.parent.resolve()
AGENTS_DIR = Path.home() / ".agents"
installed = []

# ── 1. Hermes Skill Update ──
hermes_skill = AGENTS_DIR / "skills" / "brain-memory" / "SKILL.md"
if hermes_skill.exists():
    content = hermes_skill.read_text(encoding="utf-8")

    # Update version
    content = content.replace("version: 1.0.0", "version: 3.1.0")
    content = content.replace("Brain Memory (v1)", "Brain Memory (v3.1)")
    content = content.replace("当前适配层: Claude Code", "当前适配层: Hermes + MCP (v3.1)")

    # Replace old setup-hooks with real auto-ingest
    old_hooks = content.find("## +setup-hooks")
    old_end = content.find("## 平台扩展路线", old_hooks)
    if old_end == -1:
        old_end = content.find("---", old_hooks + 200)

    new_hooks = """## +auto-ingest (v3.1 自安装)

CRITICAL — 此钩子已由 install.py 自动安装。每轮对话结束后 MUST 调用以下任一通道:

### 通道 A: MCP session_append（优先）
```
MCP 工具: session_append
参数: {"speaker": "<user|agent>", "text": "<对话内容>", "goal": "<当前目标>"}
```

### 通道 B: REST API
```
POST http://127.0.0.1:8000/api/v1/session/append
Body: {"speaker": "...", "text": "...", "goal": "..."}
```

### 通道 C: 文件投递
```
echo '{"speaker":"agent","text":"...","goal":"..."}' >> sessions/current.jsonl
```

**调度器每 30 秒自动消费，无需等待。**

### +session-start
会话开始 MUST 调用 MCP session_start 获取告警+记忆+叙事。

---

"""

    if old_hooks > 0:
        content = content[:old_hooks] + new_hooks + content[old_end:]

    # Add install timestamp
    content += f"\n\n<!-- v3.1 auto-installed at {datetime.now().isoformat()} -->\n"

    hermes_skill.write_text(content, encoding="utf-8")
    installed.append("Hermes skill updated to v3.1")

# ── 2. Create sessions/ dir ──
sessions_dir = PROJECT_DIR / "sessions"
sessions_dir.mkdir(exist_ok=True)
(sessions_dir / "processed").mkdir(exist_ok=True)
(sessions_dir / ".gitkeep").write_text("")
installed.append(f"sessions/ dir ready at {sessions_dir}")

# ── Summary ──
print("=" * 50)
print("Brain Memory v3.1 — 安装完成")
print("=" * 50)
for item in installed:
    print(f"  {item}")
print()
print("Agent 现在每轮对话会自动:")
print("  1. 调 session_start 获取上下文")
print("  2. 调 session_append 写入对话")
print("  3. 30s 后自动入库")
print()
print("用户侧: 零操作")
print("=" * 50)
