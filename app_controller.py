"""
app_controller.py — Sanju AI
Full controller: apps, WhatsApp, Spotify (auto-picked or named songs), Volume, Screenshot, Sleep Timer, Mood Music.
"""

import os
import re
import subprocess
import sys
import time
import random
import winsound
import ctypes
import threading

import pyautogui
import pygetwindow as gw
import pyperclip

# ── Real playback confirmation (pycaw) ──────────────────────────────────────
# "The reply text says 'Playing X for you'" is NOT the same thing as "Spotify
# is actually making sound" — the search/Enter keystroke sequence in
# _spotify_play can land on the wrong control, or Spotify can be mid-buffer,
# and the automation still returns a normal success string. pycaw lets us
# check the real per-process audio peak meter for Spotify.exe so we only
# treat playback as confirmed once it's actually outputting sound — this is
# what the dance flow (and minimizing the window afterward) waits on.
try:
    from pycaw.pycaw import AudioUtilities
    _PYCAW_AVAILABLE = True
except ImportError:
    _PYCAW_AVAILABLE = False
    print("[Spotify] pycaw not installed — falling back to a fixed delay "
          "instead of a real playback check. Run: pip install pycaw")

pyautogui.FAILSAFE = False
pyautogui.PAUSE    = 0.03


# ══════════════════════════════════════════════════════════════════════════════
# AUTO-DETECT PYCHARM EXE PATH
# ══════════════════════════════════════════════════════════════════════════════
def _find_pycharm() -> str:
    import shutil
    jetbrains = r"C:\Program Files\JetBrains"
    if os.path.isdir(jetbrains):
        for folder in sorted(os.listdir(jetbrains), reverse=True):
            if "pycharm" in folder.lower():
                exe = os.path.join(jetbrains, folder, "bin", "pycharm64.exe")
                if os.path.exists(exe):
                    return exe
    for cmd in ("pycharm", "pycharm64"):
        if shutil.which(cmd):
            return cmd
    return "pycharm"

PYCHARM_EXE = _find_pycharm()


def _resolve_exe(candidates: list[str], fallback: str) -> str:
    """
    Try each candidate full path (after expanding %ENV% vars) and return the
    first one that actually exists on THIS machine. If none exist, fall back
    to the bare command name so Windows/PATH/App-Paths can still try to
    resolve it. Fixes apps not opening on computers where the install
    location differs from the original dev machine.
    """
    for path in candidates:
        expanded = os.path.expandvars(path)
        if os.path.exists(expanded):
            return expanded
    return fallback


_CHROME_EXE = _resolve_exe([
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
], "chrome")

_EDGE_EXE = _resolve_exe([
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
], "msedge")

_FIREFOX_EXE = _resolve_exe([
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
], "firefox")

_BRAVE_EXE = _resolve_exe([
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
], "brave")

# --profile-directory="Default" makes Chrome/Edge/Brave skip the account
# picker screen when multiple Chrome profiles/accounts are signed in, and
# always open straight into a normal browser window — which is what the
# window-focus and search-typing logic below expects to see.
_CHROME_PROFILE_FLAG = ' --profile-directory="Default"'


# ══════════════════════════════════════════════════════════════════════════════
# APP REGISTRY
# ══════════════════════════════════════════════════════════════════════════════
APP_REGISTRY = {
    "chrome":          {"cmd": f'"{_CHROME_EXE}"{_CHROME_PROFILE_FLAG}',  "window": "Google Chrome",    "type": "browser"},
    "edge":            {"cmd": f'"{_EDGE_EXE}"{_CHROME_PROFILE_FLAG}',    "window": "Microsoft Edge",   "type": "browser"},
    "firefox":         {"cmd": f'"{_FIREFOX_EXE}"',                       "window": "Mozilla Firefox",  "type": "browser"},
    "brave":           {"cmd": f'"{_BRAVE_EXE}"{_CHROME_PROFILE_FLAG}',  "window": "Brave",            "type": "browser"},
    "microsoft store": {"cmd": "start ms-windows-store:",                                        "window": "Microsoft Store",  "type": "store"},
    "store":           {"cmd": "start ms-windows-store:",                                        "window": "Microsoft Store",  "type": "store"},
    "pycharm":         {"cmd": PYCHARM_EXE,                                                      "window": "PyCharm",          "type": "pycharm"},
    "file explorer":   {"cmd": "explorer",  "window": "File Explorer",    "type": "generic"},
    "calculator":      {"cmd": "calc",      "window": "Calculator",       "type": "generic"},
    "task manager":    {"cmd": "taskmgr",   "window": "Task Manager",     "type": "generic"},
    "paint":           {"cmd": "mspaint",   "window": "Paint",            "type": "generic"},
    "cmd":             {"cmd": "cmd",       "window": "Command Prompt",   "type": "generic"},
    "terminal":        {"cmd": "wt",        "window": "Windows Terminal", "type": "generic"},
    "notepad":         {"cmd": "notepad",   "window": "Notepad",          "type": "editor"},
    "spotify":         {"cmd": "start spotify:", "window": "Spotify",     "type": "generic"},
    "discord":         {"cmd": r"%LOCALAPPDATA%\Discord\Update.exe --processStart Discord.exe", "window": "Discord", "type": "generic"},
    "whatsapp":        {"cmd": "start whatsapp:", "window": "WhatsApp",   "type": "whatsapp"},
    "telegram":        {"cmd": r"%APPDATA%\Telegram Desktop\Telegram.exe","window": "Telegram", "type": "generic"},
    "zoom":            {"cmd": r"%APPDATA%\Zoom\bin\Zoom.exe",            "window": "Zoom",      "type": "generic"},
    "word":            {"cmd": "winword",   "window": "Word",             "type": "generic"},
    "excel":           {"cmd": "excel",     "window": "Excel",            "type": "generic"},
    "powerpoint":      {"cmd": "powerpnt",  "window": "PowerPoint",       "type": "generic"},
}


# ══════════════════════════════════════════════════════════════════════════════
# SPEECH RECOGNITION ALIASES
# ══════════════════════════════════════════════════════════════════════════════
STT_ALIASES = {
    "python": "pycharm", "pi charm": "pycharm", "pi charms": "pycharm",
    "pycharms": "pycharm", "pie charm": "pycharm", "pie charms": "pycharm",
    "pychar": "pycharm", "pi char": "pycharm", "pi charge": "pycharm",
    "python charm": "pycharm", "python charms": "pycharm",
    "crome": "chrome", "chrom": "chrome",
    "microsoft store": "microsoft store", "micro store": "microsoft store",
    "my crosoft store": "microsoft store", "app store": "microsoft store",
    "windows store": "microsoft store",
}

def _normalise_app_name(name: str) -> str:
    n = name.lower().strip()
    if n in STT_ALIASES: return STT_ALIASES[n]
    for alias, correct in STT_ALIASES.items():
        if alias in n: return n.replace(alias, correct)
    return n


