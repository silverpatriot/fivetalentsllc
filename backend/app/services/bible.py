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
import difflib
import logging
import re

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# A small, hand-picked map of translation code -> api.bible bibleId,
# resolved live from GET /v1/bibles?language=eng rather than guessed.
# Not a general catalog browser — just enough to make "more translations
# through api.bible's catalog" real without building a full lookup UI.
# Anything not in this map (or when BIBLE_API_KEY isn't configured) goes
# straight to bible-api.com, which accepts an arbitrary translation code.
_API_BIBLE_IDS: dict[str, str] = {
    "kjv": "de4e12af7f28f599-01",  # King James (Authorised) Version
    "asv": "06125adad2d5898a-01",  # American Standard Version
    "web": "9879dbb7cfe39e4d-01",  # World English Bible
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
    api.bible ids this app knows about (_API_BIBLE_IDS):
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
    bible_id = _API_BIBLE_IDS.get(effective_translation)

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


def _extract_quoted_near(text: str, reference: str) -> str | None:
    """If a quoted string sits close to `reference` in `text`, return it.
    Sermon drafts attribute quotes both ways — "'quoted text' (John
    3:16)" and "John 3:16 says, 'quoted text'" are both common — so this
    checks a window on both sides and returns whichever quote is closest
    to the citation. Returns None if there's no quote nearby at all;
    plenty of citations are bare parenthetical references with nothing
    quoted next to them, and that's not itself suspicious."""
    idx = text.find(reference)
    if idx == -1:
        return None
    end = idx + len(reference)

    before = text[max(0, idx - 400) : idx]
    before_matches = list(re.finditer(r"[\"“]([^\"”]{5,400})[\"”]", before))
    best_before = (len(before) - before_matches[-1].end(), before_matches[-1].group(1)) if before_matches else None

    after = text[end : end + 400]
    after_matches = list(re.finditer(r"[\"“]([^\"”]{5,400})[\"”]", after))
    best_after = (after_matches[0].start(), after_matches[0].group(1)) if after_matches else None

    candidates = [c for c in (best_before, best_after) if c is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda c: c[0])[1]


async def verify_citation(reference: str, draft_text: str, translation: str | None = None) -> dict:
    """Check one citation from a model-generated draft against the real
    Bible text. Returns a dict matching app.schemas.generation.CitationFlag.

    Two distinct failure modes, both surfaced to the UI per Task 3 rather
    than silently passing the reference through:
      - invalid_reference: the reference itself doesn't resolve (hallucinated
        book/chapter/verse).
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
            "status": "verified",
            "quoted_text": None,
            "source_text": passage.text,
            "detail": "Reference exists; no direct quotation was attributed to it in the draft.",
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
