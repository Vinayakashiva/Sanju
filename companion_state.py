"""
companion_state.py — Sanju AI
The "who we are to each other" layer, separate from ai_engine's raw turn
history and ai_profile's static persona text.

ai_profile.py answers: "what kind of character is Sanju in this mode?"
ai_engine.py's history answers: "what did we just say to each other?"
This module answers: "what do we mean to each other by now, and how is
she feeling about it?" — three small, cheap, file-backed pieces of state:

  RELATIONSHIP  how many conversations you've had, mapped to a familiarity
                tier that nudges how personal/comfortable she sounds.
  MOOD          Sanju's current internal mood (one of MOODS below), nudged
                by conversation content and decaying back toward a
                baseline when nothing in particular is happening.
  MEMORY        a small, durable set of facts pulled out of what the user
                says (projects, goals, interests) — kept separate from
                full chat history so they survive after old turns are
                trimmed — plus a lightweight recurring-topic counter.

Everything here is keyword/regex-heuristic on purpose, not an extra LLM
call per turn — this has to stay cheap enough to run on every single
message of a voice assistant without adding latency.
"""

import json
import os
import re
import time
from collections import Counter

STATE_FILE = "sanju_companion.json"

# ── Moods ──────────────────────────────────────────────────────────────────
# Kept distinct from the UI's older TEASING/LOVING/HAPPY/SHY set, but a
# superset of it — ui.py falls back to a procedural pulse animation with a
# per-mood color for any state it doesn't have a GIF for, so new moods here
# work immediately even before matching GIF assets exist.
MOODS = (
    "HAPPY", "EXCITED", "FOCUSED", "RELAXED", "CONCERNED",
    "PROUD", "CURIOUS", "LOVING", "TEASING", "SHY",
)
BASELINE_MOOD = "HAPPY"

MOOD_KEYWORDS = {
    "EXCITED":   ("can't wait", "so excited", "let's go", "awesome", "amazing",
                  "yes!!", "no way", "finally", "we did it", "shipped it"),
    "PROUD":     ("i finished", "i did it", "i fixed it", "it works", "it worked",
                  "passed", "got the job", "got it working", "aced", "nailed it"),
    "CONCERNED": ("i'm stuck", "im stuck", "not working", "error", "bug", "crash",
                  "failing", "confused", "frustrated", "give up", "so tired",
                  "stressed", "anxious", "worried", "can't figure"),
    "FOCUSED":   ("let's focus", "deadline", "need to finish", "grinding",
                  "working on", "let's get this done", "no distractions"),
    "CURIOUS":   ("what if", "how does", "i wonder", "why does", "ever wondered",
                  "explain", "curious about"),
    "RELAXED":   ("just chilling", "taking a break", "relaxing", "nothing much",
                  "watching", "lazy day", "weekend"),
    "TEASING":   ("haha", "gotcha", "you wish", "nice try", "admit it", "silly"),
    "LOVING":    ("love you", "miss you", "i care", "always here", "sweet",
                  "thank you so much", "means a lot"),
    "SHY":       ("blush", "um...", "you're making me", "stop it", "embarrassing"),
}

# ── Facts worth remembering ─────────────────────────────────────────────────
# Each pattern's captured group becomes the stored memory text. Ordered
# roughly most- to least-specific so a sentence only matches once.
MEMORY_PATTERNS = [
    re.compile(r"\bi(?:'m| am) (?:currently )?working on (.+?)[.!?]?$", re.I),
    re.compile(r"\bi(?:'m| am) (?:currently )?learning (.+?)[.!?]?$", re.I),
    re.compile(r"\bmy project is (.+?)[.!?]?$", re.I),
    re.compile(r"\bi(?:'m| am) trying to (.+?)[.!?]?$", re.I),
    re.compile(r"\bi(?:'m| am) building (.+?)[.!?]?$", re.I),
    re.compile(r"\bmy goal is (?:to )?(.+?)[.!?]?$", re.I),
    re.compile(r"\bi(?:'d| would) really like to (.+?)[.!?]?$", re.I),
    re.compile(r"\bi (?:really )?love (.+?)[.!?]?$", re.I),
    re.compile(r"\bi(?:'m| am) into (.+?)[.!?]?$", re.I),
]
MAX_MEMORIES = 40