# ══════════════════════════════════════════════════════════════════════════════
# INTENT PARSER
# ══════════════════════════════════════════════════════════════════════════════
def _clean_name(raw: str) -> str:
    STOP = {"message","a","the","please","now","to","and","or","for","with",
            "saying","that","send","type","write","whatsapp","hi","hello","hey"}
    words = [w for w in raw.strip().split() if w.lower() not in STOP]
    return " ".join(words[:2])


def _build_wa(name_raw: str, msg_raw: str):
    name = _clean_name(name_raw)
    msg  = (msg_raw or "").strip()
    if msg.lower() in ("message", "a message", "whatsapp message"):
        msg = ""
    if not name:
        return None
    if msg:
        return {"action": "whatsapp_message", "person": name, "payload": msg}
    return {"action": "whatsapp_open", "person": name}


def _parse_whatsapp_intent(t: str):
    t = t.strip().rstrip(".,!?")
    if not re.match(r"^(?:send|text|message|whatsapp|open\s+whatsapp)", t, re.I):
        return None
    m = re.match(r"^(?:send|text|message|whatsapp)\s+(?:(?:a\s+)?(?:whatsapp\s+)?message\s+)?(?:to\s+)?([\w ]+?)\s+(?:saying|that|saying that)\s+(.+)$", t, re.I)
    if m: return _build_wa(m.group(1), m.group(2))
    m = re.match(r"^send\s+(.{1,20}?)\s+to\s+([\w]+)$", t, re.I)
    if m: return _build_wa(m.group(2), m.group(1))
    m = re.match(r"^open\s+whatsapp\s+(?:and\s+)?send\s+(?:a\s+)?(?:whatsapp\s+)?message\s+to\s+([\w]+)\s+(.+)$", t, re.I)
    if m: return _build_wa(m.group(1), m.group(2))
    m = re.match(r"^send\s+(?:a\s+)?whatsapp\s+(?:message\s+)?to\s+([\w]+)\s+(.+)$", t, re.I)
    if m: return _build_wa(m.group(1), m.group(2))
    m = re.match(r"^(?:whatsapp|text|message)\s+to\s+([\w]+)\s+(.+)$", t, re.I)
    if m: return _build_wa(m.group(1), m.group(2))
    m = re.match(r"^send\s+whatsapp\s+to\s+([\w]+)\s+(.+)$", t, re.I)
    if m: return _build_wa(m.group(1), m.group(2))
    m = re.match(r"^(?:send|text|message|whatsapp)\s+(?:(?:a\s+)?(?:whatsapp\s+)?message\s+)?to\s+([\w]+)\s*$", t, re.I)
    if m: return _build_wa(m.group(1), "")
    m = re.match(r"^(?:send|text|message|whatsapp)\s+(?:(?:a\s+)?message\s+)?to\s+([\w]+)\s+(.{2,})$", t, re.I)
    if m: return _build_wa(m.group(1), m.group(2))
    NOT_NAMES = {"message","a","an","the","hi","hello","hey","my","send","please","now","me","to","whatsapp"}
    m = re.match(r"^(?:send|text|message|whatsapp)\s+(?:(?:a\s+)?(?:whatsapp\s+)?message\s+)?([\w]+)\s+(.{2,})$", t, re.I)
    if m and m.group(1).lower() not in NOT_NAMES:
        return _build_wa(m.group(1), m.group(2))
    m = re.match(r"^(?:open\s+)?(?:whatsapp|send\s+message\s+to|message\s+to|text\s+to)\s+([\w]+(?:\s+[\w]+)?)$", t, re.I)
    if m: return _build_wa(m.group(1), "")
    return None


