"""
ai_engine.py — Sanju AI
Now runs on a LOCAL model via Ollama instead of the Gemini API — no API key,
no internet dependency for the LLM itself. Persistent memory + streaming
words + conversation mode detection are unchanged.
Emits `keep_listening` signal so main.py knows to stay in conversation mode.

── TOOL SELECTION (replaces keyword-triggered actions) ────────────────────────
Whether to search the web or store a structured memory used to be decided
by regex in app_controller.py — "if 'google' in text: search_web()" in
spirit, which is exactly what caused "I am a Google Student Ambassador" to
wrongly trigger a web search. That decision now belongs to the model
itself, via Ollama's native tool-calling (see TOOLS below): the model sees
the full conversational context and decides, per turn, whether it
genuinely needs to call web_search or remember_fact — a meaning judgment,
not a keyword match.

Latency note: tools are passed into the SAME streaming call already
happening (not a separate "classify first" call) — for the common case
(plain conversation, no tool needed), this costs nothing extra; content
streams and gets spoken exactly as before. A tool call is detected from
the FIRST chunk that contains one (Ollama returns tool_calls as a whole
unit, not token-streamed), at which point the sentence-streaming loop
bails out immediately rather than waiting for more.
"""

import difflib
import json
import os
import re

from PyQt6.QtCore import QThread, pyqtSignal
import model_manager
import companion_state
from config import SYSTEM_PROMPT, MEMORY_FILE, MAX_HISTORY_TURNS
from ai_profile import BANNED_PHRASES


# ── Tool definitions ─────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for CURRENT, real-time, or factual "
                "information you don't already confidently know — news, "
                "prices, schedules, current job/internship openings, "
                "recent events, or specific facts you're not sure of. "
                "Do NOT call this when the user is telling you something "
                "about THEMSELVES (even if it mentions a company or "
                "product name), making conversation, sharing an opinion, "
                "or asking something you already know the answer to."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The actual search query to look up",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": (
                "Store a durable fact, preference, or behavioral rule the "
                "user just told you, so you apply/remember it in future "
                "turns — e.g. a nickname they want to be called, a rule "
                "about how you should write messages for them, or a fact "
                "about who they are (their role, job, etc). Only call this "
                "for things clearly meant to be remembered going forward, "
                "not passing conversational statements."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["user_preferences", "message_rules", "user_profile"],
                        "description": (
                            "user_preferences = how to address/treat them; "
                            "message_rules = rules for composing messages on "
                            "their behalf; user_profile = facts about who "
                            "they are"
                        ),
                    },
                    "key":   {"type": "string", "description": "Short identifier, e.g. 'nickname', 'signature', 'role'"},
                    "value": {"type": "string", "description": "The value to remember"},
                },
                "required": ["category", "key", "value"],
            },
        },
    },
]


# ── Conversation-ender phrases — if response contains these, drop back to wake word
_GOODBYE_PHRASES = (
    "goodbye", "bye", "see you", "talk later", "take care",
    "have a good", "good night", "good day", "farewell",
)


def _load_history() -> list[dict]:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_history(history: list[dict]):
    trimmed = history[-(MAX_HISTORY_TURNS * 2):]
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(trimmed, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Memory] Save error: {e}")


# Backstop for BANNED_PHRASES (ai_profile.py) — the prompt tells the model
# not to say these, but a small local model doesn't reliably obey that, so
# this strips them out of whatever's actually spoken/saved no matter what
# the model outputs. Handles the curly-apostrophe variant Ollama sometimes
# emits ("you're" -> "you’re") too.
_BANNED_PATTERNS = [
    re.compile(re.escape(p).replace("'", "['’]"), re.IGNORECASE)
    for p in BANNED_PHRASES
]


def _strip_banned_phrases(text: str) -> str:
    if not text:
        return text
    cleaned = text
    for pat in _BANNED_PATTERNS:
        cleaned = pat.sub("", cleaned)
    # Tidy up whatever punctuation/whitespace the removal left behind —
    # e.g. "You're the real deal, and I mean it" -> ", and I mean it"
    # -> "and I mean it".
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.!?])", r"\1", cleaned)
    cleaned = re.sub(r"^[\s,.\-–—]+", "", cleaned)
    cleaned = re.sub(r",\s*([.!?])", r"\1", cleaned)
    cleaned = re.sub(r"([,.!?])\1+", r"\1", cleaned)
    return cleaned.strip()


