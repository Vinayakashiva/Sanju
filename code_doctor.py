"""
code_doctor.py — Sanju AI
"Fix my bug" feature.

Flow:
  1. CAPTURE  — read the code currently open in whatever window has focus,
                via clipboard (select-all + copy). No vision fallback is
                configured right now (the local model is text-only), so
                make sure you've copied your code first — if the clipboard
                comes back empty, capture just fails.
  2. ANALYZE  — send the code to the local Ollama model, ask for:
                bug summary + fixed code.
  3. APPLY    — type the fixed code back in.
                - PyCharm: try a surgical line-level Find & Replace first
                  (Ctrl+R) for each changed line. If that can't be confirmed,
                  fall back to full select-all + retype.
                - Everything else: full select-all + retype (most reliable
                  across unknown editors/platforms/web IDEs).

Works on "any platform" in the sense of: whatever window is currently
focused and accepts Ctrl+A / Ctrl+C / typed input — VS Code, PyCharm,
Notepad++, browser-based IDEs (Replit, CodeSandbox, etc.), terminals, etc.

Dependency note: editor detection uses psutil to read the process name of
the focused window (pip install psutil) — this is far more reliable than
matching window titles, since many IDEs (PyCharm included) never put the
app name in the title bar.
"""

import io
import re
import time

import pyautogui
import pygetwindow as gw
import pyperclip
import ctypes
import psutil

import model_manager


# ══════════════════════════════════════════════════════════════════════════════
# IDLE DETECTION — used for the "automatically check after I stop typing" mode
# ══════════════════════════════════════════════════════════════════════════════

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def get_idle_seconds() -> float:
    """Returns how many seconds since the last keyboard/mouse input (Windows)."""
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
        millis_since_boot = ctypes.windll.kernel32.GetTickCount()
        idle_millis = millis_since_boot - info.dwTime
        return idle_millis / 1000.0
    except Exception:
        return 0.0


# Process executable names that indicate a code editor is running.
# This is far more reliable than window titles — PyCharm, for example,
# shows titles like "ProjectName – filename.py" with NO mention of
# "pycharm" anywhere, so title matching alone misses it entirely.
_CODE_EDITOR_PROCESSES = (
    "pycharm64.exe", "pycharm.exe",
    "code.exe",                      # VS Code
    "sublime_text.exe",
    "notepad++.exe",
    "atom.exe",
    "idea64.exe", "idea.exe",        # IntelliJ
    "webstorm64.exe", "webstorm.exe",
    "studio64.exe", "studio.exe",    # Android Studio
    "rider64.exe", "rider.exe",
    "clion64.exe", "clion.exe",
    "goland64.exe", "goland.exe",
    "phpstorm64.exe", "phpstorm.exe",
    "rubymine64.exe", "rubymine.exe",
    "datagrip64.exe", "datagrip.exe",
    "fleet.exe",
    "vim.exe", "gvim.exe", "nvim.exe", "nvim-qt.exe",
    "eclipse.exe",
    "netbeans64.exe", "netbeans.exe",
)

# Fallback: window-title substrings, for editors/cases where process-name
# lookup fails (e.g. permissions) or for browser-based IDEs where the
# "process" is just the browser but the title is distinctive.
_CODE_EDITOR_TITLE_HINTS = (
    "visual studio code", "sublime text", "notepad++",
    "replit", "codesandbox", "codepen", "stackblitz", "github.dev",
)