def parse_intent(text: str) -> dict | None:
    t = _normalise_app_name(text.lower().strip().rstrip(".,!?"))

    # ── Open + search ──────────────────────────────────────────────────────────
    m = re.search(r"(?:open|launch|start)\s+(.+?)\s+and\s+search\s+(?:for\s+)?(.+)$", t)
    if m: return {"action": "open_and_search", "app": m.group(1).strip(), "payload": m.group(2).strip()}

    m = re.search(r"(?:open|launch|start)\s+(.+?)\s+and\s+(?:find|look for|look up)\s+(.+)$", t)
    if m: return {"action": "open_and_search", "app": m.group(1).strip(), "payload": m.group(2).strip()}

    # ── Open + code ────────────────────────────────────────────────────────────
    m = re.search(r"(?:open|launch|go to)\s+(.+?)\s+and\s+(?:write|code|create|type|help me (?:write|code))\s+(.+)$", t)
    if m: return {"action": "open_and_code", "app": m.group(1).strip(), "payload": m.group(2).strip()}

    m = re.search(r"help me (?:write|code|create)\s+(.+?)\s+(?:in|on|into|for)\s+(.+)$", t)
    if m: return {"action": "open_and_code", "app": m.group(2).strip(), "payload": m.group(1).strip()}

    m = re.search(r"(?:write|code|create)\s+(?:a\s+|an\s+)?(?:code\s+(?:for\s+)?)?(.+?)\s+(?:in|on|into)\s+(.+)$", t)
    if m: return {"action": "open_and_code", "app": m.group(2).strip(), "payload": m.group(1).strip()}

    # ── WhatsApp ───────────────────────────────────────────────────────────────
    wa = _parse_whatsapp_intent(t)
    if wa: return wa

    # ══════════════════════════════════════════════════════════════════════════
    # NEW: CLOSE APP
    # Phrases: "close chrome", "quit spotify", "exit notepad"
    # ══════════════════════════════════════════════════════════════════════════
    m = re.search(r"^(?:close|quit|exit)\s+(.+)$", t)
    if m: return {"action": "close_app", "app": m.group(1).strip()}

    # ══════════════════════════════════════════════════════════════════════════
    # NEW: MUSIC PAUSE / RESUME / SKIP (placed before the generic "play <song>"
    # catch-all below so phrases like "play next song" don't get treated as a
    # literal song name search)
    # ══════════════════════════════════════════════════════════════════════════
    if re.search(r"pause (?:the )?(?:music|song)", t):
        return {"action": "pause_music"}

    if re.search(r"(?:resume|unpause|continue)(?: the)? (?:music|song)", t):
        return {"action": "resume_music"}

    if re.search(r"(?:play (?:the )?next|next song|next track|skip(?: this)?(?: song| track)?)", t):
        return {"action": "next_track"}

    if re.search(r"(?:play (?:the )?previous|previous song|previous track|go back a song|last song)", t):
        return {"action": "previous_track"}

    # ══════════════════════════════════════════════════════════════════════════
    # NEW: SWITCH PERSONALITY MODE
    # Phrases: "switch to friend mode", "be my assistant now", "go to love mode"
    # ══════════════════════════════════════════════════════════════════════════
    m = re.search(r"(?:switch to|go to|be my|change to)\s+(love|friend|assistant)(?:\s+mode)?", t)
    if m: return {"action": "switch_mode", "mode": m.group(1).capitalize()}

    # ══════════════════════════════════════════════════════════════════════════
    # NEW: CLEAR MEMORY
    # Phrases: "clear your memory", "forget everything", "forget what we talked about"
    # ══════════════════════════════════════════════════════════════════════════
    if re.search(r"(?:clear your memory|forget everything|forget what we talked about|"
                 r"clear (?:our|the) (?:chat|conversation) history|wipe your memory)", t):
        return {"action": "clear_memory"}

    # ══════════════════════════════════════════════════════════════════════════
    # SING / AUTO-PICKED SONG INTENTS — all played through Spotify
    #
    # Supported phrases:
    #   "play music for me"         → Sanju picks any song herself, plays on Spotify
    #   "sing a song"               → Sanju picks any song herself, plays on Spotify
    #   "sing me a Kannada song"    → Sanju picks a Kannada song, plays on Spotify
    #   "play a Hindi song"         → Sanju picks a Hindi song, plays on Spotify
    #   "play Tum Hi Ho"            → searches that exact name on Spotify
    #   "play Tum Hi Ho hindi song" → searches that exact name on Spotify
    #   "I'm sad"                   → Sanju picks a comforting song, plays on Spotify
    #   "surprise me"               → Sanju picks any song herself, plays on Spotify
    #   "play X on spotify"         → explicit Spotify search for X
    # ══════════════════════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════════════════════
    # NEW: DANCE COMMAND — separate from plain "play a song": this starts
    # music AND signals main.py to keep her actually dancing (chaining
    # between her dance clips) for as long as that music keeps playing,
    # not just a single reaction animation. See main.py's "dance_command"
    # handler and ui.py's SING_STATES dance-chaining logic.
    # ══════════════════════════════════════════════════════════════════════════
    if re.search(r"(?:^dance$|let'?s dance|perform a dance|"
                 r"show (?:me )?your dancing skills|show (?:me )?your dance|"
                 r"dance for me|show off your moves)", t):
        return {"action": "dance_command"}

    # Mood-based music (sad/stressed → Sanju auto-picks a comforting song)
    if re.search(r"(?:i(?:'m| am)\s+(?:sad|upset|stressed|tired|lonely|bored|depressed|crying)|cheer me up)", t):
        return {"action": "play_mood_song", "mood": "comfort"}

    # Explicit Spotify request — keep Spotify for this
    m = re.search(r"(?:play|listen to)\s+(.+?)\s+on\s+spotify$", t)
    if m: return {"action": "play_music", "payload": m.group(1).strip()}

    # Kannada song (specific name or random)
    m = re.search(r"(?:sing|play|listen to)\s+(.*?)\s*(?:kannada\s+song|kannada\s+music|kannada)", t)
    if m:
        query = m.group(1).strip()
        return {"action": "play_love_song", "mood": "kannada", "query": query}
    if re.search(r"(?:kannada song|play kannada|kannada music)", t):
        return {"action": "play_love_song", "mood": "kannada", "query": ""}

    # Hindi song (specific name or random)
    m = re.search(r"(?:sing|play|listen to)\s+(.*?)\s*(?:hindi\s+song|hindi\s+music|bollywood|hindi)", t)
    if m:
        query = m.group(1).strip()
        return {"action": "play_love_song", "mood": "hindi", "query": query}
    if re.search(r"(?:hindi song|play hindi|bollywood song|desi song|hindi music)", t):
        return {"action": "play_love_song", "mood": "hindi", "query": ""}

    # Surprise / random / love song / sing for me → random from either folder
    if re.search(r"(?:surprise me|play something|something romantic|play love|love song|romantic song|"
                 r"sing.*(?:for me|to me|something)|sing a song|sing me a song|play a song)", t):
        return {"action": "play_love_song", "mood": "love", "query": ""}

    # "play <song name>" — specific local search across both folders
    m = re.search(r"^(?:play|listen to|sing)\s+(.+)$", t)
    if m:
        query = m.group(1).strip()
        # Strip trailing filler so "play music for me" reduces to "music"
        query = re.sub(r"\s*(?:for me|to me|for us|please|now)$", "", query).strip()
        # Skip generic filler words — no real song named, let Sanju auto-pick
        if query not in ("music", "a song", "songs", "something", "anything", "a tune", "tunes"):
            return {"action": "play_love_song", "mood": "any", "query": query}
        return {"action": "play_love_song", "mood": "any", "query": ""}

    # ══════════════════════════════════════════════════════════════════════════
    # NEW: VOLUME CONTROL
    # Phrases: "volume up", "increase volume", "mute", "unmute", "volume 50"
    # ══════════════════════════════════════════════════════════════════════════
    if re.search(r"(?:volume up|increase volume|louder|turn up)", t):
        return {"action": "volume_up"}
    if re.search(r"(?:volume down|decrease volume|quieter|lower volume|turn down)", t):
        return {"action": "volume_down"}
    if re.search(r"^(?:mute|silence|quiet)$", t):
        return {"action": "volume_mute"}
    if re.search(r"^unmute$", t):
        return {"action": "volume_unmute"}
    m = re.search(r"set volume (?:to\s+)?(\d+)", t)
    if m: return {"action": "volume_set", "payload": m.group(1)}

    # ══════════════════════════════════════════════════════════════════════════
    # NEW: SCREENSHOT
    # Phrases: "take a screenshot", "screenshot", "capture screen"
    # ══════════════════════════════════════════════════════════════════════════
    if re.search(r"(?:take a screenshot|screenshot|capture screen|capture the screen)", t):
        return {"action": "screenshot"}

    # ══════════════════════════════════════════════════════════════════════════
    # NEW: SLEEP TIMER
    # Phrases: "set sleep timer for 30 minutes", "sleep timer 1 hour", "cancel sleep timer"
    # ══════════════════════════════════════════════════════════════════════════
    m = re.search(r"(?:set\s+)?sleep timer\s+(?:for\s+)?(\d+)\s*(minute|min|hour|hr|second|sec)", t)
    if m:
        return {"action": "sleep_timer", "amount": int(m.group(1)), "unit": m.group(2)}
    if re.search(r"cancel sleep timer|stop sleep timer", t):
        return {"action": "sleep_timer_cancel"}

    # ══════════════════════════════════════════════════════════════════════════
    # NEW: STOP MUSIC
    # ══════════════════════════════════════════════════════════════════════════
    if re.search(r"stop (?:the )?(?:music|song|playing)", t):
        return {"action": "stop_music"}

    # ══════════════════════════════════════════════════════════════════════════
    # NEW: FIX MY CODE / CHECK MY CODE
    # Phrases: "fix this bug", "fix my code", "check my code", "find the bug",
    #          "is there a bug", "debug this", "analyze my code", "review my code"
    # ══════════════════════════════════════════════════════════════════════════
    if re.search(r"(?:fix (?:this|the|my) (?:bug|code|error)|fix (?:this|that)|"
                 r"check (?:my|this|the) code|debug (?:this|my code|it)|"
                 r"find (?:the|my|a) bug|is there a bug|analyze (?:my|this) code|"
                 r"review (?:my|this) code|what'?s wrong with (?:my|this) code)", t):
        return {"action": "fix_code"}

    # ══════════════════════════════════════════════════════════════════════════
    # NEW: WATCH MY CODE (auto-check on idle, report only, ask before fixing)
    # Phrases: "look at my code", "keep an eye on my code", "watch my code",
    #          "keep checking my code", "monitor my code"
    # Stop:    "stop checking", "stop watching my code", "stop checking my code"
    # ══════════════════════════════════════════════════════════════════════════
    if re.search(r"(?:stop (?:checking|watching)(?: my code)?)", t):
        return {"action": "watch_code_stop"}

    if re.search(r"(?:look at my code|keep an eye on my code|watch my code|"
                 r"keep checking my code|monitor my code|keep watching my code)", t):
        return {"action": "watch_code_start"}

    # ── Shutdown / restart ─────────────────────────────────────────────────────
    m = re.search(r"^(?:shut\s*down|shutdown|turn off|power off)(?:\s+(?:the\s+)?(?:pc|computer|system|laptop))?$", t)
    if m: return {"action": "shutdown"}

    m = re.search(r"^(?:restart|reboot)(?:\s+(?:the\s+)?(?:pc|computer|system|laptop))?$", t)
    if m: return {"action": "restart"}

    # ── Generic open ──────────────────────────────────────────────────────────
    m = re.search(r"^(?:open|launch|start|run|fire up)\s+(.+)$", t)
    if m: return {"action": "open", "app": m.group(1).strip()}

    # ── In-app search (ctrl+F) — narrow, explicit, unambiguous ─────────────────
    m = re.search(r"^search\s+(?:for\s+)?(.+)$", t)
    if m: return {"action": "search", "payload": m.group(1).strip()}

    # NOTE — web search is intentionally NOT decided here anymore. It used
    # to be: an explicit "search the web for X / google X / look up X"
    # regex, PLUS a generic keyword-cue fallback ("check", "internship",
    # "any", etc.) for everything else. Both were fundamentally the "if
    # 'google' in text: search_web()" pattern — the explicit regex's
    # unanchored "google" trigger is exactly what caused "I am a Google
    # Student Ambassador" (a sentence ABOUT the user) to fire a web search,
    # since "google" followed by whitespace and more words matched
    # regardless of where in the sentence it appeared or what it meant.
    #
    # Whether to search the web is now a MEANING judgment — does this
    # message need current/external information, or is it about the user,
    # opinion, or something already known — which is exactly what an LLM
    # reasoning over full context can do and keyword matching structurally
    # can't. That decision now lives in ai_engine.py's tool-calling layer
    # (see TOOLS / web_search there), where the model decides per-turn
    # whether to call the web_search tool, backed by the existing
    # uncertainty-detection fallback (_seems_uncertain in ai_engine.py) as
    # a safety net for cases where it doesn't call the tool but also
    # doesn't actually know the answer.

    return None


