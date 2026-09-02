"""Scripture text lookup + citation verification.

Two sources, deliberately layered rather than either/or:

  - api.bible (api.scripture.api.bible), when BIBLE_API_KEY is configured:
    preferred — its /search endpoint resolves a human-readable reference
    like "Romans 8:28-30" straight to an OSIS passage id (no book-name
    mapping needed on our side), and its catalog covers more translations
    than bible-api.com. Confirmed live: the key that ended up working was
    off by one leading character from what first landed in .env (a
    regenerated key came back missing its first character, and this was
    diagnosed by cross-checking the raw AWS API Gateway 403
    AccessDeniedException before trying anything more, than after
    catching this the corrected key returned real KJV/ASV/WEB text) — see
    the Phase 3 completion notes for the full back-and-forth.
  - bible-api.com: free, no API key, serves public-domain KJV — the
    original Task 3 choice, and still the fallback here in two distinct
    situations: a genuine api.bible SERVICE failure (network error, auth
    error, 5xx), AND a clean "no such passage" answer FROM api.bible —
    the latter is cross-checked against bible-api.com rather than trusted
    outright, so a reference-parsing edge case in api.bible's /search
    can't by itself produce a false "hallucinated citation" flag on a
    real verse. A reference is only ever reported as genuinely
    unresolvable when BOTH sources agree it doesn't exist.

Either way, verify_citation below never trusts the model's memory of
scripture text — every reference is checked against whichever source
actually answered.
"""
import asyncio
import difflib
import logging
import re

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Every English translation confirmed live against GET /v1/bibles?language=eng
# with this app's actual BIBLE_API_KEY to ACTUALLY return real passage text
# (not just appear in the catalog listing — catalog presence alone doesn't
# mean the key is licensed to fetch that translation's content; verified by
# hitting /search then /passages/{id} for each and checking real text came
# back). Of 40 English entries in that catalog, 33 returned real text and 7
# were confirmed blocked (search itself returns nothing — no license) and
# are deliberately excluded: the Brenton Septuagint (both editions), JPS
# TaNaKH 1917, Targum Onkelos Etheridge, The Orthodox Jewish Bible, and the
# World Messianic Bible (both editions). Of the 33 accessible entries, several
# are multiple bibleIds for the literal same translation (e.g. WEB had 4
# near-identical catalog rows) — deduped to one id per distinct translation
# below, 21 total. Anything not in this map (or when BIBLE_API_KEY isn't
# configured) goes straight to bible-api.com, which accepts an arbitrary
# translation code.
API_BIBLE_IDS: dict[str, str] = {
    "amp": "a81b73293d3080c9-01",  # Amplified Bible
    "bsb": "bba9f40183526463-01",  # Berean Standard Bible
    "engkjvcpb": "55212e3cf5d04d49-01",  # Cambridge Paragraph Bible of the KJV
    "engdra": "179568874c45066f-01",  # Douay-Rheims American 1899
    "fbv": "65eec8e0b60e656b-01",  # Free Bible Version
    "enggnv": "c315fa9f71d4af3a-01",  # Geneva Bible
    "kjv": "de4e12af7f28f599-01",  # King James (Authorised) Version
    "lsv": "01b29f4b342acc35-01",  # Literal Standard Version
    "niv11": "78a9f6124f344018-01",  # New International Version 2011
    "nlt": "d6e14a625393b4da-01",  # New Living Translation
    "nltce": "b907c8622b59a1f7-01",  # New Living Translation Catholic Edition
    "nltuk": "43e315b442a7c862-01",  # New Living Translation, Anglicised
    "engrv": "40072c4a5aba4022-01",  # Revised Version 1885
    "engf35": "2f0fd81d7b85b923-01",  # English NT According to Family 35
    "asv": "06125adad2d5898a-01",  # American Standard Version
    "tcent": "32339cf2f720ff8e-01",  # Text-Critical English New Testament
    "t4t": "66c22495370cdfc0-01",  # Translation for Translators
    "web": "9879dbb7cfe39e4d-01",  # World English Bible
    "webbe": "7142879509583d59-01",  # World English Bible British Edition
    "engwebu": "72f4e6dc683324df-01",  # World English Bible Updated
    "engwebus": "32664dc3288a28df-01",  # World English Bible, American English Edition, without Strong's Numbers
}

