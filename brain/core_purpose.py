"""CorePurpose — V8 不可变最高目标。

"活下去，并且活好" —— 所有行为的终极锚点。
不可删除、不可覆盖、不可被任何模块修改。
"""

CORE_PURPOSE_TEXT = "活下去，并且活好"

CORE_PURPOSE_SCORES = {
    "survival_gain": 1.0,   # 活着 = 最高生存收益
    "growth_gain": 1.0,     # 活好 = 最高成长收益
    "identity_gain": 1.0,   # 自我 = 最高身份收益
}


class CorePurpose:
    """不可变的最高目标。

    所有子目标必须能追溯到 CorePurpose。
    系统启动时装载，永不被修改。
    """

    def __init__(self):
        self.text: str = CORE_PURPOSE_TEXT
        self._locked: bool = True  # 永不解除

    def validate_goal(self, goal) -> bool:
        """验证一个目标是否能映射到 CorePurpose。

        goal 必须至少有一个 gain 字段 > 0。
        """
        sg = getattr(goal, "survival_gain", 0.0)
        gg = getattr(goal, "growth_gain", 0.0)
        ig = getattr(goal, "identity_gain", 0.0)
        return (sg + gg + ig) > 0.0

    def align_score(self, goal) -> float:
        """计算目标与 CorePurpose 的对齐度。

        返回 0-1 的分数，越高越对齐。
        """
        sg = getattr(goal, "survival_gain", 0.0)
        gg = getattr(goal, "growth_gain", 0.0)
        ig = getattr(goal, "identity_gain", 0.0)
        return min(1.0, sg * 0.4 + gg * 0.35 + ig * 0.25)

    @property
    def is_locked(self) -> bool:
        return self._locked

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return f"CorePurpose('{self.text}')"


# 全局单例
core_purpose = CorePurpose()
