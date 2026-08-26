"""
audio_engine.py — Sanju AI
Simple, clean — wake word detection + Kokoro TTS streaming.
No voice verification.

── TTS engine: Kokoro (local) ────────────────────────────────────────────────
Switched from edge_tts to Kokoro because edge_tts has to round-trip to
Microsoft's servers for every single reply — that network hop was the main
source of the "why does she take a second to start talking" delay, and the
voice itself reads a bit flat/robotic for a companion-style assistant.

Kokoro is an ~82M param open-weight TTS model that runs entirely on your
machine (CPU or GPU). No network call per reply, so time-to-first-audio is
just local inference time — and its voices sound noticeably more natural/
human than edge_tts's.

Install:
    pip install kokoro>=0.9.4 numpy
    # Kokoro's phonemizer falls back to espeak-ng for out-of-vocabulary
    # words/names, so install the system binary too:
    #   Windows: choco install espeak-ng   (or the installer from
    #            https://github.com/espeak-ng/espeak-ng/releases)
    #   macOS:   brew install espeak-ng
    #   Linux:   sudo apt install espeak-ng
    # If you have an NVIDIA GPU + CUDA torch installed, Kokoro will use it
    # automatically for faster generation; otherwise it runs fine on CPU.
"""

import difflib
import math
import struct
import threading

import numpy as np
import pyaudio
import speech_recognition as sr
from kokoro import KPipeline
from PyQt6.QtCore import QThread, pyqtSignal

# ── Porcupine (optional, recommended upgrade — see PorcupineAudioThread) ──────
try:
    import pvporcupine
    _PORCUPINE_AVAILABLE = True
except Exception:
    _PORCUPINE_AVAILABLE = False


WAKE_WORDS = ("sanju", "sanju wake up", "hey sanju")
_WAKE_CORE = "sanju"

# ── Barge-in stop words ───────────────────────────────────────────────────────
# Recognized while she's actively speaking (see the barge-in listen stage
# in AudioThread.run()), so the user can interrupt her mid-sentence. Kept
# as a short, deliberate list so an offhand word in the room doesn't
# accidentally cut her off.
STOP_WORDS = (
    "stop", "stop it", "stop that", "stop talking", "cancel",
    "shut up", "quiet", "enough", "never mind", "nevermind",
)


def _is_stop_word(text: str) -> bool:
    t = text.lower().strip().rstrip(".,!?")
    if not t:
        return False
    return any(t == w or t.startswith(w + " ") for w in STOP_WORDS)


# ── Known misrecognition variants ─────────────────────────────────────────────
# Google's STT consistently mangles "Sanju" (an uncommon name) into a small,
# predictable set of near-misses. Checking these directly first — with a high
# fixed confidence — is both faster AND more reliable than relying purely on
# fuzzy string distance, since a couple of these (e.g. "sango") aren't quite
# close enough to "sanju" by pure character-overlap to pass a generic fuzzy
# threshold without ALSO letting through too many real false positives.
_KNOWN_VARIANTS = {
    "sanju":  1.00,
    "sanjo":  0.90,
    "sango":  0.85,
    "sandhu": 0.80,
    "sanjuu": 0.90,
    "sanjay": 0.75,
}

# Below this, we don't trust it — keeps the wake word from firing on
# unrelated words that just happen to share a couple of letters.
_WAKE_CONFIDENCE_THRESHOLD = 0.62


def _wake_confidence(text: str) -> float:
    """
    Returns a 0.0-1.0 confidence that `text` contains an attempt at the
    wake word — the single highest score found across every word/phrase
    in the transcription. Checks known misrecognition variants first
    (cheap, precise), then falls back to fuzzy character-similarity for
    anything else Google's STT might have mangled it into.
    """
    t = text.lower().strip()
    if not t:
        return 0.0

    best = 0.0

    # Full-phrase wake words ("hey sanju", "sanju wake up") — strongest signal.
    for w in WAKE_WORDS:
        if w in t:
            best = max(best, 1.0)

    # Known variants, checked as whole words (avoids "sanju" matching inside
    # an unrelated longer word by substring accident).
    words = t.split()
    for word in words:
        if word in _KNOWN_VARIANTS:
            best = max(best, _KNOWN_VARIANTS[word])

    # Generic fuzzy fallback for anything not in the known-variant list —
    # catches one-off mishearings the fixed list doesn't cover.
    for word in words:
        ratio = difflib.SequenceMatcher(None, word, _WAKE_CORE).ratio()
        best = max(best, ratio)

    return best


