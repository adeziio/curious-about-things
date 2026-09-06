import json
import re
from pathlib import Path

from ai.base_ai_service import BaseAIService
from ai.providers.ollama_provider import OllamaProvider

class ContentGenerationError(RuntimeError):
    pass

# JSON schema passed to Ollama's structured-output mode. It grammar-enforces
# the response shape so the model cannot return short narrations: exactly 15
# narration sentences and 14-18 visual queries.
NARRATION_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "narration_sentences": {
            "type": "array",
            "minItems": 14,
            "maxItems": 14,
            # Character bounds are grammar-enforced by Ollama, which makes the
            # total narration word count deterministic (~130-160 words):
            # 14 sentences x 55-68 chars ~= 45-55 seconds at ~2.9 words/sec,
            # deliberately below the 60-second Shorts ceiling so nothing
            # gets cut off at the one-minute mark.
            "items": {"type": "string", "minLength": 55, "maxLength": 68},
        },
        "mood": {"type": "string"},
        "visuals": {
            "type": "array",
            "minItems": 14,
            "maxItems": 18,
            "items": {
                "type": "object",
                "properties": {
                    "context": {"type": "string"},
                    "search_query": {"type": "string"},
                },
                "required": ["context", "search_query"],
            },
        },
    },
    "required": ["title", "summary", "narration_sentences", "mood", "visuals"],
}