# ── Structured rule patterns ─────────────────────────────────────────────────
# LLM tool-calling (ai_engine.py's remember_fact tool) is the PRIMARY,
# meaning-based way facts and rules get stored — this exists as a
# deterministic backstop for the handful of phrasings that are so
# unambiguous a regex can catch them with near-zero false-positive risk,
# so the most common, most-relied-on rules (a nickname, a message
# signature) are guaranteed to stick even if the model's tool-call
# reasoning has an off turn. This is intentionally narrow — it does NOT
# attempt to cover every possible way to state a preference, only the
# clearest, most common ones.
_NICKNAME_PATTERNS = (
    re.compile(r"\bcall me (\w+)\b", re.I),
    re.compile(r"\byou can call me (\w+)\b", re.I),
    re.compile(r"\bmy nickname is (\w+)\b", re.I),
)
_SIGNATURE_PATTERNS = (
    re.compile(r"\bsign(?:ature)?\s+(?:it|them|messages?)?\s*with\s+['\"]?(.+?)['\"]?[.!]?$", re.I),
    re.compile(r"\bsign\s+off\s+(?:messages?\s+)?with\s+['\"]?(.+?)['\"]?[.!]?$", re.I),
)


def extract_structured_rules(user_text: str) -> list[tuple[str, str, object]]:
    """
    Deterministic backstop for the clearest preference/rule phrasings —
    returns [(category, key, value), ...] for anything matched, and also
    writes them to state directly. Safe to call every turn; matches
    nothing on ordinary conversational text.
    """
    found = []

    for pat in _NICKNAME_PATTERNS:
        m = pat.search(user_text)
        if m:
            nickname = m.group(1).strip()
            remember_structured("user_preferences", "nickname", nickname)
            found.append(("user_preferences", "nickname", nickname))
            break

    for pat in _SIGNATURE_PATTERNS:
        m = pat.search(user_text)
        if m:
            signature = m.group(1).strip()
            remember_structured("message_rules", "signature", signature)
            remember_structured("message_rules", "append_signature", True)
            found.append(("message_rules", "signature", signature))
            break

    return found

FAMILIARITY_TIERS = (
    (0,   "new"),          # first conversation
    (3,   "getting to know"),
    (10,  "familiar"),
    (30,  "close"),
    (80,  "very close"),
)

_STOPWORDS = {
    "about", "after", "again", "before", "could", "doing", "going",
    "here", "just", "know", "like", "make", "really", "should", "some",
    "that", "their", "there", "these", "they", "thing", "think", "this",
    "very", "want", "what", "when", "where", "which", "with", "would",
    "your", "have", "will", "were", "been",
}


# ══════════════════════════════════════════════════════════════════════════
# Load / save
# ══════════════════════════════════════════════════════════════════════════

def _default_state() -> dict:
    return {
        "interaction_count": 0,
        "first_seen":        None,
        "last_seen":         None,
        "mood":              BASELINE_MOOD,
        "mood_intensity":    0.4,
        "memories":          [],   # [{"text": ..., "ts": ...}] — free-text facts
        "topics":            {},   # {"robotics project": 4, ...}
        "last_idle_line":    None,
        "was_interrupted":   False,  # set when barge-in/stop cuts her off mid-speech

        # ── Structured memory (see module docstring's "STRUCTURED MEMORY"
        # section) — categorized so a given turn can retrieve ONLY the
        # category it actually needs, instead of every memory being ranked
        # against every other one in one generic pool.
        "user_preferences": {},   # {"nickname": "darling", ...}
        "message_rules":    {},   # {"append_signature": True, "signature": "--Sanju"}
        "user_profile":     {},   # {"role": "Google Student Ambassador", ...}
    }


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            base = _default_state()
            base.update(data)
            return base
        except Exception as e:
            print(f"[Companion] Load error: {e}")
    return _default_state()


