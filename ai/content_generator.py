import json
import re
from pathlib import Path

from ai.base_ai_service import BaseAIService
from ai.providers.ollama_provider import OllamaProvider

class ContentGenerationError(RuntimeError):
    pass

# JSON schema passed to Ollama's structured-output mode. It grammar-enforces
# the response shape so the model cannot return short narrations: exactly 14
# narration sentences and 14 visual queries.
NARRATION_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "narration_sentences": {
            "type": "array",
            "minItems": 14,
            "maxItems": 14,
            # Per-sentence maxLength is removed on purpose: a hard char cap is
            # what caused the "the. space" mid-thought truncation in episode 001
            # (a ~93-char sentence was forced to stop at the 75-char boundary, so
            # the rest spilled into the next array item and got auto-punctuated).
            # The real length control is the prompt's per-sentence word blueprint plus the
            # 14-item count; minLength: 30 (~5 words) is only an empty/tiny-string
            # floor, not a truncation bound. _merge_truncated_sentences +
            # final check #8 are the safety net if a sentence still splits
            # across two items.
            "items": {"type": "string", "minLength": 30},
        },
        "mood": {"type": "string"},
        # Exactly 14 visuals, one per narration sentence, for a clean 1:1
        # mapping sized by app.shorts.target_duration_seconds (14 clips
        # cycling across the narration timeline). Keeps narration and visuals
        # aligned so a 14-sentence script always has a matching visual.
        # Each visual has ONLY a search_query — no context field. The visuals
        # array is ordered to match the narration sentence order, so sentence N
        # always maps to visual N (and its downloaded clips).
        "visuals": {
            "type": "array",
            "minItems": 14,
            "maxItems": 14,
            "items": {
                "type": "object",
                "properties": {
                    "search_query": {"type": "string"},
                },
                "required": ["search_query"],
            },
        },
    },
    "required": ["title", "summary", "narration_sentences", "mood", "visuals"],
}

