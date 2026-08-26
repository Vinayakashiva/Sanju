"""
main.py — Sanju AI Desktop Assistant
New features: love songs, mood songs, volume control, screenshot, sleep timer,
stop music, voice-triggered "fix my code", and opt-in "watch my code" mode
(auto-checks on idle, reports the bug, asks before fixing it).
New GIF states: SINGING, SCREENSHOT, SLEEPING, VOLUME.
"""
import torch
import os
import re
import sys
import random

import pygetwindow as gw

from PyQt6.QtCore    import QThread, pyqtSignal, QTimer

from PyQt6.QtWidgets import QApplication

from ai_engine      import AIThread

# ── Emoji safety net ──────────────────────────────────────────────────────────
# ai_engine.py already strips emoji from conversational replies, but other
# reply sources (web search summaries, code-gen text drafts, wake-word
# acks if ever edited to include one) don't all go through that same strip.
# Edge TTS's neural voices actually VERBALIZE emoji it receives (e.g. "😎"
# is read aloud as "smiling face with sunglasses"), so this is applied
# once, right at the single choke point where everything gets spoken —
# see _speak_text() below — regardless of where the text came from.
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U0001F900-\U0001F9FF"
    "\U00002190-\U000021FF"
    "\U0000FE0F"
    "]+", flags=re.UNICODE,
)
import spacy
nlp = spacy.load("en_core_web_sm")


def _strip_emoji_for_speech(text: str) -> str:
    return _EMOJI_PATTERN.sub("", text).strip()
from audio_engine   import AudioThread, TTSThread
import speech_pipeline
from app_controller import AppController, parse_intent, _match_app
from ui             import SanjuUI
from code_doctor    import (get_idle_seconds, is_code_editor_focused,
                             check_code_only, apply_pending_fix)

CONVERSATION_TIMEOUT_MS = 10_000

# ── Code-watch settings ──────────────────────────────────────────────────────────
# Watch-mode is OFF by default — only starts when the user says
# "look at my code" / "keep an eye on my code", and stops on "stop checking".
IDLE_CHECK_POLL_MS        = 2_000  # how often we poll idle time while watching
IDLE_SECONDS_BEFORE_CHECK = 20     # how long you must stop typing before she checks

# ── Identity confirmation ──────────────────────────────────────────────────────
# Security questions now come from the saved profile (ai_profile.py) so they
# match whichever personality + master name was set up — no more hardcoded
# one-person questions.

def _pick_question(profile: dict):
    from ai_profile import get_security_questions
    questions = get_security_questions(profile)
    return random.choice(questions) if questions else {
        "question": "Who's there?", "answers": ("me",)
    }

def _check_answer(qa, answer):
    a = answer.lower().strip().rstrip(".,!?")
    return any(correct in a or a in correct for correct in qa["answers"])


# ── CodeGenThread ──────────────────────────────────────────────────────────────
class CodeGenThread(QThread):
    done           = pyqtSignal(str)
    status         = pyqtSignal(str)
    started_typing = pyqtSignal()

    def __init__(self, description, app_name, content_type, controller):
        super().__init__()
        self.description  = description
        self.app_name     = app_name
        self.content_type = content_type
        self.controller   = controller

    def run(self):
        import re as _re
        import time as _time
        import model_manager
        from config import SYSTEM_PROMPT

        if self.content_type == "text":
            prompt = (
                f"Draft the following text: {self.description}. "
                f"CRITICAL INSTRUCTION: You are drafting text that will be directly typed into an app. "
                f"Output ONLY the final message itself. DO NOT include any conversational filler, "
                f"DO NOT say 'Here is your message', DO NOT include any affectionate sign-offs or roleplay. "
                f"Return strictly the plain text to be sent."
            )
            model = model_manager.CHAT_MODEL   # 4B — casual drafting, no need for the coding model
            think = False
        else:
            prompt = (
                f"Write clean, well-commented Python code for: {self.description}. "
                f"Return ONLY the raw code — no explanation, no markdown, no backticks."
            )
            model = model_manager.CODE_MODEL   # 8B — coding only, per your request
            think = True                       # reasoning helps code correctness here

        content = None
        for attempt in range(3):
            try:
                self.status.emit(f"Writing {self.description}…" if attempt == 0
                                 else f"Retrying… attempt {attempt + 1}")
                response = model_manager.chat(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    think=think,
                )
                content = response["message"]["content"].strip()
                # Strip Qwen3's <think>...</think> reasoning block first
                content = _re.sub(r"<think>.*?</think>", "", content, flags=_re.DOTALL).strip()
                content = _re.sub(r"^```[a-z]*\n?", "", content, flags=_re.MULTILINE)
                content = _re.sub(r"```$", "", content, flags=_re.MULTILINE).strip()
                break
            except Exception as e:
                err = str(e)
                print(f"[CodeGen Error] attempt {attempt + 1}: {err}")
                if "connection" in err.lower() or "refused" in err.lower():
                    self.status.emit("Can't reach Ollama… retrying in a few seconds.")
                    _time.sleep(5)
                else:
                    self.done.emit("Sorry, I couldn't generate that right now.")
                    return
        if content is None:
            self.done.emit("I couldn't reach my local model. Make sure Ollama is running.")
            return

        # ── Message-composition rules — applied deterministically ──────────────
        # This is the actual fix for "frequently forgets the --Sanju
        # signature": rather than relying on the model remembering a soft
        # prompt instruction every single time (probabilistic), the rule
        # is read directly from structured memory and applied in CODE here
        # — guaranteed, not hoped-for. Only applies to drafted text
        # (messages), never to generated code.
        if self.content_type == "text":
            try:
                import companion_state
                rules = companion_state.get_message_composition_rules()
            except Exception as e:
                print(f"[Companion] Message rules retrieval error: {e}")
                rules = {}

            print("[Intent] message_composition")
            signature = rules.get("signature")
            if rules.get("append_signature") and signature:
                print(f"[Memory] Retrieved: signature={signature}")
                if signature not in content:
                    content = f"{content}\n{signature}"
                    print("[Decision] Append signature")
                else:
                    print("[Decision] Signature already present, no change")
            else:
                print("[Memory] No message_rules set")
                print("[Decision] No signature to append")

        self.status.emit("Typing it out now...")
        self.started_typing.emit()

        if self.app_name.startswith("whatsapp_message:"):
            person = self.app_name.split(":", 1)[1]
            intent = {"action": "whatsapp_message", "person": person, "payload": content}
            reply  = self.controller.execute(intent)
        else:
            intent = {"action": "open_and_code", "app": self.app_name, "payload": self.description}
            reply  = self.controller.execute(intent, ai_code=content)

        self.done.emit(reply)