def _match_app(name: str):
    name = _normalise_app_name(name.lower().strip())
    if name in APP_REGISTRY:
        return name, APP_REGISTRY[name]
    best_key, best_info = None, None
    for key, info in APP_REGISTRY.items():
        if key in name or name in key:
            if best_key is None or len(key) > len(best_key):
                best_key, best_info = key, info
    return best_key, best_info


# ══════════════════════════════════════════════════════════════════════════════
# LOW-LEVEL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _launch(cmd: str) -> bool:
    cmd = os.path.expandvars(cmd)
    try:
        subprocess.Popen(
            cmd, shell=True,
            creationflags=subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
        )
        return True
    except Exception as e:
        print(f"[Launch Error] {e}")
        return False


def _force_foreground(hwnd) -> bool:
    """
    Reliably brings a window to the foreground and gives it real keyboard
    focus, even when Windows' foreground-lock would normally block a
    background process from stealing focus (the classic reason
    SetForegroundWindow silently fails after the first call).
    Uses the standard AttachThreadInput trick. Returns True if Windows
    confirms the window is now foreground.
    """
    user32   = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    fg_hwnd       = user32.GetForegroundWindow()
    cur_thread    = kernel32.GetCurrentThreadId()
    fg_thread     = user32.GetWindowThreadProcessId(fg_hwnd, None)
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)

    attached_fg = attached_target = False
    try:
        if fg_thread and fg_thread != cur_thread:
            attached_fg = bool(user32.AttachThreadInput(cur_thread, fg_thread, True))
        if target_thread and target_thread != cur_thread:
            attached_target = bool(user32.AttachThreadInput(cur_thread, target_thread, True))

        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        result = user32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        return bool(result) and user32.GetForegroundWindow() == hwnd
    except Exception as e:
        print(f"[Force Foreground] {e}")
        return False
    finally:
        if attached_fg:
            user32.AttachThreadInput(cur_thread, fg_thread, False)
        if attached_target:
            user32.AttachThreadInput(cur_thread, target_thread, False)


def _focus_window(title: str, retries: int = 12, delay: float = 0.8) -> bool:
    for attempt in range(retries):
        wins = [w for w in gw.getAllWindows() if title.lower() in w.title.lower() and w.title.strip()]
        if wins:
            try:
                w = wins[0]
                if w.isMinimized: w.restore()
                if _force_foreground(w._hWnd):
                    time.sleep(0.35)
                    return True
            except Exception as e:
                print(f"[Focus attempt {attempt}] {e}")
        time.sleep(delay)
    print(f"[Focus] Window not found: '{title}'")
    return False


def _is_pycharm_running() -> bool:
    return any("pycharm" in w.title.lower() and w.title.strip() for w in gw.getAllWindows())


def _force_focus_pycharm() -> bool:
    user32 = ctypes.windll.user32
    wins = [w for w in gw.getAllWindows() if "pycharm" in w.title.lower() and w.title.strip()]
    if not wins: return False
    hwnd = wins[0]._hWnd
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    time.sleep(0.5)
    return True


def _paste_into_editor(app_info: dict, app_key: str, code: str, description: str) -> str:
    notepad_running = any("notepad" in w.title.lower() for w in gw.getAllWindows())
    if not notepad_running:
        _launch("notepad")
    found = False
    for _ in range(20):
        wins = [w for w in gw.getAllWindows() if "notepad" in w.title.lower() and w.title.strip()]
        if wins:
            found = True
            break
        time.sleep(0.5)
    if not found: return "I couldn't open Notepad. Please open it manually."
    w = wins[0]
    try:
        if w.isMinimized: w.restore()
        w.activate()
    except Exception: pass
    time.sleep(0.6)
    cx = w.left + w.width  // 2
    cy = w.top  + w.height // 2
    pyautogui.click(cx, cy)
    time.sleep(0.4)
    pyautogui.hotkey("ctrl", "n")
    time.sleep(0.6)
    text_lines = code.split("\n")
    for i, line in enumerate(text_lines):
        if line: pyautogui.write(line, interval=0.02)
        if i < len(text_lines) - 1: pyautogui.press("enter")
    return f"Done! I typed the {description} into Notepad."


