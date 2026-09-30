#!/usr/bin/env python3
"""Grade real files from the command line -- no Canvas, no browser, no database.

    python3 scripts/test_grading.py <rubric> <submission> ["Assignment name"]

    # one student whose work is split across several files, graded together
    python3 scripts/test_grading.py rubric.pdf ./submission_folder --together

    # a folder of one-file-per-student, graded separately
    python3 scripts/test_grading.py rubric.pdf ./submissions

A submission may be a file or a folder. By default each file in a folder is a
separate student, which is what a quick roster test wants. --together treats
the whole folder as ONE student with several attachments, which is what Canvas
actually delivers when someone uploads a write-up and their code, and is the
only mode that reproduces how the app really grades such a submission.

Prints the extraction notes as well as the score, because when a submission was
transcribed from handwriting the notes are the interesting half of the result:
the score means little until you know how well the page was read.

Writes test_grades_output.xlsx, the same formatted export the review queue
produces.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import excel_export, extract, grading  # noqa: E402


def read(path, assignment_name):
    """Extract one file, returning the document and a printable label."""
    document = extract.extract_document(path.name, path.read_bytes(), assignment_name)
    return document


def describe(document, indent="    "):
    lines = [f"{indent}{document.chars} chars"]
    if document.stats.get("from_ocr"):
        lines[0] += (f", transcribed from {document.stats.get('pages')} page image(s) "
                     f"by {document.stats.get('ocr_backend')}")
    for note in document.notes:
        lines.append(f"{indent}- {note}")
    return "\n".join(lines)


def main():
    argv = [a for a in sys.argv[1:] if a != "--together"]
    together = "--together" in sys.argv
    if len(argv) < 2:
        print(__doc__)
        return 1

    rubric_path, submissions_path = Path(argv[0]), Path(argv[1])
    assignment_name = argv[2] if len(argv) > 2 else "Test Assignment"

    # An unset shell variable arrives as "", which Path turns into "." -- and
    # the failure then surfaces as IsADirectoryError from deep inside pathlib,
    # which says nothing about which argument was wrong.
    for label, path, want_file in (("rubric", rubric_path, False),
                                   ("submission", submissions_path, False)):
        if not str(path).strip() or str(path) == ".":
            print(f"No {label} path given (an empty shell variable?).", file=sys.stderr)
            return 1
        if not path.exists():
            print(f"{label.capitalize()} not found: {path}", file=sys.stderr)
            return 1
        if want_file and path.is_dir():
            print(f"The {label} must be a file, not a folder: {path}", file=sys.stderr)
            return 1

    # A folder is read as one rubric, the way the app combines a Canvas rubric
    # with an uploaded answer key: the questions in one file and the marking
    # scheme in another is the normal shape, not a special case.
    rubric_files = (sorted(p for p in rubric_path.iterdir() if p.is_file())
                    if rubric_path.is_dir() else [rubric_path])
    if not rubric_files:
        print(f"No files in {rubric_path}", file=sys.stderr)
        return 1

    rubric_parts = []
    print(f"Rubric: {rubric_path.name}")
    for path in rubric_files:
        try:
            document = read(path, assignment_name)
        except extract.ExtractionError as exc:
            print(f"  {path.name}: REFUSED -- {exc}")
            continue
        print(f"  {path.name}")
        print(describe(document, indent="      "))
        rubric_parts.append(f"--- {path.name} ---\n{document.text}")
    if not rubric_parts:
        print("Nothing readable in the rubric.", file=sys.stderr)
        return 1
    rubric_text = "\n\n".join(rubric_parts)
    print()

    if submissions_path.is_dir():
        files = sorted(p for p in submissions_path.iterdir() if p.is_file())
    else:
        files = [submissions_path]
    if not files:
        print(f"No files found in {submissions_path}")
        return 1

    # Group into students: one per file, or everything as a single submission.
    if together:
        groups = [(submissions_path.name, files)]
    else:
        groups = [(path.stem, [path]) for path in files]

    results = []
    for student_id, paths in groups:
        print(f"{student_id}")
        parts, notes, transcribed, failures = [], [], False, []
        for path in paths:
            try:
                document = read(path, assignment_name)
            except extract.ExtractionError as exc:
                print(f"  {path.name}: REFUSED -- {exc}")
                failures.append(f"{path.name}: {exc}")
                continue
            print(f"  {path.name}")
            print(describe(document, indent="      "))
            parts.append(f"--- {path.name} ---\n{document.text}")
            notes.extend(f"{path.name}: {note}" for note in document.notes)
            transcribed = transcribed or bool(document.stats.get("from_ocr"))

        row = {"student_name": student_id, "student_id": student_id,
               "faculty_score": None, "faculty_feedback": None, "approved": 0,
               "ai_error": None, "ai_confidence": None, "ai_disagreement": None,
               "submission_attempt": 1, "review_stale": 0,
               "extraction_notes": "\n".join(notes) or None,
               "ai_score": None, "ai_total": None, "ai_feedback": None}

        # Same rule as the app: a file that failed entirely means the grade
        # would cover unknown scope, so there is no honest score to report.
        if failures:
            row["ai_error"] = ("Part of this submission could not be read, so it was "
                               "not graded: " + " ".join(failures))
            print(f"  -> NOT GRADED: {row['ai_error']}\n")
            results.append(row)
            continue
        if not parts:
            row["ai_error"] = "nothing could be read"
            print(f"  -> not graded: {row['ai_error']}\n")
            results.append(row)
            continue

        result = grading.grade_submission(
            assignment_name, rubric_text, student_id, "\n\n".join(parts))
        if result["ok"]:
            data = result["data"]
            # The app forces this for a transcript, because the evidence check
            # verifies quotes against the machine's reading rather than the page.
            confidence = "low" if transcribed else data.get("confidence")
            row.update(ai_score=data["score_awarded"], ai_total=data["total_points"],
                       ai_feedback=data["overall_feedback"], ai_confidence=confidence,
                       ai_disagreement=data.get("max_disagreement"))
            print(f"  -> {data['score_awarded']} / {data['total_points']}"
                  f"   confidence: {confidence}"
                  + ("   [TRANSCRIBED -- check against the original]" if transcribed else ""))
            for item in data.get("per_question_breakdown", []):
                print(f"       {item['question']}: "
                      f"{item['awarded_points']}/{item['max_points']}")
            print(f"     {data['overall_feedback']}\n")
        else:
            row["ai_error"] = result["error"]
            print(f"  -> FAILED: {result['error']}\n")
        results.append(row)

    out = Path("test_grades_output.xlsx")
    out.write_bytes(excel_export.build_grades_workbook(assignment_name, results).read())
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