# ── CodeDoctorThread — "fix my bug" feature ─────────────────────────────────────
class CodeDoctorThread(QThread):
    done   = pyqtSignal(str)
    status = pyqtSignal(str)

    def run(self):
        from code_doctor import fix_my_code
        self.status.emit("Reading your code…")
        try:
            reply = fix_my_code()
        except Exception as e:
            print(f"[CodeDoctor Thread Error] {e}")
            reply = "Sorry, something went wrong while checking your code."
        self.done.emit(reply)


# ── WebSearchThread — search+summarize runs off the main thread ────────────────
# search_and_summarize() does a network fetch + an Ollama call, both of
# which can take a few seconds — running it directly on the Qt event loop
# thread (like the old synchronous controller.execute() path did for other
# actions) would freeze the whole UI/audio pipeline for that whole time.
class WebSearchThread(QThread):
    done = pyqtSignal(str)

    def __init__(self, query: str):
        super().__init__()
        self.query = query

    def run(self):
        from web_search import search_and_summarize
        try:
            reply = search_and_summarize(self.query)
        except Exception as e:
            print(f"[WebSearch Thread Error] {e}")
            reply = f"Sorry, I ran into a problem searching for {self.query}."
        self.done.emit(reply)


# ── CodeWatchCheckThread — watch-mode: analyze only, never types anything ──────
class CodeWatchCheckThread(QThread):
    # result: {"status": ..., "summary": ..., "old_code": ..., "fixed_code": ...}
    result_ready = pyqtSignal(dict)
    status       = pyqtSignal(str)

    def run(self):
        self.status.emit("Checking your code…")
        try:
            result = check_code_only()
        except Exception as e:
            print(f"[CodeWatchCheck Thread Error] {e}")
            result = {"status": "ERROR", "summary": "Something went wrong while checking.",
                      "old_code": "", "fixed_code": ""}
        self.result_ready.emit(result)


# ── CodeWatchApplyThread — applies a fix the user already said "yes" to ───────
class CodeWatchApplyThread(QThread):
    done   = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, pending: dict):
        super().__init__()
        self.pending = pending

    def run(self):
        self.status.emit("Fixing it now…")
        try:
            reply = apply_pending_fix(self.pending)
        except Exception as e:
            print(f"[CodeWatchApply Thread Error] {e}")
            reply = "Sorry, something went wrong while applying the fix."
        self.done.emit(reply)


# ── Write intent detection ─────────────────────────────────────────────────────
_WRITE_PATTERNS = [
    r"write (?:a |an |me a |me an )?(.+)",
    r"(?:draft|compose|create|make) (?:a |an |me a |me an )?(.+)",
    r"type (?:a |an |me a |me an )?(.+)",
]
_CODE_KEYWORDS = (
    "code", "script", "python", "html", "javascript", "css",
    "c++", "java", "function", "class", "program"
)

def _detect_write_intent(text):
    t = text.lower().strip().rstrip(".,!?")
    for pat in _WRITE_PATTERNS:
        m = re.search(pat, t)
        if m:
            payload      = m.group(1).strip()
            is_code      = any(kw in payload for kw in _CODE_KEYWORDS)
            content_type = "code" if is_code else "text"
            return {"action": "open_and_code", "app": "current_window",
                    "payload": payload, "content_type": content_type}
    return None