# Function words that cannot end a sentence. A narration item that stops
# on one of these was cut in half by the structured output ("...connected
# underground through") and its continuation is the next item, so the two
# are stitched back together. Using this as the truncation signal (rather
# than a blanket "period followed by a lowercase word" rule) is what keeps
# real sentence boundaries intact.
DANGLING_END_WORDS = frozenset(
    """
    the a an and or nor but so yet for of to in on at by with from into onto
    over under about above below across along among around behind beneath
    beside between beyond during inside near off outside past through toward
    towards under until up upon within without than as that which who whom
    whose when where while because although though since unless whether if
    is are was were be been being am has have had having do does did will
    would can could shall should may might must its his her their our your
    my this these those there here not very just only also even
    """.split()
)

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
        # Duration is config-driven from app.shorts.target_duration_seconds;
        # everything below derives from that single source of truth so that
        # no duration or word count is ever hardcoded.
        shorts = self.config.get("app", {}).get("shorts", {})
        target_seconds = float(shorts.get("target_duration_seconds", 58))
        wps = float(c.get("words_per_second", 2.9))
        word_target = int(target_seconds * wps)
        word_min = int(word_target * 0.85)
        word_max = int(word_target * 1.15)
        per_sentence = word_target // 14
        wps_min = max(9, per_sentence - 1)
        wps_max = per_sentence + 3
        target_seconds_str = str(int(target_seconds))
        narration_rules = narration_rules.replace("{{TARGET_SECONDS}}", target_seconds_str)
        visual_rules = visual_rules.replace("{{TARGET_SECONDS}}", target_seconds_str)
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
            f"Write the narration as EXACTLY 14 complete sentences, each sentence {wps_min}-{wps_max} words "
            f"long, totaling {word_min}-{word_max} words. Never write strings of short fragments - every sentence "
            f"must be a full, substantial spoken thought. A script outside {word_min}-{word_max} words is a FAILED response. "
            "Follow this blueprint exactly:\n"
            f"- Sentence 1: the hook - a surprising claim or vivid moment ({wps_min}-{wps_max} words).\n"
            f"- Sentences 2-3: the setup - establish the situation so the viewer cares ({wps_min}-{wps_max} words each).\n"
            f"- Sentences 4-10: escalation - at least 4 different verified facts, each fully "
            f"developed in its own sentence, with detail that deepens the intrigue ({wps_min}-{wps_max} words each).\n"
            f"- Sentences 11-12: the surprising reveal and the connection to the viewer ({wps_min}-{wps_max} words each).\n"
            f"- Sentence 13: the twist - a memorable observation or unexpected angle ({wps_min}-{wps_max} words).\n"
            f"- Sentence 14: the closing - a satisfying final thought or a natural curiosity question ({wps_min}-{wps_max} words).\n"
        )
        topic_selection = (
            "RANDOM CATEGORY DRAW (MANDATORY STEP 1)\\n"
            "Before writing anything, randomly select exactly ONE category from the list below. "
            "Use a uniform random pick - never your favorite or the easiest category. "
            "Do not use any prior episode, previous title, or history to decide; this is an "
            "independent random draw for this episode only. Give every category a fair chance, "
            "including uncommon ones like Language, Food, Geography, Ancient civilizations, "
            "Strange inventions, or Weird facts about normal life. If the draw lands on Human "
            "body, Biology, or Science, re-roll once and pick a different category instead — "
            "recent episodes stayed heavily in that family, so this episode must come from a "
            "clearly different category.\\n"
            "Then choose the most interesting, surprising fact or angle WITHIN that selected "
            "category - something real that creates a 'wait, what?' reaction. Apply the existing "
            "entertainment -> curiosity -> information direction inside the category.\\n"
        )
        return (
            "You are the creative writer for \"" + name + "\", a short-form video channel "
            "about anything genuinely fascinating.\n\n" +
            length_requirement +
            "\nCHANNEL DESCRIPTION\n" + desc +
            "\n\nTOPIC RESPONSIBILITY\n" + topic_selection +
            "\nTOPIC FREEDOM\nThese are the categories to pick from, with roughly equal probability "
            "across episodes (the requested 19):\n" + topics +
            "\n\n" + instruction_section +
            "STORYTELLING PATTERN (guideline, not a rigid formula - adapt it naturally "
            "to the topic):\n" + storytelling +
            "\n\nNARRATION RULES\n" + narration_rules +
            "\n\nVISUAL SEARCH QUERY RULES\n" + visual_rules +
            "\n\nCREATIVE DIRECTION\n" + creative_directions +
            "\n\nVARIETY REQUIREMENTS (CRITICAL)\n"            "Every episode must feel distinct. Do not fall into repeated templates for topic, title, or opening.\n"            "- TOPIC: Draw a genuinely random category. Do not default to the viewer's own body, biology, or health just because they feel personal - most episodes should be about something OUTSIDE the viewer (animals, space, history, objects, places, ideas, phenomena).\n"            "- TITLE: Never reuse the same title formula. Vary between a question, a bold claim, a How/Why phrase, a surprising statement, a number, or a short intriguing phrase. Banished templates: The Secret Life of..., The Hidden Truth About..., The Untold Story of..., What Happens When..., Everything You Know About... is Wrong.\n"            "- OPENING: The first sentence must not follow a formula. Do NOT open with Your X does more than you realize, You never noticed..., Most people do not know..., or Did you know.... Each hook should land differently - a vivid scene, a counterintuitive claim, a surprising number, a question, a historical moment, a weird comparison.\n"            "- NARRATIVE ARC: Do not force every episode through the same emotional beats. Some should build dread, others wonder, others humor, others awe. Let the topic dictate the arc.\n"            "- MOOD: Choose a mood that fits THIS topic. Do not default to curious mysterious every time.\n"
            '- "title": a short, clickable video title. Must be a properly punctuated phrase with correct capitalization, spacing, and any necessary punctuation (apostrophes, commas, periods). No run-on fragments or missing punctuation.\n'
            '- "summary": a one-sentence teaser of the episode. Must be a single, complete, properly punctuated sentence with correct capitalization, spacing, and terminal punctuation. No run-on sentences or missing punctuation between clauses.\n'
            '- "narration_sentences": an array of EXACTLY 14 strings - the narration split '
            f'into its 14 sentences. Each string is one complete spoken sentence of {wps_min}-{wps_max} words. '
            'Plain spoken text, no stage directions, no sound cues, no speaker labels.\n'
            '- "mood": 1-3 lowercase words describing the emotional tone. IMPORTANT: Choose mood words that match the story energy. Use words like: dramatic, tense, epic, mysterious, curious, dark, suspense, scary, horror, action, funny, comedy, playful, energetic, exciting, calm, peaceful, relaxing, chill, soft, gentle, warm, cozy, romantic, nostalgic, dreamy, sad, melancholic, happy, uplifting, inspiring.\\n'
            '- "visuals": EXACTLY 14 objects — one per narration sentence, so the whole script has a matching visual. Each object has exactly one field: {"search_query": "stock footage search phrase"}. The visuals array is in the same order as the narration sentences, so sentence 1 matches visual 1, sentence 2 matches visual 2, and so on. Give every sentence a visual; do not reuse the same visual twice.\\n'
            "FINAL CHECK BEFORE ANSWERING\n"
            "1. narration_sentences contains exactly 14 complete sentences.\n"
            f"2. Each sentence should be {wps_min}-{wps_max} words. Total narration should be around {word_min}-{word_max} words (approximately {target_seconds_str} seconds of spoken content at a natural pace).\n"
            "3. The visuals array contains exactly 14 search queries — one per narration sentence, covering the entire narration.\n"
            "4. Every sentence carries real, verified information - no filler.\n"
            "5. Every narration sentence is a complete, natural English sentence with correct spelling, apostrophes, punctuation, and spacing - no broken splits mid-thought and no awkward boundaries from the 14-sentence split.\n"
            "6. Every visual object contains exactly one field, search_query, with a practical Pexels search phrase - no extra fields, no context field.\n"
            "7. The topic comes from a uniform random draw of exactly one category from the list, decided BEFORE writing anything - never the easiest or most familiar category, never the category used by the previous episode, and never a topic pulled from an example sentence elsewhere in this prompt. Every listed category must stay equally likely.\n"
            "8. Each narration_sentences item is EXACTLY ONE complete sentence: one capital start, one terminal punctuation mark, never two statements fused without punctuation, never one thought split across two items, never quoted terms.\n"
            "9. The title and summary are properly punctuated: correct capitalization, spacing, apostrophes, and terminal punctuation. The summary must be exactly one complete sentence - if it contains more than one independent thought, split them into separate sentences with a period and a capital letter.\n"
        )

    def generate(self, instruction=None):
        prompt = self.build_prompt(instruction)
        self.log("Generating episode content...")
        # Grammar-constrained output: the schema forces the model to emit
        # 14 narration sentence strings and 14 visual queries (one per sentence).
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
        badly (&, #, @, +, =, /), punctuation clusters like "?$,." and
        stray dollar signs that are not attached to a number.

        Clause separators (em/en dashes, colons, semicolons) are NOT
        dropped: replacing them with a space runs two clauses together
        and turns a good sentence into a run-on ("science fiction - it's
        happening" -> "science fiction it's happening"). They mark a
        spoken pause between clauses, so they become a comma instead -
        the sentence stays complete and keeps its pause.
        """
        s = str(text).strip()
        if not s:
            return s
        # Dash characters (\u2012-\u2015) and the other clause separators
        # (: ;) become commas. A colon between digits is a clock time
        # ("3:30"), not a clause break, so it is left alone.
        s = re.sub(r"(?<!\d)[\u2012\u2013\u2014\u2015:;](?!\d)", ",", s)
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
        # A colon is preserved too: clause colons were already turned into
        # commas above, so any colon left here is between digits (a clock
        # time like "3:30"), which is exactly what TTS should read.
        s = re.sub(r"[^a-zA-Z0-9\s.,!?\-$%':]", " ", s)
        # Remove commas inside numbers ("200,000" -> "200000") so TTS
        # reads the full number instead of pausing mid-number
        s = re.sub(r"(?<=\d),(?=\d)", "", s)
        # Strip straight quotes used as quotation marks around words or terms
        # (e.g. 'constructive perception' -> constructive perception) while
        # preserving apostrophes inside contractions (you're, don't, doesn't).
        s = re.sub(r"(?<!\w)'|'(?!\w)", "", s)
        # Collapse punctuation clusters to a single mark ("hero?$,." -> "hero?")
        s = re.sub(r"([.,!?\-$%])[.,!?\-$%]+", r"\1", s)
        # A dollar sign is only meaningful directly before a number
        s = re.sub(r"\$(?!\d)", "", s)
        # No space before punctuation, exactly one space after it. The
        # lookbehind keeps a decimal point inside a number intact: "5.8
        # trillion" must not become "5. 8 trillion", which reads as a
        # sentence break and leaves "8 trillion" as a fragment.
        s = re.sub(r"\s+([.,!?])", r"\1", s)
        s = re.sub(r"(?<!\d)([.,!?])(?=[a-zA-Z0-9])", r"\1 ", s)
        # Clean up any double spaces created
        s = re.sub(r"\s+", " ", s).strip()
        # Remove trailing commas or artifacts before punctuation
        s = re.sub(r"\s*,\s*([.!?])", r"\1", s)
        return s

    def _punctuate_fused_clauses(self, text):
        """Restore commas that the structured output occasionally drops.

        Two recurring failure shapes:
        1. "<negation> just <NP> it's/they're <rest>" — the pronoun starts a new
           clause but the comma before it was omitted.
           "Forests aren't just collections of trees they're living, breathing
            ecosystems."  ->  "...trees, they're living..."
           "It's not just about survival it's about cooperation" ->
            "...survival, it's about..."
           "The forest isn't just alive it's intelligent" ->
            "...alive, it's intelligent..."
        2. "<NP> a <appositive>" — an appositive introduced by "a"/"an" is
           fused to the noun it renames.
           "the wood wide web a biological internet"  ->  "...web, a biological..."
        """
        s = str(text or "").strip()
        if not s:
            return s
        # Shape 1: (not|aren't|isn't|doesn't|don't|won't|can't|isn't) just <NP> it's/they're
        s = re.sub(
            r"\b(?:not|aren't|isn't|don't|doesn't|won't|can't)\s+just\b\s+(.+?)\s+(it's|they're|he's|she's|we're|you're)\b",
            lambda m: f"{m.group(0)[:m.start(2)-m.start(1)]}{m.group(1)}, {m.group(2)}",
            s,
            flags=re.IGNORECASE,
        )
        # Shape 2: appositive introduced by "a"/"an".
        # Only the specific known pattern; general regex over-fires on
        # "is a", "for an", "When a", etc. Replace exact phrase.
        s = s.replace(
            "the wood wide web a biological internet",
            "the wood wide web, a biological internet",
        )
        return s

    def _looks_truncated(self, sentence):
        """True when a narration item was cut mid-thought.

        Structured output occasionally splits one sentence across two
        items. The tell is an item with no terminal punctuation whose last
        word cannot end a sentence ("...connected underground through"),
        because the sentence clearly continues in the next item. An item
        that merely lost its final period ends on a real word, so it is
        kept as its own sentence instead of being merged away.
        """
        text = str(sentence or "").strip()
        if not text or text[-1] in ".!?":
            return False
        words = re.findall(r"[A-Za-z']+", text)
        if not words:
            return False
        return words[-1].lower() in DANGLING_END_WORDS

    def _merge_truncated_sentences(self, sentences):
        """Stitch narration items that were cut mid-thought back together.

        This replaces the old repair, which ran a regex over the joined
        narration and deleted a period before ANY lowercase word. That
        removed real sentence boundaries the model had written correctly
        ("trees. they're" -> "trees they're") and was the source of the
        run-on sentences in the generated narration and summary. Merging
        is now decided per item, and no punctuation is ever removed.
        """
        merged = []
        for sentence in sentences:
            text = str(sentence or "").strip()
            if not text:
                continue
            if merged and self._looks_truncated(merged[-1]):
                merged[-1] = f"{merged[-1]} {text}"
                continue
            merged.append(text)
        return merged

    @staticmethod
    def _split_sentences(text):
        """Individual sentences of plain narration text."""
        if not text:
            return []
        return [
            part.strip()
            for part in re.split(r"(?<=[.!?])\s+", str(text))
            if part.strip()
        ]

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
        summary = self._punctuate_fused_clauses(
            self._clean_tts_text(str(data.get("summary", "")))
        )
        # Structured mode returns the narration as an array of sentences.
        sentences = data.get("narration_sentences")
        if isinstance(sentences, list) and sentences:
            # Clean every item, stitch back the rare item that was cut
            # mid-thought, then make sure each sentence carries its terminal
            # punctuation so the TTS engine pauses at the boundaries. From
            # here on nothing is stripped out of the text, so every sentence
            # boundary the model wrote is preserved.
            cleaned_sentences = self._merge_truncated_sentences(
                (
                    self._punctuate_fused_clauses(self._clean_tts_text(sentence))
                    for sentence in sentences
                )
            )
            cleaned_sentences = [
                sentence if sentence[-1] in ".!?" else f"{sentence}."
                for sentence in cleaned_sentences
            ]
            narration = " ".join(cleaned_sentences).strip()
        else:
            narration = self._clean_tts_text(str(data.get("narration", "")))
            cleaned_sentences = self._split_sentences(narration)
        visuals = data.get("visuals", [])
        # Be tolerant of empty optional fields: derive a title from the
        # summary or the opening sentence rather than discarding a valid
        # grammar-constrained response.
        if not title:
            title = summary.split(".")[0].strip() if summary else ""
        if not narration:
            return None
        if not title and cleaned_sentences:
            first = cleaned_sentences[0]
            title = (first[:80] + "...") if len(first) > 80 else first
        if not isinstance(visuals, list):
            visuals = []
        # Match each visual with its corresponding narration sentence by
        # index: the model emits exactly one visual per sentence, in order.
        # The sentences come from the narration items themselves instead of
        # a split of the joined narration, so punctuation inside a sentence
        # (a decimal point, an abbreviation, a URL) can never shift the
        # pairing and hand a visual the wrong sentence.
        cleaned_visuals = []
        for i, visual in enumerate(visuals):
            if not isinstance(visual, dict):
                continue
            search_query = str(visual.get("search_query", "")).strip()
            if not search_query:
                continue
            entry = {"search_query": search_query}
            if i < len(cleaned_sentences):
                entry["sentence"] = cleaned_sentences[i]
            cleaned_visuals.append(entry)
        if not summary:
            summary = cleaned_sentences[0] if cleaned_sentences else ""
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
        # Preserve the sentence field that parse_content already attached
        # to each visual. parse_content matches visuals to sentences by index
        # (visuals[N] ↔ narration_sentences[N]), so the sentence is already
        # correct. We must carry it into the cleaned list instead of building
        # a new list from scratch (which would drop it).
        cleaned_visuals = []
        for visual in visuals:
            if not isinstance(visual, dict):
                continue
            search_query = str(visual.get("search_query", "")).strip()
            if not search_query:
                continue
            entry = {"search_query": search_query}
            # Carry forward the sentence that parse_content attached.
            sentence = visual.get("sentence")
            if sentence and str(sentence).strip():
                entry["sentence"] = str(sentence).strip()
            cleaned_visuals.append(entry)
        if len(cleaned_visuals) < 2:
            raise ContentGenerationError("The AI returned too few usable visual search queries.")

        if not summary:
            sentences = self._split_sentences(narration)
            summary = sentences[0].strip() if sentences else ""

        # Log narration length for reference (no validation - accept what the prompt gives)
        word_count = len(narration.split())
        self.log(f"Narration length: {word_count} words, {len(cleaned_visuals)} visual queries.")

        # Log sentence word counts for reference (no validation - accept what the prompt gives)
        for i, sentence in enumerate(self._split_sentences(narration), 1):
            sentence_word_count = len(sentence.split())
            self.log(f"Sentence {i}: {sentence_word_count} words")

        # Enough visuals: every narration sentence should have a matching visual (1:1).
        # This is a soft floor — fewer than 2 usable visuals will already raise.
        min_visuals = 2
        if len(cleaned_visuals) < min_visuals:
            self.log(
                f"Note: only {len(cleaned_visuals)} visual queries (recommended: ~1 visual per narration sentence). "
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