def _paste_into_pycharm_current_file(code: str, description: str) -> str:
    already_running = _is_pycharm_running()
    if already_running:
        if not _force_focus_pycharm():
            return "PyCharm is open but I couldn't focus it. Please click on PyCharm and try again."
    else:
        _launch(PYCHARM_EXE)
        if not _focus_window("PyCharm", retries=30, delay=1.0):
            return "PyCharm is taking a while to start. Give it a moment to fully load, then try again."
        time.sleep(4.0)
        _force_focus_pycharm()
    pyperclip.copy(code)
    time.sleep(0.3)
    pyautogui.press("escape")
    time.sleep(0.2)
    pyautogui.press("escape")
    time.sleep(0.2)
    sw, sh = pyautogui.size()
    pyautogui.click(sw // 2, sh // 2)
    time.sleep(0.3)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.4)
    pyautogui.hotkey("ctrl", "alt", "l")
    time.sleep(0.3)
    return f"Done! I pasted the {description} code into your open PyCharm file."


def _paste_into_current_window(code: str, description: str) -> str:
    time.sleep(0.5)
    print(f"[Typing] Writing {description} in background window...")
    _human_type(code, fast=True)
    return f"Done! I wrote the {description} for you."


def _search_browser(app_info: dict, query: str):
    if not _focus_window(app_info["window"]): return
    time.sleep(0.25)
    pyautogui.hotkey("ctrl", "l")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "a")
    _human_type(query, fast=True)
    pyautogui.press("enter")


def _search_store(query: str):
    if not _focus_window("Microsoft Store"): return
    time.sleep(2.0)
    pyautogui.hotkey("ctrl", "f")
    time.sleep(0.4)
    pyautogui.hotkey("ctrl", "a")
    _human_type(query, fast=True)
    pyautogui.press("enter")


# ══════════════════════════════════════════════════════════════════════════════
# HUMAN TYPING WITH SOUND
# ══════════════════════════════════════════════════════════════════════════════
def _human_type(text: str, fast: bool = False):
    TYPO_CHARS  = "qwertyuiopasdfghjklzxcvbnm"
    TYPO_CHANCE = 0.04
    sound_path  = r"C:\Windows\Media\Windows Navigation Start.wav"
    can_play    = os.path.exists(sound_path)

    for i, ch in enumerate(text):
        if can_play and ch != " ":
            winsound.PlaySound(sound_path, winsound.SND_FILENAME | winsound.SND_ASYNC)

        if ch.lower() in TYPO_CHARS and not fast and random.random() < TYPO_CHANCE:
            pyautogui.write(random.choice(TYPO_CHARS))
            time.sleep(random.uniform(0.08, 0.18))
            if can_play:
                winsound.PlaySound(sound_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            pyautogui.press("backspace")
            time.sleep(random.uniform(0.05, 0.12))

        pyautogui.write(ch)

        if ch in ".!?":  delay = random.uniform(0.12, 0.22)
        elif ch == ",":  delay = random.uniform(0.08, 0.16)
        elif ch == " ":  delay = random.uniform(0.06, 0.13)
        elif fast:       delay = random.uniform(0.03, 0.07)
        else:            delay = random.uniform(0.04, 0.10)

        if not fast and i > 0 and i % random.randint(6, 12) == 0:
            delay += random.uniform(0.05, 0.15)
        time.sleep(delay)


# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP
# ══════════════════════════════════════════════════════════════════════════════
def _wa_focus() -> bool:
    wins = [w for w in gw.getAllWindows() if "whatsapp" in w.title.lower() and w.title.strip()]
    if not wins: return False
    try:
        w = wins[0]
        if w.isMinimized: w.restore()
        ok = _force_foreground(w._hWnd)
        time.sleep(0.4)
        return ok
    except Exception as e:
        print(f"[WA Focus] {e}")
        return False


def _whatsapp_send(person: str, message: str = "") -> str:
    wa_running = any("whatsapp" in w.title.lower() and w.title.strip() for w in gw.getAllWindows())
    if not wa_running:
        launched = _launch("start whatsapp:")
        if not launched:
            fallback_path = os.path.expandvars(r"%LOCALAPPDATA%\WhatsApp\WhatsApp.exe")
            _launch(f'"{fallback_path}"')
        if not _focus_window("WhatsApp", retries=25, delay=1.0):
            return "I couldn't open WhatsApp. Please make sure it's installed."
        time.sleep(5.0)
    if not _wa_focus():
        return "WhatsApp is open but I couldn't focus it."
    time.sleep(0.5)
    pyautogui.hotkey("ctrl", "f")
    time.sleep(0.7)
    _wa_focus()
    time.sleep(0.5)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    _human_type(person, fast=True)
    time.sleep(2.0)
    _wa_focus()
    time.sleep(0.2)
    pyautogui.press("down")
    time.sleep(0.5)
    pyautogui.press("enter")
    time.sleep(1.5)
    if not message:
        return f"Opened WhatsApp chat with {person}."
    _human_type(message, fast=False)
    time.sleep(0.4)
    _wa_focus()
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)
    return f"Message sent to {person} on WhatsApp!"


# ══════════════════════════════════════════════════════════════════════════════
# PLAYBACK CONFIRMATION — is Spotify actually making sound right now?
# ══════════════════════════════════════════════════════════════════════════════
def _confirm_spotify_audio(timeout: float = 6.0, poll_interval: float = 0.3,
                            peak_threshold: float = 0.01) -> bool:
    """
    Polls Spotify.exe's real audio peak meter (via pycaw) until it sees actual
    sound coming out, or gives up after `timeout` seconds. This is the "wait
    until music playback is confirmed" step — a search+Enter automation
    finishing without an error is NOT proof anything is audibly playing
    (Spotify can be buffering, land on the wrong row, autoplay can be off,
    etc.), so nothing downstream (minimizing the window, starting the dance)
    should trust that alone.
    Returns True the moment real audio is detected, False if the timeout
    elapses with silence, and True (best-effort) if pycaw isn't installed at
    all, so the app degrades to the old fixed-delay behavior rather than
    hard-failing every dance/play request.
    """
    if not _PYCAW_AVAILABLE:
        time.sleep(2.0)   # best-effort fallback, no dependency installed
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for session in AudioUtilities.GetAllSessions():
                proc = session.Process
                if not proc or "spotify" not in proc.name().lower():
                    continue
                meter = session._ctl.QueryInterface(
                    AudioUtilities.IAudioMeterInformation)
                peak = meter.GetPeakValue()
                if peak >= peak_threshold:
                    return True
        except Exception as e:
            print(f"[Spotify] Playback confirmation check error: {e}")
        time.sleep(poll_interval)

    return False