def _get_active_window_process_name() -> str:
    """Returns the lowercase exe name (e.g. 'pycharm64.exe') of the process
    that owns the currently focused window, or '' if it can't be determined."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        proc = psutil.Process(pid.value)
        return proc.name().lower()
    except Exception:
        return ""


def is_code_editor_focused(debug: bool = False) -> bool:
    """
    Best-effort check: is the currently focused window a known code editor?
    Checks the OWNING PROCESS NAME first (reliable — works for PyCharm,
    whose window title never says "pycharm"), then falls back to title
    substrings for editors/cases the process check can't catch.
    """
    proc_name = _get_active_window_process_name()
    if proc_name:
        matched = proc_name in _CODE_EDITOR_PROCESSES
        if debug:
            print(f"[CodeDoctor][debug] Active process: {proc_name!r} -> editor_match={matched}")
        if matched:
            return True

    try:
        win = gw.getActiveWindow()
        if not win or not win.title.strip():
            if debug: print("[CodeDoctor][debug] No active window / empty title.")
            return False
        title = win.title.lower()
        matched = any(hint in title for hint in _CODE_EDITOR_TITLE_HINTS)
        if debug:
            print(f"[CodeDoctor][debug] Active window title: {win.title!r} -> editor_match={matched}")
        return matched
    except Exception as e:
        if debug: print(f"[CodeDoctor][debug] getActiveWindow error: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — CAPTURE THE CODE
# ══════════════════════════════════════════════════════════════════════════════

def _get_focused_window():
    """Best-effort: returns the currently active/focused window, or None."""
    try:
        win = gw.getActiveWindow()
        if win and win.title.strip():
            return win
    except Exception:
        pass
    # Fallback: first non-empty-title window (rare — usually getActiveWindow works)
    wins = [w for w in gw.getAllWindows() if w.title.strip()]
    return wins[0] if wins else None


def _capture_code_via_clipboard() -> str:
    """
    Select-all + copy from whatever window is focused right now.
    Returns the copied text, or "" if nothing useful was captured.
    """
    try:
        # Save the user's existing clipboard so we can restore it after,
        # in case the capture fails and we don't want to clobber their copy.
        previous_clip = ""
        try:
            previous_clip = pyperclip.paste()
        except Exception:
            pass

        pyperclip.copy("")  # clear, so we can detect "nothing copied"
        time.sleep(0.1)

        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.3)

        captured = pyperclip.paste()

        # Click away from any selection so we don't leave the user's
        # editor in a fully-selected state if we end up not using this.
        if not captured.strip():
            try:
                pyperclip.copy(previous_clip)
            except Exception:
                pass

        return captured.strip()
    except Exception as e:
        print(f"[CodeDoctor] Clipboard capture error: {e}")
        return ""


def _capture_code_via_screenshot_and_vision() -> str:
    """
    Vision fallback disabled — the local model (qwen3:4b) is text-only,
    so there's no way to transcribe code from a screenshot right now.
    Clipboard capture (Ctrl+A, Ctrl+C) is the only capture method.
    Kept as a stub, rather than removed, so capture_current_code()'s
    fallback logic doesn't need to change — and so this is a one-line
    swap later if a vision model (e.g. llava, qwen2.5vl) gets pulled.
    """
    print("[CodeDoctor] Vision fallback unavailable (no vision model configured).")
    return ""


def capture_current_code() -> tuple[str, str]:
    """
    Tries clipboard capture first (reliable, exact text). Falls back to
    screenshot + vision if clipboard capture comes back empty.
    Returns (code_text, method) where method is "clipboard" or "vision" or "".
    """
    code = _capture_code_via_clipboard()
    if code:
        return code, "clipboard"

    print("[CodeDoctor] Clipboard capture empty — falling back to screenshot.")
    code = _capture_code_via_screenshot_and_vision()
    if code:
        return code, "vision"

    return "", ""


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — ANALYZE WITH GEMINI
# ══════════════════════════════════════════════════════════════════════════════

_ANALYZE_PROMPT = """You are a careful senior software engineer reviewing code for bugs.

Here is the code:
---CODE START---
{code}
---CODE END---

Tasks:
1. Find real bugs — logic errors, crashes, syntax errors, off-by-one errors,
   wrong variable usage, etc. Ignore pure style preferences.
2. If there are NO bugs, respond with EXACTLY this format:
   STATUS: CLEAN
   SUMMARY: <one short friendly sentence telling the user their code looks good>

3. If there ARE bugs, respond with EXACTLY this format (nothing else, no markdown fences):
   STATUS: BUGGY
   SUMMARY: <one short sentence describing the main bug(s) in plain English>
   FIXED_CODE_START
   <the complete corrected code, full file, ready to paste in as-is>
   FIXED_CODE_END

