#!/usr/bin/env python3
"""Time transcription under different settings, on a real submission.

    python3 scripts/benchmark_ocr.py "Assignment 1/AppliedAlgo_A1.pdf"
    python3 scripts/benchmark_ocr.py page.png --pages 2
    python3 scripts/benchmark_ocr.py doc.pdf --full

Transcription is the slow part of grading a handwritten submission, and which
knob matters is a question about the gateway, not about this code. Guessing has
not worked: the only useful answer comes from running the real pages.

It measures three levers against a baseline:

  model        gemma-4-31B-it reads handwriting well. granite-docling-258m is
               120x smaller and built for document images, so it should be far
               faster -- the question is whether it can read cursive at all.
  resolution   a vision model's cost scales with image area, so 150 dpi is
               roughly half the work of 200. The question is what it loses.
  concurrency  the gateway's latency has a long tail, so more workers help more
               than the averages suggest -- until the gateway pushes back.

Only the first few pages run by default, because each combination costs real
calls. The page cache is deliberately bypassed: a cached page returns instantly
and would make every setting look identical.

Read the OUTPUT column as well as the time. A setting that is twice as fast and
cannot read the handwriting is not faster, it is broken.
"""
import argparse
import io
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OCR_BACKEND", "vision")

from app import extract, ocr  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# model, dpi, concurrency
COMBINATIONS = [
    ("gemma-4-31B-it", 200, 4),
    ("gemma-4-31B-it", 150, 4),
    ("gemma-4-31B-it", 200, 8),
    ("granite-docling-258m", 200, 4),
]


def render(path, dpi):
    """Page images at a given resolution, however the file arrives."""
    data = path.read_bytes()
    if path.suffix.lower() != ".pdf":
        return [data]
    original = extract.PDF_RENDER_DPI
    try:
        extract.PDF_RENDER_DPI = dpi
        return extract._render_pdf_pages(data)
    finally:
        extract.PDF_RENDER_DPI = original


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("submission")
    parser.add_argument("--pages", type=int, default=3,
                        help="how many pages to time per setting (default 3)")
    parser.add_argument("--full", action="store_true", help="use every page")
    parser.add_argument("--context", default="", help="assignment name, as the app passes it")
    args = parser.parse_args()

    path = Path(args.submission)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        return 1

    if not ocr.is_enabled():
        print("Set OCR_BACKEND=vision (or azure) first.", file=sys.stderr)
        return 1

    print(f"Submission: {path.name}")
    results = []
    baseline = None

    for model, dpi, concurrency in COMBINATIONS:
        images = render(path, dpi)
        if not args.full:
            images = images[:args.pages]
        kb = sum(len(i) for i in images) // 1024

        ocr.VISION_MODEL = model
        ocr.CONCURRENCY = concurrency
        label = f"{model} @ {dpi}dpi x{concurrency}"
        print(f"\n{label}  ({len(images)} pages, {kb} KB)...", flush=True)

        start = time.time()
        try:
            # No cache: a cached page returns instantly and would flatten
            # every difference this script exists to measure.
            pages = ocr.read_pages(images, context=args.context)
            took = time.time() - start
            chars = sum(len(p.text) for p in pages)
            markers = sum(p.marker_count for p in pages)
            sample = " ".join(pages[0].text.split())[:150]
            results.append((label, took, chars, markers, sample, None))
            baseline = baseline or took
            print(f"  {took:.1f}s   {chars} chars   {markers} uncertain")
            print(f"  first page: {sample}")
        except ocr.OCRError as exc:
            results.append((label, None, 0, 0, "", str(exc)[:90]))
            print(f"  FAILED: {str(exc)[:120]}")

    print("\n" + "=" * 78)
    print(f"{'setting':34}{'time':>9}{'vs base':>10}{'chars':>9}{'unsure':>8}")
    print("-" * 78)
    for label, took, chars, markers, _sample, error in results:
        if took is None:
            print(f"{label:34}{'FAILED':>9}   {error}")
            continue
        ratio = f"{baseline / took:.1f}x" if baseline and took else "-"
        print(f"{label:34}{took:>8.1f}s{ratio:>10}{chars:>9}{markers:>8}")

    print("\nTime alone does not decide this. Compare the first-page samples above:")
    print("a setting that halves the time and cannot read the handwriting is not")
    print("faster. Check that the text is right before adopting a faster row.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