# ══════════════════════════════════════════════════════════════════════════════
# SPOTIFY — still used when user says "play X on spotify" explicitly
# ══════════════════════════════════════════════════════════════════════════════
def _spotify_play(song: str, confirm_playback: bool = True) -> str:
    spotify_running = any("spotify" in w.title.lower() for w in gw.getAllWindows())
    if not spotify_running:
        print("[Spotify] Not running — launching…")
        _launch("start spotify:")
        time.sleep(8.0)

    def _get_spotify_hwnd():
        wins = [w for w in gw.getAllWindows() if "spotify" in w.title.lower() and w.title.strip()]
        return wins[0] if wins else None

    w = None
    for _ in range(15):
        w = _get_spotify_hwnd()
        if w:
            if w.isMinimized: w.restore()
            break
        time.sleep(0.5)

    if not w:
        return "I couldn't find the Spotify window. Please open it first."

    # Re-assert real OS keyboard focus right before EVERY keystroke phase —
    # a single SetForegroundWindow call earlier can silently lose focus by
    # the time we actually type (this was the bug behind "plays the same
    # song every time": the keystrokes weren't reaching Spotify at all).
    if not _force_foreground(w._hWnd):
        time.sleep(0.5)
        _force_foreground(w._hWnd)   # one retry

    time.sleep(0.6)
    pyautogui.hotkey("ctrl", "l")
    time.sleep(0.5)
    pyautogui.hotkey("ctrl", "a")
    pyautogui.press("delete")
    time.sleep(0.3)

    # Re-confirm focus didn't drift before typing the actual query
    _force_foreground(w._hWnd)
    _human_type(song, fast=True)
    time.sleep(1.4)   # let the live suggestion dropdown populate

    # Use the inline suggestion dropdown instead of navigating to the full
    # search-results page. The old approach (Enter → Tab x3 → Enter x2)
    # was too fragile — the number of Tabs needed shifts depending on
    # filter chips / ads / account type, so it would "do all the work"
    # (open Spotify, type the search) but land on the wrong control and
    # never actually press play. Pressing Down highlights the top
    # suggestion directly, and Enter on it starts playback immediately —
    # far fewer blind keystrokes, far less layout-dependent.
    _force_foreground(w._hWnd)
    pyautogui.press("down")
    time.sleep(0.4)
    pyautogui.press("enter")
    time.sleep(1.2)

    # Fallback: if that only navigated to an album/artist/playlist page
    # instead of starting playback directly, one more Enter usually lands
    # on that page's big Play button once it's settled.
    _force_foreground(w._hWnd)
    pyautogui.press("enter")

    if confirm_playback:
        if not _confirm_spotify_audio():
            return (f"I tried playing {song} but I couldn't confirm it "
                    f"actually started — the search may have landed on the "
                    f"wrong thing. Could you check Spotify?")

    return f"Playing {song} on Spotify for you!"


# ══════════════════════════════════════════════════════════════════════════════
# LOCAL MUSIC PLAYER
# Priority: D:\Songs\Hindi\  or  D:\Songs\Kannada\
# Falls back to a random song from either folder if nothing specific requested.
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# AUTO-PICK MUSIC — Sanju chooses a song herself and plays it on Spotify.
# Replaces the old "play from D:\Songs local folder" feature entirely —
# everything now goes through Spotify, whether the user names a song or not.
# ══════════════════════════════════════════════════════════════════════════════

_SONG_LIBRARY = {
    "hindi": [
        "Tum Hi Ho Arijit Singh", "Kesariya Arijit Singh", "Channa Mereya Arijit Singh",
        "Raabta Arijit Singh", "Tera Ban Jaunga", "Kal Ho Naa Ho", "Agar Tum Saath Ho",
        "Hawayein", "Ae Dil Hai Mushkil", "Tujhe Kitna Chahne Lage",
    ],
    "kannada": [
        "Belageddu Kannada Song", "Olavina Ole", "Saarocchi Kannada Song",
        "Munjaane Manjalli", "Yeshu Ninna Preethige", "Aagiruvenu Naanu",
    ],
    "love": [
        "Perfect Ed Sheeran", "All of Me John Legend", "Tum Hi Ho Arijit Singh",
        "Photograph Ed Sheeran", "Thinking Out Loud Ed Sheeran", "A Thousand Years",
        "Kesariya Arijit Singh", "Just the Way You Are Bruno Mars",
    ],
    "comfort": [
        "Weightless Marconi Union", "Fix You Coldplay", "Here Comes the Sun Beatles",
        "Better Days OneRepublic", "Counting Stars OneRepublic", "Someone Like You Adele",
    ],
    "any": [
        "Blinding Lights The Weeknd", "Perfect Ed Sheeran", "Tum Hi Ho Arijit Singh",
        "Shape of You Ed Sheeran", "Believer Imagine Dragons", "Kesariya Arijit Singh",
        "Levitating Dua Lipa", "Counting Stars OneRepublic",
    ],
}

_MOOD_REPLIES = {
    "hindi":   ["I picked a beautiful Hindi song for you. Here's {name}!",
                "Here's a lovely Hindi track, just for you — {name}!"],
    "kannada": ["I found a nice Kannada song for you — {name}!",
                "Here's something in Kannada I think you'll love — {name}!"],
    "love":    ["I picked this one just for you. Playing {name}!",
                "This song makes me think of us. Enjoy {name} my love!",
                "I love this one so much. Playing {name} for you!"],
    "comfort": ["I picked something to make you feel better. Playing {name}, just for you.",
                "Here's something calming for you — {name}. I'm right here with you."],
    "any":     ["Here's a song I picked for you — {name}!",
                "Surprise! I chose {name} for you. Enjoy!"],
}


def _auto_pick_song(mood: str = "any", query: str = "", minimize_after: bool = False) -> tuple[str, str]:
    """
    Decides what to play and plays it on Spotify.
    - If the user named a specific song (query), play exactly that.
    - Otherwise, Sanju picks one herself from a curated list for the mood.
    minimize_after=True tucks the Spotify window away once playback has
    started (used for the "dance" command — background playback is
    enough, the window doesn't need to sit on screen). It stays visible
    DURING the actual search/play sequence either way, since that
    automation depends on the window holding real OS keyboard focus.
    Returns (song_name_played, spoken_reply).
    """
    if query:
        song = query
    else:
        choices = _SONG_LIBRARY.get(mood, _SONG_LIBRARY["any"])
        song = random.choice(choices)

    # _spotify_play's own return value is the real source of truth on
    # whether playback is actually confirmed — it now blocks on
    # _confirm_spotify_audio() internally. Previously this return value was
    # discarded entirely and _auto_pick_song always built its own generic
    # "Playing X for you!" reply regardless of what actually happened, which
    # meant a real Spotify failure on this path (dance / auto-picked song)
    # was invisible to main.py's failure-detection and to the
    # minimize/dance decision below.
    spotify_reply = _spotify_play(song)
    if not spotify_reply.startswith("Playing "):
        # Confirmed failure (window not found, or audio never actually
        # started) — surface it as-is so main.py's failure_markers catch it,
        # don't minimize the window, and don't pretend it worked.
        return song, spotify_reply

    if minimize_after:
        try:
            wins = [w for w in gw.getAllWindows() if "spotify" in w.title.lower() and w.title.strip()]
            if wins:
                wins[0].minimize()
        except Exception as e:
            print(f"[Spotify] Minimize-after-play error: {e}")

    reply_pool = _MOOD_REPLIES.get(mood, _MOOD_REPLIES["any"])
    message = random.choice(reply_pool).format(name=song)
    return song, message