def _strip_think(text: str) -> str:
    """
    Qwen3 (and other reasoning models) wrap their scratch-work in
    <think>...</think> before the actual answer, like in the Ollama console
    output. That block is never meant to be shown or spoken — strip it.
    """
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # symbols, pictographs, emoticons, transport, supplemental
    "\U00002600-\U000027BF"  # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"  # regional indicator (flags)
    "\U00002700-\U000027BF"
    "\U0001F900-\U0001F9FF"
    "\U00002190-\U000021FF"  # arrows (occasionally used decoratively)
    "\U0000FE0F"              # variation selector (emoji presentation)
    "]+", flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    """
    Backup for the 'no emoji' system-prompt instruction — models don't
    always follow style instructions perfectly, so anything that slips
    through gets stripped before it's shown or spoken.
    """
    return _EMOJI_PATTERN.sub("", text).strip()


def _should_keep_listening(text: str) -> bool:
    """
    Returns True if Sanju should stay in conversation mode after this response.
    True when:  response ends with a question mark
    True when:  response is a short conversational reply (not a task completion)
    False when: response contains a goodbye phrase
    False when: response is a task completion (opened app, wrote code, etc.)
    """
    t = text.lower().strip()

    if any(p in t for p in _GOODBYE_PHRASES):
        return False

    task_endings = (
        "for you!", "done!", "opened", "searching", "pasted", "written",
        "created", "launching", "i've opened", "check the editor",
    )
    if any(t.endswith(e) or e in t[-60:] for e in task_endings):
        return False

    if text.strip().endswith("?"):
        return True

    word_count = len(text.split())
    if word_count < 25:
        return True

    return False


# ── Mood / emotion — delegated to companion_state ────────────────────────────
# Sanju's mood is no longer detected fresh from each reply in isolation —
# companion_state.update_mood() looks at both what the user said and what
# she said, nudges her persistent mood accordingly, and decays it back
# toward baseline over turns with no strong signal, so she doesn't flip
# state on a single word and doesn't stay "TEASING" forever after one joke.
def _detect_emotion(user_text: str, ai_text: str) -> str:
    return companion_state.update_mood(user_text, ai_text)


# ── "I don't know" detection — triggers an automatic web search fallback ─────
# Checked against only the FIRST completed sentence of a reply (see run()
# below) — models phrase uncertainty right up front, so this catches it
# before more of the (unhelpful) "I don't know" answer gets spoken.
_UNCERTAIN_PATTERNS = (
    "i don't know", "i do not know", "i'm not sure", "i am not sure",
    "i don't have that information", "i do not have that information",
    "i'm not certain", "i am not certain", "no idea", "i can't say",
    "not sure about that", "don't have access to", "do not have access to",
    "i'm unable to", "i am unable to", "beyond my knowledge",
)


def _seems_uncertain(sentence: str) -> bool:
    t = sentence.lower()
    return any(p in t for p in _UNCERTAIN_PATTERNS)


