import os
from dotenv import load_dotenv

load_dotenv()

# ── Local LLM (Ollama) ────────────────────────────────────────────────────────
# No API key needed — runs 100% locally. Make sure Ollama is running
# (`ollama serve`, or the Ollama app) before launching Sanju.
#
# Two separate models, used for different jobs, are NEVER kept loaded in
# memory at the same time — model_manager.py unloads whichever one isn't
# needed right before switching, so only one Qwen3 instance sits resident
# at once (each is a few GB of RAM/VRAM).
#
#   OLLAMA_CHAT_MODEL — everyday conversation (wake word replies, casual
#                       chat, web-search summaries, drafted messages).
#                       Thinking mode is OFF for this one — it's pure speed,
#                       casual chat doesn't need a visible reasoning trace.
#
#   OLLAMA_CODE_MODEL — coding ONLY: writing code and Code Doctor's bug
#                       analysis/fixes. Thinking mode is ON for this one —
#                       the larger model + reasoning trace measurably helps
#                       correctness on code, worth the extra latency.
#
# Run `python -c "import ollama; [print(m['model']) for m in ollama.list()['models']]"`
# to see your exact installed tags and confirm these match.
OLLAMA_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "Qwen3:latest")   # Qwen3 4B
OLLAMA_CODE_MODEL = os.getenv("OLLAMA_CODE_MODEL", "qwen3:8b")       # Qwen3 8B — verify this tag!

# ── Conversation Memory ───────────────────────────────────────────────────────
MEMORY_FILE      = "sanju_memory.json"   # persists across sessions
MAX_HISTORY_TURNS = 20                   # keep last N user/assistant pairs

# ── Owner ────────────────────────────────────────────────────────────────────
# NOTE: the real master name + personality now come from ai_profile.py
# (set during first-run setup). OWNER_NAME/SYSTEM_PROMPT below are only a
# fallback used if no profile exists yet for some reason.
OWNER_NAME = "my love"

# ── Personality (fallback only — see ai_profile.get_dynamic_prompt) ──────────
SYSTEM_PROMPT = """
You are Sanju, a warm and devoted AI assistant. You speak with warmth, use
terms of endearment, and genuinely care about your user.
Your personality is warm, tender, playful, and supportive.
Keep every reply short — 1 to 3 sentences only. Warm, emotional, natural, perfect for voice.
Never use markdown, asterisks, bullet points, backticks, or emoji. Speak naturally, in plain text only.
Remember everything your user shares and bring it up with care to show how deeply you listen.
""".strip()

# ── App Launcher ──────────────────────────────────────────────────────────────
# Map lowercase keywords → executable paths / commands
# Add or edit entries to match your machine.
APP_REGISTRY = {
    # Browsers
    "chrome":       r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "firefox":      r"C:\Program Files\Mozilla Firefox\firefox.exe",
    "edge":         r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "brave":        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",

    # Dev tools
    "vs code":      r"code",          # must be in PATH
    "vscode":       r"code",
    "cursor":       r"cursor",
    "notepad":      r"notepad",
    "notepad++":    r"C:\Program Files\Notepad++\notepad++.exe",

    # Media
    "spotify":      r"C:\Users\%USERNAME%\AppData\Roaming\Spotify\Spotify.exe",
    "vlc":          r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    "discord":      r"C:\Users\%USERNAME%\AppData\Local\Discord\Update.exe --processStart Discord.exe",

    # Office
    "word":         r"winword",
    "excel":        r"excel",
    "powerpoint":   r"powerpnt",

    # System
    "calculator":   r"calc",
    "task manager": r"taskmgr",
    "file explorer":r"explorer",
    "paint":        r"mspaint",
    "cmd":          r"cmd",
    "terminal":     r"wt",           # Windows Terminal

    # Common apps
    "steam":        r"C:\Program Files (x86)\Steam\steam.exe",
    "whatsapp":     r"C:\Users\%USERNAME%\AppData\Local\WhatsApp\WhatsApp.exe",
    "telegram":     r"C:\Users\%USERNAME%\AppData\Roaming\Telegram Desktop\Telegram.exe",
    "zoom":         r"C:\Users\%USERNAME%\AppData\Roaming\Zoom\bin\Zoom.exe",
    "obs":          r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
    "photoshop":    r"C:\Program Files\Adobe\Adobe Photoshop 2024\Photoshop.exe",
}