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


def grade_once(assignment_name, rubric_text, student_id, student_text, expected_total):
    result = grading.grade_submission(
        assignment_name, rubric_text, student_id, student_text, expected_total=expected_total
    )
    if not result["ok"]:
        return None, result["error"]
    return result["data"], None


def grade_repeatedly(spec, text, student_id, repeats):
    """Returns (scores, errors, quality) -- quality summarising the last good run."""
    scores, errors, quality = [], [], {}
    for _ in range(repeats):
        data, error = grade_once(
            spec["assignment_name"], spec["rubric_text"], student_id, text, spec["total_points"]
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
        print(f"  triage: {summary['low_confidence_rows']} row(s) reported low confidence")

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
    parser.add_argument("--skip-probes", action="store_true")
    args = parser.parse_args()

    spec = json.loads((REPO_ROOT / args.labels).read_text())
    spec["rubric_text"] = read_text(spec["rubric_file"])
    resolve_expected_totals(spec)

    print(f"Grading {len(spec['submissions'])} submission(s) against {spec['rubric_file']}")
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
