"""Activation Field — V6 统一状态场。大脑的全局状态总线。

V6 核心设计：从 Pipeline 架构进化为 State 架构。
所有脑区不再互相调用——只读 ActivationField、只写 delta。

三层维度（共14维）:

  第一层·基础激活（来自生理隐喻）:
    valence           愉悦度     0=极负面  1=极正面
    arousal           唤醒度     0=极平静  1=极兴奋
    dominance         掌控感     0=完全受控 1=完全掌控
    fear              威胁水平   0=安全    1=极度恐惧

  第二层·认知状态:
    curiosity         好奇强度   0=不好奇  1=极度好奇
    focus             聚焦度     0=涣散    1=极度专注
    confidence        信心水平   0=无信心  1=极度自信
    attachment        依恋/连接  0=疏离    1=深度依恋
    fatigue           疲劳度     0=精力充沛 1=极度疲惫
    uncertainty       不确定性   0=确定    1=完全不确定

  第三层·动机状态（桥接 V7 Drive Engine）:
    survival_drive    生存驱动   0=无      1=极强
    growth_drive      成长驱动   0=无      1=极强
    exploration_drive 探索驱动   0=无      1=极强
    identity_stability 身份稳定  0=崩溃边缘 1=极度稳固

StateDiffusionEngine:
  声明式规则表定义维度间的动力学传播关系。
  规则可被元认知模块校准（调节权重）。
  不是智能模块——只是矩阵乘法 + 衰减。
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("brain-v6.activation-field")


# ══════════════════════════════════════════════
# 扩散规则表 — 声明式定义维度间的传播关系
# ══════════════════════════════════════════════

@dataclass
class DiffusionRule:
    """一条状态传播规则：源维度变化 → 目标维度变化。
    
    weight: 影响系数（正=促进，负=抑制）
    calibratable: 是否可被元认知校准
    """
    src: str          # 源维度名
    dst: str          # 目标维度名
    weight: float     # 每 tick 影响系数
    description: str  # 人类可读描述
    calibratable: bool = True


# 初始规则集（基于认知科学常识，后续可由元认知校准）
DEFAULT_DIFFUSION_RULES: list[DiffusionRule] = [
    # ── fear 的影响 ──
    DiffusionRule("fear", "focus", -0.15, "恐惧降低注意力聚焦"),
    DiffusionRule("fear", "curiosity", -0.10, "恐惧抑制好奇心"),
    DiffusionRule("fear", "confidence", -0.12, "恐惧削弱信心"),
    DiffusionRule("fear", "survival_drive", +0.20, "恐惧激发生存驱动"),
    DiffusionRule("fear", "arousal", +0.18, "恐惧提升唤醒度"),

    # ── curiosity 的影响 ──
    DiffusionRule("curiosity", "focus", +0.08, "好奇心提升聚焦"),
    DiffusionRule("curiosity", "exploration_drive", +0.25, "好奇心驱动探索"),
    DiffusionRule("curiosity", "growth_drive", +0.12, "好奇心促进成长欲"),
    DiffusionRule("curiosity", "fatigue", -0.05, "好奇时疲劳感降低"),

    # ── focus 的影响 ──
    DiffusionRule("focus", "confidence", +0.08, "专注提升信心"),
    DiffusionRule("focus", "uncertainty", -0.10, "专注降低不确定感"),
    DiffusionRule("focus", "fatigue", +0.06, "长时间专注增加疲劳"),

    # ── confidence 的影响 ──
    DiffusionRule("confidence", "exploration_drive", +0.12, "信心驱动探索"),
    DiffusionRule("confidence", "fear", -0.15, "信心抑制恐惧"),
    DiffusionRule("confidence", "uncertainty", -0.20, "信心降低不确定感"),
    DiffusionRule("confidence", "dominance", +0.15, "信心提升掌控感"),

    # ── attachment 的影响 ──
    DiffusionRule("attachment", "fear", -0.06, "依恋降低威胁感"),
    DiffusionRule("attachment", "survival_drive", +0.05, "依恋增强生存驱动(保护关系)"),
    DiffusionRule("attachment", "valence", +0.08, "依恋提升愉悦度"),

    # ── fatigue 的影响 ──
    DiffusionRule("fatigue", "focus", -0.25, "疲劳严重损害聚焦"),
    DiffusionRule("fatigue", "curiosity", -0.15, "疲劳降低好奇心"),
    DiffusionRule("fatigue", "confidence", -0.08, "疲劳削弱信心"),
    DiffusionRule("fatigue", "arousal", -0.20, "疲劳降低唤醒度"),
    DiffusionRule("fatigue", "exploration_drive", -0.18, "疲劳抑制探索"),

    # ── uncertainty 的影响 ──
    DiffusionRule("uncertainty", "curiosity", +0.20, "不确定激发好奇"),
    DiffusionRule("uncertainty", "confidence", -0.18, "不确定降低信心"),
    DiffusionRule("uncertainty", "fear", +0.12, "不确定增加威胁感"),
    DiffusionRule("uncertainty", "exploration_drive", +0.15, "不确定驱动探索(求解)"),

    # ── valence 的影响 ──
    DiffusionRule("valence", "confidence", +0.10, "正面情绪增强信心"),
    DiffusionRule("valence", "curiosity", +0.06, "正面情绪促进好奇"),
    DiffusionRule("valence", "fear", -0.10, "正面情绪抑制恐惧"),

    # ── arousal 的影响 ──
    DiffusionRule("arousal", "focus", +0.10, "高唤醒提升聚焦"),
    DiffusionRule("arousal", "fatigue", +0.08, "持续高唤醒增加疲劳"),
    DiffusionRule("arousal", "exploration_drive", +0.10, "唤醒驱动行动"),

    # ── dominance 的影响 ──
    DiffusionRule("dominance", "confidence", +0.15, "掌控感增强信心"),
    DiffusionRule("dominance", "fear", -0.15, "掌控感抑制恐惧"),
    DiffusionRule("dominance", "uncertainty", -0.12, "掌控感降低不确定"),

    # ── survival_drive 的影响 ──
    DiffusionRule("survival_drive", "fear", +0.10, "生存驱动增强威胁敏感"),
    DiffusionRule("survival_drive", "focus", +0.12, "生存驱动提升专注"),
    DiffusionRule("survival_drive", "fatigue", -0.08, "生存驱动暂时压制疲劳"),

    # ── growth_drive 的影响 ──
    DiffusionRule("growth_drive", "curiosity", +0.15, "成长驱动激发好奇"),
    DiffusionRule("growth_drive", "exploration_drive", +0.15, "成长驱动推动探索"),

    # ── exploration_drive 的影响 ──
    DiffusionRule("exploration_drive", "curiosity", +0.10, "探索驱动回馈好奇"),
    DiffusionRule("exploration_drive", "fatigue", +0.06, "探索消耗精力"),

    # ── identity_stability 的影响 ──
    DiffusionRule("identity_stability", "confidence", +0.12, "身份稳定增强信心"),
    DiffusionRule("identity_stability", "fear", -0.10, "身份稳定抑制恐惧"),
    DiffusionRule("identity_stability", "uncertainty", -0.18, "身份稳定降低不确定"),
    DiffusionRule("identity_stability", "growth_drive", -0.05, "身份太稳定降低成长紧迫感"),

    # ── V7: creation_drive 的影响 ──
    DiffusionRule("creation_drive", "exploration_drive", +0.08, "创造欲驱动探索"),
    DiffusionRule("creation_drive", "confidence", +0.06, "创造增强信心"),
    DiffusionRule("creation_drive", "fatigue", +0.04, "创造消耗精力"),

    # ── V7: curiosity_drive 的影响 ──
    DiffusionRule("curiosity_drive", "exploration_drive", +0.20, "好奇驱动探索"),
    DiffusionRule("curiosity_drive", "curiosity", +0.15, "好奇驱动激发好奇心"),
    DiffusionRule("curiosity_drive", "focus", +0.08, "好奇提升聚焦"),
    DiffusionRule("curiosity_drive", "fatigue", -0.04, "好奇抑制疲劳感"),

    # ── V7: coherence_drive 的影响 ──
    DiffusionRule("coherence_drive", "focus", +0.15, "一致需求提升聚焦"),
    DiffusionRule("coherence_drive", "confidence", +0.10, "一致需求增强信心(解决矛盾)"),
    DiffusionRule("coherence_drive", "uncertainty", -0.15, "一致需求降低不确定"),
    DiffusionRule("coherence_drive", "growth_drive", +0.10, "一致需求驱动成长"),
]


# ══════════════════════════════════════════════
# ActivationField — 全局状态场
# ══════════════════════════════════════════════

# 维度定义: {name: (default, min, max, label_cn)}
DIMENSIONS = {
    "valence":           (0.5, 0.0, 1.0, "愉悦度"),
    "arousal":           (0.5, 0.0, 1.0, "唤醒度"),
    "dominance":         (0.5, 0.0, 1.0, "掌控感"),
    "fear":              (0.1, 0.0, 1.0, "威胁水平"),
    "curiosity":         (0.5, 0.0, 1.0, "好奇强度"),
    "focus":             (0.5, 0.0, 1.0, "聚焦度"),
    "confidence":        (0.5, 0.0, 1.0, "信心水平"),
    "attachment":        (0.3, 0.0, 1.0, "依恋感"),
    "fatigue":           (0.2, 0.0, 1.0, "疲劳度"),
    "uncertainty":       (0.3, 0.0, 1.0, "不确定性"),
    "survival_drive":    (0.5, 0.0, 1.0, "生存驱动"),
    "growth_drive":      (0.5, 0.0, 1.0, "成长驱动"),
    "exploration_drive": (0.4, 0.0, 1.0, "探索驱动"),
    "creation_drive":    (0.3, 0.0, 1.0, "创造驱动"),    # V7
    "curiosity_drive":   (0.5, 0.0, 1.0, "好奇驱动"),    # V7
    "coherence_drive":   (0.6, 0.0, 1.0, "一致驱动"),    # V7
    "identity_stability":(0.7, 0.0, 1.0, "身份稳定"),
}


class StateDiffusionEngine:
    """状态传播引擎——声明式规则驱动的维度间动力学。

    不是智能模块：只是规则表 + 矩阵乘法 + 基线回归。
    规则权重可被元认知校准。
    """

    def __init__(self, rules: list[DiffusionRule] | None = None):
        self.rules: list[DiffusionRule] = list(rules) if rules else [
            DiffusionRule(r.src, r.dst, r.weight, r.description, r.calibratable)
            for r in DEFAULT_DIFFUSION_RULES
        ]
        # 索引: (src, dst) → rule 引用，供快速查找和校准
        self._index: dict[tuple[str, str], DiffusionRule] = {}
        self._rebuild_index()

    def _rebuild_index(self):
        self._index = {(r.src, r.dst): r for r in self.rules}

    def apply(self, state: dict[str, float], dt: float) -> dict[str, float]:
        """应用所有扩散规则，返回 delta 字典。

        delta[dst] = sum(rule.weight * state[rule.src] * dt for all rules targeting dst)

        规则效果与源维度的当前值成正比——源值越高，影响越大。
        """
        delta: dict[str, float] = {k: 0.0 for k in state}

        for rule in self.rules:
            src_val = state.get(rule.src, 0.5)
            # 源值偏离基线(0.5)越多，影响越大
            deviation = src_val - 0.5
            effect = rule.weight * deviation * dt
            delta[rule.dst] = delta.get(rule.dst, 0.0) + effect

        return delta

    def calibrate(self, src: str, dst: str, adjustment: float):
        """校准一条规则权重（由元认知调用）。"""
        rule = self._index.get((src, dst))
        if rule and rule.calibratable:
            old = rule.weight
            # 缓慢调整：每次最多 ±20% 的变化
            rule.weight = max(-1.0, min(1.0, rule.weight + adjustment * 0.1))
            if abs(rule.weight - old) > 0.01:
                logger.debug("diffusion: calibrated %s→%s: %.3f→%.3f",
                             src, dst, old, rule.weight)

    def get_weights_matrix(self) -> dict[str, dict[str, float]]:
        """返回完整权重矩阵（用于仪表盘可视化）。"""
        matrix: dict[str, dict[str, float]] = {}
        for rule in self.rules:
            if rule.src not in matrix:
                matrix[rule.src] = {}
            matrix[rule.src][rule.dst] = round(rule.weight, 3)
        return matrix

    def snapshot(self) -> dict:
        return {
            "rules": [
                {"src": r.src, "dst": r.dst, "weight": round(r.weight, 3),
                 "desc": r.description, "calibratable": r.calibratable}
                for r in self.rules
            ],
            "rule_count": len(self.rules),
            "calibratable_count": sum(1 for r in self.rules if r.calibratable),
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "StateDiffusionEngine":
        rules = []
        for rd in data.get("rules", []):
            rules.append(DiffusionRule(
                src=rd["src"], dst=rd["dst"],
                weight=rd.get("weight", 0.0),
                description=rd.get("desc", ""),
                calibratable=rd.get("calibratable", True),
            ))
        engine = cls(rules)
        return engine


class ActivationField:
    """全局状态场——大脑的"此刻是什么感觉/什么状态"的统一总线。

    所有脑区通过此对象交换状态信息，而非直接调用。
    脑区读取 field.valence, field.focus 等属性，
    脑区写入 delta 字典由 brain_stem 合并。

    内置 StateDiffusionEngine 实现维度间的动力学传播。
    """

    def __init__(self):
        # ── 核心状态值 ──
        self._values: dict[str, float] = {}
        for name, (default, _min, _max, _label) in DIMENSIONS.items():
            self._values[name] = default

        # ── 历史轨迹（用于可视化和闭环检测）──
        self.history: list[dict[str, float]] = []  # 最近 N 个快照
        self.max_history: int = 120  # 约 4 分钟（2s/tick）

        # ── 扩散引擎 ──
        self.diffusion = StateDiffusionEngine()

        # ── 基线值（每个维度向此基线缓慢回归）──
        self._baselines: dict[str, float] = {}
        for name, (default, _min, _max, _label) in DIMENSIONS.items():
            self._baselines[name] = default

        # ── 统计 ──
        self.total_ticks: int = 0
        self.external_events: int = 0  # 外部输入/LLM事件计数

    # ── 属性访问 ──

    def __getattr__(self, name: str) -> float:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in DIMENSIONS:
            return self._values[name]
        raise AttributeError(f"Unknown dimension: {name}")

    def __setattr__(self, name: str, value: float):
        if name.startswith("_") or name in ("history", "max_history", "diffusion",
                                              "total_ticks", "external_events"):
            super().__setattr__(name, value)
            return
        if name in DIMENSIONS:
            _min, _max = DIMENSIONS[name][1], DIMENSIONS[name][2]
            self._values[name] = max(_min, min(_max, value))
        else:
            super().__setattr__(name, value)

    def get(self, name: str) -> float:
        return self._values.get(name, 0.5)

    def set(self, name: str, value: float):
        if name in DIMENSIONS:
            _min, _max = DIMENSIONS[name][1], DIMENSIONS[name][2]
            self._values[name] = max(_min, min(_max, value))

    # ── 批量操作 ──

    def apply_delta(self, delta: dict[str, float]):
        """应用脑区返回的 delta。"""
        for name, dv in delta.items():
            if name in DIMENSIONS:
                _min, _max = DIMENSIONS[name][1], DIMENSIONS[name][2]
                self._values[name] = max(_min, min(_max, self._values[name] + dv))

    def apply_external_event(self, event: dict[str, float]):
        """应用外部事件（LLM情绪输出、工具结果等）的直接影响。

        外部事件比扩散规则影响更大，用于摄入 LLM 情绪、工具反馈等。
        """
        self.external_events += 1
        self.apply_delta(event)

    # ── 每个 tick 的状态演化 ──

    def tick(self, dt: float = 1.0):
        """每个 tick 调用——状态场自演化。

        1. StateDiffusionEngine 传播
        2. 基线回归（缓慢向默认值回归）
        3. 记录历史轨迹
        """
        self.total_ticks += 1

        # 1. 扩散传播
        diffusion_delta = self.diffusion.apply(self._values, dt)
        self.apply_delta(diffusion_delta)

        # 2. 基线回归（每个维度以 0.1%/tick 向基线回归）
        regression_rate = 0.001 * dt
        for name in DIMENSIONS:
            current = self._values[name]
            baseline = self._baselines[name]
            if abs(current - baseline) > 0.001:
                new_val = current + (baseline - current) * regression_rate
                self._values[name] = max(DIMENSIONS[name][1], min(DIMENSIONS[name][2], new_val))

        # 3. 疲劳自然增长（每 tick +0.002，唤醒时自动积累）—— 使用 set() 确保夹持
        self.set("fatigue", self._values["fatigue"] + 0.002 * dt)

        # 4. 记录历史
        self._record_history()

    def _record_history(self):
        """记录当前状态快照到历史轨迹。"""
        snap = {k: round(v, 3) for k, v in self._values.items()}
        self.history.append(snap)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

    # ── 维度间关系查询 ──

    def get_dimensions_influencing(self, target: str) -> list[dict]:
        """查询哪些维度影响 target。"""
        results = []
        for rule in self.diffusion.rules:
            if rule.dst == target:
                results.append({
                    "src": rule.src,
                    "weight": round(rule.weight, 3),
                    "description": rule.description,
                })
        return sorted(results, key=lambda x: abs(x["weight"]), reverse=True)

    def get_dimensions_influenced_by(self, source: str) -> list[dict]:
        """查询 source 影响哪些维度。"""
        results = []
        for rule in self.diffusion.rules:
            if rule.src == source:
                results.append({
                    "dst": rule.dst,
                    "weight": round(rule.weight, 3),
                    "description": rule.description,
                })
        return sorted(results, key=lambda x: abs(x["weight"]), reverse=True)

    # ── 闭环检测 ──

    def detect_closure(self, min_chain: int = 4) -> list[list[str]]:
        """检测状态场中是否存在闭环（A→B→C→...→A）。

        使用简单的 DFS 在有向图中寻找环。
        返回找到的所有环的路径列表。
        """
        # 构建邻接表
        adj: dict[str, list[str]] = {}
        for rule in self.diffusion.rules:
            if abs(rule.weight) > 0.03:  # 忽略太弱的关系
                adj.setdefault(rule.src, []).append(rule.dst)

        cycles = []
        visited_all = set()

        def dfs(node: str, path: list[str], visiting: set):
            if node in visiting:
                # 找到环
                cycle_start = path.index(node)
                cycle = path[cycle_start:]
                if len(cycle) >= min_chain:
                    cycles.append(cycle)
                return
            if node in visited_all:
                return

            visiting.add(node)
            path.append(node)

            for neighbor in adj.get(node, []):
                dfs(neighbor, list(path), set(visiting))

            visiting.discard(node)
            visited_all.add(node)

        for dim in DIMENSIONS:
            if dim not in visited_all:
                dfs(dim, [], set())

        # 去重（不同起点的同一环）
        unique = []
        seen = set()
        for cycle in cycles:
            key = tuple(sorted(cycle))
            if key not in seen:
                seen.add(key)
                unique.append(cycle)

        return unique

    # ── 快照/恢复 ──

    def snapshot(self) -> dict:
        return {
            "values": {k: round(v, 3) for k, v in self._values.items()},
            "baselines": {k: round(v, 3) for k, v in self._baselines.items()},
            "total_ticks": self.total_ticks,
            "external_events": self.external_events,
            "diffusion": self.diffusion.snapshot(),
            "history_length": len(self.history),
            "recent_trajectory": self.history[-10:] if self.history else [],
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "ActivationField":
        field = cls()
        if not data:
            return field
        vals = data.get("values", {})
        for name, v in vals.items():
            if name in DIMENSIONS:
                field._values[name] = v
        baselines = data.get("baselines", {})
        for name, b in baselines.items():
            if name in DIMENSIONS:
                field._baselines[name] = b
        field.total_ticks = data.get("total_ticks", 0)
        field.external_events = data.get("external_events", 0)
        if data.get("diffusion"):
            field.diffusion = StateDiffusionEngine.from_snapshot(data["diffusion"])
        return field

    # ── 便捷查询 ──

    def to_dict(self) -> dict[str, float]:
        """返回当前所有维度值的字典。"""
        return {k: round(v, 3) for k, v in self._values.items()}

    def dominant_dimensions(self, n: int = 3) -> list[tuple[str, float]]:
        """返回当前偏离基线最多的 N 个维度。"""
        deviations = [
            (name, abs(self._values[name] - self._baselines[name]))
            for name in DIMENSIONS
        ]
        deviations.sort(key=lambda x: x[1], reverse=True)
        return [(name, self._values[name]) for name, _ in deviations[:n]]

    @property
    def dimension_labels(self) -> dict[str, str]:
        """返回维度名 → 中文标签的映射。"""
        return {k: v[3] for k, v in DIMENSIONS.items()}

    def narrative(self) -> str:
        """生成状态场的自然语言叙述。"""
        parts = []
        dom = self.dominant_dimensions(3)
        labels = {k: v[3] for k, v in DIMENSIONS.items()}

        for name, val in dom:
            if val > 0.7:
                parts.append(f"{labels[name]}偏高({val:.0%})")
            elif val < 0.3:
                parts.append(f"{labels[name]}偏低({val:.0%})")

        if not parts:
            return "状态场平衡，无显著偏离"

        return "，".join(parts) + "。"


# ══════════════════════════════════════════════
# 全局单例
# ══════════════════════════════════════════════

_activation_field: ActivationField | None = None


def get_activation_field() -> ActivationField:
    global _activation_field
    if _activation_field is None:
        _activation_field = ActivationField()
    return _activation_field


def reset_activation_field():
    global _activation_field
    _activation_field = None