# Human-readable display names for the picker in the comparison UI —
# GET /bible/translations returns these paired with the codes above so the
# frontend never hardcodes its own copy of this list.
TRANSLATION_LABELS: dict[str, str] = {
    "amp": "Amplified Bible",
    "bsb": "Berean Standard Bible",
    "engkjvcpb": "Cambridge Paragraph Bible of the KJV",
    "engdra": "Douay-Rheims American 1899",
    "fbv": "Free Bible Version",
    "enggnv": "Geneva Bible",
    "kjv": "King James Version",
    "lsv": "Literal Standard Version",
    "niv11": "New International Version",
    "nlt": "New Living Translation",
    "nltce": "New Living Translation, Catholic Edition",
    "nltuk": "New Living Translation, Anglicised",
    "engrv": "Revised Version 1885",
    "engf35": "English NT According to Family 35",
    "asv": "American Standard Version",
    "tcent": "Text-Critical English New Testament",
    "t4t": "Translation for Translators",
    "web": "World English Bible",
    "webbe": "World English Bible, British Edition",
    "engwebu": "World English Bible Updated",
    "engwebus": "World English Bible, American English Edition",
}


class ApiBibleError(RuntimeError):
    """A genuine api.bible SERVICE failure — network error, non-2xx that
    isn't a clean 'not found', or an unparseable response. Distinct from
    fetch_passage returning None, which means api.bible understood the
    query and confirmed no such passage exists."""


# Matches "John 3:16", "1 John 3:16", "I John 3:16", "First John 3:16",
# "Song of Solomon 3:16", "Romans 8:28-30". Book names are a single
# capitalized word, optionally prefixed by one of the ordinal forms a
# model might reasonably use for the nine numbered books (digit, Roman
# numeral, or spelled out — "2 Timothy" / "II Timothy" / "Second Timothy"
# are all real usage), or one of the two known multi-word English book
# names, spelled out explicitly.
#
# Deliberately NOT "any run of capitalized words", which was tried first
# and, confirmed live in the Phase 3 smoke test, swallows an unrelated
# preceding capitalized word ("And Philippians 4:19" extracted whole,
# which then correctly fails to resolve for the wrong reason: not because
# the verse is fake, but because the regex mangled a real one into a
# fake-looking one).
#
# The ordinal-form gap this replaced (matching only a bare digit prefix)
# had the identical failure mode from the other direction — confirmed
# live: "I Corinthians 13:4" extracted as "Corinthians 13:4" (prefix
# dropped), which then genuinely doesn't resolve against either Bible
# source, so a real, accurately-quoted verse got reported as a
# hallucinated citation. Both directions are the same lesson: a
# false-positive extra lookup (matching too much) is a cheap 404 to
# shrug off; a mangled real reference reported as "invalid_reference" is
# the worse failure, since that's exactly the false alarm Task 3 exists
# to avoid.
#
# Still-known gaps, not fixed here (see the Phase 3 completion notes):
# a fully lowercased book name ("romans 8:28") isn't matched at all
# (silent miss, not a false positive — capitalization is required to
# keep the "any capitalized word" version from coming back), and a
# chapter-only reference with no verse ("Psalm 23") isn't matched either,
# since the colon+verse is required.
_CITATION_RE = re.compile(
    r"\b((?:(?:[1-3]|III|II|I|First|Second|Third)\s)?(?:Song of Solomon|Song of Songs|[A-Z][a-zA-Z]+))"
    r"\s+(\d{1,3}:\d{1,3}(?:-\d{1,3})?)\b"
)


class ScripturePassage:
    def __init__(self, reference: str, text: str, translation: str, source: str = "bible-api.com") -> None:
        self.reference = reference
        self.text = text.strip()
        self.translation = translation
        self.source = source


