"""
speech_pipeline.py — Sanju AI
Producer/consumer speech pipeline for streamed AI conversation replies.

    ai_engine.py's AIThread streams from Ollama and batches sentences
        ↓  (sentence_ready signal, unchanged)
    TEXT QUEUE                                          (this module)
        ↓
    TTSProducerThread   — synthesizes audio for each chunk, continuously,
                           fully decoupled from playback
        ↓
    AUDIO QUEUE (ready-to-play PCM bytes)                (this module)
        ↓
    AudioPlayerThread   — plays chunks back-to-back on the shared output
                           stream, consuming the queue continuously

WHY THIS ELIMINATES THE PAUSES
-------------------------------
The old design spawned one TTSThread per sentence-chunk, and the NEXT
chunk's synthesis only started after the PREVIOUS chunk finished PLAYING
(main.py's _play_next_queued() created a new thread inside the previous
one's `finished` callback). That strict "play, THEN synthesize next, THEN
play" chain is exactly what produced the dead-air gap between chunks —
Kokoro's synthesis time (real, non-zero) was sitting in the critical path
between every pair of chunks.

Here, TTSProducerThread runs continuously in the background, synthesizing
chunk N+1 (and N+2, ...) WHILE AudioPlayerThread is still playing chunk N.
As long as synthesis keeps pace with playback (true in practice — Kokoro
generates faster than realtime on CPU for short chunks), the audio queue
rarely runs dry, so the player can go straight from one chunk to the next
with no wait. Synthesis is no longer in the playback critical path at all
except for the very first chunk of a reply.

Both threads are created ONCE and reused for the app's entire session —
blocking on their queues between turns rather than being spawned and torn
down per utterance, so there's no thread-startup latency on every reply
either.

Thread safety: queue.Queue is used for both queues (thread-safe by
design); each thread's own threading.Event guards barge-in stop state.
"""

import math
import queue
import struct
import threading
import time

from PyQt6.QtCore import QThread, pyqtSignal

import audio_engine


# Sentinel marking "no more chunks are coming for THIS turn" — lets the
# player know a turn has genuinely ended (vs. just being between chunks,
# still in-turn, waiting on the producer to catch up).
_END_OF_TURN = object()
# Sentinel telling a thread to shut down entirely (app exit).
_SHUTDOWN = object()


class TTSProducerThread(QThread):
    """
    Pulls text chunks off `text_queue`, synthesizes each one via
    audio_engine.synthesize_kokoro(), and pushes the resulting PCM bytes
    onto `audio_queue`. Runs for the app's whole lifetime, blocking on
    text_queue.get() between chunks/turns rather than being recreated —
    reuses the same (already-loaded) Kokoro pipeline object every time,
    so there's no per-chunk model-load cost.
    """

    def __init__(self, text_queue: "queue.Queue", audio_queue: "queue.Queue"):
        super().__init__()
        self.text_queue  = text_queue
        self.audio_queue = audio_queue
        self._stop_event = threading.Event()

    def request_stop_current_turn(self):
        """Barge-in: stop synthesizing chunks for the CURRENT turn. Chunks
        already queued in text_queue get drained (not spoken); the thread
        itself keeps running, ready for the next turn."""
        self._stop_event.set()

    def run(self):
        while True:
            item = self.text_queue.get()

            if item is _SHUTDOWN:
                return

            if item is _END_OF_TURN:
                # All real chunks for this turn are already either in
                # audio_queue or dropped (if barge-in'd) — forward the
                # marker so the player knows the turn is truly complete,
                # then reset for the next one.
                self._stop_event.clear()
                self.audio_queue.put(_END_OF_TURN)
                continue

            if self._stop_event.is_set():
                # Mid-barge-in — drop chunks from the interrupted turn
                # instead of wastefully synthesizing audio nobody will
                # hear, but keep consuming so nothing lingers.
                continue

            text = item
            t0 = time.monotonic()
            try:
                pcm = audio_engine.synthesize_kokoro(text)
            except Exception as e:
                print(f"[TTS Producer] Synthesis error: {e}")
                continue
            dt_ms = (time.monotonic() - t0) * 1000
            print(f"[TTS Producer] Generated chunk ({dt_ms:.0f}ms): {text[:60]!r}")

            if self._stop_event.is_set():
                continue  # went stale while synthesizing — drop it

            self.audio_queue.put((text, pcm))
            print(f"[Audio Queue] Size: {self.audio_queue.qsize()}")


