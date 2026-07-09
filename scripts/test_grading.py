#!/usr/bin/env python3
"""Standalone smoke test for the grading pipeline -- no Canvas or browser needed.

Usage:
    python3 scripts/test_grading.py <rubric_file> <submission_file_or_dir> [assignment_name]

Example (using the bundled sample data):
    python3 scripts/test_grading.py sample_data/rubric.txt sample_data/submissions

Each submission's filename (minus extension) is used as the student id.
Writes test_grades_output.xlsx in the current directory so you can see the
same formatted export the review queue produces.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import extract, grading, excel_export  # noqa: E402


def load_text(path: Path) -> str:
    return extract.extract_text(path.name, path.read_bytes())


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    rubric_path = Path(sys.argv[1])
    submissions_path = Path(sys.argv[2])
    assignment_name = sys.argv[3] if len(sys.argv) > 3 else "Test Assignment"

    rubric_text = load_text(rubric_path)
    print(f"Loaded rubric from {rubric_path} ({len(rubric_text)} chars)\n")

    if submissions_path.is_dir():
        submission_files = sorted(p for p in submissions_path.iterdir() if p.is_file())
    else:
        submission_files = [submissions_path]

    if not submission_files:
        print(f"No submission files found in {submissions_path}")
        sys.exit(1)

    results = []
    for sub_path in submission_files:
        student_id = sub_path.stem
        print(f"Grading {sub_path.name} (student_id={student_id})...")
        student_text = load_text(sub_path)
        result = grading.grade_submission(assignment_name, rubric_text, student_id, student_text)

        if result["ok"]:
            data = result["data"]
            print(f"  Score: {data['score_awarded']} / {data['total_points']}")
            print(f"  Feedback: {data['overall_feedback']}\n")
            results.append({
                "student_name": student_id, "student_id": student_id,
                "ai_score": data["score_awarded"], "ai_total": data["total_points"],
                "faculty_score": None, "faculty_feedback": None,
                "ai_feedback": data["overall_feedback"], "approved": 0, "ai_error": None,
            })
        else:
            print(f"  FAILED: {result['error']}\n")
            results.append({
                "student_name": student_id, "student_id": student_id,
                "ai_score": None, "ai_total": None,
                "faculty_score": None, "faculty_feedback": None,
                "ai_feedback": None, "approved": 0, "ai_error": result["error"],
            })

    out_path = Path("test_grades_output.xlsx")
    buffer = excel_export.build_grades_workbook(assignment_name, results)
    out_path.write_bytes(buffer.read())
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