# ══════════════════════════════════════════════════════════════════════════════
# NEW: VOLUME CONTROL (Windows Audio API via nircmd or ctypes)
# ══════════════════════════════════════════════════════════════════════════════
def _volume_control(action: str, level: int = None) -> str:
    """
    Controls system volume using PowerShell + Windows Core Audio API.
    Works without any 3rd party tools.
    """
    try:
        if action == "up":
            # Increase volume by 10%
            for _ in range(5):
                pyautogui.press("volumeup")
                time.sleep(0.05)
            return "Volume turned up for you!"

        elif action == "down":
            for _ in range(5):
                pyautogui.press("volumedown")
                time.sleep(0.05)
            return "Volume turned down my love."

        elif action == "mute":
            pyautogui.press("volumemute")
            return "I muted the volume. Peace and quiet!"

        elif action == "unmute":
            pyautogui.press("volumemute")
            return "Volume is back on!"

        elif action == "set" and level is not None:
            # Use PowerShell to set exact volume level
            script = f"""
$volume = {level}
$wshShell = New-Object -ComObject WScript.Shell
Add-Type -TypeDefinition @"
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {{ }}
"@
"""
            # Simpler approach: use VBScript via PowerShell
            ps_cmd = f'powershell -Command "$obj = New-Object -ComObject WScript.Shell; for($i=0; $i -lt 50; $i++) {{ $obj.SendKeys([char]174) }}; for($i=0; $i -lt {level // 2}; $i++) {{ $obj.SendKeys([char]175) }}"'
            subprocess.Popen(ps_cmd, shell=True)
            return f"Volume set to {level} percent!"

    except Exception as e:
        print(f"[Volume Error] {e}")
        return "I had trouble adjusting the volume. Try the keyboard shortcut!"


