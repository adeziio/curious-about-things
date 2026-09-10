import json
import re
from pathlib import Path

from ai.base_ai_service import BaseAIService
from ai.providers.ollama_provider import OllamaProvider

class ContentGenerationError(RuntimeError):
    pass

# JSON schema passed to Ollama's structured-output mode. It grammar-enforces
# the response shape so the model cannot return short narrations: exactly 14
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
            # Word count per sentence is grammar-enforced by Ollama.
            # Target: 170-190 words total (60 seconds × 2.9 words/sec)
            # 14 sentences × 10-13 words = 140-182 words
            # Valid range: 150-210 words (generous buffer for TTS timing)
            "items": {"type": "string", "minLength": 50, "maxLength": 75},
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
            "Write the narration as EXACTLY 14 complete sentences, each sentence 10-13 words "
            "long, totaling 170-190 words. Never write strings of short fragments - every sentence "
            "must be a full, substantial spoken thought. A script outside 150-210 words is a FAILED response. "
            "Follow this blueprint exactly:\n"
            "- Sentence 1: the hook - a surprising claim or vivid moment (10-13 words).\n"
            "- Sentences 2-3: the setup - establish the situation so the viewer cares (10-13 words each).\n"
            "- Sentences 4-10: escalation - at least 4 different verified facts, each fully "
            "developed in its own sentence, with detail that deepens the intrigue (10-13 words each).\n"
            "- Sentences 11-12: the surprising reveal and the connection to the viewer (10-13 words each).\n"
            "- Sentence 13: the twist - a memorable observation or unexpected angle (10-13 words).\n"
            "- Sentence 14: the closing - a satisfying final thought or a natural curiosity question (10-13 words).\n"
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
            'into its 14 sentences. Each string is one complete spoken sentence of 9-13 words. '
            'Plain spoken text, no stage directions, no sound cues, no speaker labels.\n'
            '- "mood": 1-3 lowercase words describing the emotional tone. IMPORTANT: Choose mood words that match the story energy. Use words like: dramatic, tense, epic, mysterious, curious, dark, suspense, scary, horror, action, funny, comedy, playful, energetic, exciting, calm, peaceful, relaxing, chill, soft, gentle, warm, cozy, romantic, nostalgic, dreamy, sad, melancholic, happy, uplifting, inspiring.\\n'
            '- "visuals": an array of 14-18 objects, each {"context": "which part of the narration this footage supports", "search_query": "stock footage search phrase"}.\n\n'
            "FINAL CHECK BEFORE ANSWERING\n"
            "1. narration_sentences contains exactly 14 complete sentences.\n"
            "2. Each sentence should be 9-13 words. Total narration should be around 160-190 words (approximately 1 minute of spoken content at a natural pace).\n"
            "3. The visuals array contains at least 14 search queries covering the ENTIRE narration.\n"
            "4. Every sentence carries real, verified information - no filler.\n"
        )

    def generate(self, instruction=None):
        prompt = self.build_prompt(instruction)
        self.log("Generating episode content...")
        # Grammar-constrained output: the schema forces the model to emit
        # 14 narration sentence strings and 14-18 visual queries, which even
        # a small local model counts reliably (unlike word counts in prose).
        response = self.llm.generate(prompt, response_format=NARRATION_SCHEMA)
        content = self.parse_content(response)
        if content is None:
            raise ContentGenerationError("The AI response could not be parsed into valid episode content.")
        content = self.validate_content(content)
        self.log("Episode content generated: " + content["title"])
        return content

    def _clean_tts_text(self, text):
        """Sanitize text so only TTS-friendly characters remain.

        Removes JSON artifacts, symbols that text-to-speech engines read
        badly (&, #, @, +, =, /, ;, :), punctuation clusters like "?$,."
        and stray dollar signs that are not attached to a number.
        """
        s = str(text).strip()
        if not s:
            return s
        # Remove double quotes and curly apostrophes (normalize to straight apostrophes for TTS)
        s = s.replace('"', "").replace("\u2019", "'").replace("\u2018", "'")
        s = re.sub(r"[\[\]{}()]", "", s)
        # Remove JSON field names that might leak into narration
        # (cut everything from the leaked field name to the end)
        s = re.sub(
            r"\b(mood|title|summary|visuals|search_query|context)\s*:.*$",
            "",
            s,
            flags=re.IGNORECASE,
        )
        s = re.sub(
            r",\s*mood\b.*$",
            "",
            s,
            flags=re.IGNORECASE,
        )
        # Replace forbidden symbols with a space (keeps words separated)
        # instead of gluing them together ("obviously/sure" -> "obviously sure").
        # Apostrophes are intentionally preserved for contractions (you're, doesn't).
        s = re.sub(r"[^a-zA-Z0-9\s.,!?\-$%']", " ", s)
        # Remove commas inside numbers ("200,000" -> "200000") so TTS
        # reads the full number instead of pausing mid-number
        s = re.sub(r"(?<=\d),(?=\d)", "", s)
        # Collapse punctuation clusters to a single mark ("hero?$,." -> "hero?")
        s = re.sub(r"([.,!?\-$%])[.,!?\-$%]+", r"\1", s)
        # A dollar sign is only meaningful directly before a number
        s = re.sub(r"\$(?!\d)", "", s)
        # No space before punctuation, exactly one space after it
        s = re.sub(r"\s+([.,!?])", r"\1", s)
        s = re.sub(r"([.,!?])(?=[a-zA-Z0-9])", r"\1 ", s)
        # Clean up any double spaces created
        s = re.sub(r"\s+", " ", s).strip()
        # Remove trailing commas or artifacts before punctuation
        s = re.sub(r"\s*,\s*([.!?])", r"\1", s)
        return s

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
        title = self._clean_tts_text(str(data.get("title", "")))
        summary = self._clean_tts_text(str(data.get("summary", "")))
        # Structured mode returns the narration as an array of sentences.
        sentences = data.get("narration_sentences")
        if isinstance(sentences, list) and sentences:
            # Ensure each sentence ends with terminal punctuation so the TTS
            # engine pauses naturally at sentence boundaries.
            cleaned_sentences = []
            for s in sentences:
                s = self._clean_tts_text(s)
                if not s:
                    continue
                if s[-1] not in ".!?":
                    s += "."
                cleaned_sentences.append(s)
            narration = " ".join(cleaned_sentences).strip()
        else:
            narration = self._clean_tts_text(str(data.get("narration", "")))
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

        # Log narration length for reference (no validation - accept what the prompt gives)
        word_count = len(narration.split())
        self.log(f"Narration length: {word_count} words, {len(cleaned_visuals)} visual queries.")

        # Log sentence word counts for reference (no validation - accept what the prompt gives)
        narration_sentences = content.get("narration_sentences", [])
        sentences = narration_sentences if narration_sentences else re.split(r"(?<=[.!?])\s+", narration)
        for i, sentence in enumerate(sentences, 1):
            sentence_word_count = len(sentence.split())
            self.log(f"Sentence {i}: {sentence_word_count} words")

        # Enough visuals to keep a new clip roughly every 3-4 seconds (soft floor)
        min_visuals = 6
        if len(cleaned_visuals) < min_visuals:
            self.log(
                f"Note: only {len(cleaned_visuals)} visual queries (recommended: ~1 every 3-4 seconds). "
                "Proceeding anyway."
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
