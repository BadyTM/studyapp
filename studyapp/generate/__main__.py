"""The generation pilot: run a few chunks and see what comes back.

    python -m studyapp.generate --limit 5 --dry-run

This is the tool the prompt gets tuned with, so it reports the things that decide
whether the prompt is ready: what fraction of questions survived validation, what
the rejected ones died of, how many quotes were found in the PDF, and what it
cost. Tuning after six hundred questions have been generated is wasted money.

Running the whole document happens in the app (phase 4), where it can be paused
and resumed. This stays single-threaded on purpose -- it writes as it goes, and
one writer is the rule.
"""

import argparse
import sys
import textwrap

from .. import config
from ..db import connection as db_connection
from ..db import repo
from . import client
from .pipeline import generate_for_chunk


def show_question(question: dict, index: int) -> str:
    """One question, laid out to be read rather than parsed."""
    lines = [f"  {index}. [{question['type']}] " + "\n     ".join(
        textwrap.wrap(question["question"], 76))]
    if question["type"] == "mcq":
        for position, option in enumerate(question["options"]):
            mark = "->" if position == question["correct_index"] else "  "
            lines.append(f"      {mark} {position + 1}. {option}")
    else:
        lines.append("      answer: " + "\n              ".join(
            textwrap.wrap(question["correct_text"], 68)))
    page = question.get("page")
    where = f"p. {page + 1}" if page is not None else "PAGE NOT FOUND"
    boxes = len(question.get("rects") or [])
    lines.append(f"      quote ({question['quote_score']:.2f}, {where}, "
                 f"{boxes} rect{'s' if boxes != 1 else ''}): "
                 f"{question['source_quote'][:90]}...")
    return "\n".join(lines)


def report(results, dry_run: bool) -> int:
    """Prints the summary and returns the process exit code."""
    accepted = sum(len(result.questions) for result in results)
    rejected = sum(len(result.validation.rejected) for result in results
                   if result.validation)
    located = sum(result.located for result in results)
    failed = [result for result in results if result.error]
    skipped = [result for result in results if not result.error and not result.questions]

    reasons: dict[str, int] = {}
    for result in results:
        if result.validation:
            for reason, count in result.validation.reason_counts().items():
                reasons[reason] = reasons.get(reason, 0) + count

    total = accepted + rejected
    pass_rate = accepted / total if total else 0.0
    located_rate = located / accepted if accepted else 0.0

    print("\n" + "=" * 78)
    print(f"chunks       : {len(results)} run, {len(failed)} failed, "
          f"{len(skipped)} skipped (no questions)")
    print(f"questions    : {accepted} kept, {rejected} rejected "
          f"-> {pass_rate:.0%} pass rate      (phase 3 wants >85%)")
    print(f"quotes found : {located}/{accepted} "
          f"-> {located_rate:.0%} with a page and rects  (phase 5 wants >90%)")
    if reasons:
        print("rejected for :")
        for reason, count in sorted(reasons.items(), key=lambda item: -item[1]):
            print(f"               {count:>3}  {reason}")
    for result in failed:
        print(f"  chunk {result.chunk_id} failed: {result.error}")
    if dry_run:
        print("\nDry run: nothing was written.")
    print("=" * 78)

    # A non-zero exit when the gates are not met, so this is usable from a script.
    return 0 if (pass_rate > 0.85 and located_rate > 0.90) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m studyapp.generate")
    parser.add_argument("--limit", type=int, default=5,
                        help="how many pending chunks to run (default 5)")
    parser.add_argument("--document", type=int, help="document id; default is the newest")
    parser.add_argument("--dry-run", action="store_true",
                        help="generate and report, write nothing")
    parser.add_argument("--backend", choices=["auto", "api", "claude-code"],
                        default=config.BACKEND)
    parser.add_argument("--quiet", action="store_true", help="summary only")
    arguments = parser.parse_args(argv)

    db_connection.ensure_schema()
    database = db_connection.connect()
    try:
        documents = repo.list_documents(database)
        if not documents:
            print("No documents imported yet. Run: python -m studyapp.ingest <pdf>",
                  file=sys.stderr)
            return 1
        document = (repo.get_document(database, arguments.document)
                    if arguments.document else documents[0])
        if document is None:
            print(f"No document #{arguments.document}.", file=sys.stderr)
            return 1

        chunks = repo.get_pending_chunks(database, document["id"], arguments.limit)
        if not chunks:
            print(f"Nothing pending for {document['filename']}.")
            return 0

        try:
            backend = client.create_backend(arguments.backend)
        except client.GenerationError as error:
            print(str(error), file=sys.stderr)
            return 1

        print(f"{document['filename']} · {len(chunks)} chunks · backend: {backend.name}")

        results = []
        for position, chunk in enumerate(chunks, start=1):
            title = chunk["title"] or "(untitled)"
            print(f"\n[{position}/{len(chunks)}] chunk #{chunk['ordinal']} "
                  f"p.{chunk['page_start'] + 1}-{chunk['page_end'] + 1} · {title}",
                  flush=True)

            result = generate_for_chunk(backend, chunk, document)
            results.append(result)

            if result.error:
                print(f"  FAILED: {result.error}")
            elif not arguments.quiet:
                for index, question in enumerate(result.questions, start=1):
                    print(show_question(question, index))
                if result.validation and result.validation.rejected:
                    for item in result.validation.rejected:
                        print(f"  rejected ({item.reason.value}): {item.detail[:70]}")

            if not arguments.dry_run:
                # Single-threaded here, so writing inline is safe. The app does
                # this from one thread while the workers only compute.
                if result.questions:
                    # Retire the chunk's previous generated questions first, as
                    # the app does. Without this, regenerating a chunk from here
                    # left both copies active and the bank asked the same thing
                    # twice -- and the hand-edited ones are spared either way.
                    repo.deactivate_unedited_questions(database, chunk["id"])
                    repo.insert_questions(database, chunk["id"], result.questions)
                repo.set_chunk_status(database, chunk["id"], result.status, result.error)

            print(f"  {backend.costs.status_text()}", flush=True)

        return report(results, arguments.dry_run)
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
