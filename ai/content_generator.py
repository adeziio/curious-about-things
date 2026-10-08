import json
import re
from pathlib import Path

import requests

from ai.base_ai_service import BaseAIService
from ai.providers.ollama_provider import OllamaProvider

class ContentGenerationError(RuntimeError):
    pass

# How long each visual should cover: 60 / 14 = the 14 segments per episode
# this project was tuned to. The only duration setting is
# app.json -> shorts.target_duration_seconds, and the segment count is
# derived from it, so there is no second number to keep in sync.
SECONDS_PER_VISUAL = 60 / 14

DEFAULT_TARGET_SECONDS = 60

WIKIPEDIA_CANDIDATE_COUNT = 100
# One API call must return every candidate: the MediaWiki API serves at
# most ONE whole-article extract per request (exlimit is silently lowered
# to 1), so candidates are requested as lead sections (exintro), which also
# gives every candidate the same summary-length basis for the topic choice.
WIKIPEDIA_RANDOM_API = (
    "https://en.wikipedia.org/w/api.php?"
    "action=query&generator=random&grnnamespace=0"
    f"&grnlimit={WIKIPEDIA_CANDIDATE_COUNT}"
    "&prop=extracts&explaintext=1&exintro=1"
    f"&exlimit={WIKIPEDIA_CANDIDATE_COUNT}&format=json"
)
WIKIPEDIA_USER_AGENT = "CuriousAboutThings/1.0 (content generation)"

# How much of each candidate the topic-selection call sees. A random
# article's lead can run to tens of KB, so the preview is sliced to keep
# the pick prompt well inside the model's context window. The chosen
# article's FULL extract - not this preview - is what feeds the episode's
# source material below.
SELECTION_PREVIEW_CHARS = 1200

# A floor on the sentence blueprint, not a tuning setting: sentence_blueprint()
# spends 7 sentences on fixed beats (hook, setup, reveal, twist, closing), so
# fewer than 8 would leave no room for escalation. Episodes are written to fill
# the target duration, so this only bites on unusually short targets.
MIN_SEGMENT_COUNT = 8
MAX_SEGMENT_COUNT = 40

# Sentences the blueprint always spends on the same named beats, whatever the
# configured count is. Everything left over becomes escalation.
BLUEPRINT_FIXED_SENTENCES = 7


def sentence_blueprint(segment_count):
    """
    Split the episode's sentences into named beats, scaled to segment_count.

    Returns (description, spans_multiple_sentences) pairs so the prompt can
    phrase the word target correctly. At the default 14 this reproduces the
    original hand-written blueprint exactly: 1 hook, 2 setup, 7 escalation,
    2 reveal, 1 twist, 1 closing.
    """

    escalation = max(1, segment_count - BLUEPRINT_FIXED_SENTENCES)
    last_escalation = 3 + escalation
    reveal_last = last_escalation + 2
    twist = reveal_last + 1
    closing = twist + 1
    return [
        ("Sentence 1: the hook - a surprising claim or vivid moment", False),
        ("Sentences 2-3: the setup - establish the situation so the viewer cares", True),
        (f"Sentences 4-{last_escalation}: escalation - at least 4 different verified "
         "facts, each fully developed in its own sentence, with detail that deepens "
         "the intrigue", True),
        (f"Sentences {last_escalation + 1}-{reveal_last}: the surprising reveal and "
         "the connection to the viewer", True),
        (f"Sentence {twist}: the twist - a memorable observation or unexpected angle", False),
        (f"Sentence {closing}: the closing - a satisfying final thought or a natural "
         "curiosity question", False),
    ]


