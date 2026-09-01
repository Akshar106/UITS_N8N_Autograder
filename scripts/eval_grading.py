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


def grade_once(assignment_name, rubric_text, student_id, student_text, expected_total):
    result = grading.grade_submission(
        assignment_name, rubric_text, student_id, student_text, expected_total=expected_total
    )
    if not result["ok"]:
        return None, result["error"]
    return result["data"]["score_awarded"], None


def grade_repeatedly(spec, text, student_id, repeats):
    scores, errors = [], []
    for _ in range(repeats):
        score, error = grade_once(
            spec["assignment_name"], spec["rubric_text"], student_id, text, spec["total_points"]
        )
        (errors if error else scores).append(error if error else score)
    return scores, errors


def evaluate_submissions(spec, repeats):
    rows = []
    for entry in spec["submissions"]:
        name = Path(entry["file"]).stem
        print(f"  grading {name} ...", flush=True)
        scores, errors = grade_repeatedly(spec, read_text(entry["file"]), name, repeats)

        row = {
            "name": name,
            "expected": entry["expected_total"],
            "note": entry.get("note", ""),
            "scores": scores,
            "errors": errors,
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
        base, base_errs = grade_repeatedly(spec, read_text(probe["baseline"]), "baseline", repeats)
        var, var_errs = grade_repeatedly(spec, read_text(probe["variant"]), "variant", repeats)

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
    }


def print_report(spec, rows, probes, summary, repeats):
    print("\n" + "=" * 78)
    print(f"GRADING EVALUATION  --  {spec['assignment_name']}  ({repeats} run(s) per submission)")
    print("=" * 78)

    print(f"\n{'submission':<20}{'expected':>10}{'scored':>10}{'error':>9}"
          f"{'spread':>9}  note")
    print("-" * 78)
    for row in rows:
        if "score" not in row:
            print(f"{row['name']:<20}{row['expected']:>10}{'FAILED':>10}{'':>9}{'':>9}  "
                  f"{(row['errors'] or ['unknown'])[0]}")
            continue
        print(f"{row['name']:<20}{row['expected']:>10}{row['score']:>10}"
              f"{row['error_points']:>+9.1f}{row['spread']:>9.1f}  {row['note']}")

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

    if probes:
        print("\nBIAS PROBES  (same content, different writing)")
        print("-" * 78)
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
        print("\n" + "!" * 78)
        print("PROVISIONAL LABELS -- these expected scores were estimated, not assigned by a")
        print("TA. Treat the accuracy figures above as a regression check only. Do not quote")
        print("them to faculty or to UITS as the tool's accuracy.")
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
