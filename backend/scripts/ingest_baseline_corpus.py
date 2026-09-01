#!/usr/bin/env python3
"""Populates the baseline reference corpus (reference_documents /
reference_chunks — migration 0007) from two live, public-domain/CC-
licensed sources, confirmed reachable before building this:

  - Cross-references: a.openbible.info/data/cross-references.zip
    (~340K weighted verse-pair rows, CC-BY, TSK-derived). Grouped by
    "From Verse" into one reference_document per source verse, listing
    its cross-referenced passages by vote count.
  - Commentary: bible.helloao.org's API — 7 public-domain/CC0-marked
    commentaries available at the identical API shape (COMMENTARY_LABELS
    below; --commentary-id picks one, default matthew-henry). One
    reference_document per (book, chapter) per commentary, one chunk per
    commentary entry (these commentaries comment in verse RANGES, not
    verse-by-verse — each entry is already a natural chunk boundary;
    chunk_text further splits only an unusually long entry).

NOT run by anything automatically — this populates shared, cross-tenant
content, run deliberately, same category as scripts/stripe_setup.py.
Upserts on (reference_type, passage_reference, source_id) (migration
0008's unique constraint — widened from 0007's 2-column version
specifically so multiple commentaries can coexist), so re-running is
safe and replaces existing entries rather than accumulating duplicates.

Bounded/testable runs, not just "ingest everything or nothing" — the
full corpus is ~31K cross-reference groups and up to ~1200 commentary
chapters PER commentary, which costs real embedding-API time/spend;
--book and --limit let you verify the pipeline on a small slice before
committing to a full run:

    python scripts/ingest_baseline_corpus.py --source commentary --book JHN --limit 5
    python scripts/ingest_baseline_corpus.py --source commentary --commentary-id adam-clarke --book JHN --limit 5
    python scripts/ingest_baseline_corpus.py --source cross-references --book Gen --limit 20
    python scripts/ingest_baseline_corpus.py --source all   # matthew-henry + cross-references, the full run
"""
import argparse
import io
import re
import sys
import zipfile
from collections import defaultdict

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

sys.path.insert(0, ".")  # run from backend/, matches this repo's other scripts/alembic

from app.db.session import SyncSessionLocal  # noqa: E402
from app.models.reference import COMMENTARY_LABELS, ReferenceChunk, ReferenceDocument, ReferenceType  # noqa: E402
from app.services.chunking import chunk_text  # noqa: E402
from app.services.embeddings import embed_batch_sync  # noqa: E402

CROSS_REFERENCES_URL = "https://a.openbible.info/data/cross-references.zip"
HELLOAO_BASE = "https://bible.helloao.org/api"

# Top N cross-references kept per source verse, by vote count — the raw
# dataset has some verses with dozens of weak (low-vote) matches; keeping
# all of them would bury the strong ones in noise for both the embedding
# and whatever an LLM does with the retrieved chunk.
CROSS_REFS_PER_VERSE = 10


def _readable_reference(raw: str) -> str:
    """"Gen.1.1" -> "Gen 1:1" — light formatting only, not a remap to
    full book names (a second dataset to keep correct); see
    app/models/reference.py's docstring."""
    parts = raw.split(".")
    if len(parts) != 3:
        return raw
    book, chapter, verse = parts
    return f"{book} {chapter}:{verse}"


def _upsert_document(
    session, *, reference_type: str, title: str, passage_reference: str | None, source_id: str | None = None
) -> ReferenceDocument:
    stmt = (
        pg_insert(ReferenceDocument)
        .values(reference_type=reference_type, title=title, passage_reference=passage_reference, source_id=source_id)
        .on_conflict_do_update(
            # 3-column conflict target — migration 0008 widened the unique
            # constraint this upserts against from (reference_type,
            # passage_reference) specifically so multiple commentaries can
            # each have their own row for the same passage_reference. This
            # MUST match that constraint exactly, or a second commentary's
            # ingest fails loudly on the first conflicting row rather than
            # upserting correctly — see that migration's docstring.
            index_elements=[
                ReferenceDocument.reference_type,
                ReferenceDocument.passage_reference,
                ReferenceDocument.source_id,
            ],
            set_={"title": title},
        )
        .returning(ReferenceDocument.id)
    )
    doc_id = session.execute(stmt).scalar_one()
    return session.get(ReferenceDocument, doc_id)


def _upsert_chunks(session, *, reference_type: str, document_id, texts: list[str]) -> None:
    if not texts:
        return
    vectors = embed_batch_sync(texts)
    for chunk_index, (text, vector) in enumerate(zip(texts, vectors)):
        stmt = pg_insert(ReferenceChunk).values(
            reference_type=reference_type,
            reference_document_id=document_id,
            chunk_index=chunk_index,
            content=text,
            embedding=vector,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ReferenceChunk.reference_document_id, ReferenceChunk.chunk_index],
            set_={"content": stmt.excluded.content, "embedding": stmt.excluded.embedding},
        )
        session.execute(stmt)


