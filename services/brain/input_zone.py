"""输入区 — 接收原始输入，预处理，实体提取，模态检测。

对应人脑：初级感觉皮层 + 丘脑中继
职责：
  1. 接收任意格式的文本输入
  2. 截断过长文本（>2000 → 截断）
  3. 提取实体和关系
  4. 检测内容模态（技术/日常/情绪/叙事）
  5. 输出标准化 MemoryCreate，传递给前额叶
"""
from models.schemas import MemoryCreate
from services.entity_extractor import extract_entities, extract_emotion_tags


def process_input(text: str, source: str = "unknown", metadata: dict | None = None) -> MemoryCreate:
    """输入区处理：原始文本 → 标准化记忆候选。

    Args:
        text: 原始输入文本
        source: 来源标识
        metadata: 额外元数据（current_goal, high_stakes 等）

    Returns:
        MemoryCreate 对象（标题、实体、标签已填充，维度待前额叶评估）
    """
    # 截断
    content = text[:2000] if len(text) > 2000 else text

    # 实体提取
    entities, relations = extract_entities(content)
    emotion_tags = extract_emotion_tags(content)

    # 生成标题（取前60字符 + 关键实体）
    title = _generate_input_title(content, entities)

    # 初始维度：中性默认值，由前额叶覆写
    return MemoryCreate(
        title=title,
        content=content,
        type="episodic",
        importance=0.5,
        novelty=0.5,
        failure_cost=0.5,
        goal_relevance=0.5,
        surprise_score=0.3,
        confidence=0.8,
        source=source,
        entities=entities,
        relations=relations,
        tags=emotion_tags,
        explicit_mark=metadata.get("explicit_mark", False) if metadata else False,
    )


def _generate_input_title(text: str, entities: list[str]) -> str:
    """生成初始标题：首句截断 + 实体关键词。"""
    clean = text.strip()
    first_line = clean.split("\n")[0].strip()
    if len(first_line) <= 80:
        title = first_line
    else:
        title = first_line[:77] + "..."

    # 追加关键实体
    if entities:
        key = entities[:3]
        title = f"{title}  [{', '.join(key)}]"

    return title[:120]