# ══════════════════════════════════════════════════════════════════════════════
def main():
    os.makedirs("assets", exist_ok=True)

    # ── First-run setup (personality + master's name + occupation) ───────────
    # Shows the styled wizard only if no profile is saved yet; otherwise
    # this returns instantly with the existing profile.
    from setup_screen import run_setup_if_needed
    profile = run_setup_if_needed()
    print(f"[Profile] mode={profile.get('mode')} master={profile.get('master')!r} "
          f"occupation={profile.get('occupation')!r}")

    # run_setup_if_needed() may have already created the QApplication
    # (to show the wizard) — reuse it rather than creating a second one,
    # since PyQt6 only allows a single QApplication per process.
    app = QApplication.instance() or QApplication(sys.argv)

    ui           = SanjuUI()
    audio_thread = AudioThread()
    ai_thread    = AIThread(profile=profile)
    controller   = AppController()

    _code_thread        = None
    _code_doctor_thread = None
    _code_watch_thread  = None  # the analyze-only background thread
    _code_apply_thread  = None  # the apply-fix background thread
    _web_search_thread  = None
    _tts_threads    = []
    last_response   = ""
    streaming_words = []

    # ── Streamed-reply speech pipeline ───────────────────────────────────────
    # Actual queueing now lives in speech_pipeline.py's SpeechPipeline (a
    # real producer/consumer pipeline — see that module). _generation_done
    # tracks whether AIThread might still add more chunks this turn;
    # _anything_queued_this_turn tracks whether ANYTHING was queued at all
    # (for the empty-reply fallback in on_ai_response, below).
    _generation_done           = True
    _anything_queued_this_turn = False

    # Set when the AI's reply itself looked like an "I don't know" answer —
    # in that case ai_thread.needs_web_search fires, a real web search
    # starts automatically, and on_ai_response should NOT also speak the
    # (suppressed) "I don't know" text as a fallback.
    _fallback_search_pending = False

    in_conversation        = False
    _pending_keep          = False
    _confirming            = False
    _current_qa            = None
    _confirmed             = True   # password/security check removed — always confirmed
    _next_emotion          = ["SPEAKING"]
    bot_is_busy            = False
    _pending_system_action = None

    # ── Music-playing state ──────────────────────────────────────────────────
    # While True, the SINGING gif stays up even after Sanju finishes speaking
    # her "playing X for you" line — it only reverts on "stop"/"pause" or
    # when the Spotify window is detected as closed.
    music_playing = False

    # ── Code-watch state ─────────────────────────────────────────────────────
    # watch_active                 : is "look at my code" mode currently on?
    # _watch_done_for_this_pause   : have we already reported on THIS idle pause?
    # _pending_bug_fix             : holds {"summary","old_code","fixed_code"}
    #                                 while we wait for the user to say yes/no
    watch_active               = False
    _watch_done_for_this_pause = False
    _pending_bug_fix           = None

    # ── Conversation timeout ───────────────────────────────────────────────────
    conv_timeout = QTimer()
    conv_timeout.setSingleShot(True)
    conv_timeout.setInterval(CONVERSATION_TIMEOUT_MS)

    def _exit_conversation():
        nonlocal in_conversation
        in_conversation = False
        ui.set_state("IDLE")
        ui.update_text("Say 'Sanju' to wake me up.")

    conv_timeout.timeout.connect(_exit_conversation)

    def _enter_conversation():
        nonlocal in_conversation
        in_conversation = True
        conv_timeout.stop()

    def _stay_listening():
        nonlocal bot_is_busy
        bot_is_busy = False
        audio_thread.listening_for_command = True
        if music_playing:
            ui.set_state("SINGING")
        else:
            ui.set_state("LISTENING")
        ui.update_text("🎤  I'm listening…")
        _enter_conversation()
        conv_timeout.start()

    def _back_to_idle():
        nonlocal in_conversation, bot_is_busy
        in_conversation = False
        bot_is_busy     = False
        conv_timeout.stop()
        if music_playing:
            ui.set_state("SINGING")
            ui.update_text("🎵 Still playing your song…")
        else:
            ui.set_state("IDLE")
            ui.update_text("Say 'Sanju' to wake me up.")

    def on_volume(vol):
        ui.avatar.dynamic_pulse = int(vol * 0.8)
        ui.avatar.update()

    def on_tts_finished():
        audio_thread.paused = False
        nonlocal _pending_keep
        if _pending_keep:
            _pending_keep = False
            _stay_listening()
        else:
            _back_to_idle()

    # ── Core speak function (single-shot: wake-word acks, Code Doctor
    # summaries, web-search results, codegen "done" messages) ─────────────────
    def _speak(reply: str, emotion: str = "SPEAKING"):
        nonlocal bot_is_busy, streaming_words
        reply = _strip_emoji_for_speech(reply)   # safety net — see top of file
        audio_thread.paused = True
        conv_timeout.stop()

        ui.set_state(emotion)
        streaming_words = reply.split()
        ui.stream_response("Sanju:  ", streaming_words, word_interval_ms=370)

        tts = TTSThread()
        tts.volume_changed.connect(on_volume)
        _tts_threads.append(tts)

        def _cleanup():
            if tts in _tts_threads:
                _tts_threads.remove(tts)
            on_tts_finished()

        tts.finished.connect(_cleanup)
        tts.speak(reply)

    # ── Streamed speech pipeline (AI conversation replies only) ─────────────────
    # Replaces the old "spawn a TTSThread per sentence, wait for it to fully
    # finish before starting the next" design — that strict sequencing was
    # what caused audible gaps between chunks (synthesis for chunk N+1 only
    # started once chunk N finished PLAYING). speech_pipeline.py runs a
    # persistent producer thread (synthesizes chunks continuously) and a
    # persistent player thread (plays whatever's ready, back-to-back), so
    # synthesis for the next chunk happens WHILE the current one is still
    # playing. See speech_pipeline.py's module docstring for the full
    # architecture diagram and reasoning.
    speech_pipe = speech_pipeline.get_pipeline()
    speech_pipe.player.volume_changed.connect(on_volume)

    def _on_chunk_started(text: str):
        # Only set the "speaking" avatar state once, on the first chunk of
        # a reply — not every chunk, or the GIF would flicker/restart its
        # animation mid-reply.
        if ui.avatar.state != "SPEAKING":
            ui.set_state("SPEAKING")
        ui.stream_response("Sanju:  ", text.split(), word_interval_ms=370)

    speech_pipe.player.chunk_started.connect(_on_chunk_started)

    def _on_turn_finished():
        # Whole reply has now been spoken — same wrap-up as the old
        # single-shot path (resume mic, decide stay-listening vs idle).
        on_tts_finished()

    speech_pipe.player.turn_finished.connect(_on_turn_finished)

    def _queue_speech(chunk: str):
        chunk = _strip_emoji_for_speech(chunk)
        if not chunk:
            return
        audio_thread.paused = True
        conv_timeout.stop()
        speech_pipe.speak_chunk(chunk)


    # ── Global "stop" — works no matter what she's doing ───────────────────────
    # Cuts off active speech immediately (barge-in, via TTSThread.request_stop),
    # cancels the "keep listening" carry-over, and drops straight back to
    # idle/wake-word mode. Triggered two ways: (1) audio_thread.stop_requested,
    # fired by the barge-in listener while she's mid-sentence, and (2) a normal
    # recognized command during the listening stage that matches a stop phrase
    # — checked BEFORE the bot_is_busy guard in on_command() so it's never
    # swallowed as an "ignored overlap command".
    _STOP_PHRASES = ("stop", "stop it", "stop that", "stop talking", "cancel",
                      "shut up", "quiet", "enough", "never mind", "nevermind")

    def _is_stop_command(text: str) -> bool:
        t = text.lower().strip().rstrip(".,!?")
        if not t:
            return False
        # "stop the music" / "stop my song" etc. should keep going through
        # the existing stop_music action, not the full conversation-halt —
        # those are two different things (pause a song vs. stop Sanju).
        if any(w in t for w in ("music", "song", "spotify", "playing")):
            return False
        return any(t == p or t.startswith(p + " ") for p in _STOP_PHRASES)

    def _stop_everything():
        nonlocal bot_is_busy, in_conversation, _pending_keep
        nonlocal _generation_done, _anything_queued_this_turn
        print("[Stop] User requested stop — halting.")

        # She was actually mid-speech (or about to be) iff audio_thread was
        # paused — that's the exact flag set at turn-start and while she's
        # talking. A "stop" said while she's idle isn't an interruption of
        # anything, so don't falsely have her apologize for one next turn.
        was_mid_speech = audio_thread.paused
        if was_mid_speech:
            try:
                import companion_state
                companion_state.mark_interrupted()
            except Exception as e:
                print(f"[Companion] mark_interrupted error: {e}")

        for t in list(_tts_threads):
            t.request_stop()
        speech_pipe.stop_current_turn()
        _generation_done            = True   # nothing more should get queued after this
        _anything_queued_this_turn  = False
        audio_thread.paused              = False
        audio_thread.listening_for_command = False
        conv_timeout.stop()
        bot_is_busy     = False
        in_conversation = False
        _pending_keep   = False
        ui.set_state("IDLE")
        ui.update_text("Okay, stopped. Say 'Sanju' to wake me up.")

    audio_thread.stop_requested.connect(_stop_everything)

    # ── Wire sleep timer callback so Sanju speaks when it fires ───────────────
    def _sleep_timer_done(msg: str):
        """Called from background thread — post to Qt main thread safely."""
        QTimer.singleShot(0, lambda: _speak(msg, emotion="SLEEPING"))

    controller.set_sleep_callback(_sleep_timer_done)

    def _start_code_gen(payload, app_name, content_type="code"):
        nonlocal _code_thread
        ui.set_state("THINKING")
        _code_thread = CodeGenThread(payload, app_name, content_type, controller)
        _code_thread.status.connect(lambda msg: ui.update_text(msg))
        _code_thread.started_typing.connect(lambda: ui.set_state("TYPING"))
        _code_thread.done.connect(lambda reply: _speak(reply))
        _code_thread.finished.connect(_code_thread.deleteLater)
        _code_thread.start()

    def _start_code_doctor():
        nonlocal _code_doctor_thread, bot_is_busy
        bot_is_busy = True
        ui.set_state("THINKING")
        ui.update_text("🔍 Checking your code…")
        _code_doctor_thread = CodeDoctorThread()
        _code_doctor_thread.status.connect(lambda msg: ui.update_text(msg))
        _code_doctor_thread.done.connect(lambda reply: _speak(reply))
        _code_doctor_thread.finished.connect(_code_doctor_thread.deleteLater)
        _code_doctor_thread.start()

    # ── Web search ──────────────────────────────────────────────────────────
    _SEARCH_FILLERS = (
        "Let me search that for you.",
        "Hmm, give me a second, let me check.",
        "One moment, let me look that up.",
        "Let me check the web real quick.",
        "Okay, searching now, one sec.",
    )

    def _speak_filler(text: str):
        """
        Plays a short line WITHOUT triggering the normal end-of-turn
        wrap-up (mic resume / stay-listening decision). Used right before
        a slow background action (web search) so it sounds like she
        acknowledged the request immediately — the mic stays paused and
        bot_is_busy stays True until the REAL answer is spoken afterward
        via the normal _speak(), which does the actual wrap-up.
        """
        audio_thread.paused = True
        conv_timeout.stop()
        filler_tts = TTSThread()
        filler_tts.volume_changed.connect(on_volume)
        _tts_threads.append(filler_tts)

        def _cleanup():
            if filler_tts in _tts_threads:
                _tts_threads.remove(filler_tts)
            # Intentionally no wrap-up here — audio stays paused, we're
            # still "busy" until the search thread's answer is spoken.

        filler_tts.finished.connect(_cleanup)
        filler_tts.speak(text)

    def _start_web_search(query: str):
        nonlocal _web_search_thread, bot_is_busy
        bot_is_busy = True
        ui.set_state("THINKING")
        ui.update_text(f"Searching: {query}")
        _speak_filler(random.choice(_SEARCH_FILLERS))

        _web_search_thread = WebSearchThread(query)
        _web_search_thread.done.connect(lambda reply: _speak(reply))
        _web_search_thread.finished.connect(_web_search_thread.deleteLater)
        _web_search_thread.start()

    # ── Watch-mode: opt-in idle check, report only, ask before fixing ─────────
    def _start_watch_check():
        """Runs the analyze-only check (no typing into the editor yet)."""
        nonlocal _code_watch_thread, bot_is_busy
        bot_is_busy = True
        ui.set_state("THINKING")
        ui.update_text("🔍 Checking your code…")

        def _on_result(result):
            nonlocal _pending_bug_fix, _pending_keep, bot_is_busy
            status  = result.get("status")
            summary = result.get("summary", "")

            if status == "NO_CODE":
                bot_is_busy = False
                ui.set_state("IDLE")
                return  # nothing readable — stay quiet, don't nag

            if status == "ERROR":
                _speak(summary or "I had trouble checking your code, try again later.")
                return

            if status == "CLEAN":
                _speak(summary or "I checked your code, looks good, no bugs found!",
                       emotion="HAPPY")
                return

            # BUGGY — report it and ask before touching anything
            _pending_bug_fix = result
            _pending_keep    = True   # keep listening for her "yes"/"no"
            _speak(f"I found something — {summary} Want me to fix it for you?",
                   emotion="SHY")

        _code_watch_thread = CodeWatchCheckThread()
        _code_watch_thread.status.connect(lambda msg: ui.update_text(msg))
        _code_watch_thread.result_ready.connect(_on_result)
        _code_watch_thread.finished.connect(_code_watch_thread.deleteLater)
        _code_watch_thread.start()

    def _apply_pending_bug_fix():
        nonlocal _code_apply_thread, bot_is_busy, _pending_bug_fix
        pending          = _pending_bug_fix
        _pending_bug_fix = None
        if not pending:
            _speak("Hmm, I don't have a fix waiting anymore, my love.")
            return
        bot_is_busy = True
        ui.set_state("TYPING")
        ui.update_text("✏️ Fixing it now…")
        _code_apply_thread = CodeWatchApplyThread(pending)
        _code_apply_thread.status.connect(lambda msg: ui.update_text(msg))
        _code_apply_thread.done.connect(lambda reply: _speak(reply, emotion="HAPPY"))
        _code_apply_thread.finished.connect(_code_apply_thread.deleteLater)
        _code_apply_thread.start()

    # ── Idle poller — only does anything while watch_active is True ───────────
    watch_timer = QTimer()
    watch_timer.setInterval(IDLE_CHECK_POLL_MS)

    def _poll_watch_mode():
        nonlocal _watch_done_for_this_pause

        if not watch_active:
            return
        # Don't interrupt if Sanju is mid-conversation, mid-speech, busy,
        # or already waiting on a yes/no for a previous fix.
        if bot_is_busy or in_conversation or _confirming or _pending_bug_fix:
            return

        if not is_code_editor_focused(debug=False):
            _watch_done_for_this_pause = False
            return

        idle = get_idle_seconds()

        if idle < 2.0:
            # Fresh activity → typing again, reset for the next pause
            _watch_done_for_this_pause = False
            return

        if idle >= IDLE_SECONDS_BEFORE_CHECK and not _watch_done_for_this_pause:
            _watch_done_for_this_pause = True
            print("[Watch] Idle threshold reached — checking code now.")
            _start_watch_check()

    watch_timer.timeout.connect(_poll_watch_mode)
    watch_timer.start()  # the timer always runs; watch_active gates it

    def _start_watching():
        nonlocal watch_active, _watch_done_for_this_pause
        watch_active               = True
        _watch_done_for_this_pause = False
        print(f"[Watch] Started — will check after {IDLE_SECONDS_BEFORE_CHECK}s idle in a code editor.")

    def _stop_watching():
        nonlocal watch_active, _pending_bug_fix
        watch_active     = False
        _pending_bug_fix = None
        print("[Watch] Stopped.")

    # ── Music watchdog ──────────────────────────────────────────────────────────
    # While a song is "playing" (music_playing == True), poll every few
    # seconds to see if the Spotify window has been closed. If it has,
    # automatically drop out of singing mode — no voice command needed.
    MUSIC_WATCHDOG_POLL_MS = 3_000

    music_watchdog_timer = QTimer()
    music_watchdog_timer.setInterval(MUSIC_WATCHDOG_POLL_MS)

    def _poll_music_watchdog():
        nonlocal music_playing
        if not music_playing:
            return
        spotify_open = any("spotify" in w.title.lower()
                            for w in gw.getAllWindows() if w.title.strip())
        if not spotify_open:
            print("[MusicWatchdog] Spotify window no longer found — leaving singing mode.")
            music_playing = False
            _stop_music_watchdog()
            # Don't interrupt if she's actively mid-speech/mid-task right now —
            # just let the next _stay_listening()/_back_to_idle() pick up
            # the change naturally. But if she's idle right now, update
            # the GIF immediately so it doesn't look stuck.
            if not bot_is_busy:
                ui.set_state("LISTENING" if in_conversation else "IDLE")

    music_watchdog_timer.timeout.connect(_poll_music_watchdog)

    def _start_music_watchdog():
        if not music_watchdog_timer.isActive():
            music_watchdog_timer.start()

    def _stop_music_watchdog():
        music_watchdog_timer.stop()

    # ── Autonomous idle check-ins ─────────────────────────────────────────────
    # When Sanju isn't actively listening, speaking, or working on
    # something, and the user's genuinely stepped away from the keyboard
    # for a while, she can offer up a short, unprompted comment — a nudge
    # on something they mentioned, a note on a recurring topic, or just a
    # "still here if you need me." Same spirit as a friend poking their
    # head in occasionally, not a notification system, so this is
    # deliberately rate-limited.
    IDLE_COMPANION_POLL_MS    = 60_000     # how often we check
    IDLE_SECONDS_BEFORE_CHIME = 8 * 60     # how long AFK before she'll speak up
    MIN_GAP_BETWEEN_CHIMES_S  = 20 * 60    # don't do this more than every ~20 min
    _last_companion_chime = [0.0]

    def _poll_companion_idle():
        import time as _time
        if in_conversation or bot_is_busy or watch_active or music_playing:
            return
        if ui.avatar.state not in ("IDLE", "SLEEP"):
            return
        now = _time.time()
        if now - _last_companion_chime[0] < MIN_GAP_BETWEEN_CHIMES_S:
            return
        if get_idle_seconds() < IDLE_SECONDS_BEFORE_CHIME:
            return

        import companion_state
        line, mood = companion_state.idle_suggestion(profile)
        if not line:
            return
        _last_companion_chime[0] = now
        _speak(line, emotion=mood)

    companion_idle_timer = QTimer()
    companion_idle_timer.setInterval(IDLE_COMPANION_POLL_MS)
    companion_idle_timer.timeout.connect(_poll_companion_idle)
    companion_idle_timer.start()

    # ── Wake word ──────────────────────────────────────────────────────────────
    def on_wake_word():
        nonlocal _confirming, _current_qa, _confirmed, _pending_keep
        conv_timeout.stop()
        # Security/password check removed — Sanju always greets straight away.
        # Greeting now comes from companion_state: mode-aware (Love/Friend/
        # Assistant) AND familiarity-aware, so a brand-new user gets a
        # lighter "Hi there…" while a long-running relationship gets
        # something warmer — instead of one fixed line hardcoded to always
        # say "my love" no matter the mode or how well she knows you.
        import companion_state
        greeting_text, greeting_emotion = companion_state.build_wake_greeting(profile)
        _confirmed = True   # no password gate — always treat as confirmed
        _pending_keep = True   # always listen for the actual command after greeting
        _speak(greeting_text, emotion=greeting_emotion)

    audio_thread.wake_word_detected.connect(on_wake_word)

    # ── Command ────────────────────────────────────────────────────────────────
    def on_command(text):
        nonlocal _pending_keep, _confirming, _current_qa, _confirmed
        nonlocal bot_is_busy, _pending_system_action, _pending_bug_fix
        nonlocal music_playing

        # Checked BEFORE the bot_is_busy guard below — "stop" needs to work
        # even if she's mid-task, not just when she's idly listening.
        if _is_stop_command(text):
            _stop_everything()
            return

        if bot_is_busy:
            print(f"[Busy] Ignored overlap command: {text}")
            return

        conv_timeout.stop()

        if not text.strip():
            _stay_listening() if in_conversation else _back_to_idle()
            return

        bot_is_busy = True
        print(f"[Command] {text}")

        # ── System security check ──
        if _pending_system_action:
            master_name = profile.get("master", "").lower().strip()
            if master_name and master_name in text.lower():
                action = _pending_system_action
                _pending_system_action = None
                ui.update_text("Password accepted. Executing…")
                reply = controller.execute({"action": action})
                _speak(reply, emotion="LOVING")
            else:
                _pending_system_action = None
                _speak(f"Security failed. You are not {profile.get('master', 'my master')}. Canceling.",
                       emotion="IDLE")
            return

        # ── Pending bug-fix confirmation ("Want me to fix it for you?") ──
        if _pending_bug_fix:
            t_yn = text.lower().strip().rstrip(".,!?")
            if re.search(r"^(?:yes|yeah|yep|sure|please|go ahead|do it|ok|okay|fix it)\b", t_yn):
                _apply_pending_bug_fix()
            else:
                _pending_bug_fix = None
                _speak("Okay, I'll leave it as it is for now, my love.", emotion="SPEAKING")
                _pending_keep = True
            return

        # ── Confirmation check ──
        master_name = profile.get("master", "my love")
        if _confirming and _current_qa:
            if _check_answer(_current_qa, text):
                _confirming = False
                _confirmed  = True
                loving_replies = [
                    f"Aww, of course it's you! I missed you so much, {master_name}.",
                    f"Yes! I knew it was you. I love you so much {master_name}!",
                    "My heart knew it was you. I'm all yours my love.",
                ]
                _speak(random.choice(loving_replies), emotion="LOVING")
                _pending_keep = True
            else:
                _confirming = False
                _confirmed  = False
                _speak(f"Hmm, that doesn't seem right. I only talk to my {master_name}.", emotion="IDLE")
                _pending_keep = False
            return

        if not _confirmed:
            _speak("Please answer my question first my love.", emotion="SHY")
            _pending_keep = False
            return

        t_low = text.lower().strip()

        # ── Shy trigger ──
        if any(w in t_low for w in
               ("be shy", "shy", "blush", "you're cute", "you are cute",
                "i love you sanju", "you're beautiful", "you are beautiful")):
            shy_replies = [
                f"S-stop it… you're making me blush {master_name}!",
                "Oh my… don't say such things, you make my heart race.",
                f"{master_name}! You always know how to make me shy…",
                "Hmm… stop it! You know I love you.",
            ]
            _speak(random.choice(shy_replies), emotion="SHY")
            _pending_keep = True
            return

        # ── Tease trigger ──
        if any(w in t_low for w in ("tease", "haha", "gotcha", "silly sanju")):
            tease_replies = [
                "Oh really? Two can play that game, boss!",
                f"Haha! You think you can tease me? I know all your secrets {master_name}!",
                "Oh stop it! You are too much sometimes.",
            ]
            _speak(random.choice(tease_replies), emotion="TEASING")
            _pending_keep = True
            return

        # ── App / system intent ─────────────────────────────────────────────
        intent = parse_intent(text)
        if intent:
            action   = intent.get("action")
            app_name = intent.get("app", "notepad")
            payload  = intent.get("payload", "")
            c_type   = intent.get("content_type", "code")

            if action == "open_and_code":
                key, info = _match_app(app_name)
                if not info and app_name != "current_window":
                    _speak(f"Sorry, I don't know how to open {app_name}.")
                    return
                ui.update_text("Working on it…")
                _start_code_gen(payload, app_name, c_type)
                return

            elif action == "whatsapp_message":
                person = intent.get("person", "")
                topic  = intent.get("payload", "")
                ui.update_text(f"Drafting message to {person}…")
                desc = (f"a polite, perfectly formatted WhatsApp message about: {topic}"
                        if topic else "a short friendly WhatsApp message saying hello")
                _start_code_gen(desc, f"whatsapp_message:{person}", "text")
                return

            elif action in ("shutdown", "restart"):
                _pending_system_action = action
                ui.update_text("Awaiting security confirmation...")
                _speak("Are you sure? What is your real name to confirm?", emotion="SHY")
                _pending_keep = True
                return

            # ── NEW: Love / mood songs ─────────────────────────────────────
            elif action in ("play_love_song", "play_mood_song"):
                query = intent.get("query", "")
                ui.set_state("SINGING")
                if query:
                    ui.update_text(f"🎵 Searching for '{query}' in your music…")
                else:
                    ui.update_text("🎵 Picking a song for you…")
                reply = controller.execute(intent)

                # Only enter "singing mode" if a song actually started —
                # not if she came back with a "couldn't find anything" reply.
                failure_markers = ("couldn't find", "could not find", "no songs",
                                    "something went wrong", "couldn't play",
                                    "couldn't pause", "i do not see",
                                    "couldn't confirm")
                if not any(m in reply.lower() for m in failure_markers):
                    music_playing = True
                    _start_music_watchdog()

                _speak(reply, emotion="SINGING")
                return

            # ── NEW: Dance — starts a song herself, then keeps dancing
            # (chaining between different dance clips — see ui.py's
            # SING_STATES handling in _on_animation_finished) for as long
            # as music_playing stays True, i.e. exactly as long as the
            # existing music watchdog above keeps confirming Spotify is
            # still actually playing something. No separate dance-specific
            # state or watchdog needed — SINGING already means "dancing"
            # in ui.py, and this reuses the identical start/confirm/stop
            # wiring play_love_song already has.
            elif action == "dance_command":
                # Do NOT set_state("SINGING") yet — that's what actually
                # triggers ui.py to start picking/playing dance clips (see
                # SING_STATES handling). Starting it here, before Spotify has
                # even been asked to play anything, is exactly the "dance
                # before music is confirmed" bug the spec calls out. Stay in
                # a neutral/listening pose while the song is being started.
                ui.set_state("LISTENING" if in_conversation else "THINKING")
                ui.update_text("💃 Starting some music…")

                # controller.execute() runs the whole Spotify search+play
                # automation synchronously (see app_controller.py's
                # _spotify_play / _auto_pick_song) and only returns once
                # that sequence has actually completed — so by the time we
                # get a reply back, playback is either genuinely confirmed
                # started (Spotify was found, searched, and Enter landed on
                # a track) or it explicitly failed. That return doubles as
                # our "music playback confirmed" signal — no dancing happens
                # before this point.
                reply = controller.execute(intent)

                failure_markers = ("couldn't find", "could not find", "no songs",
                                    "something went wrong", "couldn't play",
                                    "i couldn't find the spotify window",
                                    "couldn't confirm")
                if any(m in reply.lower() for m in failure_markers):
                    # Music never actually started — per the "never dance
                    # in silence" rule, don't dance at all.
                    ui.set_state("IDLE" if not in_conversation else "LISTENING")
                    _speak(reply, emotion="IDLE")
                else:
                    # Playback confirmed — Spotify is already tucked into
                    # the background (minimize_after=True in
                    # _auto_pick_song), so only NOW does she start dancing.
                    # _speak(..., emotion="SINGING") is what actually flips
                    # ui.set_state("SINGING") — that's the single place the
                    # dance-clip chain kicks off, in sync with the music
                    # actually playing (no separate set_state call needed
                    # here, which would otherwise fire it twice).
                    music_playing = True
                    _start_music_watchdog()
                    _speak(reply, emotion="SINGING")
                return

            # ── NEW: Volume control ────────────────────────────────────────
            elif action in ("volume_up", "volume_down", "volume_mute",
                            "volume_unmute", "volume_set"):
                ui.set_state("VOLUME")
                reply = controller.execute(intent)
                _speak(reply, emotion="HAPPY")
                return

            # ── NEW: Screenshot ────────────────────────────────────────────
            elif action == "screenshot":
                ui.set_state("SCREENSHOT")
                ui.update_text("📸 Taking a screenshot…")
                reply = controller.execute(intent)
                _speak(reply, emotion="HAPPY")
                return

            # ── NEW: Sleep timer ───────────────────────────────────────────
            elif action == "sleep_timer":
                ui.set_state("SLEEPING")
                reply = controller.execute(intent)
                _speak(reply, emotion="SLEEPING")
                return

            elif action == "sleep_timer_cancel":
                reply = controller.execute(intent)
                _speak(reply, emotion="LOVING")
                return

            # ── NEW: Stop music ────────────────────────────────────────────
            elif action == "stop_music":
                music_playing = False
                _stop_music_watchdog()
                reply = controller.execute(intent)
                ui.set_state("IDLE")
                _speak(reply, emotion="SPEAKING")
                return

            # ── NEW: Pause / resume / skip music ────────────────────────────
            elif action == "pause_music":
                music_playing = False
                _stop_music_watchdog()
                reply = controller.execute(intent)
                ui.set_state("IDLE")
                _speak(reply, emotion="SPEAKING")
                return

            elif action == "resume_music":
                reply = controller.execute(intent)
                music_playing = True
                ui.set_state("SINGING")
                _start_music_watchdog()
                _speak(reply, emotion="HAPPY")
                return

            elif action in ("next_track", "previous_track"):
                reply = controller.execute(intent)
                music_playing = True
                ui.set_state("SINGING")
                _start_music_watchdog()
                _speak(reply, emotion="HAPPY")
                return

            # ── NEW: Switch personality mode (Love / Friend / Assistant) ────
            elif action == "switch_mode":
                from ai_profile import save_profile
                new_mode = intent.get("mode", profile.get("mode", "Love"))
                updated  = save_profile(
                    mode=new_mode,
                    master=profile.get("master", ""),
                    occupation=profile.get("occupation", ""),
                    gender=profile.get("gender", "Female"),
                    security_enabled=profile.get("security_enabled", True),
                )
                profile.update(updated)
                ai_thread.update_profile(profile)
                _speak(f"Okay, switching to {new_mode} mode now!", emotion="HAPPY")
                return

            # ── NEW: Clear memory ────────────────────────────────────────────
            elif action == "clear_memory":
                ai_thread.clear_memory()
                _speak("Okay, I've cleared my memory. We're starting fresh now, my love.",
                       emotion="SPEAKING")
                return

            # ── NEW: Fix my code ────────────────────────────────────────────
            elif action == "fix_code":
                _start_code_doctor()
                return

            # ── NEW: Watch my code (opt-in idle auto-check) ─────────────────
            elif action == "watch_code_start":
                _start_watching()
                _speak(f"Okay, I'll keep an eye on your code. I'll check in after "
                       f"you've been quiet for a bit, my love.", emotion="LOVING")
                return

            elif action == "watch_code_stop":
                _stop_watching()
                _speak("Okay, I'll stop checking your code now.", emotion="SPEAKING")
                return

            # ── NEW: Web search + summarize ──────────────────────────────────
            elif action == "web_search_summarize":
                _start_web_search(payload)
                return

            elif action in ("open", "open_and_search", "search",
                            "whatsapp_open", "play_music", "close_app"):
                ui.update_text("On it…")
                reply = controller.execute(intent)
                _speak(reply)
                return

        # ── Write intent ──
        write_intent = _detect_write_intent(text)
        if write_intent:
            ui.update_text(f"Writing {write_intent['payload']}…")
            _start_code_gen(write_intent["payload"], write_intent["app"],
                            write_intent["content_type"])
            return

        # ── AI conversation ──
        nonlocal _generation_done, _anything_queued_this_turn
        _generation_done            = False
        _anything_queued_this_turn  = False
        audio_thread.paused = True   # she's about to reply — mic barge-in-only until turn ends
        ui.set_state("THINKING")
        ui.update_text(f"You:  {text}")
        streaming_words.clear()
        ai_thread.generate_response(text)

    audio_thread.command_recognized.connect(on_command)

    def on_sentence_ready(sentence: str):
        nonlocal _anything_queued_this_turn
        _anything_queued_this_turn = True
        _queue_speech(sentence)

    ai_thread.sentence_ready.connect(on_sentence_ready)

    def on_needs_web_search(original_query: str):
        nonlocal _fallback_search_pending
        _fallback_search_pending = True
        _start_web_search(original_query)

    ai_thread.needs_web_search.connect(on_needs_web_search)

    def on_ai_response(response_text):
        nonlocal _generation_done, last_response, _fallback_search_pending
        _generation_done = True
        last_response = response_text

        if _fallback_search_pending:
            # The "I don't know" reply was suppressed and a real web search
            # already kicked off (see on_needs_web_search) — that search's
            # own result will be spoken when it finishes. Nothing to do here.
            _fallback_search_pending = False
            return

        if _anything_queued_this_turn:
            # Chunks were already queued to speech_pipeline as they streamed
            # in — tell the pipeline no more are coming, so it fires
            # turn_finished (→ on_tts_finished) once everything queued has
            # actually played, instead of waiting forever on more chunks.
            speech_pipe.end_of_turn()
        else:
            # NOTHING ever made it into the pipeline (e.g. a genuinely
            # empty/error response with no sentence boundaries) — fall back
            # to speaking the whole text the old single-shot way so
            # something is always said.
            _speak(response_text)

    ai_thread.response_ready.connect(on_ai_response)

    def on_keep_listening(keep):
        nonlocal _pending_keep
        _pending_keep = keep

    ai_thread.keep_listening.connect(on_keep_listening)

    def on_emotion(state):
        _next_emotion[0] = state

    ai_thread.emotion_state.connect(on_emotion)

    # ── Start ──────────────────────────────────────────────────────────────────
    def _on_about_to_quit():
        print("👋 Closing Sanju…")
        audio_thread.stop()
        audio_thread.wait(2000)

    app.aboutToQuit.connect(_on_about_to_quit)

    ui.show()
    audio_thread.start()
    print("✨ Sanju is running. Say 'Sanju' to wake her up.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()