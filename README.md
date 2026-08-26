# Sanju VRM Avatar Integration — Full Report

## 1. Starting point

Sanju already worked as a full voice assistant (wake word, LLM chat via Ollama,
Kokoro TTS, app launching, music/Spotify control, code editing, web search).
The character display was a set of animated **GIFs** switched per state
(idle / listening / thinking / speaking) inside `ui.py`'s `SanjuUI` window.

Separately, an **already-working standalone VRM prototype** existed
(`assistant.py`, `vrm_bridge.py`, `web/app.js`) — a Three.js + `@pixiv/three-vrm`
avatar renderer that was never wired into the actual app you run.

The work below ports that VRM renderer into the real app and fixes it up
into a genuinely usable companion display.

---

## 2. What changed, file by file

### `vrm_avatar/web/index.html` — **new file**
Hosts the Three.js/`@pixiv/three-vrm` canvas that `app.js` drives. Loaded
inside a `QWebEngineView` embedded in `ui.py`, fully transparent background,
importmap pulling `three`/`@pixiv/three-vrm` from a CDN.

### `ui.py` — GIF renderer replaced with VRM renderer
- `AvatarWidget` no longer uses `QMovie`/GIFs — it hosts a `QWebEngineView`
  pointed at `vrm_avatar/web/index.html`, with a `QWebChannel` bridge so
  JS can call back into Python (`animationFinished`, debug logging).
- **Public API preserved exactly** — `set_state()`, `.state`, `.dynamic_pulse`,
  `.update()`. `main.py`, `app_controller.py`, `ai_engine.py` needed **zero
  changes** for the swap itself.
- `.dynamic_pulse` (already fed by the live TTS volume elsewhere in the app)
  now doubles as the lip-sync driver — no new call site needed anywhere else.
- Removed the dark rounded "card" background and the green
  waveform/equalizer overlay that used to sit behind/on the GIF — she now
  renders directly on the transparent desktop with no visual chrome besides
  the close button.
- State → animation logic, all self-contained here:

| State(s) | Behavior |
|---|---|
| `IDLE`, `SPEAKING`, `SHY`, `CONCERNED`, `FOCUSED`, `RELAXED`, `CURIOUS`, `VOLUME`, `SCREENSHOT` | Loop `idle` (breathing/blinking only; lip-sync layers on top during `SPEAKING`) |
| `LISTENING` | Loop `wait` (attentive pose) |
| `THINKING` | Loop `thinking` |
| `TYPING` | One-shot `sit_to_type` → loop `typing` |
| `SLEEP` / `SLEEPING` | Loop `sitting_idle` |
| `HAPPY`, `EXCITED`, `LOVING`, `PROUD`, `TEASING` | One-shot `happy` |
| `SINGING` | Chains dance clips (never repeats the last one) for as long as the state stays `SINGING` |
| `POSE` | One-shot random hold from 4 pose clips (built, **not yet wired to a voice command**) |
| Waking from sleep (any transition out of `SLEEP*`) | Random greeting (`waving`/`standing_greeting`) plays first, *then* settles into the real target state |
| Change place / move left / move right | `slide_to_side()` (window position) + `avatar.walk_and_turn()` (turn + walk cycle), ported from the original `vrm_bridge.py` |

### `web/app.js` — extended, not redesigned
- **Fixed a real T-pose bug**: rapid repeated calls into the same
  in-progress animation (e.g. `LISTENING` firing twice quickly) were
  resetting that action's blend weight to 0 with nothing else weighted to
  cover the gap — a visible flash to the raw bind pose. Fixed by skipping
  the crossfade entirely when the target clip is already current/mid-fade.
- **Fixed lip-sync being barely visible**: `setMouthLevel()` (fed by TTS
  volume) was being stored but never actually applied to any expression —
  only `setViseme()` (real phoneme data, which this app's TTS pipeline
  never sends) drove the mouth. Wired volume → `aa` (mouth-open) directly,
  with a 1.8× gain for visibility.
- Added `playAnimationLoop()` — clips like `wait`/`sitting_idle`/`thinking`
  need to hold indefinitely, not play once (the original `playAnimation()`
  always plays exactly once, by design, for reaction clips).
- Corrected the loaded clip file list to match your actual `animations/`
  folder (several names were wrong — case mismatches, wrong subfolder after
  you reorganized the dance clips into `animations/dance/`).