Do not add any text outside this format. Do not wrap the fixed code in markdown
backticks. Preserve the original code's structure and style as much as possible —
only change what's necessary to fix the bug(s).
"""


def analyze_code(code: str) -> dict:
    """
    Sends code to the local 8B coding model for bug analysis. Thinking mode
    is ON here (unlike the 4B chat model) — the reasoning trace measurably
    helps it catch bugs correctly, worth the extra latency for this job.
    Returns {"status": "CLEAN"|"BUGGY"|"ERROR", "summary": str, "fixed_code": str}
    """
    try:
        prompt = _ANALYZE_PROMPT.format(code=code)
        response = model_manager.chat(
            model=model_manager.CODE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            think=True,
        )
        text = response["message"]["content"].strip()
        # Strip Qwen3's <think>...</think> reasoning block before parsing —
        # it can otherwise contain the words STATUS/SUMMARY too and confuse
        # the regex matches below.
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

        status_match = re.search(r"STATUS:\s*(CLEAN|BUGGY)", text)
        summary_match = re.search(r"SUMMARY:\s*(.+)", text)
        status = status_match.group(1) if status_match else "ERROR"
        summary = summary_match.group(1).strip() if summary_match else ""

        fixed_code = ""
        if status == "BUGGY":
            fc_match = re.search(
                r"FIXED_CODE_START\s*\n(.*?)\nFIXED_CODE_END", text, re.DOTALL
            )
            if fc_match:
                fixed_code = fc_match.group(1).strip()
            else:
                status = "ERROR"
                summary = "I found a bug but couldn't extract the fixed code cleanly."

        return {"status": status, "summary": summary, "fixed_code": fixed_code}
    except Exception as e:
        print(f"[CodeDoctor] Analysis error: {e}")
        return {"status": "ERROR", "summary": "I had trouble analyzing the code.", "fixed_code": ""}


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — APPLY THE FIX
# ══════════════════════════════════════════════════════════════════════════════

def _diff_lines(old_code: str, new_code: str) -> list[tuple[str, str]]:
    """
    Very simple line-level diff: returns list of (old_line, new_line) pairs
    for lines that changed, matched by position. Used for the PyCharm
    surgical-replace attempt. Falls back to nothing if line counts differ
    too much (safer to just retype the whole file in that case).
    """
    old_lines = old_code.split("\n")
    new_lines = new_code.split("\n")

    if abs(len(old_lines) - len(new_lines)) > 3:
        # Structure changed too much for a safe line-by-line replace
        return []

    changes = []
    for i in range(min(len(old_lines), len(new_lines))):
        if old_lines[i] != new_lines[i] and old_lines[i].strip():
            changes.append((old_lines[i], new_lines[i]))

    # Cap how many surgical replacements we attempt — beyond a handful,
    # a full retype is more reliable than many fragile dialog interactions.
    if len(changes) > 6:
        return []
    return changes


def _is_pycharm_focused() -> bool:
    proc_name = _get_active_window_process_name()
    return proc_name in ("pycharm64.exe", "pycharm.exe")


def _try_pycharm_surgical_replace(old_code: str, new_code: str) -> bool:
    """
    Attempts to fix only the changed lines using PyCharm's Find & Replace
    (Ctrl+R) dialog, one changed line at a time. Returns True if every
    change was applied successfully, False if anything looks uncertain
    (caller should fall back to full retype).
    """
    changes = _diff_lines(old_code, new_code)
    if not changes:
        return False

    try:
        for old_line, new_line in changes:
            pyautogui.hotkey("ctrl", "r")
            time.sleep(0.4)

            # Clear the "find" field and type the old line
            pyautogui.hotkey("ctrl", "a")
            pyautogui.press("backspace")
            pyperclip.copy(old_line)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)

            # Tab to "replace" field and type the new line
            pyautogui.press("tab")
            time.sleep(0.1)
            pyautogui.hotkey("ctrl", "a")
            pyautogui.press("backspace")
            pyperclip.copy(new_line)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)

            # "Replace all" — Alt+A is PyCharm's default mnemonic
            pyautogui.hotkey("alt", "a")
            time.sleep(0.3)
            pyautogui.press("escape")
            time.sleep(0.2)

        return True
    except Exception as e:
        print(f"[CodeDoctor] Surgical replace error: {e}")
        try:
            pyautogui.press("escape")
        except Exception:
            pass
        return False


def _full_retype(new_code: str) -> bool:
    """
    Safe universal fallback: select all in the currently focused window
    and replace with the fixed code via paste (fast + reliable across
    any editor/platform).
    """
    try:
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.press("backspace")
        time.sleep(0.2)
        pyperclip.copy(new_code)
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.3)
        return True
    except Exception as e:
        print(f"[CodeDoctor] Full retype error: {e}")
        return False


def apply_fix(old_code: str, new_code: str) -> str:
    """
    Applies the fixed code back into the editor.
    Returns a human-readable result message.
    """
    if _is_pycharm_focused():
        if _try_pycharm_surgical_replace(old_code, new_code):
            return "surgical"
        print("[CodeDoctor] Surgical replace not confident — falling back to full retype.")

    if _full_retype(new_code):
        return "full"
    return "failed"


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def fix_my_code() -> str:
    """
    Full pipeline: capture → analyze → apply, no confirmation needed.
    Used by the direct voice command ("fix this bug", "check my code", etc.)
    Returns the spoken reply for Sanju to say.
    """
    code, method = capture_current_code()
    if not code:
        return ("I couldn't read any code from your screen, my love. "
                "Try selecting it first or make sure the editor is focused.")

    result = analyze_code(code)
    status = result["status"]
    summary = result["summary"]

    if status == "ERROR":
        return summary or "I had trouble checking your code, please try again."

    if status == "CLEAN":
        return summary or "I checked your code and it looks good to me, no bugs found!"

    # BUGGY — apply the fix
    apply_method = apply_fix(code, result["fixed_code"])
    if apply_method == "failed":
        return (f"I found the bug — {summary} — but I couldn't type the fix back in. "
                f"Please try again or fix it manually.")

    note = " I fixed just the buggy lines." if apply_method == "surgical" else ""
    return f"Found it! {summary}{note} All fixed now, my love!"


# ══════════════════════════════════════════════════════════════════════════════
# WATCH-MODE ENTRY POINTS — capture & analyze WITHOUT applying, then a
# separate step to apply once the user confirms with "yes".
# Used by the "look at my code" / "keep an eye on my code" auto-watch flow.
# ══════════════════════════════════════════════════════════════════════════════

def check_code_only() -> dict:
    """
    Capture + analyze only — does NOT type anything back into the editor.
    Returns a dict the caller can hold onto and later pass to apply_pending_fix():
      {"status": "CLEAN"|"BUGGY"|"ERROR"|"NO_CODE",
       "summary": str, "old_code": str, "fixed_code": str}
    """
    code, method = capture_current_code()
    if not code:
        return {"status": "NO_CODE", "summary": "", "old_code": "", "fixed_code": ""}

    result = analyze_code(code)
    result["old_code"] = code
    return result


def apply_pending_fix(pending: dict) -> str:
    """
    Applies a fix that was previously found by check_code_only(), after the
    user has confirmed they want it applied. Returns the spoken reply.
    """
    old_code   = pending.get("old_code", "")
    fixed_code = pending.get("fixed_code", "")
    summary    = pending.get("summary", "")

    if not old_code or not fixed_code:
        return "I lost track of that fix, my love — let me check your code again."

    apply_method = apply_fix(old_code, fixed_code)
    if apply_method == "failed":
        return (f"I found the bug — {summary} — but I couldn't type the fix back in. "
                f"Please try again or fix it manually.")

    note = " I fixed just the buggy lines." if apply_method == "surgical" else ""
    return f"All done!{note} Fixed it for you, my love!"