"""
tts.py — Kokoro text-to-speech worker thread.

Runs speech synthesis + playback off the Qt main thread, and derives real
lip-sync data instead of just an amplitude pulse:

Kokoro's pipeline() generator yields (graphemes, phonemes, audio) per
segment — the phonemes it actually used to synthesize that exact audio are
already known, for free, with zero recognition error and zero extra
latency. So rather than analyzing the output audio (e.g. with a local
speech/phoneme recognizer), this classifies Kokoro's own phoneme string
into the 5 standard VRM lip-sync viseme presets (aa/ih/ee/oh/ou) and
streams that alongside the audio.
"""

import numpy as np
import sounddevice as sd
from kokoro import KPipeline

from PyQt6.QtCore import QThread, pyqtSignal


# ══════════════════════════════════════════════════════════════════════════
# Phoneme → VRM viseme classification
# ══════════════════════════════════════════════════════════════════════════
# Kokoro (via the misaki phonemizer) emits IPA-ish symbols plus a few
# uppercase shorthand letters for diphthongs (A ~ /eɪ/, I ~ /aɪ/, O ~ /oʊ/,
# W ~ /aʊ/). This is a best-effort reduction of that symbol set down to 5
# mouth shapes — not phonetically perfect, but far more accurate than a
# single amplitude-driven "aa" pulse, and it costs nothing extra since the
# phonemes are already computed by the TTS engine.
_PHONEME_VISEME_MAP = {
    # open / "ah" family
    'a': 'aa', 'ɑ': 'aa', 'ʌ': 'aa', 'æ': 'aa', 'ə': 'aa', 'ɐ': 'aa',
    'A': 'aa', 'I': 'aa',
    # close front — "ih"
    'ɪ': 'ih', 'ɨ': 'ih', 'y': 'ih',
    # mid front — "ee"
    'i': 'ee', 'e': 'ee', 'ɛ': 'ee', 'ɜ': 'ee', 'ɝ': 'ee',
    # mid/back rounded — "oh"
    'o': 'oh', 'ɔ': 'oh', 'O': 'oh',
    # close back rounded — "ou"
    'u': 'ou', 'ʊ': 'ou', 'w': 'ou', 'W': 'ou',
}
# Stress/length marks carry no sound of their own — stripped before
# dividing the phoneme string into equal time slices.
_PHONEME_SKIP_CHARS = set("ˈˌːˑ' \t\n")


def clean_phonemes(ps: str) -> str:
    return "".join(ch for ch in (ps or "") if ch not in _PHONEME_SKIP_CHARS)


def phoneme_to_viseme(ch: str) -> str:
    """Consonants and anything unrecognized fall back to '' (relaxed/
    closed mouth), which is a reasonable approximation for stops and
    fricatives anyway."""
    return _PHONEME_VISEME_MAP.get(ch, '')


class KokoroTTSWorker(QThread):
    speech_started = pyqtSignal()
    speech_finished = pyqtSignal()
    volume_updated = pyqtSignal(int)          # 0..100
    viseme_updated = pyqtSignal(str, float)    # (viseme name or '', weight 0..1)
    error_occurred = pyqtSignal(str)

    SAMPLE_RATE = 24000
    CHUNK_SIZE = 1024  # ~42.7ms per chunk at 24kHz — small enough for smooth lip sync

    def __init__(self, text: str, voice: str = "af_heart", lang_code: str = "a"):
        super().__init__()
        self.text = text
        self.voice = voice
        self.lang_code = lang_code
        self._is_running = True
        self._stream = None

    def run(self):
        try:
            pipeline = KPipeline(lang_code=self.lang_code)
            generator = pipeline(self.text, voice=self.voice, speed=1.0)
        except Exception as e:
            self.error_occurred.emit(f"Failed to start Kokoro pipeline: {e}")
            self.speech_finished.emit()
            return

        try:
            self.speech_started.emit()

            # A single persistent OutputStream, written to one chunk at a
            # time. stream.write() blocks until the device has actually
            # consumed that chunk, so emitting volume/viseme immediately
            # before each write keeps the mouth locked to real playback
            # time (rather than computing everything up front and playing
            # the whole utterance afterward, which desyncs lip sync
            # completely).
            self._stream = sd.OutputStream(
                samplerate=self.SAMPLE_RATE, channels=1, dtype="float32"
            )
            self._stream.start()

            for gs, ps, audio in generator:
                if not self._is_running:
                    break

                if hasattr(audio, "numpy"):
                    audio_data = audio.numpy().astype(np.float32)
                else:
                    audio_data = np.array(audio, dtype=np.float32)

                # Spread this segment's phoneme string uniformly across its
                # real audio duration. Not forced-aligned (individual
                # phoneme durations aren't known), but synced to the
                # correct audio and dominated by vowels anyway, which
                # carry almost all of the perceived mouth shape.
                cleaned = clean_phonemes(ps)
                n_phonemes = len(cleaned)
                seg_duration = len(audio_data) / self.SAMPLE_RATE if self.SAMPLE_RATE else 0

                for i in range(0, len(audio_data), self.CHUNK_SIZE):
                    if not self._is_running:
                        break
                    chunk = audio_data[i:i + self.CHUNK_SIZE]
                    if len(chunk) == 0:
                        continue

                    rms = np.sqrt(np.mean(chunk ** 2))
                    volume = int(np.clip(rms * 400, 0, 100))
                    # Raw rms*4.0 keeps most normal speech well under full
                    # mouth-open weight. A sqrt curve lifts quieter/softer
                    # sounds up disproportionately (perceived mouth
                    # openness isn't linear with amplitude), and the
                    # steeper 8.0 multiplier lets normal speaking volume
                    # actually reach a fully open mouth instead of just a
                    # small twitch.
                    weight = float(np.clip(np.sqrt(rms) * 2.8, 0.0, 1.0))

                    if n_phonemes > 0 and seg_duration > 0:
                        t_in_segment = i / self.SAMPLE_RATE
                        idx = min(n_phonemes - 1, int((t_in_segment / seg_duration) * n_phonemes))
                        viseme = phoneme_to_viseme(cleaned[idx])
                    else:
                        viseme = ''

                    self.volume_updated.emit(volume)
                    self.viseme_updated.emit(viseme, weight)

                    if len(chunk) < self.CHUNK_SIZE:
                        chunk = np.pad(chunk, (0, self.CHUNK_SIZE - len(chunk)))
                    self._stream.write(chunk.reshape(-1, 1))

        except Exception as e:
            self.error_occurred.emit(f"TTS playback error: {e}")
        finally:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            self.volume_updated.emit(0)
            self.viseme_updated.emit('', 0.0)
            self.speech_finished.emit()

    def stop(self):
        self._is_running = False
        if self._stream is not None:
            try:
                self._stream.abort()
            except Exception:
                pass
        try:
            sd.stop()
        except Exception:
            pass