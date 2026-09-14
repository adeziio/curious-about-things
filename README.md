# 🤔 Curious About Things

Curious About Things is a one-click production pipeline for curiosity-driven
YouTube Shorts and Instagram Reels. The AI is free to choose any genuinely
fascinating topic — science, history, animals, geography, technology,
psychology, space, mysteries, unusual events, human behavior, or anything
else — and the application turns it into a finished vertical Short:

```text
Prompt (optional topic instruction)
  ↓
AI generates title + summary + narration + visual search queries
  ↓
Generate Video (one click)
  ├─ narration audio with word-level timings
  ├─ stock footage per visual search query
  ├─ royalty-free background music
  ├─ synchronized captions
  └─ compose & render 1080×1920 @ 30 fps Short
  ↓
Preview
  ↓
Upload to YouTube / Instagram
```

---

## Screenshots

![App overview — episode workflow](web/screenshots/overview.png)

The Web UI drives the whole pipeline: create an episode, run the video
job, preview the rendered Short in the episode grid, and publish it.

|                        |                          |
| ---------------------- | ------------------------ |
| ![YouTube upload](web/screenshots/youtube.png) | ![Instagram publish](web/screenshots/instagram.png) |

The images above are placeholders. Capture fresh screenshots of the app
and drop them into `web/screenshots/` — `overview.png`, `instagram.png`,
and `youtube.png` — and the README picks them up automatically.

---

## Requirements

* Python 3.12
* Chrome (for the Selenium-based stock footage provider)
* A local [Ollama](https://ollama.com) instance for content generation
* FFmpeg (bundled automatically via `imageio-ffmpeg`)

## Install

### Windows

```bat
install_windows.bat
```

### Linux / macOS

```bash
./install_linux.sh
```

## Run

### Windows

```bat
run_windows.bat
```

### Linux / macOS

```bash
./run_linux.sh
```

Then open **http://localhost:8000**.

There is also a CLI entry point (`python main.py`) that creates one episode
end-to-end without the web UI.

---

## Using the Web UI

1. **Create Episode** — optionally type a topic or instruction for the AI.
   With *Prompt Only* checked, only the AI content stage runs.
2. **Generate Video** — runs the full production pipeline.
3. **Preview** — watch the rendered episode right in the episode grid.
4. **Upload** — YouTube and Instagram upload forms are prefilled from the
   episode title and summary.

The **Videos** tab is a placeholder for future long-form support.

---

## Episode Workspace

Each episode lives in its own numbered directory under `media/output/shorts/`:

```text
media/output/shorts/<episode>/
├── content.json         # Structured AI content (title, summary, narration, mood, visuals)
├── prompt.txt           # Human-readable TITLE/PROMPT/SUMMARY (upload metadata)
├── audio/
│   ├── narration.mp3    # Generated narration audio
│   └── words.json       # Word-level timings from the TTS stream
├── footage/
│   └── <query>/         # Downloaded stock footage per visual search query
│       └── clip_NNN.mp4
├── music/               # Downloaded royalty-free background track + license metadata
├── captions/
│   └── captions.srt     # Captions synchronized with the narration
├── sfx/                 # Optional: sound effects mixed into the video
├── episode.mp4          # Final rendered Short
└── upload.txt           # Per-platform upload flags (created on first upload)
```

Optional SFX: any audio file in the episode's `sfx/` folder is mixed into
the video. A numeric filename prefix sets the offset —
`sfx/1.5__whoosh.mp3` plays 1.5 seconds into the video.

---

## Configuration

All configuration lives in `config/`:

| File             | Purpose                                                             |
| ---------------- | ------------------------------------------------------------------- |
| `app.json`       | `video_provider` / `music_provider` selection, Shorts specs (duration, 1080×1920, 30 fps), audio, captions |
| `pexels.json`    | All Pexels-specific settings (headless, timeouts, candidates, profile) |
| `freesafemusic.json` | FreeSafeMusic search/download settings for background music      |
| `content.json`   | Channel identity and AI content-generation guidance                 |
| `ai_models.json` | Language model settings (Ollama)                                    |
| `youtube.json`   | YouTube API settings and metadata defaults                          |
| `instagram.json` | Instagram API settings and caption defaults                         |

### Stock footage providers

The video-generation pipeline works with a generic provider interface:

```text
search(query)   → candidate videos
download(...)   → local footage
```

The active provider is selected with a single generic value:

```json
{ "video_provider": "pexels" }
```

All provider-specific settings are isolated in a dedicated section
(`config/pexels.json`), so adding another provider later (e.g. Pixabay)
means adding one module under `production/footage/` and one config file — the
rest of the pipeline stays unchanged.

### Shorts requirements

Configured in `app.json` under `shorts`:

* 9:16, 1080 × 1920, 30 FPS
* Target ≈ 50 seconds of narration

The **narration is the primary timeline**. The episode is synthesized at
its natural speaking pace and is never trimmed to fit a time window — the
video always matches the narration (plus a short tail), so nothing is
ever cut off. Resolution and frame rate are fixed by the `shorts`
settings when the video is rendered.

### AI content structure

The Prompt stage returns structured data the pipeline depends on:

```json
{
  "title": "...",
  "summary": "...",
  "mood": "...",
  "narration_sentences": ["... x14 ..."],
  "narration": "...",
  "visuals": [
    { "context": "...", "search_query": "..." }
  ]
}
```

The narration follows a flexible storytelling arc — hook, setup,
escalation, surprising reveal, connection, twist, closing thought — and
is generated as exactly 14 sentences targeting roughly 50 seconds when
spoken. Each sentence maps 1:1 to a visual search query, so the footage
changes in sync with what is being said. Visual search queries describe
what should appear in the footage; the application handles finding and
downloading it.

### Background music and SFX

The Generate Video stage downloads a royalty-free background track from
FreeSafeMusic through the configured `music_provider`; the file is stored
in the episode's `music/` directory for the render and its license
metadata is kept afterwards. The composer loops the track under the
narration at a low volume, and any optional SFX placed in the episode's
`sfx/` folder is mixed in as well. Narration is always clearly audible;
music and SFX support it without overpowering it.

---

## Project Layout

```text
ai/           AI content generation (Prompt stage, Ollama provider)
core/         Config loading and the top-level pipeline
production/   Video production: narration, footage & music providers, captions, composer, render
web/          HTTP server, job worker, and the single-page UI
youtube/      YouTube authentication and upload
instagram/    Instagram authentication and publishing
config/       Configuration files
media/        Generated episodes (output/shorts/) and provider browser profile
```