class ContentGenerator(BaseAIService):
    def __init__(self, config):
        super().__init__(config, "CONTENT")
        self.llm = OllamaProvider(config)
        self.channel_config = config["content"]
        self.generation_config = self.channel_config.get("content_generation", {})

    def format_bullets(self, values):
        if not isinstance(values, list):
            return ""
        return "\n".join(f"- {str(v).strip()}" for v in values if str(v).strip())

    def build_prompt(self, instruction=None):
        c = self.generation_config
        ch = self.channel_config.get("channel", {})
        name = str(ch.get("name", "Curious About Things"))
        desc = str(ch.get("channel_description", ch.get("description", "")))
        topics = self.format_bullets(c.get("topics", []))
        storytelling = self.format_bullets(c.get("storytelling", []))
        narration_rules = self.format_bullets(c.get("narration_rules", []))
        visual_rules = self.format_bullets(c.get("visual_rules", []))
        creative_directions = self.format_bullets(c.get("creative_directions", []))
        target_seconds = c.get("narration_target_seconds", 58)
        wps = c.get("words_per_second", 2.7)
        word_min = int(53 * wps)
        word_target = int(target_seconds * wps)
        instruction = str(instruction or "").strip()
        instruction_section = ""
        if instruction:
            instruction_section = ("USER INSTRUCTION\nThe user gave the following direction "
                "for this episode. Follow it as closely as possible while keeping every "
                "requirement below:\n" + instruction + "\n\n")
        # The narration-length requirement is stated FIRST (models weight the
        # start of the prompt most) as an explicit sentence-by-sentence
        # blueprint - LLMs follow sentence counts far more reliably than word
        # counts, and must be stopped from stacking tiny fragments.
        length_requirement = (
            "ABSOLUTE REQUIREMENT - NARRATION LENGTH\n"
            "Write the narration as EXACTLY 14 complete sentences, each sentence 9-12 words "
            "long, totaling about " + str(word_target) + " words. Never write strings of short "
            "fragments - every sentence must be a full, substantial spoken thought. A shorter "
            "script is a FAILED response. Follow this blueprint exactly:\n"
            "- Sentence 1: the hook - a surprising claim or vivid moment.\n"
            "- Sentences 2-3: the setup - establish the situation so the viewer cares.\n"
            "- Sentences 4-10: escalation - at least 4 different verified facts, each fully "
            "developed in its own sentence, with detail that deepens the intrigue.\n"
            "- Sentences 11-12: the surprising reveal and the connection to the viewer.\n"
            "- Sentence 13: the twist - a memorable observation or unexpected angle.\n"
            "- Sentence 14: the closing - a satisfying final thought or a natural curiosity question.\n"
        )
        return (
            "You are the creative writer for \"" + name + "\", a short-form video channel "
            "about anything genuinely fascinating.\n\n" +
            length_requirement +
            "\nCHANNEL DESCRIPTION\n" + desc +
            "\n\nTOPIC FREEDOM\nYou may choose ANY subject that is interesting, surprising, "
            "or delightful. Example areas (you are not limited to these):\n" + topics +
            "\n\n" + instruction_section +
            "STORYTELLING PATTERN (guideline, not a rigid formula - adapt it naturally "
            "to the topic):\n" + storytelling +
            "\n\nNARRATION RULES\n" + narration_rules +
            "\n\nVISUAL SEARCH QUERY RULES\n" + visual_rules +
            "\n\nCREATIVE DIRECTION\n" + creative_directions +
            "\n\nOUTPUT FORMAT\nReturn a single JSON object with exactly these fields:\n"
            '- "title": a short, clickable video title.\n'
            '- "summary": a one-sentence teaser of the episode.\n'
            '- "narration_sentences": an array of EXACTLY 14 strings - the narration split '
            'into its 14 sentences. Each string is one complete spoken sentence of 9-12 words. '
            'Plain spoken text, no stage directions, no sound cues, no speaker labels.\n'
            '- "mood": 1-3 lowercase words describing the emotional tone (for example: curious, mysterious, uplifting).\n'
            '- "visuals": an array of 14-18 objects, each {"context": "which part of the narration this footage supports", "search_query": "stock footage search phrase"}.\n\n'
            "FINAL CHECK BEFORE ANSWERING\n"
            "1. narration_sentences contains exactly 14 complete sentences of 9-12 words each.\n"
            "2. Every sentence is a full, substantial spoken thought of 12-14 words.\n"
            "3. The visuals array contains at least 14 search queries covering the ENTIRE narration.\n"
            "4. Every sentence carries real, verified information - no filler.\n"
        )

    def generate(self, instruction=None):
        prompt = self.build_prompt(instruction)
        self.log("Generating episode content...")
        # Grammar-constrained output: the schema forces the model to emit
        # 15 narration sentence strings and 14-18 visual queries, which even
        # a small local model counts reliably (unlike word counts in prose).
        response = self.llm.generate(prompt, response_format=NARRATION_SCHEMA)
        content = self.parse_content(response)
        if content is None:
            raise ContentGenerationError("The AI response could not be parsed into valid episode content.")
        content = self.validate_content(content)
        self.log("Episode content generated: " + content["title"])
        return content

    def parse_content(self, response):
        if not response:
            return None
        cleaned = response.strip()
        if "```" in cleaned:
            cleaned = re.sub(r"```(?:json)?", "", cleaned, flags=re.IGNORECASE)
            cleaned = cleaned.replace("```", "").strip()
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            data = self._extract_json(cleaned)
            if data is None:
                return None
        if not isinstance(data, dict):
            return None
        title = str(data.get("title", "")).strip()
        summary = str(data.get("summary", "")).strip()
        # Structured mode returns the narration as an array of sentences.
        sentences = data.get("narration_sentences")
        if isinstance(sentences, list) and sentences:
            # Ensure each sentence ends with terminal punctuation so the TTS
            # engine pauses naturally at sentence boundaries.
            cleaned_sentences = []
            for s in sentences:
                s = str(s).strip()
                if not s:
                    continue
                if s[-1] not in ".!?…":
                    s += "."
                cleaned_sentences.append(s)
            narration = " ".join(cleaned_sentences).strip()
        else:
            narration = str(data.get("narration", "")).strip()
        visuals = data.get("visuals", [])
        # Be tolerant of empty optional fields: derive a title from the
        # summary or the opening sentence rather than discarding a valid
        # grammar-constrained response.
        if not title:
            title = summary.split(".")[0].strip() if summary else ""
        if not narration:
            return None
        if not title:
            first = re.split(r"(?<=[.!?])\s+", narration)[0].strip()
            title = (first[:80] + "...") if len(first) > 80 else first
        if not isinstance(visuals, list):
            visuals = []
        cleaned_visuals = []
        for visual in visuals:
            if not isinstance(visual, dict):
                continue
            context = str(visual.get("context", "")).strip()
            search_query = str(visual.get("search_query", "")).strip()
            if not search_query:
                continue
            cleaned_visuals.append({"context": context, "search_query": search_query})
        if not summary:
            sentences = re.split(r"(?<=[.!?])\s+", narration)
            summary = sentences[0].strip() if sentences else ""
        mood = str(data.get("mood", "")).strip()
        return {"title": title, "summary": summary, "narration": narration, "mood": mood, "visuals": cleaned_visuals}

    def _extract_json(self, text):
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except (json.JSONDecodeError, TypeError):
            return None

    def validate_content(self, content):
        title = str(content.get("title", "")).strip()
        summary = str(content.get("summary", "")).strip()
        narration = str(content.get("narration", "")).strip()
        visuals = content.get("visuals", [])
        if not title:
            raise ContentGenerationError("The AI did not return a title.")
        if not narration:
            raise ContentGenerationError("The AI did not return a narration.")
        if not isinstance(visuals, list):
            visuals = []
        cleaned_visuals = []
        for visual in visuals:
            if not isinstance(visual, dict):
                continue
            context = str(visual.get("context", "")).strip()
            search_query = str(visual.get("search_query", "")).strip()
            if not search_query:
                continue
            cleaned_visuals.append({"context": context, "search_query": search_query})
        if len(cleaned_visuals) < 2:
            raise ContentGenerationError("The AI returned too few usable visual search queries.")

        if not summary:
            sentences = re.split(r"(?<=[.!?])\s+", narration)
            summary = sentences[0].strip() if sentences else ""

        # Validate narration length - reject scripts that cannot fill a
        # 50-58 second Short at an energetic speaking pace (~2.9 wps).
        word_count = len(narration.split())
        self.log(f"Narration length: {word_count} words, {len(cleaned_visuals)} visual queries.")

        wps = float(self.generation_config.get("words_per_second", 2.9))
        min_words = int(45 * wps)   # 45-second floor (the user-required minimum)
        max_words = int(56 * wps)   # 56-second ceiling; narration.py
                                    # applies a tiny (<5%) speed-up if slightly over

        if word_count < min_words:
            raise ContentGenerationError(
                f"Narration is too short ({word_count} words, minimum is {min_words}). "
                "Please expand the story with more verified facts, context, and escalation "
                "to reach 50-58 seconds."
            )
        if word_count > max_words:
            raise ContentGenerationError(
                f"Narration is too long ({word_count} words, maximum is {max_words}). "
                "Please condense the story to fit 50-58 seconds."
            )

        # Enough visuals to keep a new clip every 3-4 seconds.
        min_visuals = 12
        if len(cleaned_visuals) < min_visuals:
            raise ContentGenerationError(
                f"Too few usable visual search queries ({len(cleaned_visuals)}, minimum is "
                f"{min_visuals}). Add a distinct, findable stock query for roughly every "
                "3-4 seconds of the narration."
            )

        mood = self._normalize_mood(content.get("mood", ""))
        if mood:
            self.log(f"Episode mood: {' '.join(mood)}")
        return {"title": title, "summary": summary, "narration": narration, "mood": mood, "visuals": cleaned_visuals}

    def _normalize_mood(self, value):
        words = re.findall(r"[a-z]+", str(value or "").lower())
        seen = []
        for word in words:
            if word not in seen:
                seen.append(word)
        return seen[:3]

def write_content_files(episode_directory, content):
    episode_directory = Path(episode_directory)
    episode_directory.mkdir(parents=True, exist_ok=True)
    content_path = episode_directory / "content.json"
    with open(content_path, "w", encoding="utf-8") as file:
        json.dump(content, file, indent=2, ensure_ascii=False)
    divider = "=" * 72
    prompt_path = episode_directory / "prompt.txt"
    # IMPORTANT: TITLE, PROMPT, and SUMMARY must all be in the
    # same section between dividers so parse_prompt_file can find
    # them when it splits the file by the divider string.
    lines = [
        divider,
        f"TITLE: {content['title']}",
        f"PROMPT: {content['narration']}",
        f"SUMMARY: {content['summary']}",
        divider,
        ""
    ]
    prompt_path.write_text("\n".join(lines), encoding="utf-8")
    return content_path

def read_content_file(episode_directory):
    content_path = Path(episode_directory) / "content.json"
    if not content_path.is_file():
        return None
    with open(content_path, "r", encoding="utf-8") as file:
        return json.load(file)
