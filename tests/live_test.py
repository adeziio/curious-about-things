"""Live test: edge-tts narration + Pexels Selenium provider."""
import sys
import json
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config_loader import ConfigLoader

config = ConfigLoader().load_all()

# 1. edge-tts narration
from production.narration import generate_narration, load_word_timings

tmp = Path("media/output/shorts/_livetest")
if tmp.exists():
    shutil.rmtree(tmp, ignore_errors=True)
tmp.mkdir(parents=True)

result = generate_narration(
    "There is an animal on Earth that never sleeps. "
    "Not once. Scientists tested bullfrogs for weeks, "
    "and they reacted just as fast at midnight as at noon.",
    config["app"]["audio"],
    tmp / "audio",
    notify=lambda m: print("  [tts]", m),
)
print("narration duration:", result["duration"])
words = load_word_timings(result["words_path"])
print("word timings captured:", len(words))
assert result["duration"] > 2 and words, "TTS failed"

# 2. Pexels provider (single clip, headless)
from production.footage.pexels import PexelsVideoProvider

provider = PexelsVideoProvider(config, notify=lambda m: print("  [pexels]", m))
clips = provider.fetch(
    "bullfrog close up",
    tmp / "footage" / "bullfrog",
    max_videos=2,
)
print("downloaded clips:", [c.name for c in clips])
for clip in clips:
    size = clip.stat().st_size
    print(f"  {clip.name}: {size / 1024:.0f} KB")
    assert size > 50_000, "clip suspiciously small"

shutil.rmtree(tmp, ignore_errors=True)
print("LIVE TEST PASSED")