def _is_wake_word(text: str) -> bool:
    """
    Detects the wake word, tolerating near-miss speech-recognition
    transcriptions. "Sanju" is an uncommon name, so Google's recognizer
    sometimes mis-transcribes it (e.g. "sanjo", "sango", "sandhu") — an
    exact substring match alone was causing legit wake attempts to be
    silently ignored, forcing the user to repeat themselves 2-3 times.
    Kept for backward compatibility — prefer _wake_confidence() directly
    where you want the actual score (e.g. for logging).
    """
    return _wake_confidence(text) >= _WAKE_CONFIDENCE_THRESHOLD


# ══════════════════════════════════════════════════════════════════════════════
# KOKORO TTS CONFIG
# ══════════════════════════════════════════════════════════════════════════════

# Language code Kokoro expects — 'a' = American English, 'b' = British English.
KOKORO_LANG_CODE = "a"

# Pick the voice's vibe here. A few natural-sounding options that ship with
# Kokoro (American female unless noted):
#   af_heart   — warm, expressive (recommended default, closest to the old
#                EmmaMultilingualNeural warmth)
#   af_bella   — soft, gentle
#   af_nicole  — calm, breathy
#   bf_emma    — British female, if you liked the "Emma" name/tone
#   am_adam / am_michael — male options
KOKORO_VOICE = "af_heart"

# 1.0 = normal pace. Nudge down slightly (e.g. 0.92) for a slower, more
# tender delivery, similar to the old VOICE_RATE = "-5%".
KOKORO_SPEED = 0.95

KOKORO_SAMPLE_RATE = 24000  # Kokoro always outputs 24kHz mono audio


# ══════════════════════════════════════════════════════════════════════════════
# KOKORO PIPELINE — LOADED ONCE, REUSED FOR EVERY REPLY
# ══════════════════════════════════════════════════════════════════════════════
# Loading the model has a one-time cost (reading weights off disk). Doing
# that lazily on the first reply would recreate the exact "delay before she
# responds" problem edge_tts's voice-list lookup used to cause — so it's
# preloaded in the background as soon as this module is imported, same
# pattern as the old voice-cache warm-up.

_pipeline: "KPipeline | None" = None
_pipeline_lock = threading.Lock()


def _get_pipeline() -> KPipeline:
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    with _pipeline_lock:
        if _pipeline is None:
            print("[TTS] Loading Kokoro model…")
            _pipeline = KPipeline(lang_code=KOKORO_LANG_CODE)
            print("[TTS] Kokoro model ready.")
    return _pipeline