async def _fetch_from_api_bible(reference: str, bible_id: str) -> ScripturePassage | None:
    """Two calls: /search resolves the human-readable reference to an
    OSIS passage id (handles book-name parsing and verse ranges for us —
    confirmed live that "Romans 8:28-30" resolves straight to
    "ROM.8.28-ROM.8.30"), then /passages/{id} fetches that id's clean
    plain text (content-type=text&include-verse-numbers=false — /search's
    own `content` field is HTML-formatted regardless of that param, so it
    isn't used for the actual text).

    Returns None for a clean "no such passage" (search succeeds, finds
    nothing) — an authoritative answer, not a failure. Raises
    ApiBibleError for anything that should fall back to bible-api.com
    instead: a network error, a non-2xx that isn't that clean not-found
    shape, or a response that doesn't parse the way it did when this was
    checked live.
    """
    headers = {"api-key": settings.bible_api_key}
    base = f"{settings.api_bible_base_url}/v1/bibles/{bible_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            search_resp = await client.get(f"{base}/search", params={"query": reference}, headers=headers)
            if search_resp.status_code != 200:
                raise ApiBibleError(f"api.bible search {search_resp.status_code}: {search_resp.text[:300]}")
            passages = search_resp.json().get("data", {}).get("passages") or []
            if not passages:
                return None  # confirmed live: this is api.bible's clean "no such passage" shape

            passage_id = passages[0]["id"]
            passage_resp = await client.get(
                f"{base}/passages/{passage_id}",
                params={"content-type": "text", "include-verse-numbers": "false"},
                headers=headers,
            )
            if passage_resp.status_code != 200:
                raise ApiBibleError(f"api.bible passage {passage_resp.status_code}: {passage_resp.text[:300]}")
            content = passage_resp.json()["data"]["content"]
    except httpx.HTTPError as exc:
        raise ApiBibleError(f"api.bible request failed: {exc}") from exc
    except (KeyError, ValueError) as exc:
        raise ApiBibleError(f"api.bible returned an unexpected response shape: {exc}") from exc

    return ScripturePassage(
        reference=passages[0].get("reference", reference), text=content, translation="", source="api.bible"
    )


async def _fetch_from_bible_api_com(reference: str, translation: str) -> ScripturePassage | None:
    """The original Task 3 source — see module docstring. 404 means "not
    found", handled the same way as api.bible's clean not-found: an
    authoritative answer, not an error to swallow."""
    url = f"{settings.bible_api_base_url}/{reference}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, params={"translation": translation})
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    return ScripturePassage(
        reference=data.get("reference", reference), text=data["text"], translation=translation, source="bible-api.com"
    )


async def fetch_passage(reference: str, translation: str | None = None) -> ScripturePassage | None:
    """Fetch the actual text for `reference` (e.g. "John 3:16-18").

    Returns None only when the reference doesn't resolve against EITHER
    source — callers (verify_citation) treat that as "not a real
    reference, likely hallucinated" and flag it visibly, so a false
    negative here is a worse failure than an extra API call: a real verse
    getting reported as fabricated is exactly the wrong kind of mistake
    for a trust/accuracy feature to make.

    Prefers api.bible (see _fetch_from_api_bible) when BIBLE_API_KEY is
    configured AND the requested translation is one of the handful
    api.bible ids this app knows about (API_BIBLE_IDS):
      - api.bible resolves the reference -> that's the answer, returned
        directly.
      - api.bible SERVICE fails (ApiBibleError: network error, non-2xx,
        bad shape) -> falls back to bible-api.com, which becomes the
        answer.
      - api.bible cleanly says "no such passage" -> cross-checked against
        bible-api.com rather than trusted outright. If bible-api.com
        resolves it, ITS answer is used (api.bible's negative was a
        parser edge case, not a real "this doesn't exist"). Only if
        bible-api.com agrees it doesn't exist either does this return
        None.
    """
    effective_translation = (translation or settings.bible_translation).lower()
    bible_id = API_BIBLE_IDS.get(effective_translation)

    if not (settings.bible_api_key and bible_id):
        return await _fetch_from_bible_api_com(reference, effective_translation)

    try:
        result = await _fetch_from_api_bible(reference, bible_id)
    except ApiBibleError:
        logger.warning("api.bible failed for %r — falling back to bible-api.com", reference, exc_info=True)
        return await _fetch_from_bible_api_com(reference, effective_translation)

    if result is not None:
        result.translation = effective_translation
        return result

    # api.bible's own answer was a clean "no such passage" — not treated
    # as authoritative on its own; see docstring.
    cross_check = await _fetch_from_bible_api_com(reference, effective_translation)
    if cross_check is not None:
        logger.warning(
            "api.bible reported no such passage for %r, but bible-api.com resolved it — "
            "using bible-api.com's answer instead of api.bible's negative",
            reference,
        )
    return cross_check