def ingest_cross_references(*, book_filter: str | None, limit: int | None) -> int:
    print("Downloading cross-references dataset...")
    resp = httpx.get(CROSS_REFERENCES_URL, timeout=60)
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    with zf.open("cross_references.txt") as f:
        lines = f.read().decode("utf-8").splitlines()

    groups: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for line in lines[1:]:  # header: "From Verse\tTo Verse\tVotes\t..."
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        from_verse, to_verse, votes_str = parts[0], parts[1], parts[2]
        if book_filter and not from_verse.startswith(book_filter):
            continue
        try:
            votes = int(votes_str)
        except ValueError:
            continue
        groups[from_verse].append((to_verse, votes))

    from_verses = sorted(groups.keys())
    if limit:
        from_verses = from_verses[:limit]

    print(f"Ingesting {len(from_verses)} cross-reference groups...")
    count = 0
    with SyncSessionLocal() as session:
        for i, from_verse in enumerate(from_verses):
            targets = sorted(groups[from_verse], key=lambda t: -t[1])[:CROSS_REFS_PER_VERSE]
            readable_from = _readable_reference(from_verse)
            body = "\n".join(f"- {_readable_reference(to)} ({votes} votes)" for to, votes in targets)
            text = f"Cross-references for {readable_from}:\n{body}"

            doc = _upsert_document(
                session,
                reference_type=ReferenceType.CROSS_REFERENCE.value,
                title=f"Cross-references: {readable_from}",
                passage_reference=readable_from,
            )
            _upsert_chunks(session, reference_type=ReferenceType.CROSS_REFERENCE.value, document_id=doc.id, texts=[text])
            session.commit()
            count += 1
            if (i + 1) % 25 == 0:
                print(f"  {i + 1}/{len(from_verses)}...")
    return count


def _commentary_books(client: httpx.Client, commentary_id: str, book_filter: str | None) -> list[dict]:
    resp = client.get(f"{HELLOAO_BASE}/c/{commentary_id}/books.json", timeout=30)
    resp.raise_for_status()
    books = resp.json()["books"]
    if book_filter:
        books = [b for b in books if b["id"] == book_filter]
    return books


def ingest_commentary(*, commentary_id: str, book_filter: str | None, limit: int | None) -> int:
    label = COMMENTARY_LABELS[commentary_id]
    count = 0
    with httpx.Client() as client, SyncSessionLocal() as session:
        books = _commentary_books(client, commentary_id, book_filter)
        print(f"Ingesting {label}'s commentary for {len(books)} book(s)...")
        for book in books:
            num_chapters = book["numberOfChapters"]
            for chapter_num in range(1, num_chapters + 1):
                if limit is not None and count >= limit:
                    return count
                resp = client.get(f"{HELLOAO_BASE}/c/{commentary_id}/{book['id']}/{chapter_num}.json", timeout=30)
                if resp.status_code != 200:
                    continue
                chapter = resp.json()["chapter"]
                entries = [e for e in chapter.get("content", []) if e.get("type") == "verse"]
                if not entries:
                    continue

                passage_reference = f"{book['commonName']} {chapter_num}"
                doc = _upsert_document(
                    session,
                    reference_type=ReferenceType.COMMENTARY.value,
                    title=f"{label}: {passage_reference}",
                    passage_reference=passage_reference,
                    source_id=commentary_id,
                )

                texts: list[str] = []
                for entry in entries:
                    entry_text = "\n".join(entry.get("content", []))
                    if not entry_text.strip():
                        continue
                    texts.extend(c.content for c in chunk_text(entry_text))

                _upsert_chunks(session, reference_type=ReferenceType.COMMENTARY.value, document_id=doc.id, texts=texts)
                session.commit()
                count += 1
                print(f"  {passage_reference} ({len(texts)} chunk(s))")
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=["cross-references", "commentary", "all"], default="all")
    parser.add_argument(
        "--commentary-id",
        choices=list(COMMENTARY_LABELS),
        default="matthew-henry",
        help="Which commentary to ingest when --source is commentary/all (default: matthew-henry)",
    )
    parser.add_argument("--book", default=None, help="Filter to one book (e.g. 'Gen' for cross-refs, 'JHN' for commentary — different id formats per source)")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of documents processed, for a bounded test run")
    args = parser.parse_args()

    total = 0
    if args.source in ("cross-references", "all"):
        total += ingest_cross_references(book_filter=args.book, limit=args.limit)
    if args.source in ("commentary", "all"):
        total += ingest_commentary(commentary_id=args.commentary_id, book_filter=args.book, limit=args.limit)
    print(f"Done — {total} reference document(s) ingested/updated.")


if __name__ == "__main__":
    main()
