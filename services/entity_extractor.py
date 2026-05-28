"""Entity Extractor -- rule-based entity and relation extraction.

Populates entities and relations fields that auto_ingest currently leaves empty.
Pure regex engine, no LLM dependency -- any agent can call this via the ingest API.
"""
import re

# Technology entity patterns (keyword -> canonical name)
TECH_PATTERNS = {
    "python": r'\bpython\b',
    "fastapi": r'\bfastapi\b',
    "sqlite": r'\bsqlite\b',
    "mcp": r'\bmcp\b',
    "windows": r'\bwindows\b',
    "linux": r'\blinux\b',
    "stm32": r'\bstm32f?\d*\b',
    "docker": r'\bdocker\b',
    "git": r'\bgit\b',
    "node": r'\bnode(?:\.js)?\b',
    "typescript": r'\btypescript\b',
    "javascript": r'\bjavascript\b',
    "obsidian": r'\bobsidian\b',
    "claude": r'\bclaude\b',
    "codex": r'\bcodex\b',
    "whisper": r'\bwhisper\b',
    "pyaudio": r'\bpyaudio\b',
    "tkinter": r'\btkinter\b',
    "react": r'\breact\b',
    "nvic": r'\bnvic\b',
    "timer": r'\btim\d*\b',
    "rcc": r'\brcc\b',
    "pll": r'\bpll\b',
    "hsi": r'\bhsi\b',
    "gpio": r'\bgpio\b',
    "apb": r'\bapb\d?\b',
    "ahb": r'\bahb\b',
    "uart": r'\buart\b',
    "spi": r'\bspi\b',
    "i2c": r'\bi2c\b',
    "pytest": r'\bpytest\b',
    "uvicorn": r'\buvicorn\b',
    "venv": r'\bvenv\b',
    "bash": r'\.bashrc\b',
    "yaml": r'\.ya?ml\b',
    "toml": r'\.toml\b',
    "jsonl": r'\bjsonl\b',
    "cron": r'\bcron\b',
    "makefile": r'\bmakefile\b',
    "playwright": r'\bplaywright\b',
    "chromium": r'\bchromium\b',
    "tkinter": r'\btkinter\b',
    "subprocess": r'\bsubprocess\b',
    "sd卡": r'\bsd\s*卡\b',
    "wsl": r'\bwsl\b',
}

# File path pattern (Windows and Unix)
PATH_PATTERN = re.compile(
    r'(?:[A-Za-z]:[\\/][\w\\/\-\.]+|'
    r'~?/[\w/\-\.]+|'
    r'(?:/c|/d|/e)/[\w/\-\.]+)'
)

# Project name patterns (Chinese + English named projects)
PROJECT_PATTERNS = [
    (r'[《「]([^》」]+)[》」]', None),  # captured group in Chinese brackets
    (r'(?:Brain\s*Memory|brain[\s\-]?memory)', 'brain-memory'),
    (r'(?:Hermes\s*Agent|hermes[\s\-]?agent)', 'hermes-agent'),
    (r'(?:Claude\s*Code|claude[\s\-]?code)', 'claude-code'),
    (r'客服(?:聚合)?平台', 'customer-service-platform'),
    (r'Whisper\s*GUI', 'whisper-gui'),
    (r'UUMit', 'uumit'),
    (r'Obsidian\s*Vault', 'obsidian-vault'),
    (r'比翼鸟[协協]议', 'biyiniao-protocol'),
    (r'永劫无间', 'naraka-bladepoint'),
    (r'Brain\s*Memory', 'brain-memory'),
    (r'自动赚钱', 'auto-earn'),
]

# Relation extraction patterns
RELATION_PATTERNS = [
    (r'(?:因为|由于|because)\s*(.+?)(?:[，。,\.；;]|$)', 'causes'),
    (r'(?:导致|引起|造成|causes?|leads?\s*to)\s*(.+?)(?:[，。,\.；;]|$)', 'causes'),
    (r'(?:修复|解决|fix(?:ed)?|solved?|resolved?)\s*(.+?)(?:[，。,\.；;]|$)', 'fixes'),
    (r'(?:属于|是|part\s*of|belongs?\s*to)\s*(.+?)(?:[，。,\.；;]|$)', 'part_of'),
    (r'(?:依赖|需要|requires?|depends?\s*on|needs?)\s*(.+?)(?:[，。,\.；;]|$)', 'depends_on'),
    (r'(?:之前|before|prior\s*to)\s*(.+?)(?:[，。,\.；;]|$)', 'precedes'),
    (r'(?:之后|然后|after|then)\s*(.+?)(?:[，。,\.；;]|$)', 'follows'),
    (r'(?:发现|找到|发现|found|discovered)\s*(.+?)(?:[，。,\.；;]|$)', 'discovers'),
    (r'(?:配置|设置|config(?:ured?)?|set\s*up)\s*(.+?)(?:[，。,\.；;]|$)', 'configures'),
]

# Emotional keywords for tagging
EMOTION_TAGS = {
    'error': [r'错误', r'失败', r'bug', r'error', r'fail', r'崩溃', r'crash'],
    'resolved': [r'解决', r'修复', r'fixed', r'solved', r'搞定', r'终于'],
    'decision': [r'决定', r'选择', r'decide', r'choose', r'采用', r'方案'],
    'milestone': [r'完成', r'建成', r'上线', r'完工', r'里程碑', r'done', r'completed'],
    'breakthrough': [r'突破', r'发现', r'找到根因', r'breakthrough', r'关键发现'],
}


def extract_entities(text: str):
    """Extract entities and relations from text.
    
    Returns:
        (entities, relations)
        entities: list of str (canonical entity names)
        relations: list of dict with type, target, confidence
    """
    text_lower = text.lower()
    entities = []
    seen = set()

    # 1. Technology entities
    for tech, pattern in TECH_PATTERNS.items():
        if re.search(pattern, text_lower):
            if tech not in seen:
                entities.append(tech)
                seen.add(tech)

    # 2. Project names
    for pattern, proj in PROJECT_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            name = proj if proj else m.group(1)
            name = name.strip().lower().replace(' ', '-')
            if name and name not in seen and len(name) > 1:
                entities.append(name)
                seen.add(name)

    # 3. File paths (up to 5)
    paths = PATH_PATTERN.findall(text)
    for p in paths[:5]:
        # Extract just the filename
        short = p.replace('\\', '/').rstrip('/')
        filename = short.split('/')[-1] if '/' in short else short
        if filename and filename not in seen and len(filename) > 1:
            entities.append(filename)
            seen.add(filename)

    # 4. Relation extraction
    relations = []
    for pattern, rel_type in RELATION_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches[:3]:
            target = match.strip()[:60]
            if not target or len(target) < 2:
                continue
            rel_key = f"{rel_type}:{target[:30]}"
            if rel_key not in seen:
                relations.append({
                    "type": rel_type,
                    "target": target,
                    "confidence": 0.6,
                })
                seen.add(rel_key)

    return entities, relations


def extract_emotion_tags(text: str) -> list[str]:
    """Extract emotion-based tags from text."""
    text_lower = text.lower()
    tags = []
    for tag, patterns in EMOTION_TAGS.items():
        for p in patterns:
            if re.search(p, text_lower):
                tags.append(tag)
                break
    return tags