class AIThread(QThread):
    response_ready = pyqtSignal(str)
    word_ready = pyqtSignal(str)
    keep_listening = pyqtSignal(bool)
    # Emits the detected emotional state for the UI GIF
    emotion_state = pyqtSignal(str)
    # Fired once per completed sentence WHILE generation is still streaming
    # in — lets main.py start speaking the first sentence immediately
    # instead of waiting for the entire reply to finish generating.
    sentence_ready = pyqtSignal(str)
    # Fired instead of speaking, when the reply's first sentence looks like
    # an "I don't know" answer — carries the ORIGINAL user question so
    # main.py can automatically run a real web search for it instead.
    needs_web_search = pyqtSignal(str)

    # Learned once per process (see run()'s fallback handling) — if the
    # installed Ollama/model combo rejects tools+streaming together, we
    # stop paying for that failed attempt on every subsequent turn.
    _tools_supported = True

    def __init__(self, profile: dict | None = None):
        super().__init__()
        self.prompt  = ""
        self.profile = profile
        self.system_prompt = self._build_system_prompt(profile)
        self._raw_history: list[dict] = _load_history()

    @staticmethod
    def _build_system_prompt(profile: dict | None) -> str:
        if profile:
            from ai_profile import get_dynamic_prompt
            return get_dynamic_prompt(profile)
        return SYSTEM_PROMPT

    def update_profile(self, profile: dict):
        """
        Live-switches personality (e.g. Love → Friend → Assistant mode)
        without restarting the app. Keeps existing conversation memory —
        only the system prompt / personality changes.
        """
        self.profile = profile
        self.system_prompt = self._build_system_prompt(profile)
        print(f"[Profile] Switched to {profile.get('mode', '?')} mode.")

    def generate_response(self, text: str):
        self.prompt = text
        self.start()

    def inject_action_memory(self, action_description: str):
        """Silently injects a memory of a physical action into her persistent history."""
        try:
            self._raw_history.append({"role": "user", "text": f"[System Memory Update: {action_description}]"})
            self._raw_history.append({"role": "assistant", "text": "Acknowledged. I will remember I did this."})
            _save_history(self._raw_history)
            print(f"[Memory Injected] {action_description}")
        except Exception as e:
            print(f"[Memory Injection Error] {e}")

    def clear_memory(self):
        self._raw_history = []
        _save_history([])
        print("[Memory] Cleared.")

    def _build_messages(self) -> list[dict]:
        """Turns saved history + the new prompt into Ollama's chat message format."""
        # Companion context (relationship tier, current mood, relevant
        # memories) is layered on top of the static persona text fresh
        # every turn, since it changes turn to turn — the persona itself
        # (self.system_prompt) doesn't need rebuilding each time.
        try:
            context_block = companion_state.build_context_block(self.prompt)
            self._log_memory_retrieval()
        except Exception as e:
            print(f"[Companion] Context build error: {e}")
            context_block = ""

        system_content = self.system_prompt
        if context_block:
            system_content = f"{self.system_prompt}\n\n{context_block}"

        messages = [{"role": "system", "content": system_content}]
        for entry in self._raw_history:
            role = "assistant" if entry["role"] in ("model", "assistant") else "user"
            messages.append({"role": role, "content": entry["text"]})
        messages.append({"role": "user", "content": self.prompt})
        return messages

    @staticmethod
    def _log_memory_retrieval():
        """Explainability logging — what structured memory actually got
        pulled into context for this turn (not a dump of everything stored)."""
        try:
            prefs = companion_state.get_user_preferences()
            if prefs.get("nickname"):
                print(f"[Memory] Retrieved: nickname={prefs['nickname']}")
            profile = companion_state.get_user_profile()
            for k, v in profile.items():
                print(f"[Memory] Retrieved: {k}={v}")
        except Exception:
            pass

    def run(self):
        try:
            # Relationship bookkeeping for this turn — familiarity tier and
            # last-seen timestamp used by build_context_block() above.
            try:
                companion_state.record_interaction()
            except Exception as e:
                print(f"[Companion] record_interaction error: {e}")

            full_text     = ""
            sentence_buf  = ""
            first_checked = False
            suppress      = False   # True once uncertainty is detected — stop speaking this reply
            # Matches up through the next sentence-ending punctuation (or a
            # newline) — greedy enough to grab a whole sentence as soon as
            # one is available in the streamed buffer.
            # NOTE: matches an ellipsis ("...", "…", or any run of 2+ dots)
            # as ONE boundary, not three separate ones — otherwise a natural
            # interjection like "Ah..." gets chopped into "Ah.", ".", "." as
            # three garbage micro-fragments instead of one flowing pause.
            sentence_re = re.compile(r"([^.!?\n]*(?:\.{2,}|…|[.!?\n]))")

            # ── Chunk batching ────────────────────────────────────────────────
            # With the new producer/consumer speech pipeline, synthesis for
            # the NEXT chunk overlaps with playback of the current one, so
            # the old worry about "every chunk boundary = an audible gap"
            # matters much less than before. The remaining reason to batch
            # is that very short, isolated fragments just sound clipped and
            # unnatural on their own — so we flush after whichever comes
            # first: 3 complete sentences, ~120-150 characters, or a real
            # conversational pause (a blank line / paragraph break in the
            # model's own output, which is a genuine "new thought" cue).
            MAX_SENTENCES_PER_CHUNK = 3
            TARGET_CHUNK_CHARS      = 140   # within the 120-150 target band
            pending = []
            tool_call_detected = None   # set the moment the model requests a tool

            def _flush_pending(force: bool = False):
                nonlocal pending
                if not pending:
                    return
                joined = _strip_banned_phrases(" ".join(pending).strip())
                pending = []
                if joined and not suppress:
                    self.sentence_ready.emit(joined)

            def _consume(stream):
                """Runs the sentence-batching + tool-call-detection loop over
                a chat_stream() generator. Uses nonlocal so both the primary
                (tools-enabled) attempt and the fallback (no tools) attempt
                below can share the exact same logic."""
                nonlocal full_text, sentence_buf, first_checked, suppress
                nonlocal pending, tool_call_detected
                for chunk in stream:
                    msg = chunk.get("message", {})

                    # Ollama returns tool_calls as a complete unit (not
                    # token-streamed the way text is) — the moment one shows
                    # up, stop consuming and go handle it. For the common
                    # case (no tool needed) this never triggers and normal
                    # streaming proceeds with zero added cost.
                    tc = msg.get("tool_calls")
                    if tc:
                        tool_call_detected = tc[0]
                        return

                    piece = msg.get("content", "")
                    if not piece:
                        continue
                    full_text    += piece
                    sentence_buf += piece

                    while True:
                        m = sentence_re.match(sentence_buf)
                        if not m:
                            break
                        raw_sentence = m.group(1)
                        sentence_buf = sentence_buf[m.end():]

                        # A blank line / paragraph break with no real content
                        # is a natural pause point in the model's own output —
                        # flush whatever's pending now rather than waiting to
                        # hit the size threshold.
                        if raw_sentence.strip() == "" and "\n" in raw_sentence:
                            if not suppress:
                                _flush_pending()
                            continue

                        sentence = _strip_emoji(raw_sentence.replace("*", "").replace("#", "")).strip()
                        if not sentence:
                            continue

                        if not first_checked:
                            first_checked = True
                            if _seems_uncertain(sentence):
                                suppress = True
                                self.needs_web_search.emit(self.prompt)
                                pending = []
                                continue

                        if suppress:
                            continue

                        pending.append(sentence)
                        joined_len = sum(len(s) for s in pending) + len(pending) - 1
                        if (len(pending) >= MAX_SENTENCES_PER_CHUNK
                                or joined_len >= TARGET_CHUNK_CHARS):
                            _flush_pending()

            # ── Primary attempt: tools enabled ─────────────────────────────────
            # AIThread._tools_supported is a class-level flag, learned once
            # per process — if the installed Ollama/model combo rejects
            # tools+streaming together, we don't want to pay that failed
            # attempt's latency on every single subsequent turn.
            used_tools = AIThread._tools_supported
            try:
                stream_kwargs = dict(
                    model=model_manager.CHAT_MODEL,
                    messages=self._build_messages(),
                    think=False,
                )
                if used_tools:
                    stream_kwargs["tools"] = TOOLS
                _consume(model_manager.chat_stream(**stream_kwargs))
            except Exception as e:
                if used_tools:
                    # Could be this Ollama/model combo doesn't support
                    # tools+streaming together — fall back to a plain
                    # streaming call (no tool selection this turn), and
                    # stop trying tools for future turns this session.
                    print(f"[AI Engine] Tool-enabled stream failed ({e}) — "
                          f"falling back to plain streaming, tools disabled "
                          f"for the rest of this session.")
                    AIThread._tools_supported = False
                    full_text, sentence_buf   = "", ""
                    first_checked, suppress   = False, False
                    pending, tool_call_detected = [], None
                    _consume(model_manager.chat_stream(
                        model=model_manager.CHAT_MODEL,
                        messages=self._build_messages(),
                        think=False,
                    ))
                else:
                    raise

            # ── Tool call handling ────────────────────────────────────────────
            # If the model decided to call a tool, handle it here and return
            # early — full_text/sentence_buf are irrelevant in this branch
            # since the model chose to act rather than produce a text reply.
            if tool_call_detected:
                fn        = tool_call_detected.get("function", {})
                fn_name   = fn.get("name", "")
                fn_args   = fn.get("arguments", {}) or {}
                print(f"[Tool] {fn_name}({fn_args})")

                if fn_name == "web_search":
                    query = (fn_args.get("query") or self.prompt).strip()
                    print(f"[Intent] web_search")
                    print(f"[Decision] Calling web_search: {query!r}")
                    # Reuses the exact same path as the uncertainty fallback —
                    # main.py's on_needs_web_search starts a real search with
                    # the "let me check that" filler and speaks the result.
                    self.needs_web_search.emit(query)
                    self._raw_history.append({"role": "user", "text": self.prompt})
                    self._raw_history.append({"role": "assistant", "text": f"[Searched the web for: {query}]"})
                    cap = MAX_HISTORY_TURNS * 2
                    self._raw_history = self._raw_history[-cap:]
                    _save_history(self._raw_history)
                    self.response_ready.emit("")
                    self.keep_listening.emit(True)
                    return

                elif fn_name == "remember_fact":
                    category = fn_args.get("category", "")
                    key      = fn_args.get("key", "")
                    value    = fn_args.get("value", "")
                    print(f"[Intent] memory_update")
                    stored = companion_state.remember_structured(category, key, value)
                    print(f"[Decision] {'Stored' if stored else 'Failed to store'} {category}.{key}")
                    # Short, fixed acknowledgment rather than a second full
                    # model call — keeps this fast; storing a fact doesn't
                    # need fresh generation to confirm.
                    ack = "Got it, I'll remember that."
                    self.sentence_ready.emit(ack)
                    self._raw_history.append({"role": "user", "text": self.prompt})
                    self._raw_history.append({"role": "assistant", "text": ack})
                    cap = MAX_HISTORY_TURNS * 2
                    self._raw_history = self._raw_history[-cap:]
                    _save_history(self._raw_history)
                    self.response_ready.emit(ack)
                    self.keep_listening.emit(False)
                    return

                else:
                    print(f"[Tool] Unrecognized tool requested: {fn_name!r} — ignoring, falling back to plain reply.")
                    # Falls through to normal handling below with whatever
                    # (likely empty) text was gathered before the tool call.

            print("[Intent] conversation")
            print("[Tool] None")
            print("[Decision] Conversation only")

            # Flush whatever's left mid-stream-loop before handling the
            # no-trailing-punctuation leftover below.
            _flush_pending()

            # Whatever's left with no trailing punctuation still needs saying
            # — unless this reply got suppressed in favor of a web search.
            if not suppress:
                leftover = _strip_banned_phrases(
                    _strip_emoji(sentence_buf.replace("*", "").replace("#", "")).strip()
                )
                if leftover:
                    self.sentence_ready.emit(leftover)

            clean_text = _strip_think(full_text)
            clean_text = _strip_emoji(clean_text)
            clean_text = clean_text.replace("*", "").replace("#", "").strip()
            clean_text = _strip_banned_phrases(clean_text)

            if not clean_text:
                clean_text = "Sorry, I got a bit tongue-tied there. Could you say that again?"

            # Diagnostic only (not an auto-fix — regenerating would double
            # latency): flags when a new reply is suspiciously close to a
            # recent one, so a new repeat-offender phrase surfaces in the
            # console before it becomes as annoying as the last one. Add
            # anything you spot here to BANNED_PHRASES in ai_profile.py.
            recent_replies = [e["text"] for e in self._raw_history[-6:] if e["role"] == "assistant"]
            for prev in recent_replies:
                ratio = difflib.SequenceMatcher(None, clean_text.lower(), prev.lower()).ratio()
                if ratio >= 0.6:
                    print(f"[Repetition] New reply is {ratio:.0%} similar to a recent one: {prev[:60]!r}")
                    break

            # Save memory
            self._raw_history.append({"role": "user", "text": self.prompt})
            self._raw_history.append({"role": "assistant", "text": clean_text})

            # Cap history so context sent to the model doesn't grow unbounded
            cap = MAX_HISTORY_TURNS * 2
            self._raw_history = self._raw_history[-cap:]
            _save_history(self._raw_history)

            # Pull durable facts and recurring topics out of what the user
            # said, and let it (plus her own reply) nudge her mood — all
            # heuristic, no extra model call, so this stays fast.
            try:
                companion_state.extract_memories(self.prompt)
                companion_state.track_topics(self.prompt)
                for category, key, value in companion_state.extract_structured_rules(self.prompt):
                    print(f"[Memory] Captured rule: {category}.{key} = {value!r}")
            except Exception as e:
                print(f"[Companion] Memory/topic tracking error: {e}")

            emotion = _detect_emotion(self.prompt, clean_text)
            self.emotion_state.emit(emotion)
            # response_ready still fires with the full text at the end —
            # main.py uses it as a signal that generation is DONE (so it
            # knows when the sentence queue won't get any more additions),
            # not as the thing that actually gets spoken anymore.
            self.response_ready.emit(clean_text)
            self.keep_listening.emit(_should_keep_listening(clean_text))

        except Exception as e:
            print(f"[AI Engine Error] {e}")
            self.response_ready.emit(
                "I'm sorry, I can't reach my local model right now. "
                "Make sure Ollama is running in the background."
            )
            self.keep_listening.emit(False)