def build_schema(segment_count):
    """
    JSON schema passed to Ollama's structured-output mode. It grammar-enforces
    the response shape so the model cannot return short narrations: exactly
    segment_count narration sentences and segment_count visual queries.

    Built per call instead of held as a module constant because the count
    comes from config.
    """

    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "narration_sentences": {
                "type": "array",
                "minItems": segment_count,
                "maxItems": segment_count,
                # Per-sentence maxLength is removed on purpose: a hard char cap is
                # what caused the "the. space" mid-thought truncation in episode 001
                # (a ~93-char sentence was forced to stop at the 75-char boundary, so
                # the rest spilled into the next array item and got auto-punctuated).
                # The real length control is the prompt's per-sentence word blueprint plus the
                # item count; minLength: 30 (~5 words) is only an empty/tiny-string
                # floor, not a truncation bound. _merge_truncated_sentences +
                # final check #8 are the safety net if a sentence still splits
                # across two items.
                "items": {"type": "string", "minLength": 30},
            },
            "mood": {"type": "string"},
            # Exactly one visual per narration sentence, for a clean 1:1
            # mapping sized by app.shorts.target_duration_seconds (the clips
            # cycle across the narration timeline). Keeps narration and visuals
            # aligned so the script always has a matching visual.
            # Each visual has ONLY a search_query — no context field. The visuals
            # array is ordered to match the narration sentence order, so sentence N
            # always maps to visual N (and its downloaded clips).
            "visuals": {
                "type": "array",
                "minItems": segment_count,
                "maxItems": segment_count,
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

def build_selection_schema(candidate_count):
    """
    Reply shape for the topic-selection call, enforced by Ollama's
    structured-output mode. Grammar-constraining the response to a single
    integer in 1..candidate_count means the pick cannot come back as free
    text, so one parse is all the selection step needs.
    """
    return {
        "type": "object",
        "properties": {
            "choice": {
                "type": "integer",
                "enum": list(range(1, candidate_count + 1)),
            }
        },
        "required": ["choice"],
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
        self.last_source_material = ""

    def format_bullets(self, values):
        if not isinstance(values, list):
            return ""
        return "\n".join(f"- {str(v).strip()}" for v in values if str(v).strip())

    def random_wikipedia_candidates(self):
        """
        Fetch WIKIPEDIA_CANDIDATE_COUNT random articles in ONE API call.

        Returns a list of (title, extract) pairs. The API only honors
        exlimit > 1 for intro extracts, which is why the URL requests
        exintro; whole-article mode would silently return just one
        extractable page and break the multi-candidate choice.
        """
        response = requests.get(
            WIKIPEDIA_RANDOM_API,
            headers={"User-Agent": WIKIPEDIA_USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
        pages = response.json()["query"]["pages"]
        candidates = []
        for page in pages.values():
            title = page.get("title", "")
            extract = page.get("extract", "")
            if title and extract:
                candidates.append((title, extract))
        return candidates

    def choose_wikipedia_article(self, candidates):
        """
        One Ollama call: have the model pick the most interesting and
        curiosity-provoking candidate. Returns the chosen (title, extract).

        The reply is grammar-constrained to an integer in 1..N, so it is
        always parseable; if a reply still fails to parse, the first
        candidate is used rather than retrying (no retry logic here).
        """
        listing = "\n\n".join(
            f"CANDIDATE {index}\nTitle: {title}\n{extract[:SELECTION_PREVIEW_CHARS]}"
            for index, (title, extract) in enumerate(candidates, 1)
        )
        prompt = (
            "TOPIC SELECTION\n"
            "You are choosing the source article for the next episode of a "
            "short-form video channel focused primarily on entertainment while "
            "teaching the viewer something interesting. From the numbered Wikipedia "
            "article candidates below, choose the ONE that would make the most "
            "entertaining and curiosity-provoking short video for a general audience. "
            "Prioritize subjects that are surprising, unusual, fascinating, "
            "unexpected, mysterious, visually interesting, or likely to make someone "
            "think, 'I didn't know that.' The goal is ENTERTAINMENT first, while "
            "giving the viewer something genuinely interesting to learn. Do not favor "
            "a topic simply because it sounds academic, important, or traditionally "
            "educational. Judge each candidate's topic and content, not its writing "
            "style.\n\n"
            "Entertainment stays the top priority; among candidates that are "
            "equally entertaining, prefer topics that can be visually told using "
            "generic stock footage such as Pexels. Favor subjects involving "
            "places, animals, nature, geography, science, technology, objects, "
            "structures, unusual phenomena, or processes. Prefer stories whose "
            "visuals do not depend heavily on specific people - named individuals, "
            "historical figures, celebrities - or specific events. A topic may "
            "still mention people, but avoid choosing it when the story's main "
            "appeal or visual storytelling depends on showing a particular person "
            "or event that would be difficult to represent with generic stock "
            "footage. The goal is a topic that is both highly entertaining and "
            "practical to illustrate with the available stock footage.\n\n"
            f"{listing}\n\n"
            f"Respond with the number of your chosen candidate (1-{len(candidates)})."
        )
        response = self.llm.generate(
            prompt,
            response_format=build_selection_schema(len(candidates)),
        )
        try:
            choice = int(json.loads(response)["choice"])
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            choice = 1
        if not 1 <= choice <= len(candidates):
            choice = 1
        title, extract = candidates[choice - 1]
        self.log(
            f"Selected Wikipedia candidate {choice}/{len(candidates)}: {title}"
        )
        return title, extract

    def segment_count(self):
        """
        How many spoken segments (and therefore visuals) an episode has.

        Derived from app.shorts.target_duration_seconds alone - one visual
        per SECONDS_PER_VISUAL of narration - so changing the target duration
        changes the segment count with nothing else to update. The words per
        segment come from that same target duration.
        """

        shorts = self.config.get("app", {}).get("shorts", {})
        try:
            target_seconds = float(
                shorts.get("target_duration_seconds", DEFAULT_TARGET_SECONDS)
            )
        except (TypeError, ValueError):
            target_seconds = DEFAULT_TARGET_SECONDS
        count = int(round(target_seconds / SECONDS_PER_VISUAL))
        return max(MIN_SEGMENT_COUNT, min(count, MAX_SEGMENT_COUNT))

    def build_prompt(self, instruction=None):
        c = self.generation_config
        ch = self.channel_config.get("channel", {})
        name = str(ch.get("name", "Curious About Things"))
        desc = str(ch.get("channel_description", ch.get("description", "")))
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
        segment_count = self.segment_count()
        candidates = self.random_wikipedia_candidates()
        wikipedia_title, wikipedia_content = self.choose_wikipedia_article(candidates)
        per_sentence = word_target // segment_count
        wps_min = max(9, per_sentence - 1)
        wps_max = per_sentence + 3
        target_seconds_str = str(int(target_seconds))
        segment_count_str = str(segment_count)
        narration_rules = narration_rules.replace("{{TARGET_SECONDS}}", target_seconds_str)
        visual_rules = visual_rules.replace("{{TARGET_SECONDS}}", target_seconds_str)
        narration_rules = narration_rules.replace("{{SEGMENT_COUNT}}", segment_count_str)
        visual_rules = visual_rules.replace("{{SEGMENT_COUNT}}", segment_count_str)
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
        blueprint = "".join(
            f"- {description} ({wps_min}-{wps_max} {'words each' if plural else 'words'}).\n"
            for description, plural in sentence_blueprint(segment_count)
        )
        length_requirement = (
            "ABSOLUTE REQUIREMENT - NARRATION LENGTH\n"
            f"Write the narration as EXACTLY {segment_count} complete sentences, each sentence {wps_min}-{wps_max} words "
            f"long, totaling {word_min}-{word_max} words. Never write strings of short fragments - every sentence "
            f"must be a full, substantial spoken thought. A script outside {word_min}-{word_max} words is a FAILED response. "
            "Follow this blueprint exactly:\n"
            f"{blueprint}"
        )
        source_material = (
            "WIKIPEDIA SOURCE MATERIAL - FACTUAL SOURCE OF TRUTH\n"
            f"Article title: {wikipedia_title}\n"
            "This article is the only factual source for the episode. Follow the SINGLE "
            "SOURCE-ACCURACY RULE below and pull its strongest interesting facts from here "
            "rather than summarizing it in full.\n\n"
            f"{wikipedia_content}\n"
        )
        self.last_source_material = source_material
        topic_selection = (
            "SOURCE-BOUND TOPIC SELECTION\n"
            "The supplied Wikipedia article is the topic. Select its most interesting supported angle; "
            "never swap in a subject from the category list.\n"
        )
        factual_storytelling = (
            "SINGLE SOURCE-ACCURACY RULE (HIGHEST PRIORITY)\n"
            "The supplied Wikipedia content is the factual source for the narration. Accurately "
            "paraphrase those facts and present them creatively through wording, pacing, "
            "structure, and delivery - but never change, contradict, or invent factual details.\n"
            "- Do not add facts, explanations, causes, purposes, scientific interpretations, "
            "conservation claims, or conclusions unless the article explicitly supports them.\n"
            "- Do not fill gaps with background knowledge, even when a detail is commonly known "
            "or likely true.\n"
            "- If the article states what something is or does, describe only that. If it does "
            "not explain why something happens, say only that it happens - never invent a reason.\n"
            "- Never restate a supported fact as a broader or stronger claim.\n"
            "- Every factual claim in the narration must be traceable to the supplied article.\n\n"
            "Write every name exactly as it appears in the supplied article, preserving all "
            "accented characters and diacritics. Never strip, replace, or anglicize "
            "accents, and never let TTS-friendly spelling change a name: keep the "
            "article's exact letters in the narration. For example, write S\u00e3o Tom\u00e9 with its "
            "accents intact, never as S o Tom or Sao Tome.\n\n"
            "Before returning the final narration, carefully proofread the entire response for:\n"
            "- duplicated punctuation such as \",,\" or \"..\"\n"
            "- missing spaces between words such as \"justfor\"\n"
            "- malformed or accidentally merged words\n"
            "- spelling errors\n"
            "- other obvious typographical errors\n"
            "The final narration should be clean, naturally written, and free of accidental "
            "formatting or typing mistakes. For example, a recent episode wrongly contained: "
            "\"Its story is not just about mabout machines,, it's about human ingenuity and "
            "the pursuit of flight.\" Never return errors like these.\n\n"
        )
        return (
            "You are the creative writer for \"" + name + "\", a short-form video channel "
            "about anything genuinely fascinating.\n\n" +
            length_requirement +
            "\n\n" + source_material +
            "\nCHANNEL DESCRIPTION\n" + desc +
            "\n\nTOPIC RESPONSIBILITY\n" + topic_selection +
            "\n\n" + instruction_section +
            factual_storytelling +
            "STORYTELLING PATTERN (guideline, not a rigid formula - adapt it naturally "
            "to the topic):\n" + storytelling +
            "\n\nNARRATION RULES\n" + narration_rules +
            "\n\nVISUAL SEARCH QUERY RULES\n" + visual_rules +
            "\n\nCREATIVE DIRECTION\n" + creative_directions +
            "\n\nVARIETY REQUIREMENTS (CRITICAL)\n"
            "Every episode must feel distinct. Do not fall into repeated templates for title or opening.\n"
            "- TOPIC: The supplied Wikipedia article is the only topic source. Do not replace it with a separate category or subject.\n"
            "- TITLE: Never reuse the same title formula. Vary between a question, a bold claim, a How/Why phrase, a surprising statement, a number, or a short intriguing phrase. Banished templates: The Secret Life of..., The Hidden Truth About..., The Untold Story of..., What Happens When..., Everything You Know About... is Wrong.\n"
            "- OPENING: The first sentence must not follow a formula. Do NOT open with Your X does more than you realize, You never noticed..., Most people do not know..., or Did you know.... Each hook should land differently - a vivid scene, a counterintuitive claim, a surprising number, a question, a historical moment, a weird comparison.\n"
            "- NARRATIVE ARC: Do not force every episode through the same emotional beats. Some should build dread, others wonder, others humor, others awe. Let the topic dictate the arc.\n"
            "- MOOD: Choose a mood that fits THIS topic. Do not default to curious mysterious every time.\n"
            '- "title": a short, clickable video title. Must be a properly punctuated phrase with correct capitalization, spacing, and any necessary punctuation (apostrophes, commas, periods). No run-on fragments or missing punctuation.\n'
            '- "summary": a one-sentence teaser of the episode. Must be a single, complete, properly punctuated sentence with correct capitalization, spacing, and terminal punctuation. No run-on sentences or missing punctuation between clauses.\n'
            f'- "narration_sentences": an array of EXACTLY {segment_count} strings - the narration split '
            f'into its {segment_count} sentences. Each string is one complete spoken sentence of {wps_min}-{wps_max} words. '
            'Plain spoken text, no stage directions, no sound cues, no speaker labels.\n'
            '- "mood": 1-3 lowercase words describing the emotional tone. IMPORTANT: Choose mood words that match the story energy. Use words like: dramatic, tense, epic, mysterious, curious, dark, suspense, scary, horror, action, funny, comedy, playful, energetic, exciting, calm, peaceful, relaxing, chill, soft, gentle, warm, cozy, romantic, nostalgic, dreamy, sad, melancholic, happy, uplifting, inspiring.\n'
            f'- "visuals": EXACTLY {segment_count} objects — one per narration sentence, so the whole script has a matching visual. Each object has exactly one field: {{"search_query": "stock footage search phrase"}}. The visuals array is in the same order as the narration sentences, so sentence 1 matches visual 1, sentence 2 matches visual 2, and so on. Give every sentence a visual; do not reuse the same visual twice.\n'
            "ACCURACY AND PROOFREADING\n"
            "Write clean, correctly spelled content with no typos, accidental punctuation, or malformed words. "
            "Before returning the final output, carefully proofread the entire generated content for spelling and punctuation errors. "
            "Do not introduce accidental changes to quoted or source-derived text.\n"
            "FINAL CHECK BEFORE ANSWERING\n"
            f"1. narration_sentences contains exactly {segment_count} complete sentences.\n"
            f"2. Each sentence should be {wps_min}-{wps_max} words. Total narration should be around {word_min}-{word_max} words (approximately {target_seconds_str} seconds of spoken content at a natural pace).\n"
            f"3. The visuals array contains exactly {segment_count} search queries — one per narration sentence, covering the entire narration.\n"
            "4. Every sentence carries real, verified information - no filler.\n"
            f"5. Every narration sentence is a complete, natural English sentence with correct spelling, apostrophes, punctuation, and spacing - no broken splits mid-thought and no awkward boundaries from the {segment_count}-sentence split.\n"
            "6. Every visual object contains exactly one field, search_query, with a practical Pexels search phrase - no extra fields, no context field.\n"
            "7. The topic and every factual claim obey the SINGLE SOURCE-ACCURACY RULE: they come only from the supplied Wikipedia article, with no background knowledge, outside examples, comparisons, analogies, historical connections, hypothetical scenarios, unsupported conclusions, or exaggerated article facts.\n"
            "8. Each narration_sentences item is EXACTLY ONE complete sentence: one capital start, one terminal punctuation mark, never two statements fused without punctuation, never one thought split across two items, never quoted terms.\n"
            "9. The title and summary are properly punctuated: correct capitalization, spacing, apostrophes, and terminal punctuation. The summary must be exactly one complete sentence - if it contains more than one independent thought, split them into separate sentences with a period and a capital letter.\n"
            f"SOURCE ARTICLE THAT MUST GOVERN THE ANSWER\nArticle title: {wikipedia_title}\n{wikipedia_content}\n"
        )

    def generate(self, instruction=None):
        prompt = self.build_prompt(instruction)
        self.log("Generating episode content...")
        # Grammar-constrained output: the schema forces the model to emit
        # segment_count narration sentence strings and segment_count visual
        # queries (one per sentence). A small local model counts reliably
        # (unlike word counts in prose).
        response = self.llm.generate(
            prompt,
            response_format=build_schema(self.segment_count()),
            system_prompt=(
                "The supplied Wikipedia article is your only factual source. Follow its SINGLE "
                "SOURCE-ACCURACY RULE: paraphrase its facts accurately and creatively, never "
                "invent or contradict them, and write about no other subject.\n\n"
                + self.last_source_material
            ),
        )
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
        s = re.sub(r"[^\w\s.,!?\-$%':]|_", " ", s)
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
        s = re.sub(r"(?<!\d)([.,!?])(?=\w)", r"\1 ", s)
        # Clean up any double spaces created
        s = re.sub(r"\s+", " ", s).strip()
        # Remove trailing commas or artifacts before punctuation
        s = re.sub(r"\s*,\s*([.!?])", r"\1", s)
        s = self._repair_generation_artifacts(s)
        return s

    def _repair_generation_artifacts(self, text):
        """Repair conservative, clearly accidental LLM text artifacts.

        Local models occasionally emit duplicated words or insert a short
        fragment between a word and its duplicate (for example,
        ``social ba social behavior``). These repairs are intentionally narrow
        so normal repetition and stylistic phrasing are left alone.
        """
        s = str(text or "")
        if not s:
            return s

        # Remove a duplicated word: "the the answer" -> "the answer".
        s = re.sub(
            r"\b([a-zA-Z]+)(\s+\1\b)+",
            r"\1",
            s,
            flags=re.IGNORECASE,
        )

        # Remove a short accidental fragment between duplicated words:
        # "social ba social behavior" -> "social behavior".
        s = re.sub(
            r"\b([a-zA-Z]+)\s+[a-zA-Z]{1,3}\s+\1\b",
            r"\1",
            s,
            flags=re.IGNORECASE,
        )

        # Collapse punctuation runs left by malformed model output.
        s = re.sub(r"([.,!?])\1+", r"\1", s)
        s = re.sub(r"\s+([,.;:!?])", r"\1", s)
        s = re.sub(r"([,.;:!?])(?=[A-Za-z])", r"\1 ", s)
        return re.sub(r"\s+", " ", s).strip()

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
        # Shape 1: (not|aren't|isn't|doesn't|don't|won't|can't) just <NP> it's/they're.
        # Rebuild the match from its own pieces. The old lambda sliced group(0)
        # with absolute match offsets (m.start(2)-m.start(1)), which cut
        # mid-word and merged fragments: "not just about machines, it's"
        # became "not just about mabout machines,, it's" - the exact artifact
        # seen in episode narration. If the clause already ends in
        # punctuation, keep its separator instead of stacking a comma.
        def _insert_clause_comma(match):
            prefix = match.group(0)[: match.start(1) - match.start(0)]
            clause = match.group(1)
            separator = (
                " "
                if clause.rstrip().endswith((",", ".", ";", ":", "?", "!"))
                else ", "
            )
            return f"{prefix}{clause}{separator}{match.group(2)}"

        s = re.sub(
            r"\b(?:not|aren't|isn't|don't|doesn't|won't|can't)\s+just\b\s+(.+?)\s+(it's|they're|he's|she's|we're|you're)\b",
            _insert_clause_comma,
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

def write_content_files(episode_directory, content, source_material=""):
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
        source_material.strip(),
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
