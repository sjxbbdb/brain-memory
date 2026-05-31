"""Agent Tool Registry — v5.0 工具注册系统。

融合 Claude Code 和 Hermes 的优点：
  - CC 风格: 每个工具是带 prompt() 自描述 + isConcurrencySafe + isReadOnly 的完整合约
  - Hermes 风格: AST 扫描自动发现 + 统一注册表 + check_fn 可用性检查

大脑通过 intent 输出"我想用 X 工具"，此模块负责调度执行。
大脑不直接 import 此模块——只通过 agent_bridge 间接调用。
"""

import asyncio
import importlib
import ast
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("brain-v5.tool-registry")


# ── Tool 合约定义 ──

@dataclass
class ToolDef:
    """一个工具的完整定义——Claude Code 风格。

    每个工具自己负责：
      - prompt(): 生成 LLM 可读的使用说明
      - schema: OpenAI function calling 格式的输入 schema
      - isEnabled(): 运行时是否可用
      - isReadOnly(): 是否只读（决定并发安全分区）
      - isConcurrencySafe(): 是否可以和其他工具并发执行
      - call(): 实际执行函数
    """
    name: str
    description: str
    schema: dict
    call: Callable  # async def call(args, context) -> str
    # 能力标记
    is_read_only: bool = False
    concurrency_safe: bool = False
    # 可用性
    check_fn: Callable | None = None  # () -> bool
    required_env: list[str] = field(default_factory=list)
    # 元数据
    emoji: str = "⚡"
    max_result_chars: int = 50000
    # 动态 prompt 生成（CC 风格）
    prompt_fn: Callable | None = None  # async (context) -> str

    def is_enabled(self) -> bool:
        if self.check_fn:
            try:
                return bool(self.check_fn())
            except Exception:
                return False
        return True

    def to_openai_schema(self) -> dict:
        """生成 OpenAI function calling 格式的 schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema.get("parameters", {}),
            },
        }

    async def get_prompt(self, context: dict | None = None) -> str:
        """获取工具的 LLM 可读描述（CC 风格）。"""
        if self.prompt_fn:
            try:
                return await self.prompt_fn(context or {})
            except Exception:
                pass
        return self.description


# ── 工具注册表 ──

class ToolRegistry:
    """统一工具注册表。

    每个工具文件在模块顶层调用 registry.register(ToolDef(...))。
    通过 discover_tools() 自动扫描并 import agent/tools/ 目录下的所有工具。
    """

    def __init__(self):
        self._tools: Dict[str, ToolDef] = {}
        self._lock = threading.RLock()
        self._generation: int = 0  # 每次注册注销递增，外部可据此缓存

    def register(self, tool: ToolDef) -> None:
        with self._lock:
            existing = self._tools.get(tool.name)
            if existing:
                logger.warning("Tool '%s' re-registered (overwriting)", tool.name)
            self._tools[tool.name] = tool
            self._generation += 1
            logger.debug("Tool registered: %s (read_only=%s)", tool.name, tool.is_read_only)

    def deregister(self, name: str) -> None:
        with self._lock:
            if name in self._tools:
                del self._tools[name]
                self._generation += 1

    def get(self, name: str) -> ToolDef | None:
        with self._lock:
            return self._tools.get(name)

    def get_all(self) -> List[ToolDef]:
        with self._lock:
            return list(self._tools.values())

    def get_enabled(self) -> List[ToolDef]:
        return [t for t in self.get_all() if t.is_enabled()]

    def get_names(self) -> List[str]:
        return sorted(self._tools.keys())

    def get_schemas(self) -> List[dict]:
        """获取所有已启用工具的 OpenAI schema 列表。"""
        return [t.to_openai_schema() for t in self.get_enabled()]

    async def get_tool_prompts(self, context: dict | None = None) -> str:
        """获取所有工具的 LLM 可读描述（用于注入系统提示）。"""
        tools = self.get_enabled()
        lines = ["可用工具:"]
        for t in tools:
            prompt = await t.get_prompt(context)
            ro = " [只读]" if t.is_read_only else " [读写]"
            lines.append(f"  {t.emoji} {t.name}{ro}: {prompt[:200]}")
        return "\n".join(lines)

    async def dispatch(self, name: str, args: dict, context: dict | None = None) -> str:
        """执行工具。同步和异步 handler 均支持。"""
        tool = self.get(name)
        if not tool:
            return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)
        try:
            if asyncio.iscoroutinefunction(tool.call):
                return await tool.call(args, context or {})
            return tool.call(args, context or {})
        except Exception as e:
            logger.exception("Tool dispatch error: %s", name)
            return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)

    @property
    def generation(self) -> int:
        return self._generation


# ── 自动发现 ──

def _module_registers_tools(module_path: Path) -> bool:
    """AST 扫描：检查模块顶层是否调用了 registry.register()。"""
    try:
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))
    except (OSError, SyntaxError):
        return False
    for stmt in tree.body:
        if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Attribute)
                and stmt.value.func.attr == "register"
                and isinstance(stmt.value.func.value, ast.Name)
                and stmt.value.func.value.id == "registry"):
            return True
    return False


def discover_tools(tools_dir: Path | None = None) -> List[str]:
    """自动扫描并导入 agent/tools/ 下的所有工具模块。"""
    if tools_dir is None:
        tools_dir = Path(__file__).resolve().parent / "tools"
    if not tools_dir.exists():
        return []
    imported = []
    for path in sorted(tools_dir.glob("*.py")):
        if path.name.startswith("_") or path.name == "__init__.py":
            continue
        if _module_registers_tools(path):
            mod_name = f"tools.{path.stem}"
            try:
                # 把 tools_dir 的父目录加到 sys.path
                import sys
                parent = str(tools_dir.parent)
                if parent not in sys.path:
                    sys.path.insert(0, parent)
                mod_name = path.stem
                importlib.import_module(mod_name)
                imported.append(mod_name)
            except Exception as e:
                logger.warning("Could not import tool module %s: %s", mod_name, e)
    return imported


# ── 全局单例 ──
registry = ToolRegistry()
