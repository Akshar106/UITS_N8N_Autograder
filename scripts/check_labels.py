#!/usr/bin/env python3
"""Validate an evaluation label set without calling the model.

    python3 scripts/check_labels.py path/to/labels.json

Confirms the rubric sub-parts sum to the stated total, every submission has a
verdict on every part, every referenced file exists and reads, and prints the
resulting score distribution. Run it after editing a label set: a corpus whose
labels are wrong measures nothing, and the mistake is invisible from the eval
output alone.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import extract  # noqa: E402
from scripts.eval_grading import build_rubric, resolve_expected_totals  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _key(name):
    """Match a label's part name to a criterion's: 'Step1' is 'Step 1'."""
    return "".join((name or "").split()).lower()


def check_one(path):
    spec = json.loads(path.read_text())
    print(f"\n=== {path.relative_to(ROOT)} ===")
    print(f"{spec['assignment_name']}  ({spec['total_points']} points)")

    resolve_expected_totals(spec)  # raises SystemExit with a specific message

    # Built the same way the app builds it, so a label set pointed at a Canvas
    # rubric is checked against the prompt the grader will really see.
    rubric_text, criteria = build_rubric(spec)
    scale = ""
    if criteria:
        binary = sum(1 for c in criteria if c.get("binary"))
        scale = f", {len(criteria)} criteria ({binary} all-or-nothing)"
    print(f"rubric reads: {len(rubric_text)} chars, "
          f"{len(spec.get('rubric_parts', {}))} sub-parts{scale}")

    rows, problems = [], []

    if criteria:
        declared = sum(c["points"] for c in criteria if c["points"] is not None)
        if abs(declared - spec["total_points"]) > 0.01:
            problems.append(f"the Canvas criteria sum to {declared} but total_points "
                            f"is {spec['total_points']}")
        # A criterion with only Full Marks / No Marks cannot be half satisfied,
        # so a label that says 0.5 describes a grade the rubric cannot express.
        binary = {_key(c["description"]) for c in criteria if c.get("binary")}
        for entry in spec["submissions"]:
            for part, value in (entry.get("satisfies") or {}).items():
                if 0 < value < 1 and _key(part) in binary:
                    problems.append(f"{entry['file']}: {part} is all-or-nothing but the "
                                    f"label says {value}")
    for entry in spec["submissions"] + [
        {"file": p[k], "note": f"probe {p['name']} ({k})"}
        for p in spec.get("probes", []) for k in ("baseline", "variant")
    ]:
        file_path = ROOT / entry["file"]
        if not file_path.exists():
            problems.append(f"missing file: {entry['file']}")
            continue
        try:
            doc = extract.extract_document(file_path.name, file_path.read_bytes())
        except extract.ExtractionError as exc:
            problems.append(f"{entry['file']}: will not extract -- {exc}")
            continue
        if doc.looks_incomplete:
            problems.append(f"{entry['file']}: extraction flagged incomplete")
        rows.append((file_path.name, entry.get("expected_total"), doc.chars, entry.get("note", "")))

    print(f"\n{'submission':<38}{'expected':>10}{'chars':>8}")
    for name, expected, chars, _ in rows:
        shown = f"{expected:.1f}" if expected is not None else "-"
        print(f"  {name:<36}{shown:>10}{chars:>8}")

    scored = [e for _, e, _, _ in rows if e is not None]
    if scored:
        buckets = {"0-25": 0, "25-50": 0, "50-75": 0, "75-100": 0}
        for value in scored:
            share = value / spec["total_points"]
            key = "0-25" if share < .25 else "25-50" if share < .5 else "50-75" if share < .75 else "75-100"
            buckets[key] += 1
        print("\ndistribution: " + "  ".join(f"{k}%: {v}" for k, v in buckets.items()))
        middle = buckets["25-50"] + buckets["50-75"]
        if middle < len(scored) / 3:
            print("  NOTE: thin in the middle. The extremes are where any grader agrees;")
            print("        the middle is where one is actually tested.")

    if problems:
        print("\nPROBLEMS:")
        for problem in problems:
            print(f"  - {problem}")
    return problems


def main():
    targets = [Path(a) for a in sys.argv[1:]]
    if not targets:
        print("usage: check_labels.py path/to/labels.json [...]", file=sys.stderr)
        return 2
    problems = []
    for target in targets:
        problems += check_one(target if target.is_absolute() else ROOT / target)
    print()
    print(f"{len(problems)} problem(s)" if problems else "ALL LABEL SETS VALID")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
