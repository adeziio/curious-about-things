import json
import re
from pathlib import Path

from ai.base_ai_service import BaseAIService
from ai.providers.ollama_provider import OllamaProvider

class ContentGenerationError(RuntimeError):
    pass

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
        desc = str(ch.get("description", ""))
        topics = self.format_bullets(c.get("topics", []))
        storytelling = self.format_bullets(c.get("storytelling", []))
        narration_rules = self.format_bullets(c.get("narration_rules", []))
        visual_rules = self.format_bullets(c.get("visual_rules", []))
        creative_directions = self.format_bullets(c.get("creative_directions", []))
        target_seconds = c.get("narration_target_seconds", 58)
        wps = c.get("words_per_second", 2.7)
        word_target = int(target_seconds * wps)
        instruction = str(instruction or "").strip()
        instruction_section = ""
        if instruction:
            instruction_section = ("\nUSER INSTRUCTION\nThe user gave the following direction "
                "for this episode. Follow it as closely as possible while keeping the "
                "quality requirements:\n" + instruction + "\n")
        return ("You are the creative writer for \"" + name + "\", a short-form video channel "
                "about anything genuinely fascinating.\n\nCHANNEL DESCRIPTION\n" + desc +
                "\n\nTOPIC FREEDOM\nYou may choose ANY subject that is interesting, surprising, "
                "or delightful. Example areas (you are not limited to these):\n" + topics +
                instruction_section +
                "\nSTORYTELLING PATTERN (guideline, not a rigid formula - adapt it naturally "
                "to the topic):\n" + storytelling +
                "\n\nNARRATION RULES\n" + narration_rules +
                "\n\nNARRATION LENGTH (HARD REQUIREMENT)\nThe narration must run 50-58 seconds "
                "when read aloud at a natural, energetic pace. That means the script MUST "
                "contain at least " + str(int(53 * wps)) + " words and should target " +
                str(word_target) + " words (acceptable range: " + str(int(53 * wps)) + " to " +
                str(int(59 * wps)) + " words). If the story needs more room, add more real "
                "facts, context, and escalation. Do not pad with filler or repetition - "
                "every sentence should carry meaningful information."
                "\n\nVISUAL SEARCH QUERY RULES\n" + visual_rules +
                "\n\nCREATIVE DIRECTION\n" + creative_directions +
                "\n\nOUTPUT FORMAT\nRespond with a single JSON object and nothing else, exactly in this shape:\n\n"
                '{\n  "title": "A short, clickable video title",\n  "summary": "One-sentence '
                'teaser of the episode",\n  "narration": "The full narration script. Plain spoken text. '
                'No stage directions, no sound cues, no speaker labels.",\n  "mood": '
                '"1-3 lowercase words describing the emotional tone of the story '
                '(for example: curious, mysterious, uplifting)",\n  "visuals": '
                '[\n    {"context": "Which part of the narration this footage supports", '
                '"search_query": "stock footage search phrase"}\n  ]\n}\n')

    def generate(self, instruction=None):
        prompt = self.build_prompt(instruction)
        max_attempts = 3
        for attempt in range(max_attempts):
            self.log(f"Generating episode content (attempt {attempt + 1}/{max_attempts})...")
            response = self.llm.generate(prompt, response_format="json")
            content = self.parse_content(response)
            if content is None:
                if attempt < max_attempts - 1:
                    self.log(f"Parse attempt {attempt + 1} failed, retrying...")
                    continue
                raise ContentGenerationError("The AI response could not be parsed into valid episode content.")
            try:
                content = self.validate_content(content)
            except ContentGenerationError as error:
                if attempt >= max_attempts - 1:
                    raise
                rejection = str(error)
                self.log(f"Validation attempt {attempt + 1} failed: {rejection}. Retrying...")
                lower = rejection.lower()
                if "too short" in lower or "too few usable visual" in lower:
                    follow = ("Your previous attempt was rejected. Rewrite with a substantially "
                              "longer, more detailed narration: it must contain 155-170 words so "
                              "it runs 52-58 seconds when spoken, and include at least 14 distinct, "
                              "findable stock-footage search queries (one new visual every 3-4 "
                              "seconds). Keep every factual claim verifiable; do not pad with "
                              "filler.")
                elif "too long" in lower:
                    follow = ("Your previous attempt was rejected because the narration was too "
                              "long. Rewrite with a tighter narration of 150-165 words so it runs "
                              "50-58 seconds when spoken, and keep 14-18 visual search queries.")
                else:
                    follow = ("Your previous attempt was rejected: " + rejection +
                              " Fix the problem and return a fully compliant JSON response.")
                prompt = self.build_prompt(follow)
                continue
            self.log("Episode content generated: " + content["title"])
            return content
        raise ContentGenerationError("Failed to generate valid content after multiple attempts.")

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
        narration = str(data.get("narration", "")).strip()
        visuals = data.get("visuals", [])
        if not title or not narration:
            return None
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
        min_words = int(53 * wps)   # ~53 seconds of spoken content (at energetic pace)
        max_words = int(59 * wps)   # hard cap so narration fits the Shorts window

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