class AudioPlayerThread(QThread):
    """
    Continuously consumes (text, pcm_bytes) off `audio_queue` and plays
    them back-to-back on the shared persistent output stream
    (audio_engine.get_shared_output_stream() — the same stream used
    elsewhere, so no extra open/close overhead is introduced). Because
    TTSProducerThread is generating chunk N+1 while THIS thread plays
    chunk N, the next chunk is very often already sitting in the queue
    by the time the current one finishes.
    """

    volume_changed = pyqtSignal(int)
    chunk_started   = pyqtSignal(str)   # chunk text — available for UI use
    turn_finished   = pyqtSignal()      # whole reply has now been spoken

    def __init__(self, audio_queue: "queue.Queue"):
        super().__init__()
        self.audio_queue = audio_queue
        self._stop_event = threading.Event()

    def request_stop_current_turn(self):
        """Barge-in: cuts the CURRENTLY playing chunk off mid-audio and
        drops anything else queued for this turn."""
        self._stop_event.set()

    def run(self):
        while True:
            item = self.audio_queue.get()

            if item is _SHUTDOWN:
                return

            if item is _END_OF_TURN:
                self._stop_event.clear()
                self.turn_finished.emit()
                continue

            if self._stop_event.is_set():
                continue  # draining a barge-in'd turn — skip playback

            text, pcm = item
            print(f"[Player] Playing chunk: {text[:60]!r}")
            self.chunk_started.emit(text)

            try:
                stream = audio_engine.get_shared_output_stream()
            except Exception as e:
                print(f"[Player] Stream error: {e}")
                continue

            offset = 0
            total  = len(pcm)
            block  = 1024 * 2   # bytes per sub-chunk, for smooth volume updates
            while offset < total:
                if self._stop_event.is_set():
                    break
                end    = min(offset + block, total)
                data   = pcm[offset:end]
                offset += len(data)
                count  = len(data) // 2
                if count > 0:
                    shorts = struct.unpack(f"<{count}h", data)
                    rms    = math.sqrt(sum(s * s for s in shorts) / count)
                    self.volume_changed.emit(int(min(100, (rms / 32768.0) * 420)))
                stream.write(data)

            self.volume_changed.emit(0)
            print("[Player] Finished chunk")


class SpeechPipeline:
    """
    Public API used by main.py. Owns ONE producer + ONE player thread for
    the app's whole session — created here, started once, reused for
    every conversational turn (no per-utterance thread spin-up cost).
    """

    def __init__(self):
        self.text_queue  = queue.Queue()
        self.audio_queue = queue.Queue()
        self.producer = TTSProducerThread(self.text_queue, self.audio_queue)
        self.player   = AudioPlayerThread(self.audio_queue)
        self.producer.start()
        self.player.start()

    # ── main.py-facing API ──────────────────────────────────────────────
    def speak_chunk(self, text: str):
        """Queue one batched sentence-chunk (from AIThread's sentence_ready)
        to be synthesized and spoken as soon as its turn comes up."""
        if text and text.strip():
            self.text_queue.put(text.strip())

    def end_of_turn(self):
        """Call once the whole reply has finished generating (AIThread's
        response_ready) — signals no more chunks are coming for this turn,
        so turn_finished fires once everything queued has actually played."""
        self.text_queue.put(_END_OF_TURN)

    def stop_current_turn(self):
        """Barge-in: cut off whatever's playing/queued immediately."""
        self.producer.request_stop_current_turn()
        self.player.request_stop_current_turn()
        for q in (self.text_queue, self.audio_queue):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass

    def shutdown(self):
        """Call on app exit for a clean thread teardown (optional — daemon
        behavior aside, this avoids the process hanging on QThread.wait())."""
        self.text_queue.put(_SHUTDOWN)
        self.audio_queue.put(_SHUTDOWN)
        self.producer.wait(2000)
        self.player.wait(2000)


# Lazy singleton — created explicitly by main.py during its normal setup
# (same point AudioThread() is created), NOT at raw module-import time.
# QThreads that use pyqtSignal need a QApplication to already exist for
# their cross-thread signal delivery to work correctly, and main.py
# imports this module before QApplication(sys.argv) has necessarily run —
# get_pipeline() defers creation until main.py explicitly asks for it.
_pipeline_instance: "SpeechPipeline | None" = None


def get_pipeline() -> "SpeechPipeline":
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = SpeechPipeline()
    return _pipeline_instance

