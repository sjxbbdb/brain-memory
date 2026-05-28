"""记忆覆写权值 — 五维量化计算引擎。

overwrite_weight = importance×0.25 + verifiability×0.20
                 + temporal×0.20 + specificity×0.20 + access×0.15

决定新记忆是该覆写旧记忆、合并、还是独立存储。
"""
import math
import re
from datetime import datetime, timezone
from config import OVERWRITE_WEIGHTS


def compute_overwrite_weight(
    importance: float,
    corroboration_count: int,
    created: str,
    access_count: int,
    content: str,
    weights: dict | None = None,
) -> float:
    """计算五维覆写权值。

    Args:
        importance: 记忆重要性 [0,1]
        corroboration_count: 被交叉印证次数
        created: ISO8601 创建时间
        access_count: 检索命中次数
        content: 记忆文本（用于特异度计算）
        weights: 自定义权重（默认 OVERWRITE_WEIGHTS）

    Returns:
        overwrite_weight [0,1]
    """
    w = weights or OVERWRITE_WEIGHTS

    verifiability = min(1.0, corroboration_count / 10)
    temporal = _compute_temporal(created)
    specificity = _estimate_specificity(content)
    access = min(1.0, access_count / 20)

    score = (
        importance * w["importance"]
        + verifiability * w["verifiability"]
        + temporal * w["temporal"]
        + specificity * w["specificity"]
        + access * w["access"]
    )
    return min(1.0, max(0.0, round(score, 4)))


def _compute_temporal(created: str) -> float:
    """时间新鲜度：exp(-days_since_created / 30)。"""
    try:
        dt = datetime.fromisoformat(created)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        return round(math.exp(-days / 30.0), 4)
    except (ValueError, TypeError):
        return 0.1


def _estimate_specificity(text: str) -> float:
    """估算文本特异度：路径/数字/命令/版本号等精确信息密度。

    高特异度（0.7+）：含具体路径、端口、版本号、命令
    低特异度（<0.3）：纯描述性文字、模糊印象
    """
    if not text:
        return 0.15

    high_spec = [
        r"[A-Za-z]:[\\/][\w\\/\-\.]+",        # Windows 绝对路径
        r"~?/[\w/\-\.]+/\w+",                  # Unix 路径（至少两层）
        r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",  # IP 地址
        r"\b(port|端口)\s*:?\s*\d{2,5}\b",     # 端口号
        r"\b(v|version)\s*\d+\.\d+(\.\d+)?\b", # 版本号
        r"\b(sk-|api[_\s]?key|token)\b",       # API Key / Token
        r"\b(\d{4}-\d{2}-\d{2})\b",            # 日期
        r"\b(pip\s+install|npm\s+(i|install)|git\s+(clone|pull|push))\b",  # 命令
    ]
    medium_spec = [
        r"\b(exe|\.py|\.md|\.yaml|\.json|\.toml|\.js|\.ts|\.html)\b",  # 扩展名
        r"\b\d+\s*(条|个|次|秒|分钟|小时|天)\b",  # 量词+数字
        r"(https?://|github\.com)",             # URL
        r"\b(bool|int|str|float|list|dict)\b",  # 类型名
    ]

    high_hits = sum(1 for p in high_spec if re.search(p, text, re.IGNORECASE))
    medium_hits = sum(1 for p in medium_spec if re.search(p, text, re.IGNORECASE))

    score = min(1.0, 0.15 + high_hits * 0.15 + medium_hits * 0.08)
    return score


def decide_overwrite(
    new_weight: float,
    existing_weight: float,
    new_specificity: float,
    existing_specificity: float,
    has_contradiction: bool = False,
) -> str:
    """覆写决策矩阵：根据权值差和特异度决定覆写行为。

    Returns:
        "full_overwrite"  — 全量覆写，旧版进 versions
        "weighted_merge"  — 加权合并
        "append_only"     — 追加补充，不覆写核心内容
        "conflict"        — 检测到矛盾，双方降置信度，标记仲裁
    """
    if has_contradiction:
        return "conflict"

    weight_diff = new_weight - existing_weight

    if new_specificity >= 0.6 and existing_specificity < 0.5:
        return "full_overwrite"
    if new_specificity < 0.4 and existing_specificity >= 0.6:
        return "append_only"
    if weight_diff > 0.3:
        return "full_overwrite"
    if weight_diff < -0.3:
        return "append_only"
    return "weighted_merge"


def merge_content(
    existing_content: str,
    new_content: str,
    existing_weight: float,
    new_weight: float,
    existing_title: str,
    new_title: str,
) -> tuple[str, str]:
    """加权合并两个记忆的内容和标题。

    alpha = new_weight / (new_weight + existing_weight)
    merged = existing * (1-alpha) + new * alpha

    高特异度信息优先保留：路径、数字、命令不受 alpha 削减。
    """
    alpha = new_weight / (new_weight + existing_weight) if (new_weight + existing_weight) > 0 else 0.5

    # 标题：直接用高权值者的标题
    merged_title = new_title if alpha >= 0.5 else existing_title

    # 内容：简单拼接 + 权值标记
    merged_content = _merge_texts(existing_content, new_content, alpha)

    return merged_title, merged_content


def _merge_texts(old: str, new: str, alpha: float) -> str:
    """合并两段文本，保留高特异度片段优先。

    策略：
    - alpha >= 0.75: 新内容主导，旧内容提取特异度片段追加
    - 0.4 < alpha < 0.75: 新旧拼接，新在前
    - alpha <= 0.4: 旧内容主导，新内容提取特异度片段追加
    """
    old_specifics = _extract_specifics(old)
    new_specifics = _extract_specifics(new)

    if alpha >= 0.75:
        base = new.strip()
        extra = [s for s in old_specifics if s not in new]
        if extra:
            base += "\n[来自旧记忆的精确信息: " + "; ".join(extra) + "]"
        return base
    elif alpha >= 0.4:
        return new.strip() + "\n--\n[旧版本]\n" + old.strip()
    else:
        base = old.strip()
        extra = [s for s in new_specifics if s not in old]
        if extra:
            base += "\n[更新: " + "; ".join(extra) + "]"
        return base


def _extract_specifics(text: str) -> list[str]:
    """从文本中提取高特异度片段（路径、数字、命令等）。"""
    patterns = [
        r"[A-Za-z]:[\\/][\w\\/\-\.]+",
        r"~?/[\w/\-\.]+/\w+",
        r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?",
        r"\b(port|端口)\s*:?\s*\d{2,5}\b",
        r"\bv?\d+\.\d+(\.\d+)?\b",
        r"\b(sk-\w+|api[_\s]?key\s*\w+)\b",
        r"\b(pip\s+install\s+\S+|npm\s+i\s+\S+|git\s+(clone|pull|push)\s+\S+)\b",
        r"https?://[^\s]+",
    ]
    found = set()
    for p in patterns:
        for m in re.findall(p, text, re.IGNORECASE):
            found.add(m[:80])
    return list(found)[:10]
