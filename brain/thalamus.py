"""Thalamus — 丘脑。感知输入 + 注意力过滤（规则引擎）。

职责:
  1. 接收外部 agent 的调用（文本消息）
  2. 合并内部信号（默认模式的自发思考）
  3. 路由到杏仁核做情绪标记
"""

import re
import logging

logger = logging.getLogger("brain-v5.thalamus")


class Thalamus:
    """Sensory relay and attention filter."""

    # 纯噪音模式 — 直接丢弃
    NOISE_PATTERNS = [
        r"^[。，、！？\s]*$",      # 纯标点空白
        r"^[a-zA-Z]{1,2}$",          # 单字母回复
        r"^(嗯|哦|好|行|对)+$",       # 单字应答
        r"^ok+$",
    ]

    # 高优先级模式 — 不经过滤直接路由
    HIGH_PRIORITY_PATTERNS = [
        r"(bug|error|crash|崩溃|报错|异常|失败)",
        r"(fixed|solved|修复|解决|搞定|突破)",
        r"(记得|记住|别忘了|重要|关键|必须)",
        r"\?|？",                      # 问句直接路由
        r"!|！{2,}",                  # 多感叹号
    ]

    def __init__(self):
        self.last_input: str = ""
        self.noise_discarded: int = 0
        self.total_relayed: int = 0

    def relay(self, input_text: str | None, inner_signal: str | None = None) -> dict:
        """Relay input through attention filter.

        Args:
            input_text: external agent message (None if no external input)
            inner_signal: internal signal from default mode (None if no internal)

        Returns:
            {
                "has_input": bool,
                "text": str,           # 过滤后的归一化文本
                "source": "external"|"internal",
                "priority": "high"|"normal"|"low",
                "discarded": bool,
            }
        """
        # Merge external + internal signals
        if input_text and inner_signal:
            text = "{0}\n[internal: {1}]".format(input_text, inner_signal)
            source = "external"
        elif input_text:
            text = input_text
            source = "external"
        elif inner_signal:
            text = inner_signal
            source = "internal"
        else:
            return {
                "has_input": False, "text": "", "source": "none",
                "priority": "low", "discarded": True,
            }

        self.last_input = text

        # Noise filter
        for pattern in self.NOISE_PATTERNS:
            if re.match(pattern, text.strip()):
                self.noise_discarded += 1
                logger.debug("thalamus: noise discarded: %s", text[:40])
                return {
                    "has_input": False, "text": "", "source": source,
                    "priority": "low", "discarded": True,
                }

        # Priority detection
        priority = "normal"
        for pattern in self.HIGH_PRIORITY_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                priority = "high"
                break

        self.total_relayed += 1
        return {
            "has_input": True,
            "text": text[:4000],
            "source": source,
            "priority": priority,
            "discarded": False,
        }