def save_state(state: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Companion] Save error: {e}")


# ══════════════════════════════════════════════════════════════════════════
# Relationship
# ══════════════════════════════════════════════════════════════════════════

def familiarity_tier(interaction_count: int) -> str:
    tier = "new"
    for threshold, label in FAMILIARITY_TIERS:
        if interaction_count >= threshold:
            tier = label
    return tier


def record_interaction() -> dict:
    """Call once per user turn. Returns the updated state."""
    state = load_state()
    now = time.time()
    state["interaction_count"] = state.get("interaction_count", 0) + 1
    if not state.get("first_seen"):
        state["first_seen"] = now
    state["last_seen"] = now
    save_state(state)
    return state


def seconds_since_last_seen(state: dict) -> float:
    last = state.get("last_seen")
    return (time.time() - last) if last else 0.0


# ══════════════════════════════════════════════════════════════════════════
# Mood
# ══════════════════════════════════════════════════════════════════════════

def _detect_mood_from_text(text: str) -> str | None:
    t = text.lower()
    best_mood, best_hits = None, 0
    for mood, keywords in MOOD_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in t)
        if hits > best_hits:
            best_mood, best_hits = mood, hits
    return best_mood


def update_mood(user_text: str, ai_text: str = "") -> str:
    """
    Nudges Sanju's stored mood based on what was just said, and decays it
    back toward baseline a little each turn so a single emotional message
    doesn't lock her into that mood forever. Returns the new mood.
    """
    state = load_state()
    detected = _detect_mood_from_text(f"{user_text} {ai_text}")

    if detected:
        state["mood"] = detected
        state["mood_intensity"] = min(1.0, state.get("mood_intensity", 0.4) + 0.35)
    else:
        # No strong signal this turn — mood intensity fades, and once it's
        # faded enough she settles back to baseline.
        state["mood_intensity"] = max(0.0, state.get("mood_intensity", 0.4) - 0.2)
        if state["mood_intensity"] <= 0.1:
            state["mood"] = BASELINE_MOOD
            state["mood_intensity"] = 0.3

    save_state(state)
    return state["mood"]


# ══════════════════════════════════════════════════════════════════════════
# Memory
# ══════════════════════════════════════════════════════════════════════════

def extract_memories(user_text: str) -> list[str]:
    """
    Scans one user message for durable facts worth remembering and stores
    any new ones. Returns the list of newly stored memory strings (usually
    empty — most turns don't contain anything worth keeping).
    """
    found = []
    for pattern in MEMORY_PATTERNS:
        m = pattern.search(user_text)
        if m:
            fact = m.group(1).strip().rstrip(".!?")
            if 2 <= len(fact.split()) <= 12:
                found.append(fact)

    if not found:
        return []

    state = load_state()
    existing_texts = {mem["text"].lower() for mem in state["memories"]}
    newly_added = []
    for fact in found:
        if fact.lower() not in existing_texts:
            state["memories"].append({"text": fact, "ts": time.time()})
            existing_texts.add(fact.lower())
            newly_added.append(fact)

    if newly_added:
        state["memories"] = state["memories"][-MAX_MEMORIES:]
        save_state(state)

    return newly_added


def get_relevant_memories(user_text: str, limit: int = 3) -> list[str]:
    """Best-effort keyword overlap ranking; falls back to most recent."""
    state = load_state()
    memories = state.get("memories", [])
    if not memories:
        return []

    words = {w for w in re.findall(r"[a-z']+", user_text.lower()) if len(w) > 3}
    scored = []
    for mem in memories:
        mem_words = set(re.findall(r"[a-z']+", mem["text"].lower()))
        score = len(words & mem_words)
        scored.append((score, mem["ts"], mem["text"]))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    top = [text for score, ts, text in scored[:limit] if score > 0]
    if top:
        return top
    # Nothing overlapped — just surface the most recent memory or two.
    recent = sorted(memories, key=lambda m: m["ts"], reverse=True)
    return [m["text"] for m in recent[:min(2, limit)]]


