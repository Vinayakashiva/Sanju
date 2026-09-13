# Sanju — A Voice Companion with a Living 3D Avatar

Sanju is a desktop AI companion you talk to, not type to. Say the wake word
and she listens, thinks, and replies out loud — while a real-time 3D avatar
(rendered live with Three.js + VRM) reacts with expressions, lip-sync, and
full-body animation instead of a static chat window.

**What she can do:**
- Hold a natural voice conversation (wake-word activated, local LLM via Ollama)
- Speak with natural-sounding TTS (Kokoro), lip-synced to a live 3D model
- Launch apps, edit code, and run web searches on voice command
- Control music/Spotify playback, including dancing along to a song
- Express mood through the avatar — idle, thinking, listening, happy,
  concerned, and more — driven by her actual conversational state, not
  pre-scripted triggers

**Why the avatar matters:** most voice assistants are a text box with a
waveform. Sanju replaces that with a persistent, embodied character whose
body language matches what she's actually doing — turning "an assistant
that talks" into "a companion that's present."

---

## Under the hood

*(Everything below is the engineering changelog for the GIF → 3D VRM avatar
migration — kept here for technical reference.)*
