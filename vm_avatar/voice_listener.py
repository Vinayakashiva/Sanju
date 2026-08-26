"""
voice_listener.py — Background microphone listener with wake-word gating.

Only forwards a command after "Hey Sanju" (or a close mishearing of it) has
been heard, either in the same breath ("hey sanju dance") or as a
follow-up utterance within a short window afterward. Plain background
speech with no wake word is ignored.

Also supports pause()/resume(): the assistant calls pause() right before
speaking and resume() shortly after, so the microphone can't pick up
Sanju's own voice coming out of the speakers and misinterpret it as a new
command — a real, easy-to-hit bug in any always-listening voice app that
plays audio through the same room the mic is listening to.
"""

import time

import speech_recognition as sr
from PyQt6.QtCore import QThread, pyqtSignal

WAKE_PHRASES = ("hey sanju", "hay sanju", "hey sanjo", "hey sanjoo", "a sanju")
COMMAND_WINDOW_SECONDS = 6


class VoiceListenerWorker(QThread):
    wake_word_detected = pyqtSignal()
    command_detected = pyqtSignal(str)
    listener_error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.recognizer = sr.Recognizer()
        self.recognizer.energy_threshold = 300
        self.recognizer.dynamic_energy_threshold = True
        self._is_listening = True
        self._paused = False
        self._awaiting_command_until = 0.0

    def run(self):
        try:
            mic = sr.Microphone()
        except Exception as e:
            self.listener_error.emit(f"Microphone unavailable: {e}")
            return

        try:
            with mic as source:
                try:
                    self.recognizer.adjust_for_ambient_noise(source, duration=1)
                except Exception as e:
                    self.listener_error.emit(f"Ambient noise calibration failed: {e}")

                print("[VoiceListener] Listening for wake word 'Hey Sanju'...")

                while self._is_listening:
                    if self._paused:
                        # Don't even attempt to listen while Sanju is
                        # talking — her own voice through the speakers
                        # could otherwise be captured and misheard as a
                        # command (e.g. as "stop"), stepping on whatever
                        # she was just told to do.
                        self.msleep(100)
                        continue

                    try:
                        audio = self.recognizer.listen(source, timeout=3, phrase_time_limit=5)
                        if self._paused:
                            # She started speaking partway through this
                            # listen() call — the captured audio may well
                            # be her own voice, so drop it.
                            continue
                        text = self.recognizer.recognize_google(audio).lower().strip()
                    except sr.WaitTimeoutError:
                        continue
                    except sr.UnknownValueError:
                        continue
                    except sr.RequestError as e:
                        self.listener_error.emit(f"Speech recognition service error: {e}")
                        self.msleep(1000)
                        continue
                    except Exception as e:
                        self.listener_error.emit(f"Listener error: {e}")
                        continue

                    if self._paused or not text:
                        continue

                    print(f"[VoiceListener] Heard: '{text}'")
                    self._route(text)

        except Exception as e:
            self.listener_error.emit(f"Microphone stream error: {e}")

    def _route(self, text: str):
        awaiting_command = time.monotonic() < self._awaiting_command_until
        wake_hit, remainder = self._strip_wake_word(text)

        if wake_hit:
            self.wake_word_detected.emit()
            if remainder:
                # "hey sanju dance" — command said in the same breath.
                self.command_detected.emit(remainder)
                self._awaiting_command_until = 0.0
            else:
                # Give a short window for a follow-up command.
                self._awaiting_command_until = time.monotonic() + COMMAND_WINDOW_SECONDS
        elif awaiting_command:
            self.command_detected.emit(text)
            self._awaiting_command_until = 0.0
        # else: ordinary background speech with no wake word — ignored.

    @staticmethod
    def _strip_wake_word(text: str):
        for phrase in WAKE_PHRASES:
            if phrase in text:
                remainder = text.split(phrase, 1)[1].strip()
                return True, remainder
        return False, ""

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self._is_listening = False
