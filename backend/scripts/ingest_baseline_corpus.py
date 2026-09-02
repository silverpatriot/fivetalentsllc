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
0008's unique constraint, widened from 0007's 2-column version
specifically so multiple commentaries can coexist, then migration 0013's
NULLS NOT DISTINCT — required for the upsert to actually collide on
cross-reference rows, which all carry source_id IS NULL; without it
every re-run silently duplicated the whole cross-reference corpus rather
than upserting it, confirmed live before 0013 shipped), so re-running is
safe and replaces existing entries rather than accumulating duplicates.

Bounded/testable runs, not just "ingest everything or nothing" — the
full corpus is ~29K cross-reference groups and ~7,366 commentary
chapters total across all 7 sources, which costs real embedding-API
time/spend; --book and --limit let you verify the pipeline on a small
slice before committing to a full run:

    python scripts/ingest_baseline_corpus.py --source commentary --commentary-id matthew-henry --book JHN --limit 5
    python scripts/ingest_baseline_corpus.py --source commentary --commentary-id adam-clarke --book JHN --limit 5
    python scripts/ingest_baseline_corpus.py --source cross-references --book Gen --limit 20
    python scripts/ingest_baseline_corpus.py --source all   # ALL 7 commentaries + cross-references — the real full run

Omitting --commentary-id (the default) now means every commentary in
COMMENTARY_LABELS, not just matthew-henry — --source all used to mean
"matthew-henry + cross-references" specifically, which undersold what
"all" said on the tin; pass --commentary-id explicitly (as the examples
above do) to restrict a run to one source, same as before.

Cross-reference embedding calls are batched (CROSS_REF_EMBED_BATCH_SIZE
below) — one embed_batch_sync() call per batch of verse groups, not one
per verse group. Pure transport optimization: same text per chunk, same
chunk_index, same upsert semantics; confirmed live before this shipped
that OpenRouter's embeddings endpoint returns bit-identical vectors for
the same input whether it's embedded alone or alongside other inputs in
one batched call (each input's embedding doesn't depend on what else is
in the request) — see the batching change's own commit for that check.
Commentary was already batched per-chapter and is unchanged.
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

# How many verse groups' texts go into one embed_batch_sync() call.
# ~29K groups at one embedding call each (the original shape) is ~29K
# sequential OpenRouter round-trips — measured live at ~0.3s each, so
# ~2.5 hours dominated entirely by network latency, not token cost
# (the tokens themselves cost cents). Batching cuts that to ~294 calls
# for the same total tokens/$ — same content embedded, just fewer trips.
CROSS_REF_EMBED_BATCH_SIZE = 100


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


def _insert_chunks_with_vectors(
    session, *, reference_type: str, document_id, texts: list[str], vectors: list[list[float]]
) -> None:
    """The actual per-chunk upsert, split out from _upsert_chunks so a
    caller that's already batched its own embed_batch_sync() call across
    multiple documents (ingest_cross_references, below) can reuse this
    without triggering a second embedding call."""
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


def _upsert_chunks(session, *, reference_type: str, document_id, texts: list[str]) -> None:
    """One document's worth of chunks, embedded in their own call — what
    ingest_commentary uses (already one call per chapter, no further
    batching needed there)."""
    if not texts:
        return
    vectors = embed_batch_sync(texts)
    _insert_chunks_with_vectors(session, reference_type=reference_type, document_id=document_id, texts=texts, vectors=vectors)


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

    print(f"Ingesting {len(from_verses)} cross-reference groups "
          f"(batched {CROSS_REF_EMBED_BATCH_SIZE} per embedding call)...")
    count = 0
    with SyncSessionLocal() as session:
        for batch_start in range(0, len(from_verses), CROSS_REF_EMBED_BATCH_SIZE):
            batch = from_verses[batch_start : batch_start + CROSS_REF_EMBED_BATCH_SIZE]

            docs = []
            texts = []
            for from_verse in batch:
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
                docs.append(doc)
                texts.append(text)

            # ONE embedding call for the whole batch — identical text per
            # chunk to the unbatched version, just fewer OpenRouter
            # round-trips (see CROSS_REF_EMBED_BATCH_SIZE's comment).
            vectors = embed_batch_sync(texts)
            for doc, text, vector in zip(docs, texts, vectors):
                _insert_chunks_with_vectors(
                    session,
                    reference_type=ReferenceType.CROSS_REFERENCE.value,
                    document_id=doc.id,
                    texts=[text],
                    vectors=[vector],
                )
            session.commit()
            count += len(batch)
            print(f"  {count}/{len(from_verses)}...")
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
        default=None,
        help="Restrict commentary ingestion to one source. Omit (the default) to ingest all "
        f"{len(COMMENTARY_LABELS)} sources in COMMENTARY_LABELS — this used to default to "
        "matthew-henry only, which meant --source all silently skipped the other 6.",
    )
    parser.add_argument("--book", default=None, help="Filter to one book (e.g. 'Gen' for cross-refs, 'JHN' for commentary — different id formats per source)")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of documents processed per source, for a bounded test run")
    args = parser.parse_args()

    total = 0
    if args.source in ("cross-references", "all"):
        total += ingest_cross_references(book_filter=args.book, limit=args.limit)
    if args.source in ("commentary", "all"):
        commentary_ids = [args.commentary_id] if args.commentary_id else list(COMMENTARY_LABELS)
        for commentary_id in commentary_ids:
            total += ingest_commentary(commentary_id=commentary_id, book_filter=args.book, limit=args.limit)
    print(f"Done — {total} reference document(s) ingested/updated.")


if __name__ == "__main__":
    main()