# ══════════════════════════════════════════════════════════════════════════════
# NEW: SCREENSHOT
# ══════════════════════════════════════════════════════════════════════════════
def _take_screenshot() -> str:
    """
    Takes a screenshot and saves it to the Desktop with a timestamp.
    """
    try:
        import datetime
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        os.makedirs(desktop, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename  = os.path.join(desktop, f"Sanju_Screenshot_{timestamp}.png")

        screenshot = pyautogui.screenshot()
        screenshot.save(filename)

        print(f"[Screenshot] Saved: {filename}")
        return f"Screenshot taken and saved to your Desktop as Sanju Screenshot {timestamp}!"
    except Exception as e:
        print(f"[Screenshot Error] {e}")
        return "I couldn't take the screenshot. Please try again!"


# ══════════════════════════════════════════════════════════════════════════════
# NEW: SLEEP TIMER
# ══════════════════════════════════════════════════════════════════════════════
_sleep_timer_thread = None
_sleep_timer_active = False

def _sleep_timer(amount: int, unit: str, on_done_callback=None) -> str:
    """
    Schedules a system shutdown after the given time.
    Runs in a background thread so Sanju stays responsive.
    """
    global _sleep_timer_active, _sleep_timer_thread

    unit = unit.lower()
    if "hour" in unit or unit == "hr":
        seconds = amount * 3600
        display = f"{amount} hour{'s' if amount > 1 else ''}"
    elif "min" in unit:
        seconds = amount * 60
        display = f"{amount} minute{'s' if amount > 1 else ''}"
    else:
        seconds = amount
        display = f"{amount} second{'s' if amount > 1 else ''}"

    # Cancel any existing timer
    _sleep_timer_active = False

    def _do_sleep():
        global _sleep_timer_active
        _sleep_timer_active = True
        print(f"[Sleep Timer] Shutting down in {seconds} seconds…")
        time.sleep(seconds)
        if _sleep_timer_active:
            if on_done_callback:
                on_done_callback("Sleep timer done! Shutting down now. Goodnight my love.")
            os.system("shutdown /s /t 10")

    _sleep_timer_thread = threading.Thread(target=_do_sleep, daemon=True)
    _sleep_timer_thread.start()

    return f"Sleep timer set! I will shut down your PC in {display}. Sleep well my love."


def _cancel_sleep_timer() -> str:
    global _sleep_timer_active
    if _sleep_timer_active:
        _sleep_timer_active = False
        os.system("shutdown /a")  # Abort any pending shutdown
        return "Sleep timer cancelled! Staying awake with you."
    return "There is no active sleep timer my love."


# ══════════════════════════════════════════════════════════════════════════════
# NEW: STOP MUSIC (Spotify pause)
# ══════════════════════════════════════════════════════════════════════════════
def _stop_music() -> str:
    """Pauses whatever's currently playing (Spotify, VLC, browser tab, etc.)
    using the global media key — no window-focus needed, so it works
    reliably regardless of which app is actually playing audio."""
    if _media_key(_VK_MEDIA_PLAY_PAUSE):
        return "Music paused. I will be quiet now!"
    return "I could not pause the music. Try it manually!"


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM COMMANDS
# ══════════════════════════════════════════════════════════════════════════════
def _close_all_windows():
    for w in gw.getAllWindows():
        if w.title.strip() and w.visible and w.title != "Program Manager":
            try:
                w.close()
            except Exception:
                pass
    time.sleep(1.5)


# ══════════════════════════════════════════════════════════════════════════════
# NEW: CLOSE APP
# ══════════════════════════════════════════════════════════════════════════════
def _close_app(app_name: str) -> str:
    """
    Closes a running app by matching its window title (preferred — gentle,
    closes just that window) or falling back to taskkill by process name
    if no matching window is found (handles apps with no visible window,
    or whose window title doesn't match what we expect).
    """
    key, info = _match_app(app_name)
    window_hint = info["window"] if info else app_name

    closed_any = False
    for w in gw.getAllWindows():
        if window_hint.lower() in w.title.lower() and w.title.strip():
            try:
                w.close()
                closed_any = True
            except Exception as e:
                print(f"[Close App] Window close failed: {e}")

    if closed_any:
        return f"Closed {key or app_name} for you!"

    # Fallback — try killing the process directly by likely exe name
    proc_guess = (key or app_name).replace(" ", "")
    try:
        result = subprocess.run(
            ["taskkill", "/IM", f"{proc_guess}.exe", "/F"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return f"Closed {key or app_name} for you!"
    except Exception as e:
        print(f"[Close App] taskkill failed: {e}")

    return f"I couldn't find {app_name} running right now."


# ══════════════════════════════════════════════════════════════════════════════
# NEW: MUSIC PAUSE / RESUME / SKIP
# Sends real Windows hardware media-key virtual-key codes directly via
# keybd_event — works globally on whatever app owns the system media
# session (Spotify, browser tab, etc.) and needs no window focus at all.
# (pyautogui.press("playpause") was unreliable here — PyAutoGUI's named-key
# mapping for media keys isn't guaranteed to hit the real VK code on every
# Windows setup, which is why pause/resume/skip weren't doing anything.)
# ══════════════════════════════════════════════════════════════════════════════
_VK_MEDIA_NEXT_TRACK = 0xB0
_VK_MEDIA_PREV_TRACK = 0xB1
_VK_MEDIA_PLAY_PAUSE = 0xB3
_KEYEVENTF_KEYUP     = 0x0002


def _media_key(vk_code: int) -> bool:
    try:
        user32 = ctypes.windll.user32
        user32.keybd_event(vk_code, 0, 0, 0)                  # key down
        time.sleep(0.05)
        user32.keybd_event(vk_code, 0, _KEYEVENTF_KEYUP, 0)   # key up
        return True
    except Exception as e:
        print(f"[Media Key Error] {e}")
        return False


def _pause_music() -> str:
    if _media_key(_VK_MEDIA_PLAY_PAUSE):
        return "Music paused for you!"
    return "I couldn't pause the music. Try it manually!"


def _resume_music() -> str:
    if _media_key(_VK_MEDIA_PLAY_PAUSE):
        return "Resuming the music now!"
    return "I couldn't resume the music. Try it manually!"


def _next_track() -> str:
    if _media_key(_VK_MEDIA_NEXT_TRACK):
        return "Skipping to the next song!"
    return "I couldn't skip the song. Try it manually!"


def _previous_track() -> str:
    if _media_key(_VK_MEDIA_PREV_TRACK):
        return "Going back to the previous song!"
    return "I couldn't go back a song. Try it manually!"


def _shutdown():
    def do_shutdown():
        time.sleep(5)
        os.system("shutdown /s /t 0")
    threading.Thread(target=do_shutdown, daemon=True).start()


def _restart():
    def do_restart():
        time.sleep(5)
        os.system("shutdown /r /t 0")
    threading.Thread(target=do_restart, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC CONTROLLER
# ══════════════════════════════════════════════════════════════════════════════

class AppController:

    def __init__(self):
        self._sleep_callback = None   # Set by main.py so Sanju can speak when timer fires

    def set_sleep_callback(self, callback):
        """main.py passes in a _speak() function so the timer can announce itself."""
        self._sleep_callback = callback

    def execute(self, intent: dict, ai_code: str = "") -> str:
        action   = intent.get("action", "")
        app_name = intent.get("app", "")
        payload  = intent.get("payload", "")

        # ── Open app ──────────────────────────────────────────────────────────
        if action == "open":
            key, info = _match_app(app_name)
            if not info:
                return f"Sorry, I don't know how to open {app_name}."
            _launch(info["cmd"])
            return f"Opening {key} for you!"

        # ── NEW: Close app ───────────────────────────────────────────────────
        elif action == "close_app":
            return _close_app(app_name)

        # ── Open + search ─────────────────────────────────────────────────────
        elif action == "open_and_search":
            key, info = _match_app(app_name)
            if not info:
                return f"I don't know how to open {app_name}."
            _launch(info["cmd"])
            app_type = info.get("type", "generic")
            if app_type == "store":
                _search_store(payload)
                return f"Opened Microsoft Store and searched for {payload}."
            elif app_type == "browser":
                _search_browser(info, payload)
                return f"Searching for {payload} in {key}."
            else:
                return f"Opened {key}. I can search inside browsers and the Store."

        # ── Open + code ───────────────────────────────────────────────────────
        elif action == "open_and_code":
            if not ai_code:
                return "I haven't generated the code yet. Give me a moment!"
            if app_name == "current_window":
                return _paste_into_current_window(ai_code, payload)
            key, info = _match_app(app_name)
            if not info:
                return f"I don't know how to open {app_name}."
            if info.get("type") == "pycharm":
                return _paste_into_pycharm_current_file(ai_code, payload)
            elif info.get("type") == "editor":
                return _paste_into_editor(info, key, ai_code, payload)
            else:
                _launch(info["cmd"])
                return f"Opened {key}."

        # ── WhatsApp ──────────────────────────────────────────────────────────
        elif action == "whatsapp_open":
            return _whatsapp_send(intent.get("person", ""), "")

        elif action == "whatsapp_message":
            return _whatsapp_send(intent.get("person", ""), intent.get("payload", ""))

        # ── Spotify (specific song) ───────────────────────────────────────────
        elif action == "play_music":
            return _spotify_play(payload)

        # ── Auto-picked songs (Sanju chooses, plays via Spotify) ───────────────
        elif action == "play_love_song":
            _, message = _auto_pick_song(
                mood=intent.get("mood", "any"),
                query=intent.get("query", ""),
            )
            return message

        elif action == "play_mood_song":
            _, message = _auto_pick_song(mood="comfort", query="")
            return message

        # ── NEW: Dance — starts music herself (biased toward soft/romantic
        # Kannada, Hindi, or love picks, never the sadder "comfort" pool or
        # anything aggressive) and tucks the Spotify window away since the
        # dancing itself is the point, not the player window.
        elif action == "dance_command":
            mood = random.choice(["kannada", "hindi", "love"])
            _, message = _auto_pick_song(mood=mood, query="", minimize_after=True)
            return message

        # ── NEW: Stop music ───────────────────────────────────────────────────
        elif action == "stop_music":
            return _stop_music()

        # ── NEW: Pause / resume / skip music ───────────────────────────────────
        elif action == "pause_music":
            return _pause_music()

        elif action == "resume_music":
            return _resume_music()

        elif action == "next_track":
            return _next_track()

        elif action == "previous_track":
            return _previous_track()

        # ── NEW: Fix my code ──────────────────────────────────────────────────
        elif action == "fix_code":
            from code_doctor import fix_my_code
            return fix_my_code()

        # ── NEW: Volume control ───────────────────────────────────────────────
        elif action == "volume_up":
            return _volume_control("up")

        elif action == "volume_down":
            return _volume_control("down")

        elif action == "volume_mute":
            return _volume_control("mute")

        elif action == "volume_unmute":
            return _volume_control("unmute")

        elif action == "volume_set":
            try:
                level = int(payload)
                return _volume_control("set", level)
            except (ValueError, TypeError):
                return "Please tell me the volume level as a number, like set volume to 50."

        # ── NEW: Screenshot ───────────────────────────────────────────────────
        elif action == "screenshot":
            return _take_screenshot()

        # ── NEW: Sleep timer ──────────────────────────────────────────────────
        elif action == "sleep_timer":
            amount = intent.get("amount", 30)
            unit   = intent.get("unit", "minute")
            return _sleep_timer(amount, unit,
                                on_done_callback=self._sleep_callback)

        elif action == "sleep_timer_cancel":
            return _cancel_sleep_timer()

        # ── Shutdown / restart ────────────────────────────────────────────────
        elif action == "shutdown":
            _close_all_windows()
            _shutdown()
            return "Shutting down your system in 5 seconds. Goodbye my love!"

        elif action == "restart":
            _close_all_windows()
            _restart()
            return "Restarting your system in 5 seconds!"

        # ── In-app search ─────────────────────────────────────────────────────
        elif action == "search":
            pyautogui.hotkey("ctrl", "f")
            time.sleep(0.25)
            pyautogui.write(payload, interval=0.03)
            pyautogui.press("enter")
            return f"Searching for {payload}."

        return "I'm not sure what to do with that."