def preload_kokoro_async():
    """Kicks off model loading in a background thread immediately, so the
    first reply of a session doesn't pay the load cost either."""
    threading.Thread(target=_get_pipeline, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# STREAMING TTS
# ══════════════════════════════════════════════════════════════════════════════
# Kokoro splits long text into chunks (roughly per-sentence) internally and
# yields audio for each chunk as it's generated — so, just like the old
# edge_tts streaming, playback of the first chunk can start before later
# chunks finish generating. No asyncio needed here since Kokoro's generator
# is plain, synchronous Python — this already runs inside TTSThread's own
# background thread.

# ══════════════════════════════════════════════════════════════════════════════
# PERSISTENT AUDIO OUTPUT STREAM
# ══════════════════════════════════════════════════════════════════════════════
# Real fix for the "voice sounds choppy, not smooth" issue: main.py now
# queues and speaks a NEW TTSThread per sentence (see the streaming reply
# work), and every TTSThread was opening a brand-new PyAudio stream and
# tearing it down afterward. That open/close cycle happening between every
# single sentence is exactly what produced the clicky, disjointed feel —
# each teardown/rebuild introduces a small pop and a gap. Keeping ONE
# output stream open and reusing it across every sentence (and every
# reply) gives genuinely continuous playback, closer to how a person
# actually talks.

_pa_instance = None
_pa_stream   = None
_pa_lock     = threading.Lock()


def _get_output_stream():
    global _pa_instance, _pa_stream
    with _pa_lock:
        if _pa_instance is None:
            _pa_instance = pyaudio.PyAudio()
        if _pa_stream is None or not _pa_stream.is_active():
            _pa_stream = _pa_instance.open(
                format=pyaudio.paInt16, channels=1,
                rate=KOKORO_SAMPLE_RATE, output=True,
            )
        return _pa_stream


def shutdown_audio_output():
    """Optional cleanup — call on app exit if you want a clean teardown.
    Not calling it is harmless; the OS reclaims the audio device anyway."""
    global _pa_instance, _pa_stream
    with _pa_lock:
        try:
            if _pa_stream is not None:
                _pa_stream.stop_stream()
                _pa_stream.close()
        except Exception:
            pass
        try:
            if _pa_instance is not None:
                _pa_instance.terminate()
        except Exception:
            pass
        _pa_stream    = None
        _pa_instance  = None


# ══════════════════════════════════════════════════════════════════════════════
# STREAMING TTS
# ══════════════════════════════════════════════════════════════════════════════
# Kokoro splits long text into chunks (roughly per-sentence) internally and
# yields audio for each chunk as it's generated — so, just like the old
# edge_tts streaming, playback of the first chunk can start before later
# chunks finish generating. No asyncio needed here since Kokoro's generator
# is plain, synchronous Python — this already runs inside TTSThread's own
# background thread.

def get_shared_output_stream():
    """Public accessor for speech_pipeline.py — same persistent stream
    used everywhere else in this file."""
    return _get_output_stream()


def synthesize_kokoro(text: str, voice: str = KOKORO_VOICE) -> bytes:
    """
    PURE synthesis — no playback, no side effects on the output stream.
    Runs the Kokoro pipeline for `text` and returns the full utterance as
    16-bit mono PCM bytes at KOKORO_SAMPLE_RATE.

    This is what makes real pipelining possible: speech_pipeline.py's
    TTSProducerThread calls this to generate audio for chunk N+1 WHILE
    AudioPlayerThread is still playing chunk N — synthesis and playback
    are two fully independent steps now, instead of one function
    (_play_kokoro, below) doing both back-to-back for a single utterance.
    """
    pipeline = _get_pipeline()
    parts = []
    generator = pipeline(text, voice=voice, speed=KOKORO_SPEED)
    for _graphemes, _phonemes, audio in generator:
        audio_np = np.asarray(audio, dtype=np.float32)
        parts.append((np.clip(audio_np, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes())
    return b"".join(parts)


def _play_kokoro(text: str, voice: str, volume_cb, finished_cb, should_stop=None):
    should_stop = should_stop or (lambda: False)

    if not text or not text.strip():
        finished_cb()
        return

    try:
        pipeline = _get_pipeline()
    except Exception as e:
        print(f"[TTS Load Error] {e}")
        finished_cb()
        return

    try:
        stream = _get_output_stream()
    except Exception as e:
        print(f"[TTS Playback Error] {e}")
        finished_cb()
        return

    try:
        generator = pipeline(text, voice=voice, speed=KOKORO_SPEED)
        for _graphemes, _phonemes, audio in generator:
            if should_stop():
                break

            # audio comes back as a float32 array (torch tensor or numpy)
            # in roughly [-1, 1] — convert once per chunk to 16-bit PCM.
            audio_np = np.asarray(audio, dtype=np.float32)
            pcm_bytes = (np.clip(audio_np, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()

            offset = 0
            total  = len(pcm_bytes)
            chunk  = 1024 * 2  # bytes per sub-chunk, for smooth volume updates

            while offset < total:
                if should_stop():
                    break
                end    = min(offset + chunk, total)
                data   = pcm_bytes[offset:end]
                offset += len(data)
                count  = len(data) // 2
                if count > 0:
                    shorts = struct.unpack(f"<{count}h", data)
                    rms    = math.sqrt(sum(s * s for s in shorts) / count)
                    volume_cb(int(min(100, (rms / 32768.0) * 420)))
                stream.write(data)

            if should_stop():
                break
    except Exception as e:
        print(f"[TTS Generation Error] {e}")
        # The shared stream may be in a bad state after a real playback
        # error — drop it so the next call rebuilds a fresh one instead of
        # repeatedly failing on a broken stream.
        global _pa_stream
        with _pa_lock:
            _pa_stream = None

    volume_cb(0)
    finished_cb()


# ══════════════════════════════════════════════════════════════════════════════
# AUDIO THREAD
# ══════════════════════════════════════════════════════════════════════════════

class AudioThread(QThread):
    wake_word_detected = pyqtSignal()
    command_recognized = pyqtSignal(str)
    # Fired when a stop word is heard WHILE she's speaking (paused=True) —
    # kept separate from command_recognized so main.py can react instantly
    # without it being routed through normal command/intent handling.
    stop_requested      = pyqtSignal()

    # ── Tuned recognizer parameters ─────────────────────────────────────────
    # Why each of these changed from the old fixed values:
    #
    # pause_threshold (0.6 → 0.5): how long a silence has to last before the
    #   recognizer decides you've stopped talking and cuts the recording.
    #   0.6s added a small but noticeable extra delay after every utterance
    #   before Stage 2 (command listening) could even start — 0.5s is still
    #   comfortably long enough to not clip a real sentence early, just
    #   trims the dead time.
    # non_speaking_duration: must be <= pause_threshold. Lower value here
    #   means the recognizer commits to "silence" faster once you've
    #   actually stopped, rather than a lot fixed at the pause_threshold
    #   itself with no lead-in.
    # dynamic_energy_ratio (default 1.5 → 1.3): how much louder than the
    #   ambient noise floor speech needs to be before it's treated as
    #   speech at all. 1.5 is tuned for a quiet room; in a noisy hostel/fan
    #   environment the ambient floor itself is already elevated, so
    #   requiring speech to be 1.5x THAT on top is what was causing missed
    #   wake attempts — 1.3x still filters real noise but triggers on a
    #   normal speaking voice more reliably against a loud floor.
    PAUSE_THRESHOLD          = 0.5
    NON_SPEAKING_DURATION    = 0.3
    DYNAMIC_ENERGY_RATIO     = 1.3
    AMBIENT_CALIBRATION_SECS = 1.5   # up from 1.0 — a touch more data = a
                                       # steadier initial noise-floor read
    RECALIBRATE_EVERY_SECS   = 25    # periodic re-read of the noise floor
                                       # so a fan/AC turning on mid-session
                                       # doesn't leave the threshold stale
    WAKE_PHRASE_TIME_LIMIT   = 3      # "hey sanju" is short — down from 4,
                                       # trims worst-case per-attempt latency

    def __init__(self):
        super().__init__()
        self.listening_for_command = False
        self.paused                = False
        self._running              = True

    def stop(self):
        self._running = False

    def run(self):
        import time as _time

        recognizer = sr.Recognizer()
        recognizer.dynamic_energy_threshold        = True
        recognizer.dynamic_energy_ratio             = self.DYNAMIC_ENERGY_RATIO
        recognizer.pause_threshold                  = self.PAUSE_THRESHOLD
        recognizer.non_speaking_duration            = self.NON_SPEAKING_DURATION

        with sr.Microphone() as source:
            print("[Mic] Calibrating ambient noise…")
            recognizer.adjust_for_ambient_noise(source, duration=self.AMBIENT_CALIBRATION_SECS)
            print(f"[Mic] Initial energy threshold: {recognizer.energy_threshold:.0f}")
            print("🎤  Audio Engine Online. Say 'Hey Sanju' to wake up…")

            last_recalibration = _time.monotonic()

            while self._running:
                # Periodic recalibration — only while idly waiting for the
                # wake word (never mid-command or mid-speech), so a slowly
                # changing noise floor (fan, AC, hostel hallway traffic)
                # doesn't leave the threshold stuck at whatever it was when
                # the app launched.
                if (not self.paused and not self.listening_for_command
                        and _time.monotonic() - last_recalibration > self.RECALIBRATE_EVERY_SECS):
                    try:
                        recognizer.adjust_for_ambient_noise(source, duration=0.5)
                        print(f"[Mic] Recalibrated — energy threshold now {recognizer.energy_threshold:.0f}")
                    except Exception:
                        pass
                    last_recalibration = _time.monotonic()

                if self.paused:
                    # She's speaking right now. Rather than going fully
                    # deaf, keep a short, low-overhead ear open just for a
                    # stop word — lets the user interrupt her mid-sentence.
                    # Only stop-phrases do anything here (see _is_stop_word),
                    # which keeps false triggers unlikely even without echo
                    # cancellation on the mic input.
                    try:
                        audio = recognizer.listen(source, timeout=1,
                                                  phrase_time_limit=3)
                        text  = recognizer.recognize_google(audio).lower()
                        if _is_stop_word(text):
                            print(f"[Audio] Barge-in stop: '{text}'")
                            self.stop_requested.emit()
                    except (sr.WaitTimeoutError, sr.UnknownValueError):
                        pass
                    except Exception:
                        pass
                    continue

                if not self.listening_for_command:
                    # Stage 1 — wake word
                    print("[Mic] Listening...")
                    try:
                        audio = recognizer.listen(source, timeout=1,
                                                  phrase_time_limit=self.WAKE_PHRASE_TIME_LIMIT)
                        text  = recognizer.recognize_google(audio).lower()
                        print(f"[Mic] Heard: {text}")
                        confidence = _wake_confidence(text)
                        print(f"[Wake] Fuzzy Match: {confidence:.2f}")
                        if confidence >= _WAKE_CONFIDENCE_THRESHOLD:
                            print("[Wake] Activated")
                            self.wake_word_detected.emit()
                            self.listening_for_command = True
                    except sr.WaitTimeoutError:
                        pass
                    except sr.UnknownValueError:
                        # Google couldn't transcribe anything at all — most
                        # often a transient noise (keyboard click, chair
                        # creak, a door) rather than a failed wake attempt,
                        # since real speech almost always yields SOME text.
                        pass
                    except Exception as e:
                        print(f"[Mic Error] {e}")

                else:
                    # Stage 2 — command
                    try:
                        audio = recognizer.listen(source, timeout=10,
                                                  phrase_time_limit=20)
                        text  = recognizer.recognize_google(audio)
                        print(f"[Mic] Heard command: {text}")
                        self.command_recognized.emit(text)
                    except sr.WaitTimeoutError:
                        self.command_recognized.emit("")
                    except sr.UnknownValueError:
                        self.command_recognized.emit("")
                    except Exception as e:
                        print(f"[Audio Error] {e}")
                        self.command_recognized.emit("")
                    finally:
                        self.listening_for_command = False


# ══════════════════════════════════════════════════════════════════════════════
# PORCUPINE WAKE WORD (recommended upgrade — optional, drop-in alternative)
# ══════════════════════════════════════════════════════════════════════════════
#
# WHY THIS IS THE ACTUAL FIX FOR "I HAVE TO SAY IT MULTIPLE TIMES"
# -------------------------------------------------------------------------
# AudioThread (above) uses Google's cloud speech-to-text for wake-word
# detection too — it records up to WAKE_PHRASE_TIME_LIMIT seconds of audio,
# uploads it to Google, gets back a full transcription, THEN checks if that
# transcription resembles "sanju". That's fundamentally the wrong tool for
# the job: Google's STT is tuned for accurate DICTATION of arbitrary long
# speech, not for spotting one specific short phrase reliably and fast —
# and every single attempt pays a network round-trip before you even find
# out whether it heard you.
#
# Porcupine (by Picovoice) is a purpose-built wake-word engine: a small,
# on-device model trained SPECIFICALLY to recognize one phrase, running
# continuously on raw audio frames with no network call and no full
# transcription step at all. That's why it's simultaneously MORE reliable
# (dedicated acoustic model beats generic-dictation-plus-fuzzy-match) AND
# MUCH faster (no cloud round-trip — typically well under the 500ms target).
# It's also naturally more robust to fan/keyboard/background noise, since
# it's pattern-matching against a trained acoustic model rather than an
# energy-threshold voice-activity gate deciding whether to even attempt
# a cloud transcription.
#
# ── One-time setup you have to do (can't be scripted from here) ──────────────
# 1. pip install pvporcupine pyaudio
# 2. Sign up free at https://console.picovoice.ai — get an AccessKey.
# 3. In the console, use "Porcupine" → create a custom wake word: type
#    "Hey Sanju", pick your platform (e.g. Windows x86_64), and download
#    the generated .ppn keyword file. This training step happens on
#    Picovoice's servers — there's no way to generate this file locally
#    or from this codebase, it's a one-time manual step per platform.
# 4. Put the .ppn file in your project, e.g. wake_words/hey-sanju.ppn
# 5. Set the two constants below (PORCUPINE_ACCESS_KEY, PORCUPINE_KEYWORD_PATH)
#    — .env / os.getenv works fine here too, same pattern as OLLAMA_MODEL.
#
# ── How to switch to it ───────────────────────────────────────────────────
# In main.py:
#     from audio_engine import AudioThread          # old
#     from audio_engine import PorcupineAudioThread as AudioThread   # new
# That's the ONLY change needed — same signals (wake_word_detected,
# command_recognized, stop_requested), same .paused / .listening_for_command
# attributes, same threading model. Everything downstream (Stage 2 command
# capture, barge-in stop, main.py's whole command flow) is untouched —
# Porcupine only replaces WAKE detection; command transcription still uses
# Google STT via speech_recognition exactly as before.

import os as _os
PORCUPINE_ACCESS_KEY   = _os.getenv("PORCUPINE_ACCESS_KEY", "")
PORCUPINE_KEYWORD_PATH = _os.getenv("PORCUPINE_KEYWORD_PATH", "wake_words/hey-sanju.ppn")


class PorcupineAudioThread(QThread):
    """
    Drop-in replacement for AudioThread. Wake-word detection runs on
    Porcupine (on-device, frame-by-frame, no cloud call) instead of
    Google STT + fuzzy match. Once woken, Stage 2 (command capture) and
    the barge-in stop-word listener reuse the exact same
    speech_recognition-based approach as AudioThread, unchanged — only
    the wake mechanism itself is different.
    """

    wake_word_detected = pyqtSignal()
    command_recognized = pyqtSignal(str)
    stop_requested      = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.listening_for_command = False
        self.paused                = False
        self._running              = True

        if not _PORCUPINE_AVAILABLE:
            raise RuntimeError(
                "pvporcupine isn't installed — run `pip install pvporcupine`, "
                "or use AudioThread instead of PorcupineAudioThread."
            )
        if not PORCUPINE_ACCESS_KEY:
            raise RuntimeError(
                "PORCUPINE_ACCESS_KEY isn't set — get a free key at "
                "https://console.picovoice.ai and set it in your .env file."
            )

    def stop(self):
        self._running = False

    def run(self):
        try:
            porcupine = pvporcupine.create(
                access_key=PORCUPINE_ACCESS_KEY,
                keyword_paths=[PORCUPINE_KEYWORD_PATH],
            )
        except Exception as e:
            print(f"[Porcupine] Failed to load keyword model: {e}")
            print(f"[Porcupine] Expected file at: {PORCUPINE_KEYWORD_PATH}")
            return

        pa = pyaudio.PyAudio()
        pa_stream = pa.open(
            rate=porcupine.sample_rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=porcupine.frame_length,
        )

        # Stage 2 (command) and the barge-in stop listener reuse a normal
        # speech_recognition Recognizer — same tuned params as AudioThread.
        # NOTE: Porcupine's raw pa_stream and speech_recognition's own
        # Microphone stream can't both hold the input device open on every
        # OS/driver combination — pa_stream is paused (stop_stream) whenever
        # we need to use sr.Microphone for Stage 2 or the stop-word
        # listener, then resumed for Stage 1. If you hit a "device busy" /
        # "invalid stream" error on your system, this is the spot to look —
        # some backends need the stream fully closed and reopened instead
        # of just paused, which is a small latency trade-off worth testing.
        recognizer = sr.Recognizer()
        recognizer.dynamic_energy_threshold = True
        recognizer.dynamic_energy_ratio      = AudioThread.DYNAMIC_ENERGY_RATIO
        recognizer.pause_threshold           = AudioThread.PAUSE_THRESHOLD
        recognizer.non_speaking_duration      = AudioThread.NON_SPEAKING_DURATION
        _sr_calibrated = False

        print("[Porcupine] Wake engine online (on-device, no cloud call). "
              "Say 'Hey Sanju' to wake up…")

        try:
            while self._running:
                if self.paused or self.listening_for_command:
                    # Needs speech_recognition's Microphone — release
                    # Porcupine's stream for this stage (see NOTE above).
                    pa_stream.stop_stream()
                    with sr.Microphone() as sr_source:
                        if not _sr_calibrated:
                            recognizer.adjust_for_ambient_noise(sr_source, duration=1.5)
                            _sr_calibrated = True

                        if self.paused:
                            # Same barge-in stop-word listener as AudioThread.
                            try:
                                audio = recognizer.listen(sr_source, timeout=1, phrase_time_limit=3)
                                text  = recognizer.recognize_google(audio).lower()
                                if _is_stop_word(text):
                                    print(f"[Audio] Barge-in stop: '{text}'")
                                    self.stop_requested.emit()
                            except (sr.WaitTimeoutError, sr.UnknownValueError):
                                pass
                            except Exception:
                                pass
                        else:
                            # Stage 2 — command (unchanged from AudioThread).
                            try:
                                audio = recognizer.listen(sr_source, timeout=10, phrase_time_limit=20)
                                text  = recognizer.recognize_google(audio)
                                print(f"[Mic] Heard command: {text}")
                                self.command_recognized.emit(text)
                            except sr.WaitTimeoutError:
                                self.command_recognized.emit("")
                            except sr.UnknownValueError:
                                self.command_recognized.emit("")
                            except Exception as e:
                                print(f"[Audio Error] {e}")
                                self.command_recognized.emit("")
                            finally:
                                self.listening_for_command = False
                    pa_stream.start_stream()
                    continue

                # Stage 1 — Porcupine frame-by-frame wake detection.
                # pa_stream.read() blocks for exactly one frame's worth of
                # audio (~32ms at 16kHz/512 samples) — this is what keeps
                # latency low: no multi-second "listen for a phrase, then
                # transcribe" round trip at all.
                pcm = pa_stream.read(porcupine.frame_length, exception_on_overflow=False)
                pcm = struct.unpack_from("h" * porcupine.frame_length, pcm)
                keyword_index = porcupine.process(pcm)
                if keyword_index >= 0:
                    print("[Wake] Activated (Porcupine)")
                    self.wake_word_detected.emit()
                    self.listening_for_command = True
        finally:
            pa_stream.stop_stream()
            pa_stream.close()
            pa.terminate()
            porcupine.delete()


# ══════════════════════════════════════════════════════════════════════════════
# TTS THREAD
# ══════════════════════════════════════════════════════════════════════════════

class TTSThread(QThread):
    finished       = pyqtSignal()
    volume_changed = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.text_to_speak = ""
        self._stop_flag     = False

    def speak(self, text: str):
        self.text_to_speak = text
        self._stop_flag     = False
        self.start()

    def request_stop(self):
        """Called from main.py when the user says a stop word — cuts
        playback off within roughly one audio chunk (~a few ms)."""
        self._stop_flag = True

    def run(self):
        _play_kokoro(
            self.text_to_speak, KOKORO_VOICE,
            lambda vol: self.volume_changed.emit(vol),
            lambda:     self.finished.emit(),
            lambda:     self._stop_flag,
        )


# Warm the Kokoro model the moment this module is imported (main.py imports
# it at startup), instead of lazily on the first spoken reply.
preload_kokoro_async()