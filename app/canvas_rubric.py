"""Read a rubric that faculty built in Canvas, straight off the assignment.

When a rubric is attached to an assignment, Canvas returns it inline on the
assignment object as `rubric` (the criteria) and `rubric_settings` (title and
total). That means this needs no Developer Key scope beyond the assignment
endpoint the app already calls -- nothing new to request from UITS.

What a Canvas rubric gives us is *structure*: criterion names, the points on
each, and the rating levels. What it does not give us is an answer key -- a
criterion called "Q1: Binary search trees, 10 points, Exceeds/Mastery/Near"
says how many points are available and nothing about what a correct answer
looks like. That substance still comes from the file faculty upload, which is
why the two are combined rather than one replacing the other.
"""


def _clean(value):
    return (value or "").strip()


def from_assignment(assignment):
    """Return a dict describing the assignment's Canvas rubric, or None.

    {"criteria": [...], "points_possible": float|None, "title": str, "text": str}
    """
    raw_criteria = assignment.get("rubric")
    if not isinstance(raw_criteria, list) or not raw_criteria:
        return None

    criteria = []
    for criterion in raw_criteria:
        if not isinstance(criterion, dict):
            continue
        ratings = [
            {
                "description": _clean(r.get("description")),
                "long_description": _clean(r.get("long_description")),
                "points": r.get("points"),
            }
            for r in (criterion.get("ratings") or [])
            if isinstance(r, dict)
        ]
        criteria.append({
            "id": criterion.get("id"),
            "description": _clean(criterion.get("description")) or "Untitled criterion",
            "long_description": _clean(criterion.get("long_description")),
            "points": criterion.get("points"),
            "ratings": ratings,
        })

    if not criteria:
        return None

    settings = assignment.get("rubric_settings") or {}
    points_possible = settings.get("points_possible")
    if points_possible is None:
        # Older payloads omit rubric_settings; the criteria still add up.
        totals = [c["points"] for c in criteria if isinstance(c["points"], (int, float))]
        points_possible = sum(totals) if totals else None

    return {
        "criteria": criteria,
        "points_possible": points_possible,
        "title": _clean(settings.get("title")) or "Rubric",
        "text": render(criteria, points_possible),
    }


def render(criteria, points_possible=None):
    """Flatten the criteria into the plain text the grading prompt consumes."""
    lines = []
    if points_possible is not None:
        lines.append(f"Total points available: {_num(points_possible)}")
        lines.append("")

    for index, criterion in enumerate(criteria, start=1):
        points = _num(criterion["points"]) if criterion["points"] is not None else "unspecified"
        lines.append(f"Criterion {index}: {criterion['description']} ({points} points)")
        if criterion["long_description"]:
            lines.append(f"  What this criterion requires: {criterion['long_description']}")
        for rating in criterion["ratings"]:
            label = rating["description"] or "Level"
            rating_points = _num(rating["points"]) if rating["points"] is not None else "?"
            detail = f" - {rating['long_description']}" if rating["long_description"] else ""
            lines.append(f"    {rating_points} points: {label}{detail}")
        lines.append("")

    return "\n".join(lines).strip()


def _num(value):
    """Render 10.0 as 10 so point values read the way faculty wrote them."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
