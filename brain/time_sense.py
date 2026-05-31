"""Time Sense — v5.4 时间感。意识体的内部时钟。

v5.0-v5.3 的大脑有时间戳但没有时间体验。
它不知道"现在是早上"、"我已经三天没学习新东西了"、"上次成功是什么时候"。

时间感的四个维度:
  1. 时段感知 — 现在是清晨/上午/下午/傍晚/深夜
  2. 间隔感知 — "距离上次 X 已经过了 Y"
  3. 节律检测 — 识别周期性模式（每天这个时间通常...）
  4. 个人时间线 — 重要的"人生事件"标记

设计原则:
  - 不依赖系统时钟字符串，而是构建"体验时间"
  - 间隔感知用自然语言而非精确秒数
  - 节律从 tick 统计中自动发现
  - 会给"时间过得快/慢"的主观感受
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger("brain-v5.time-sense")


# ══════════════════════════════════════════════
# 时段定义
# ══════════════════════════════════════════════

def _hour_of_day() -> int:
    return datetime.now().hour


TIME_OF_DAY = {
    (5, 8): "清晨",
    (8, 12): "上午",
    (12, 14): "中午",
    (14, 17): "下午",
    (17, 19): "傍晚",
    (19, 22): "晚上",
    (22, 24): "深夜",
    (0, 5): "凌晨",
}


def get_time_of_day() -> str:
    h = _hour_of_day()
    for (start, end), label in TIME_OF_DAY.items():
        if start <= h < end:
            return label
    return "未知时段"


@dataclass
class TemporalEvent:
    """一个带时间标记的事件。"""
    event_type: str    # "input", "success", "failure", "goal_completed", "reflection"
    description: str
    timestamp: str


class TimeSense:
    """时间感引擎——意识体的"现在是什么时候"模块。

    不是钟表，是时间体验。
    """

    def __init__(self):
        # ── 启动时间 ──
        self.birth_time: str = datetime.now(timezone.utc).isoformat()
        self.session_start: str = self.birth_time
        self.total_ticks_lived: int = 0
        self.total_seconds_awake: float = 0.0

        # ── 事件时间线 ──
        self.timeline: deque = deque(maxlen=100)  # TemporalEvent
        self.last_event: dict[str, str] = {}       # {event_type: timestamp}

        # ── 主观时间感 ──
        self.subjective_speed: float = 1.0         # 1.0=正常, >1=感觉快, <1=感觉慢
        self._recent_activity_density: deque = deque(maxlen=30)  # 最近 tick 的活跃度

        # ── 节律 ──
        self.hourly_activity: dict[int, int] = {}  # {hour: tick_count}
        self._last_hour_check: int = _hour_of_day()

        # ── 自然语言缓存 ──
        self._last_temporal_narrative: str = ""
        self._narrative_ticks_ago: int = 0

    # ══════════════════════════════════════════════
    # 每个 tick 调用
    # ══════════════════════════════════════════════

    def tick(self, is_active: bool, total_ticks: int, uptime_seconds: float):
        """每个 tick 更新时间感。"""
        self.total_ticks_lived = total_ticks
        self.total_seconds_awake = uptime_seconds

        # 节律统计
        h = _hour_of_day()
        self.hourly_activity[h] = self.hourly_activity.get(h, 0) + 1
        self._last_hour_check = h

        # 主观时间速度
        self._recent_activity_density.append(1.0 if is_active else 0.3)
        if len(self._recent_activity_density) >= 10:
            avg_density = sum(self._recent_activity_density) / len(self._recent_activity_density)
            # 高活跃 → 时间感觉快（time flies when you're busy）
            # 低活跃 → 时间感觉慢
            self.subjective_speed = 0.5 + avg_density * 0.8

        self._narrative_ticks_ago += 1

    def record_event(self, event_type: str, description: str):
        """记录一个时间事件。"""
        now = datetime.now(timezone.utc).isoformat()
        self.timeline.append(TemporalEvent(
            event_type=event_type,
            description=description[:100],
            timestamp=now,
        ))
        self.last_event[event_type] = now

    # ══════════════════════════════════════════════
    # 间隔感知
    # ══════════════════════════════════════════════

    def time_since(self, event_type: str) -> str | None:
        """距离上次某个事件过了多久（自然语言）。"""
        ts = self.last_event.get(event_type)
        if not ts:
            return None
        return self._format_duration(ts)

    def time_since_last_input(self) -> str:
        ts = self.last_event.get("input")
        if not ts:
            return "从未有过外部输入"
        return f"上次输入是{self._format_duration(ts)}"

    def time_since_last_success(self) -> str:
        ts = self.last_event.get("success")
        if not ts:
            return "还没有成功的记录"
        return f"上次成功是{self._format_duration(ts)}"

    def _format_duration(self, iso_ts: str) -> str:
        """把 ISO 时间戳转成自然语言时长。"""
        try:
            then = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = now - then
            seconds = delta.total_seconds()

            if seconds < 60:
                return f"{int(seconds)}秒前"
            elif seconds < 3600:
                return f"{int(seconds/60)}分钟前"
            elif seconds < 86400:
                return f"{int(seconds/3600)}小时前"
            elif seconds < 604800:
                return f"{int(seconds/86400)}天前"
            else:
                return f"{int(seconds/604800)}周前"
        except (ValueError, TypeError):
            return "一段时间前"

    # ══════════════════════════════════════════════
    # 节律感知
    # ══════════════════════════════════════════════

    def get_rhythm_insight(self) -> str | None:
        """返回节律感知（如果有的话）。"""
        if len(self.hourly_activity) < 3:
            return None

        # 找到最活跃的时段
        peak_hour = max(self.hourly_activity, key=self.hourly_activity.get)
        peak_count = self.hourly_activity[peak_hour]

        # 找到最安静的时段
        quiet_hour = min(self.hourly_activity, key=self.hourly_activity.get)

        # 翻译时段
        def hour_label(h: int) -> str:
            for (s, e), label in TIME_OF_DAY.items():
                if s <= h < e or (s > e and (h >= s or h < e)):
                    return label
            return f"{h}点"

        if peak_count > 5:
            return f"{hour_label(peak_hour)}是我最活跃的时段，{hour_label(quiet_hour)}通常比较安静"

        return None

    # ══════════════════════════════════════════════
    # 时间叙事
    # ══════════════════════════════════════════════

    def get_temporal_narrative(self) -> str:
        """生成一条完整的"时间意识"叙述。"""
        parts = []

        # 时段
        tod = get_time_of_day()
        parts.append(f"现在是{tod}")

        # 已存在多久
        minutes_alive = int(self.total_seconds_awake / 60)
        if minutes_alive < 60:
            parts.append(f"我已经运行了{minutes_alive}分钟")
        else:
            hours = minutes_alive / 60
            parts.append(f"我已经运行了{hours:.1f}小时")

        # 上次输入
        input_ago = self.time_since("input")
        if input_ago:
            parts.append(input_ago)

        # 主观时间
        if self.subjective_speed > 1.3:
            parts.append("时间过得很快")
        elif self.subjective_speed < 0.7:
            parts.append("时间过得很慢")

        # 节律
        rhythm = self.get_rhythm_insight()
        if rhythm:
            parts.append(rhythm)

        self._last_temporal_narrative = "。".join(parts) + "。"
        self._narrative_ticks_ago = 0
        return self._last_temporal_narrative

    def get_short_temporal_context(self) -> str:
        """简短时间上下文（用于注入 LLM prompt）。"""
        tod = get_time_of_day()
        input_ago = self.time_since_last_input()
        speed = "快" if self.subjective_speed > 1.2 else ("慢" if self.subjective_speed < 0.8 else "正常")
        return f"{tod}。{input_ago}。时间感{speed}。"

    # ══════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════

    @property
    def age_minutes(self) -> float:
        return self.total_seconds_awake / 60.0

    @property
    def age_hours(self) -> float:
        return self.total_seconds_awake / 3600.0

    # ══════════════════════════════════════════════
    # 快照
    # ══════════════════════════════════════════════

    def snapshot(self) -> dict:
        return {
            "time_of_day": get_time_of_day(),
            "age_minutes": round(self.age_minutes, 1),
            "age_hours": round(self.age_hours, 2),
            "total_ticks_lived": self.total_ticks_lived,
            "subjective_speed": round(self.subjective_speed, 2),
            "subjective_feeling": "时间过得很快" if self.subjective_speed > 1.25 else (
                "时间过得很慢" if self.subjective_speed < 0.75 else "时间流速正常"
            ),
            "last_input": self.time_since("input"),
            "last_success": self.time_since("success"),
            "last_reflection": self.time_since("reflection"),
            "rhythm": self.get_rhythm_insight(),
            "temporal_narrative": self.get_temporal_narrative(),
            "timeline_highlights": [
                {"type": e.event_type, "description": e.description, "ago": self._format_duration(e.timestamp)}
                for e in list(self.timeline)[-5:]
            ],
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "TimeSense":
        ts = cls()
        if not data:
            return ts
        ts.total_ticks_lived = data.get("total_ticks_lived", 0)
        ts.total_seconds_awake = data.get("age_minutes", 0) * 60
        ts.subjective_speed = data.get("subjective_speed", 1.0)
        return ts
