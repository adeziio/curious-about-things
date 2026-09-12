"""Temporary smoke test for the Curious About Things refactor."""
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config_loader import ConfigLoader
from ai.content_generator import ContentGenerator, write_content_files, read_content_file
from production.captions import build_caption_cues, write_srt
from production.footage.manager import create_video_provider
from production.footage.pexels import PexelsVideoProvider
from production.composer import Composer

config = ConfigLoader().load_all()
assert "video_provider" in config["app"], "video_provider missing"
assert config["app"]["video_provider"] == "pexels"
assert config["pexels"]["base_url"] == "https://www.pexels.com"
print("1. config OK")

# Content generator parsing (no Ollama call)
cg = ContentGenerator(config)
sample = '''```json
{
  "title": "The Animal That Never Sleeps",
  "summary": "Bullfrogs never truly sleep and it changes how we think about rest.",
  "narration": "There is an animal on Earth that seems to never truly sleep. Not once. Bullfrogs stay alert around the clock, and scientists who tested them for weeks found they respond just as fast at midnight as at noon. Sleep was long thought to be universal among vertebrates. The bullfrog challenges that idea. Its brain shows rest-like activity, yet it stays fully responsive. Researchers have puzzled over these frogs for decades. How does a creature survive without the deep sleep humans need? The answer may lie in how differently brains handle recovery. Some species enter brief micro-rests throughout the day instead of one long nap. If an animal can stay vigilant around the clock, what does that say about our need for eight hours of sleep? Maybe sleep is a negotiation between survival and recovery, not an absolute rule. Next time you yawn, remember the bullfrog watching from the pond. It makes you wonder what we might learn from its strange, sleepless biology.",
  "visuals": [
    {"context": "introduce bullfrog", "search_query": "bullfrog close up pond"},
    {"context": "bullfrog resting in water", "search_query": "frog sitting on lily pad"},
    {"context": "night testing", "search_query": "scientist working at night laboratory"},
    {"context": "frog observing", "search_query": "frog eyes close up"},
    {"context": "predator alert", "search_query": "heron hunting in shallow water"},
    {"context": "frog reaction", "search_query": "frog jumping slow motion"},
    {"context": "brain activity", "search_query": "brain scan MRI monitor"},
    {"context": "researchers studying", "search_query": "biologist writing notes in field"},
    {"context": "sleep concept", "search_query": "person sleeping in bed"},
    {"context": "micro rest idea", "search_query": "cat napping during the day"},
    {"context": "alert animal", "search_query": "deer alert in forest"},
    {"context": "night pond", "search_query": "pond at night moonlight"},
    {"context": "morning yawn", "search_query": "person yawning stretching"},
    {"context": "closing thought", "search_query": "starry night sky timelapse"}
  ]
}
```'''
parsed = cg.parse_content(sample)
assert parsed is not None and parsed["title"].startswith("The Animal")
validated = cg.validate_content(parsed)
assert len(validated["visuals"]) == 14
assert validated["summary"]
print("2. content generator OK:", validated["title"])

# write/read content files
tmp = Path("media/output/shorts/_smoketest")
if tmp.exists():
    shutil.rmtree(tmp)
tmp.mkdir(parents=True)
write_content_files(tmp, validated)
assert read_content_file(tmp)["narration"] == validated["narration"]
prompt_txt = (tmp / "prompt.txt").read_text(encoding="utf-8")
assert "TITLE: The Animal That Never Sleeps" in prompt_txt
assert "SUMMARY:" in prompt_txt
print("3. content files OK")

# Captions
words = [
    {"word": w, "start": i * 0.4, "end": i * 0.4 + 0.35}
    for i, w in enumerate(
        "There is an animal that never sleeps. Not even once. Scientists tested it for weeks.".split()
    )
]
cues = build_caption_cues(words, max_words_per_line=4)
assert cues and all(1 <= len(c["text"].split()) <= 8 for c in cues)
srt = write_srt(cues, tmp / "captions" / "captions.srt")
assert "1\n" in srt.read_text(encoding="utf-8")
print(f"4. captions OK ({len(cues)} cues)")

# Provider factory + helpers
provider = create_video_provider(config, notify=lambda m: None)
assert isinstance(provider, PexelsVideoProvider)
assert provider.slugify("person scrolling phone in bed!") == "person-scrolling-phone-in-bed"
assert provider._clip_id("https://videos.pexels.com/video-files/856973/856973-hd_1080_1920_25fps.mp4") == "856973"
assert provider._clip_id("https://example.com/no-id-here") == ""
print("5. provider factory + helpers OK")
print("5. provider factory + URL extraction OK")

# Composer end-to-end with synthetic footage (landscape -> must be cropped/scaled)
from moviepy import ColorClip, AudioFileClip, AudioClip
import numpy as np

footage_dir = tmp / "footage" / "test"
footage_dir.mkdir(parents=True)
ColorClip((640, 360), color=(180, 60, 90), duration=10).with_fps(30).write_videofile(
    str(footage_dir / "clip_001.mp4"), logger=None
)
footage2 = footage_dir / "clip_002.mp4"
ColorClip((720, 1280), color=(40, 120, 200), duration=4).with_fps(30).write_videofile(
    str(footage2), logger=None
)

# synthetic narration: 6-second tone (bypasses edge-tts network dependency here)
sr = 22050
frames = int(6 * sr)
snd = (np.sin(2 * np.pi * 220 * np.arange(frames) / sr) * 0.3).astype(np.float32)
audio = AudioClip(lambda t: np.stack([np.interp(t * sr, np.arange(frames), snd, left=0, right=0)] * 2, axis=-1), duration=6, fps=sr)
audio.write_audiofile(str(tmp / "audio" / "narration.mp3") if (tmp / "audio").exists() else str(tmp / "audio_narration.mp3"), logger=None)
(tmp / "audio").mkdir(exist_ok=True)
shutil.move(str(tmp / "audio_narration.mp3"), str(tmp / "audio" / "narration.mp3"))

import json
(tmp / "audio" / "words.json").write_text(json.dumps(words), encoding="utf-8")
narration = {
    "audio_path": str(tmp / "audio" / "narration.mp3"),
    "words_path": str(tmp / "audio" / "words.json"),
    "duration": 6.0,
    "voice": "test",
}

composer = Composer(config)
out = composer.compose(
    tmp, narration, cues,
    [[str(footage_dir / "clip_001.mp4")], [str(footage2)]],
    visuals=[{"search_query": "cat"}, {"search_query": "office"}],
    segment_count=3,
)
print("6. composer OK:", out)

# Verify the rendered video exists and is non-empty
assert out
video_path = Path(out)
assert video_path.exists(), "rendered video missing"
assert video_path.stat().st_size > 1024, "rendered video too small"
print("7. rendered video OK:", video_path.stat().st_size, "bytes")

shutil.rmtree(tmp)
print("ALL SMOKE TESTS PASSED")
