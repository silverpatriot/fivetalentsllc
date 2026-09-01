#!/usr/bin/env python3
"""Populates bible_verses (migration 0009) for the concordance feature —
exact/stemmed word search across scripture ("every verse containing
'grace'"). Source: bible.helloao.org's API, translation eng_kjv (public
domain — see https://ebible.org/Scriptures/details.php?id=eng-kjv2006),
same host already integrated for commentary/cross-references
(scripts/ingest_baseline_corpus.py) — confirmed live this session that it
also serves raw verse-level text at /api/{translation_id}/{book}/{chapter}.json,
same {type: "verse", number, content} shape ingest_commentary already
parses, so no second external data source is needed.

KJV-only for this initial ingest — it's this app's own default
translation (see app/core/config.py's bible_translation), and the schema
(UNIQUE(translation, book, chapter, verse)) is already multi-translation-
ready for extending to ASV/WEB/NIV/NLT later via the same live-confirmed
api.bible ids app/services/bible.py's API_BIBLE_IDS uses — a deliberate
follow-up, not built here.

NOT run by anything automatically — same category as
scripts/ingest_baseline_corpus.py and scripts/stripe_setup.py, run
deliberately. Upserts on (translation, book, chapter, verse), so
re-running is safe.

No embedding calls anywhere in this script — bible_verses.search_vector
is a Postgres-generated column (computed from `text` at write time), not
an embedding column, so this is meaningfully cheaper/faster than
ingest_baseline_corpus.py's commentary path and has zero OpenRouter
dependency.

    python scripts/ingest_bible_verses.py --book JHN --limit 5   # bounded test run
    python scripts/ingest_bible_verses.py                        # the full KJV, ~31K verses
"""
import argparse
import sys

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

sys.path.insert(0, ".")  # run from backend/, matches this repo's other scripts/alembic

from app.db.session import SyncSessionLocal  # noqa: E402
from app.models.bible_verse import BibleVerse  # noqa: E402

HELLOAO_BASE = "https://bible.helloao.org/api"
TRANSLATION_ID = "eng_kjv"  # helloao's id for this app's default KJV translation
OUR_TRANSLATION_CODE = "kjv"  # matches app.services.bible.API_BIBLE_IDS's "kjv" key


def _verse_text(content_items: list) -> str:
    """Each verse's `content` is a list where an item is either a plain
    string or {"text": ..., "wordsOfJesus": true} (red-letter marking) —
    confirmed live against /api/eng_kjv/JHN/3.json. Flattened to plain
    text; wordsOfJesus formatting isn't preserved (nothing in this app
    renders it specially yet)."""
    parts = []
    for item in content_items:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and "text" in item:
            parts.append(item["text"])
    return " ".join(parts).strip()


def _upsert_verse(session, *, translation: str, book: str, chapter: int, verse: int, text: str) -> None:
    stmt = pg_insert(BibleVerse).values(translation=translation, book=book, chapter=chapter, verse=verse, text=text)
    stmt = stmt.on_conflict_do_update(
        index_elements=[BibleVerse.translation, BibleVerse.book, BibleVerse.chapter, BibleVerse.verse],
        set_={"text": stmt.excluded.text},
    )
    session.execute(stmt)


def ingest_kjv(*, book_filter: str | None, limit: int | None) -> int:
    count = 0
    with httpx.Client() as client, SyncSessionLocal() as session:
        resp = client.get(f"{HELLOAO_BASE}/{TRANSLATION_ID}/books.json", timeout=30)
        resp.raise_for_status()
        books = resp.json()["books"]
        if book_filter:
            books = [b for b in books if b["id"] == book_filter]
        print(f"Ingesting KJV verse text for {len(books)} book(s)...")

        for book in books:
            book_id = book["id"]
            num_chapters = book["numberOfChapters"]
            for chapter_num in range(1, num_chapters + 1):
                resp = client.get(f"{HELLOAO_BASE}/{TRANSLATION_ID}/{book_id}/{chapter_num}.json", timeout=30)
                if resp.status_code != 200:
                    continue
                chapter = resp.json()["chapter"]
                verses = [e for e in chapter.get("content", []) if e.get("type") == "verse"]
                for v in verses:
                    if limit is not None and count >= limit:
                        session.commit()
                        return count
                    text = _verse_text(v.get("content", []))
                    if not text:
                        continue
                    _upsert_verse(
                        session, translation=OUR_TRANSLATION_CODE, book=book_id, chapter=chapter_num,
                        verse=v["number"], text=text,
                    )
                    count += 1
                session.commit()
            print(f"  {book['commonName']} ({num_chapters} chapter(s))")
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--book", default=None, help="Filter to one book (e.g. 'JHN')")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of verses processed, for a bounded test run")
    args = parser.parse_args()

    total = ingest_kjv(book_filter=args.book, limit=args.limit)
    print(f"Done — {total} verse(s) ingested/updated.")


if __name__ == "__main__":
    main()