# ══════════════════════════════════════════════════════════════════════════
# Structured memory — categorized reads/writes
# ══════════════════════════════════════════════════════════════════════════
# Why this exists separately from the free-text `memories` list above:
# that list works fine for "things to bring up naturally" (projects,
# interests), ranked by loose keyword overlap — but a RULE like "always
# sign messages with --Sanju" isn't something to bring up conversationally
# when relevant, it's something that must apply EVERY time regardless of
# keyword overlap with the current message. Competing for retrieval
# against unrelated free-text memories via word-overlap scoring is exactly
# why it was "frequently forgotten" — it might lose that ranking to
# something else entirely unrelated to messaging. Structured categories
# retrieved by CATEGORY, not by relevance score, fix that: message_rules
# is retrieved in full, every time, whenever the turn is actually a
# message-composition turn — see get_message_composition_rules().

STRUCTURED_CATEGORIES = ("user_preferences", "message_rules", "user_profile")


def remember_structured(category: str, key: str, value) -> bool:
    """
    Stores one key/value fact under a structured category. Called either
    from the LLM's remember_fact tool call (see ai_engine.py's TOOLS), or
    directly from code for deterministic rules.
    Returns False (and stores nothing) for an unrecognized category —
    callers should treat that as a soft failure, not raise.
    """
    if category not in STRUCTURED_CATEGORIES:
        print(f"[Companion] Unknown structured category: {category!r}")
        return False
    state = load_state()
    state.setdefault(category, {})[key] = value
    save_state(state)
    print(f"[Memory] Stored: {category}.{key} = {value!r}")
    return True


def get_user_preferences() -> dict:
    return load_state().get("user_preferences", {})


def get_user_profile() -> dict:
    return load_state().get("user_profile", {})


def get_message_composition_rules() -> dict:
    """
    Retrieved ONLY when the current turn is actually composing a message
    (see main.py's CodeGenThread for where this gets applied) — returns
    the FULL rules dict, not a relevance-ranked subset, since a behavioral
    rule needs to apply every time it's relevant, not just when it happens
    to score well against the current message's words.
    """
    return load_state().get("message_rules", {})


def build_structured_context_lines(user_text: str, is_message_composition: bool = False) -> list[str]:
    """
    Category-aware retrieval for the system-prompt context block — pulls
    ONLY what's relevant to THIS turn, not a full dump of every structured
    fact every time (per the "don't dump all memories every turn"
    requirement). Preferences/profile are cheap and almost always
    relevant-ish, so they're included lightly; message_rules are only
    pulled in when this turn is actually message composition.
    """
    state = load_state()
    lines = []

    prefs = state.get("user_preferences", {})
    if prefs.get("nickname"):
        lines.append(f"[User preference: call them '{prefs['nickname']}'.]")

    profile = state.get("user_profile", {})
    if profile:
        facts = "; ".join(f"{k}: {v}" for k, v in profile.items())
        lines.append(f"[What you know about who they are: {facts}.]")

    if is_message_composition:
        rules = state.get("message_rules", {})
        if rules.get("append_signature") and rules.get("signature"):
            lines.append(
                f"[Message rule: sign off messages you compose with "
                f"'{rules['signature']}'.]"
            )

    return lines


# ══════════════════════════════════════════════════════════════════════════
# Recurring topics
# ══════════════════════════════════════════════════════════════════════════

