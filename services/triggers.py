"""Event-Driven Triggers -- determine when to auto-ingest and auto-retrieve.

Three trigger types:
1. Session Start -- should_inject >= 0.55, auto-load context
2. Topic Shift -- new entities detected, re-retrieve
3. Correction Capture -- user says "wrong/no/don't", high-emotion signal
"""
import re
from services.entity_extractor import extract_entities

TOPIC_SHIFT_INDICATORS = [
    "next", "switch", "back to", "change topic",
]

CORRECTION_PATTERNS = [
    r"(?:not right|wrong|incorrect|shouldn't|don't)",
    r"(?:no,|nope|not that)",
]


def detect_topic_shift(text: str, prev_entities: set) -> bool:
    """Detect if conversation topic has shifted.
    
    Condition 1: Contains topic-shift keywords.
    Condition 2: New entities have < 30% overlap with previous entities.
    """
    has_marker = any(m in text.lower() for m in TOPIC_SHIFT_INDICATORS)
    new_entities = set(extract_entities(text)[0])
    
    if not prev_entities:
        return has_marker
    
    if not new_entities:
        return has_marker
    
    overlap = len(new_entities & prev_entities) / max(len(new_entities | prev_entities), 1)
    return has_marker or overlap < 0.3


def detect_correction(text: str):
    """Detect user correction signal. Returns (is_correction, snippet)."""
    for pattern in CORRECTION_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            snippet = text[m.end():m.end()+50].strip()
            return True, snippet or text[:50]
    return False, ""


def should_auto_ingest(text: str) -> bool:
    """Determine if text is worth auto-ingesting.
    
    Conditions:
    - Length > 30 chars
    - Contains at least one entity or relation
    - Not a simple greeting
    """
    if len(text) < 30:
        return False
    
    greetings = {"hello", "hi", "hey", "good morning", "good evening"}
    if text.strip().lower() in greetings:
        return False
    
    entities, relations = extract_entities(text)
    return len(entities) >= 1 or len(relations) >= 1


def detect_trigger_reason(query: str, prev_entities: set = None) -> str:
    """Detect why context is being loaded.
    
    Returns: 'session_start', 'topic_shift', 'correction', or 'manual'
    """
    query_lower = query.lower()
    
    # Session start markers
    session_markers = ["new session", "new conversation", "getting started", "begin"]
    if any(m in query_lower for m in session_markers):
        return "session_start"
    
    # Correction detection
    is_corr, _ = detect_correction(query)
    if is_corr:
        return "correction"
    
    # Topic shift detection
    if prev_entities and detect_topic_shift(query, prev_entities):
        return "topic_shift"
    
    return "manual"