- Removed the unused `Talking.fbx` clip path (that file doesn't exist in
  your project, and per your explicit request, speaking is lip-sync-only —
  no dedicated talking-loop animation).
- Added a pulled-back/lowered camera framing for `sit_to_type`/`typing` so
  her hands are actually in frame while typing (same mechanism the
  existing catwalk camera swing already used).
- Loads all newly-added FBX files: `wait`, `dance` (×7 variants),
  `Waving`, `Standing Greeting`, `Standing Up`, `Sitting Idle`,
  `Sit To Type`, `Typing`, `Thinking` (**filename assumed** — confirm/rename
  if yours differs), 4 pose clips.

### `ai_profile.py` — banned-phrase list hoisted to module scope
`BANNED_PHRASES` (the list telling the model not to say
"you're the real deal" / "I'm here for the good stuff") was local to one
function and only ever used as a soft prompt instruction. Moved to
module level so `ai_engine.py` can import and hard-enforce it.

### `ai_engine.py` — hard phrase filter added
The prompt instruction alone wasn't reliably stopping a small local model
from repeating that phrase. Added a regex-based scrubber applied at
**every point text is actually spoken or saved to memory** — including
before saving to conversation history, since a saved past use of the
phrase was likely feeding back into her own context and reinforcing it.

### `app_controller.py` — new voice intents
- `dance_command` — "dance", "let's dance", "show your dancing skills",
  "perform a dance", "dance for me". Picks a song biased toward the
  `kannada`/`hindi`/`love` mood pools (skips the sadder `comfort` pool and
  generic `any`), plays it, then minimizes the Spotify window once
  playback has started (it must stay focused *during* the automation
  itself — that's how the keystroke-based search/play works).
- `change_place`, `move_left`, `move_right` — toggles/sets which side of
  the screen she's on.

### `main.py` (your `main__1_.py`) — dispatch wiring only
- `dance_command` reuses the **existing** `music_playing` flag + Spotify
  watchdog infrastructure that `play_love_song` already had — "only start
  dancing once playback is confirmed" and "stop dancing when music stops"
  both come from that pre-existing watchdog, not new code.
- `change_place`/`move_left`/`move_right` calls `ui.avatar.walk_and_turn()`
  and `ui.slide_to_side()` in parallel with a matching duration, so the
  walk animation and the actual window slide stay in sync.

---

## 3. Known gaps / things to verify before recording

- **`Thinking.fbx` filename is a guess** — if your file is named
  differently, `app.js`'s clip list needs that one line updated or the
  `THINKING` state will silently fall back to idle.
- **`POSE` state has no voice trigger yet** — the animation logic exists
  and works if triggered (`ui.avatar.set_state("POSE")`), but no phrase in
  `app_controller.py` calls it.
- **`walk`/`walk2`/`lazy` loading isn't confirmed** on your current
  folder layout — check the terminal on startup for any
  `[VRM] Failed to load clip 'walk'` warnings before demoing "change place."
- **Mouth-open gain (1.8×)** was chosen without seeing the actual model —
  may need a tweak up or down once you see it live.
- **Song library isn't sub-categorized** into soft/chill/emotional tiers
  per language — dance picks from the existing `kannada`/`hindi`/`love`
  lists as a whole, not a finer "soft vs. upbeat" split.
- **No real Spotify API** — "confirmed playback" is a heuristic (the
  automation sequence completing without an error reply), not a true
  playback-state check.

## 4. Quick pre-recording checklist

1. Launch the app, confirm no `[VRM] Failed to load clip '...'` warnings
   in the terminal for any clip you plan to demo.
2. Say the wake word from a cold start — confirm the greeting flourish
   plays once, then she settles into an attentive pose, not stuck mid-pose.
3. Ask her something conversational — confirm mouth movement is visible
   and no green bars/dark box appear anywhere.
4. Say "dance" — confirm Spotify briefly appears then minimizes, music
   audibly starts, and she dances continuously (not just one clip) with
   visibly different moves in a row.
5. Say "change place" — confirm she turns, walks in place, and the window
   itself visibly slides to the other screen edge in sync.
6. Trigger a typing task — confirm she sits, and her hands are in frame.
7. Say "stop" mid-dance — confirm she stops and returns to idle smoothly.
