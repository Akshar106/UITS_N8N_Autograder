#!/usr/bin/env python3
"""Derive degraded variants of the Forecast/EDA submission.

    python3 scripts/make_eda_variants.py

A label set in which every submission earns full marks can only ever detect the
grader being too harsh. The known failure mode is the opposite one, so the
fixture needs papers that should LOSE specific marks, and the cheapest honest
way to get them is to damage a complete submission in ways real students
actually produce:

  eda_02  Steps 3 and 6 written but never executed -- the single judgment a
          notebook grader has to make that a document grader never does.
  eda_03  Step 3 raises. Code that crashes did not do the step.
  eda_04  Steps 8 and 9 simply absent.

Each variant is derived mechanically from the complete notebook, so what
separates it from full marks is exactly the thing being tested and nothing else.
Cells are located by their '## Step N' markdown headings rather than by index,
so this survives the source notebook being edited.
"""
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HERE = REPO_ROOT / "sample_data/assignments/forecast_eda/submissions"
SOURCE = HERE / "eda_01_complete.ipynb"

_HEADING = re.compile(r"^#+\s*Step\s+(\d+)", re.IGNORECASE | re.MULTILINE)


def source_of(cell):
    value = cell.get("source")
    return "".join(value) if isinstance(value, list) else str(value or "")


def step_spans(cells):
    """Map each step number to the indices of the cells that implement it."""
    spans, current = {}, None
    for index, cell in enumerate(cells):
        if cell.get("cell_type") == "markdown":
            match = _HEADING.search(source_of(cell))
            if match:
                current = int(match.group(1))
                spans.setdefault(current, [])
                continue
        if current is not None:
            spans[current].append(index)
    return spans


def code_indices(cells, indices):
    return [i for i in indices if cells[i].get("cell_type") == "code" and source_of(cells[i]).strip()]


def load():
    return json.loads(SOURCE.read_text())


def write(name, notebook):
    path = HERE / name
    path.write_text(json.dumps(notebook, indent=1))
    print(f"  wrote {path.relative_to(REPO_ROOT)}")


def make_unrun(steps):
    notebook = load()
    cells = notebook["cells"]
    spans = step_spans(cells)
    touched = 0
    for step in steps:
        for index in code_indices(cells, spans[step]):
            cells[index]["execution_count"] = None
            cells[index]["outputs"] = []
            touched += 1
    print(f"  steps {steps}: {touched} cell(s) left unexecuted")
    return notebook


def make_broken(step):
    notebook = load()
    cells = notebook["cells"]
    indices = code_indices(cells, step_spans(notebook["cells"])[step])
    # Break the call that actually performs the step, and give the cell the
    # traceback Jupyter would have stored.
    target = next(i for i in indices if "drop_duplicates" in source_of(cells[i]))
    cells[target]["source"] = source_of(cells[target]).replace("drop_duplicates", "drop_duplictes")
    cells[target]["outputs"] = [{
        "output_type": "error",
        "ename": "AttributeError",
        "evalue": "'DataFrame' object has no attribute 'drop_duplictes'",
        "traceback": [
            "\u001b[0;31m---------------------------------------------------------------------------\u001b[0m",
            "\u001b[0;31mAttributeError\u001b[0m                            Traceback (most recent call last)",
            "Cell \u001b[0;32mIn[76], line 1\u001b[0m",
            "\u001b[0;31mAttributeError\u001b[0m: 'DataFrame' object has no attribute 'drop_duplictes'",
        ],
    }]
    print(f"  step {step}: cell {target + 1} now raises AttributeError")
    return notebook


def make_missing(steps):
    notebook = load()
    cells = notebook["cells"]
    spans = step_spans(cells)
    drop = {i for step in steps for i in spans[step]}
    # The markdown heading goes too, so the step is absent rather than announced
    # and then skipped -- the latter is a different (easier) thing to detect.
    for step in steps:
        for index, cell in enumerate(cells):
            if cell.get("cell_type") == "markdown" and _HEADING.search(source_of(cell)):
                if int(_HEADING.search(source_of(cell)).group(1)) == step:
                    drop.add(index)
    notebook["cells"] = [c for i, c in enumerate(cells) if i not in drop]
    print(f"  steps {steps}: {len(drop)} cell(s) removed")
    return notebook


def main():
    if not SOURCE.exists():
        print(f"missing {SOURCE.relative_to(REPO_ROOT)}", file=sys.stderr)
        return 1

    cells = load()["cells"]
    found = sorted(step_spans(cells))
    if found != list(range(1, 10)):
        print(f"expected Step 1..9 headings, found {found}", file=sys.stderr)
        return 1
    print(f"Deriving variants from {SOURCE.name} ({len(cells)} cells, steps {found[0]}-{found[-1]})")

    write("eda_02_unrun_steps_3_and_6.ipynb", make_unrun([3, 6]))
    write("eda_03_error_step_3.ipynb", make_broken(3))
    write("eda_04_missing_steps_8_and_9.ipynb", make_missing([8, 9]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
