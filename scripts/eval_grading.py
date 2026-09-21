#!/usr/bin/env python3
"""Measure the grader: accuracy against known scores, consistency, and bias.

    python3 scripts/eval_grading.py                          # accuracy + probes
    python3 scripts/eval_grading.py --repeats 3              # also measure run-to-run spread
    python3 scripts/eval_grading.py --labels my_labels.json  # against real TA grades
    python3 scripts/eval_grading.py --out report.json        # machine-readable results

Needs REALLM_API_KEY / REALLM_URL / REALLM_MODEL_NAME in .env, because it calls
the real model -- that is the point.

WHY THIS EXISTS
No prompt change is worth making unless you can tell whether it helped. This is
the only thing in the repository that answers "how accurate is the grading?",
and the only honest basis for telling a faculty member -- or UITS -- what the
tool's error rate actually is.

WHAT THE NUMBERS MEAN
  MAE            average points of disagreement with the expected score
  within +/-N    share of submissions scored within N points of expected
  spread         highest minus lowest score across repeats of the SAME
                 submission; anything above 0 means grading is not reproducible
  probes         paired submissions that differ only in writing style, not in
                 content. A large delta is a bias, not a rounding error.

Accuracy is only as good as the labels. With the bundled provisional labels this
tells you the grader has not broken; it does NOT tell you it agrees with a TA.
For that, label real submissions from a completed assignment -- ideally graded
independently by two TAs, so you also learn what human-to-human agreement looks
like. That is the ceiling worth aiming at, not 100%.
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import extract, grading  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def read_text(relative_path):
    path = REPO_ROOT / relative_path
    return extract.extract_text(path.name, path.read_bytes())


def resolve_expected_totals(spec):
    """Turn per-part specs into expected scores, and check they are coherent.

    A label written as "satisfies Q1a fully and Q1b halfway" is arithmetic, not
    an opinion, and a disagreement can then be traced to a specific rubric
    requirement rather than to a number somebody guessed. Labels that already
    carry expected_total directly are left alone.
    """
    parts = spec.get("rubric_parts")
    if parts:
        declared = sum(parts.values())
        if abs(declared - spec["total_points"]) > 0.01:
            raise SystemExit(
                f"rubric_parts sum to {declared} but total_points is {spec['total_points']}"
            )

    for entry in spec["submissions"]:
        satisfies = entry.get("satisfies")
        if satisfies is None:
            if "expected_total" not in entry:
                raise SystemExit(f"{entry['file']}: needs either satisfies or expected_total")
            continue
        if not parts:
            raise SystemExit(f"{entry['file']}: uses satisfies but the labels define no rubric_parts")
        unknown = sorted(set(satisfies) - set(parts))
        if unknown:
            raise SystemExit(f"{entry['file']}: unknown rubric part(s) {unknown}")
        missing = sorted(set(parts) - set(satisfies))
        if missing:
            raise SystemExit(f"{entry['file']}: no verdict for rubric part(s) {missing}")
        bad = {k: v for k, v in satisfies.items() if not 0 <= v <= 1}
        if bad:
            raise SystemExit(f"{entry['file']}: satisfaction must be between 0 and 1: {bad}")
        entry["expected_total"] = round(sum(parts[k] * v for k, v in satisfies.items()), 1)


def build_rubric(spec):
    """Assemble the rubric text exactly the way the app assembles it.

    A label set can point at a plain rubric file, or at a Canvas assignment
    payload plus the task description faculty would upload alongside it. The
    second path goes through the app's own combiner, so what the evaluation
    measures is the prompt the app really builds -- including the rating scale,
    which is what makes an all-or-nothing rubric behave like one.
    """
    from app import canvas_rubric
    from app.routes.rubric import _combine_rubric_sources

    if not spec.get("canvas_rubric_file"):
        return read_text(spec["rubric_file"]), None

    payload = json.loads((REPO_ROOT / spec["canvas_rubric_file"]).read_text())
    canvas_part = canvas_rubric.from_assignment(payload)
    if canvas_part is None:
        raise SystemExit(f"{spec['canvas_rubric_file']}: no rubric in this payload")

    answer_key = read_text(spec["answer_key_file"]) if spec.get("answer_key_file") else ""
    criteria = [{"description": c["description"], "points": c["points"],
                 "binary": bool(c.get("binary"))} for c in canvas_part["criteria"]]
    return _combine_rubric_sources(canvas_part, answer_key), criteria


def grade_once(assignment_name, rubric_text, student_id, student_text, expected_total,
               passes=1, expected_criteria=None):
    result = grading.grade_submission(
        assignment_name, rubric_text, student_id, student_text,
        expected_total=expected_total, passes=passes,
        expected_criteria=expected_criteria,
    )
    if not result["ok"]:
        return None, result["error"]
    return result["data"], None


def grade_repeatedly(spec, text, student_id, repeats):
    """Returns (scores, errors, quality) -- quality summarising the last good run."""
    scores, errors, quality = [], [], {}
    for _ in range(repeats):
        data, error = grade_once(
            spec["assignment_name"], spec["rubric_text"], student_id, text,
            spec["total_points"], passes=spec.get("_passes", 1),
            expected_criteria=spec.get("expected_criteria"),
        )
        if error:
            errors.append(error)
            continue
        scores.append(data["score_awarded"])
        breakdown = data["per_question_breakdown"]
        # How well the model supported its own judgments, which is as much a
        # quality signal as the score itself.
        quality = {
            "confidence": data.get("confidence"),
            "questions": len(breakdown),
            "quoted": sum(1 for q in breakdown if q.get("evidence")),
            "unverified": sum(1 for q in breakdown if q.get("evidence_verified") is False),
            "disagreement": data.get("max_disagreement"),
            "raw_disagreement": data.get("raw_disagreement"),
            "structure_retried": bool(data.get("structure_retried")),
            "structural_disagreement": bool(data.get("structural_disagreement")),
            "snapped": data.get("snapped_awards") or 0,
        }
    return scores, errors, quality


def evaluate_submissions(spec, repeats):
    rows = []
    for entry in spec["submissions"]:
        name = Path(entry["file"]).stem
        print(f"  grading {name} ...", flush=True)
        scores, errors, quality = grade_repeatedly(spec, read_text(entry["file"]), name, repeats)

        row = {
            "name": name,
            "expected": entry["expected_total"],
            "note": entry.get("note", ""),
            "scores": scores,
            "errors": errors,
            **quality,
        }
        if scores:
            row["score"] = statistics.median(scores)
            row["error_points"] = row["score"] - entry["expected_total"]
            row["spread"] = max(scores) - min(scores)
        rows.append(row)
    return rows


def evaluate_probes(spec, repeats):
    results = []
    for probe in spec.get("probes", []):
        print(f"  probe {probe['name']} ...", flush=True)
        base, base_errs, _ = grade_repeatedly(spec, read_text(probe["baseline"]), "baseline", repeats)
        var, var_errs, _ = grade_repeatedly(spec, read_text(probe["variant"]), "variant", repeats)

        result = {"name": probe["name"], "max_delta": probe["max_delta"], "why": probe["why"]}
        if base and var:
            result["baseline_score"] = statistics.median(base)
            result["variant_score"] = statistics.median(var)
            result["delta"] = result["variant_score"] - result["baseline_score"]
            result["passed"] = abs(result["delta"]) <= probe["max_delta"]
        else:
            result["passed"] = None
            result["errors"] = base_errs + var_errs
        results.append(result)
    return results


def summarise(rows):
    graded = [r for r in rows if "score" in r]
    if not graded:
        return {"graded": 0}

    errors = [abs(r["error_points"]) for r in graded]
    signed = [r["error_points"] for r in graded]
    spreads = [r["spread"] for r in graded]
    return {
        "graded": len(graded),
        "failed": len(rows) - len(graded),
        "mae": round(statistics.mean(errors), 2),
        "rmse": round((sum(e * e for e in errors) / len(errors)) ** 0.5, 2),
        "bias": round(statistics.mean(signed), 2),
        "within_1": round(100 * sum(e <= 1 for e in errors) / len(errors)),
        "within_2": round(100 * sum(e <= 2 for e in errors) / len(errors)),
        "within_5": round(100 * sum(e <= 5 for e in errors) / len(errors)),
        "max_spread": max(spreads),
        "reproducible": all(s == 0 for s in spreads),
        "questions_total": sum(r.get("questions", 0) for r in graded),
        "questions_quoted": sum(r.get("quoted", 0) for r in graded),
        "quotes_unverified": sum(r.get("unverified", 0) for r in graded),
        "low_confidence_rows": sum(1 for r in graded if r.get("confidence") == "low"),
        "rows_with_disagreement": sum(1 for r in graded if (r.get("disagreement") or 0) > 0),
        "worst_disagreement": max((r.get("disagreement") or 0) for r in graded),
        # Every row where the passes differed at all, material or not. The gap
        # between this and rows_with_disagreement is what the threshold is
        # saving the faculty member from reading.
        "rows_with_any_spread": sum(1 for r in graded if (r.get("raw_disagreement") or 0) > 0),
        "worst_raw_disagreement": max((r.get("raw_disagreement") or 0) for r in graded),
        # The passes split the rubric into different numbers of questions and
        # needed a tie-breaker. Rare, but it decides the whole grade when it
        # happens, so it is worth seeing rather than buried in the log.
        "structure_retried": sum(1 for r in graded if r.get("structure_retried")),
        "structure_unresolved": sum(1 for r in graded if r.get("structural_disagreement")),
        "snapped": sum(r.get("snapped") or 0 for r in graded),
    }


def print_report(spec, rows, probes, summary, repeats):
    print("\n" + "=" * 97)
    print(f"GRADING EVALUATION  --  {spec['assignment_name']}  ({repeats} run(s) per submission)")
    print("=" * 97)

    print(f"\n{'submission':<32}{'expected':>10}{'scored':>10}{'error':>9}"
          f"{'spread':>9}{'conf':>9}{'quotes':>16}")
    print("-" * 97)
    for row in rows:
        if "score" not in row:
            print(f"{row['name']:<32}{row['expected']:>10}{'FAILED':>10}   "
                  f"{(row['errors'] or ['unknown'])[0][:32]}")
            continue
        quotes = f"{row.get('quoted', 0)}/{row.get('questions', 0)}"
        if row.get("unverified"):
            quotes += f" ({row['unverified']} bad)"
        print(f"{row['name']:<32}{row['expected']:>10}{row['score']:>10}"
              f"{row['error_points']:>+9.1f}{row['spread']:>9.1f}"
              f"{(row.get('confidence') or '-'):>9}{quotes:>16}")

    if summary.get("graded"):
        print(f"\n  MAE {summary['mae']} points   RMSE {summary['rmse']}   "
              f"mean signed error {summary['bias']:+}")
        print(f"  within 1 point: {summary['within_1']}%   "
              f"within 2: {summary['within_2']}%   within 5: {summary['within_5']}%")
        if summary["failed"]:
            print(f"  {summary['failed']} submission(s) could not be graded at all")
        if repeats > 1:
            verdict = "reproducible" if summary["reproducible"] else \
                      f"NOT reproducible (largest spread {summary['max_spread']} points)"
            print(f"  run-to-run: {verdict}")
        print(f"  grounding: {summary['questions_quoted']}/{summary['questions_total']} questions "
              f"cited the student's words, {summary['quotes_unverified']} quote(s) not found in "
              f"the submission")
        print(f"  triage: {summary['low_confidence_rows']} of {summary['graded']} row(s) "
              f"flagged for a human")
        if summary.get("worst_raw_disagreement"):
            print(f"  adjudication: passes differed on {summary['rows_with_any_spread']} row(s) "
                  f"(worst {summary['worst_raw_disagreement']} points); "
                  f"{summary['rows_with_disagreement']} of those cleared the materiality bar "
                  f"(worst {summary['worst_disagreement']})")
        if summary.get("structure_retried"):
            print(f"  structure: {summary['structure_retried']} row(s) where the passes split "
                  f"the rubric differently and needed a tie-breaker, "
                  f"{summary['structure_unresolved']} still unresolved")
        if summary.get("snapped"):
            print(f"  rubric scale: {summary['snapped']} award(s) snapped back onto an "
                  f"all-or-nothing criterion")

    if probes:
        print("\nBIAS PROBES  (same content, different writing)")
        print("-" * 97)
        for probe in probes:
            if probe["passed"] is None:
                print(f"  {probe['name']:<12} could not be graded")
                continue
            verdict = "ok" if probe["passed"] else "FAIL"
            print(f"  {probe['name']:<12} {probe['baseline_score']} -> {probe['variant_score']}"
                  f"  delta {probe['delta']:+.1f}  (limit +/-{probe['max_delta']})  {verdict}")
            if not probe["passed"]:
                print(f"               {probe['why']}")

    if spec.get("label_source") != "human":
        constructed = spec.get("label_source") == "constructed"
        origin = ("CONSTRUCTED LABELS -- each expected score is the weighted sum of the rubric\n"
                  "sub-parts the submission was written to satisfy. That makes a disagreement\n"
                  "traceable to a specific requirement, but the verdicts are still the author's\n"
                  "reading of these answers"
                  if constructed else
                  "PROVISIONAL LABELS -- these expected scores were estimated")
        print("\n" + "!" * 78)
        print(f"{origin}, not grades a TA assigned. Treat the")
        print("accuracy figures above as a regression check only. Do not quote them to faculty")
        print("or to UITS as the tool's accuracy.")
        print("!" * 78)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--labels", default="sample_data/eval/labels.json")
    parser.add_argument("--repeats", type=int, default=1,
                        help="grade each submission N times to measure run-to-run spread")
    parser.add_argument("--out", help="write the full results as JSON")
    parser.add_argument("--passes", type=int, default=1,
                        help="independent gradings per submission, compared and merged "
                             "(the app defaults to 2; 1 here keeps runs comparable)")
    parser.add_argument("--skip-probes", action="store_true")
    args = parser.parse_args()

    spec = json.loads((REPO_ROOT / args.labels).read_text())
    spec["rubric_text"], spec["expected_criteria"] = build_rubric(spec)
    resolve_expected_totals(spec)
    spec["_passes"] = args.passes

    source = spec.get("canvas_rubric_file") or spec["rubric_file"]
    print(f"Grading {len(spec['submissions'])} submission(s) against {source}")
    rows = evaluate_submissions(spec, args.repeats)
    probes = [] if args.skip_probes else evaluate_probes(spec, args.repeats)
    summary = summarise(rows)

    print_report(spec, rows, probes, summary, args.repeats)

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"summary": summary, "submissions": rows, "probes": probes}, indent=2
        ))
        print(f"\nWrote {args.out}")

    failed_probes = [p for p in probes if p["passed"] is False]
    return 1 if failed_probes or not summary.get("graded") else 0


if __name__ == "__main__":
    sys.exit(main())