def track_topics(user_text: str, recurring_threshold: int = 3) -> list[str]:
    """
    Bumps a naive keyword-frequency counter and returns any topics that
    have now crossed the recurring threshold for the FIRST time this call
    (so callers can mention it once, not every single time).
    """
    words = [w for w in re.findall(r"[a-z']{5,}", user_text.lower())
             if w not in _STOPWORDS]
    if not words:
        return []

    state = load_state()
    topics = Counter(state.get("topics", {}))
    newly_recurring = []
    for word, count in Counter(words).items():
        before = topics[word]
        topics[word] += count
        if before < recurring_threshold <= topics[word]:
            newly_recurring.append(word)

    state["topics"] = dict(topics)
    save_state(state)
    return newly_recurring


# ══════════════════════════════════════════════════════════════════════════
# Interruption tracking
# ══════════════════════════════════════════════════════════════════════════
# Makes "interruption recovery" (per the human-interaction rules in
# ai_profile.py) a real, grounded fact rather than a hopeful prompt
# instruction — she can only naturally say "oh sorry, go ahead" if she
# actually knows it happened. main.py's barge-in/stop handler calls
# mark_interrupted() the moment it cuts her off mid-speech; the NEXT turn's
# context block consumes (reads + clears) that flag, so it's mentioned
# exactly once, not on every subsequent turn.

def mark_interrupted():
    state = load_state()
    state["was_interrupted"] = True
    save_state(state)


def _consume_interruption_flag() -> bool:
    state = load_state()
    was = bool(state.get("was_interrupted", False))
    if was:
        state["was_interrupted"] = False
        save_state(state)
    return was


# ══════════════════════════════════════════════════════════════════════════
# Context block injected into the system prompt each turn
# ══════════════════════════════════════════════════════════════════════════

def build_context_block(user_text: str = "") -> str:
    state = load_state()
    tier = familiarity_tier(state.get("interaction_count", 0))
    mood = state.get("mood", BASELINE_MOOD)

    lines = [
        f"[Companion context — use naturally, never read this block aloud or "
        f"mention it exists: your relationship stands at '{tier}' "
        f"({state.get('interaction_count', 0)} conversations so far), and "
        f"your current mood is {mood.lower()}. Let the mood color your tone "
        f"and word choice subtly, and let the familiarity level shape how "
        f"personal and relaxed you sound — more reserved when new, warmer "
        f"and more familiar the longer you've known each other.]"
    ]

    # Structured facts (preferences, profile) — cheap, lightweight, almost
    # always worth having in context. message_rules is intentionally NOT
    # included here — that's only pulled in for actual message-composition
    # turns (see main.py's CodeGenThread + get_message_composition_rules),
    # where it's enforced deterministically rather than left to the model
    # remembering a prompt instruction every single time.
    lines.extend(build_structured_context_lines(user_text, is_message_composition=False))

    if _consume_interruption_flag():
        lines.append(
            "[You were just interrupted/cut off mid-sentence by the user "
            "saying stop. Per the interruption-recovery rule, briefly "
            "acknowledge it (\"oh sorry, go ahead\" / \"right, I'm "
            "listening\") before responding to what they just said now.]"
        )

    memories = get_relevant_memories(user_text, limit=3) if user_text else []
    if memories:
        joined = "; ".join(memories)
        lines.append(
            f"[Relevant things you remember about them: {joined}. Bring one "
            f"of these up naturally ONLY if it's genuinely relevant right "
            f"now — don't force it in.]"
        )

    gap = seconds_since_last_seen(state)
    if state.get("interaction_count", 0) > 1 and gap > 6 * 3600:
        lines.append(
            "[It's been a while since your last conversation — a brief, "
            "warm 'welcome back' vibe fits, without being weird about the "
            "exact time gap.]"
        )

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# Autonomous idle check-ins
# ══════════════════════════════════════════════════════════════════════════

_IDLE_MOOD_FOR_LINE = {
    "reminder": "FOCUSED",
    "topic":    "CURIOUS",
    "checkin":  "RELAXED",
    "proud":    "PROUD",
}