async def fetch_passage_multi(reference: str, translations: list[str]) -> dict[str, ScripturePassage | None]:
    """The comparison-view seam: the same reference in N translations at
    once. Deliberately a thin fan-out over the existing, unmodified
    fetch_passage — one call per translation, concurrently — rather than a
    new fetch codepath; _fetch_from_api_bible/_fetch_from_bible_api_com and
    everything sermon generation depends on (context_assembly.py,
    generation.py) are untouched by this function existing.
    """
    results = await asyncio.gather(*(fetch_passage(reference, t) for t in translations))
    return dict(zip(translations, results))


def extract_citations(text: str) -> list[str]:
    """Pull every candidate scripture reference out of free-form model
    output. Deduplicated, order preserved."""
    seen: dict[str, None] = {}
    for book, chapter_verse in _CITATION_RE.findall(text):
        ref = f"{book.strip()} {chapter_verse}"
        seen.setdefault(ref, None)
    return list(seen.keys())


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9\s]", "", s.lower()).strip()


_SENTENCE_END_RE = re.compile(r"[.!?][\"'’”)\]]*\s+")
_PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n")


def _sentence_span(text: str, idx: int) -> tuple[int, int]:
    """The [start, end) span of the sentence containing character index
    idx — not "nearby", the SAME sentence. A citation and its
    quotation, when a draft actually attributes one to the other, are
    always written in the same sentence (see _extract_quoted_near's two
    documented patterns). Searching any wider than that is what let
    _extract_quoted_near match an unrelated quote from a different
    paragraph entirely — confirmed live (Phase 6 edit-testing): a
    passage rewritten into indirect/reported prose with no quote of its
    own got matched against a quotation two sentences and a paragraph
    break away, inside the old fixed 400-character window.

    Two boundary types, whichever is closer wins on each side:

    - _SENTENCE_END_RE: '.'/'!'/'?', optionally followed by closing
      quote/paren characters, then whitespace. The quote-mark allowance
      matters — confirmed live (2026-09-02): a sentence ending
      mid-quote ('...out.”') was NOT recognized as a boundary by an
      earlier version of this regex (bare "punctuation, then whitespace"
      — no allowance for a quote mark in between), which let the
      search window silently leak across a full paragraph
      break into a completely unrelated quote from the PREVIOUS
      paragraph — the exact same class of bug this function exists to
      prevent, just not fully closed by the first pass at it. A
      book/chapter:verse reference never contains this punctuation, so
      it can't fool this into treating the reference itself as a
      boundary.
    - _PARAGRAPH_BREAK_RE: a blank line (two or more newlines, any
      whitespace between). An unconditional hard stop regardless of
      punctuation — belt-and-suspenders for a sentence that's missing
      its terminal punctuation entirely (a heading, a truncated line):
      a real paragraph break should never be crossed either way."""
    start = 0
    for m in _SENTENCE_END_RE.finditer(text, 0, idx):
        start = max(start, m.end())
    for m in _PARAGRAPH_BREAK_RE.finditer(text, 0, idx):
        start = max(start, m.end())

    end = len(text)
    m = _SENTENCE_END_RE.search(text, idx)
    if m:
        end = min(end, m.start())
    m = _PARAGRAPH_BREAK_RE.search(text, idx)
    if m:
        end = min(end, m.start())
    return start, end