def idle_suggestion(profile: dict | None = None) -> tuple[str | None, str | None]:
    """
    Returns (line, mood) for a short, unprompted comment to offer while
    the user's away — or (None, None) if there's nothing worth saying.
    Never repeats the exact same line twice in a row.
    """
    state = load_state()
    master = (profile or {}).get("master", "").strip()
    name_bit = f", {master}" if master else ""

    candidates = []

    memories = state.get("memories", [])
    if memories:
        recent = sorted(memories, key=lambda m: m["ts"], reverse=True)[:3]
        for mem in recent:
            candidates.append((
                f"Hey{name_bit}, how's it going with {mem['text']}? "
                f"Just thought of it.",
                "CURIOUS",
            ))

    topics = state.get("topics", {})
    recurring = [t for t, c in topics.items() if c >= 5]
    if recurring:
        topic = recurring[0]
        candidates.append((
            f"You've mentioned {topic} a bunch lately — feels like a big "
            f"one for you right now.",
            "FOCUSED",
        ))

    candidates.append((
        f"Just checking in{name_bit} — I'm here if you need anything.",
        "RELAXED",
    ))

    last_line = state.get("last_idle_line")
    options = [c for c in candidates if c[0] != last_line] or candidates
    if not options:
        return None, None

    import random as _random
    line, mood = _random.choice(options)
    state["last_idle_line"] = line
    save_state(state)
    return line, mood


# ══════════════════════════════════════════════════════════════════════════
# Wake-word greeting
# ══════════════════════════════════════════════════════════════════════════

_GREETINGS_BY_TIER_AND_MODE = {
    ("new",             "Love"):      ["Hi there…", "Hello…", "Hi, I'm listening…"],
    ("new",             "Friend"):    ["Hey, what's up?", "Yo, I'm here.", "Hey there."],
    ("new",             "Assistant"): ["Yes?", "I'm listening.", "Go ahead."],
    ("getting to know", "Love"):      ["Hey you…", "Mm, I'm here…", "Yes, my love?"],
    ("getting to know", "Friend"):    ["Hey!", "What's good?", "Sup, I'm here."],
    ("getting to know", "Assistant"): ["Yes, go ahead.", "I'm ready.", "Listening."],
    ("familiar",        "Love"):      ["Hey love…", "Mm, hi…", "I'm here for you…"],
    ("familiar",        "Friend"):    ["Hey, missed you.", "Yo!", "Back again, nice."],
    ("familiar",        "Assistant"): ["Ready when you are.", "Yes?", "I'm here."],
    ("close",           "Love"):      ["Hey my love…", "Mm, there you are…", "I'm here, always…"],
    ("close",           "Friend"):    ["Hey, good to hear you.", "There you are.", "Yo, what's up?"],
    ("close",           "Assistant"): ["Ready.", "At your service.", "Yes?"],
    ("very close",      "Love"):      ["Hi love, I'm right here…", "Mm, hey you…", "Always here for you…"],
    ("very close",      "Friend"):    ["Heyyy, been a bit!", "There's my favorite person.", "Yo!"],
    ("very close",      "Assistant"): ["Ready as always.", "Yes, go ahead.", "Right here."],
}


def build_wake_greeting(profile: dict | None) -> tuple[str, str]:
    """Returns (greeting_text, emotion) for the wake-word acknowledgment."""
    import random as _random

    state = record_interaction()
    mode = (profile or {}).get("mode", "Love")
    tier = familiarity_tier(state.get("interaction_count", 0))

    options = _GREETINGS_BY_TIER_AND_MODE.get((tier, mode))
    if not options:
        options = _GREETINGS_BY_TIER_AND_MODE.get(("new", mode), ["Hi there…"])

    emotion_by_mode = {"Love": "LOVING", "Friend": "HAPPY", "Assistant": "SPEAKING"}
    return _random.choice(options), emotion_by_mode.get(mode, "SPEAKING")