def _extract_quoted_near(text: str, reference: str) -> str | None:
    """If a quoted string sits in the SAME SENTENCE as `reference`,
    return it. Sermon drafts attribute quotes both ways — "'quoted
    text' (John 3:16)" and "John 3:16 says, 'quoted text'" are both
    common — so this checks both sides of the reference within that one
    sentence and returns whichever quote is closest to it. Returns None
    if there's no quote in that sentence at all — either a bare
    parenthetical reference, or (see verify_citation's `not_quoted`
    status) a reference only mentioned in indirect/paraphrased prose.
    Deliberately does NOT fall back to searching neighboring sentences
    or the wider document — that's exactly the behavior that used to
    misattribute an unrelated nearby quotation to a reference that was
    never actually quoted at all."""
    idx = text.find(reference)
    if idx == -1:
        return None
    end = idx + len(reference)
    sent_start, sent_end = _sentence_span(text, idx)

    before = text[sent_start:idx]
    before_matches = list(re.finditer(r"[\"“]([^\"”]{5,400})[\"”]", before))
    best_before = (len(before) - before_matches[-1].end(), before_matches[-1].group(1)) if before_matches else None

    after = text[end:sent_end]
    after_matches = list(re.finditer(r"[\"“]([^\"”]{5,400})[\"”]", after))
    best_after = (after_matches[0].start(), after_matches[0].group(1)) if after_matches else None

    candidates = [c for c in (best_before, best_after) if c is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda c: c[0])[1]


async def verify_citation(reference: str, draft_text: str, translation: str | None = None) -> dict:
    """Check one citation from a model-generated draft against the real
    Bible text. Returns a dict matching app.schemas.generation.CitationFlag.

    Three distinct outcomes for a resolvable reference, not two — this
    used to collapse "no quote nearby" into status="verified", which
    conflated two different claims: "we checked the wording and it
    matches" vs. "there was nothing to check." Confirmed live (Phase 6
    edit-testing) that conflation had a real cost: _extract_quoted_near
    used to fall back to searching a wide window for ANY nearby quote
    rather than admitting there wasn't one, which could misattribute an
    unrelated quotation elsewhere in the draft to a reference that was
    never actually quoted. not_quoted reports that state honestly
    instead of either flagging it as wrong or matching it against text
    that has nothing to do with it:
      - invalid_reference: the reference itself doesn't resolve (hallucinated
        book/chapter/verse).
      - not_quoted: the reference is real, but nothing in the draft directly
        quotes it (a bare parenthetical, or a reference only mentioned in
        indirect/paraphrased prose) — not itself suspicious, and distinct
        from `verified`, which means the wording was actually checked.
      - quote_mismatch: the reference is real, but text quoted next to it
        in the draft doesn't closely match the source translation — either
        the model misquoted, or it's quoting a different translation than
        the one this app verifies against (bible_translation, KJV by
        default) — either way, a pastor should check it before preaching
        it, so it's flagged rather than guessed at.
    """
    passage = await fetch_passage(reference, translation)
    if passage is None:
        return {
            "reference": reference,
            "status": "invalid_reference",
            "quoted_text": None,
            "source_text": None,
            "detail": f"{reference!r} does not resolve against the Bible text source — likely a hallucinated reference.",
        }

    quoted = _extract_quoted_near(draft_text, reference)
    if quoted is None:
        return {
            "reference": reference,
            "status": "not_quoted",
            "quoted_text": None,
            "source_text": passage.text,
            "detail": "Reference exists but isn't directly quoted in the draft — nothing to verify the wording of.",
        }

    similarity = difflib.SequenceMatcher(None, _normalize(quoted), _normalize(passage.text)).ratio()
    if similarity < 0.6:
        return {
            "reference": reference,
            "status": "quote_mismatch",
            "quoted_text": quoted,
            "source_text": passage.text,
            "detail": (
                f"Quoted text doesn't closely match the {passage.translation.upper()} text "
                f"(similarity {similarity:.0%}) — verify before preaching."
            ),
        }

    return {
        "reference": reference,
        "status": "verified",
        "quoted_text": quoted,
        "source_text": passage.text,
        "detail": "Quoted text matches the source translation.",
    }


async def verify_all_citations(draft_text: str, translation: str | None = None) -> list[dict]:
    return [await verify_citation(ref, draft_text, translation) for ref in extract_citations(draft_text)]
