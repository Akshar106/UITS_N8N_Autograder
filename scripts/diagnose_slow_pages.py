#!/usr/bin/env python3
"""Find out WHY a page is slow, rather than guessing. Needs an LLM key.

    python3 scripts/diagnose_slow_pages.py "Assignment 1/AppliedAlgo_A1.pdf"
    python3 scripts/diagnose_slow_pages.py scan.pdf --pages 11 --serial

On a real 11-page handwritten submission, nine pages answered in 8-11s and two
ran past the 240s timeout. Those two were 83% of the time it took to read the
whole submission, and no amount of concurrency touches them, because a single
page cannot be split across workers.

Two explanations fit that, and they call for opposite fixes:

  the model did not stop    A greedy decoder on an ambiguous image repeats a
                            phrase until something stops it. With no token
                            limit, the only thing that did was the timeout.
                            Fix: cap the output. Costs nothing.

  the gateway was busy      The request sat in a queue behind other traffic.
                            Fix: more concurrency, or a different time of day.
                            Capping the output would do nothing at all.

They are easy to tell apart once you look at the RIGHT number, which is not
the time. It is how much the model generated. A page needs ~450 tokens. If a
240s page generated 7,000, it looped. If it generated 450, the gateway was
slow. This script reports both, per page, and says which it looks like.

It deliberately bypasses the page cache and ignores OCR_MAX_OUTPUT_TOKENS,
measuring what the model does when nothing stops it -- that is the behaviour
in question. Use --capped to measure with the cap in place instead, which is
how you confirm the fix worked.
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OCR_BACKEND", "vision")

import requests  # noqa: E402

from app import extract, ocr  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# What a page of handwriting actually comes to, for judging the rest against.
TYPICAL_PAGE_TOKENS = 450


def render(path):
    data = path.read_bytes()
    if path.suffix.lower() != ".pdf":
        return [data]
    return extract._render_pdf_pages(data)


def read_one(image, index, cap):
    """One page, reporting what the gateway did rather than just the text."""
    payload = {
        "model": ocr.VISION_MODEL,
        "messages": [
            {"role": "system", "content": ocr.TRANSCRIPTION_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": f"Transcribe page {index}."},
                {"type": "image_url", "image_url": {"url": ocr._data_uri(image)}},
            ]},
        ],
        "temperature": 0,
    }
    if cap:
        payload["max_tokens"] = cap

    started = time.time()
    try:
        response = requests.post(
            os.environ["REALLM_URL"],
            headers={"Authorization": f"Bearer {os.environ['REALLM_API_KEY']}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=ocr.REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        return {"page": index, "seconds": time.time() - started, "error": type(exc).__name__}
    took = time.time() - started

    if response.status_code != 200:
        return {"page": index, "seconds": took, "error": f"HTTP {response.status_code}"}

    body = response.json()
    choice = body["choices"][0]
    text = (choice["message"].get("content") or "").strip()
    usage = body.get("usage") or {}
    # Prefer the server's own count; fall back to the usual 4-chars-a-token.
    generated = usage.get("completion_tokens") or round(len(text) / 4)
    return {
        "page": index,
        "seconds": took,
        "tokens": generated,
        "chars": len(text),
        "finish": choice.get("finish_reason"),
        "looping": ocr.looks_degenerate(text),
        "rate": generated / took if took else 0,
        "text": text,
    }


def verdict(row):
    """Which of the two explanations this page's numbers support."""
    if row.get("error"):
        return "FAILED"
    if row["looping"] or row["finish"] == "length":
        return "MODEL LOOPED"
    if row["tokens"] > TYPICAL_PAGE_TOKENS * 3:
        return "over-generating"
    if row["seconds"] > 60 and row["tokens"] <= TYPICAL_PAGE_TOKENS * 2:
        return "GATEWAY SLOW"
    return "normal"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("submission")
    parser.add_argument("--pages", type=int, default=0, help="only the first N pages")
    parser.add_argument("--capped", action="store_true",
                        help=f"send max_tokens={ocr.MAX_OUTPUT_TOKENS}, to confirm the fix")
    parser.add_argument("--serial", action="store_true",
                        help="one at a time, so our own concurrency cannot be the cause")
    args = parser.parse_args()

    path = Path(args.submission)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        return 1
    if not os.getenv("REALLM_URL") or not os.getenv("REALLM_API_KEY"):
        print("Set REALLM_URL and REALLM_API_KEY first.", file=sys.stderr)
        return 1

    images = render(path)
    if args.pages:
        images = images[:args.pages]
    cap = ocr.MAX_OUTPUT_TOKENS if args.capped else None

    print(f"{path.name}: {len(images)} page(s), model {ocr.VISION_MODEL}")
    print(f"max_tokens: {cap if cap else 'UNSET (the server decides)'}"
          f"   requests: {'one at a time' if args.serial else f'{ocr.CONCURRENCY} at a time'}\n")
    print(f"{'page':>5}{'secs':>8}{'tokens':>8}{'tok/s':>7}{'finish':>10}  verdict")
    print("-" * 62)

    rows = []
    if args.serial:
        for i, image in enumerate(images, start=1):
            rows.append(read_one(image, i, cap))
            r = rows[-1]
            print(f"{r['page']:>5}{r['seconds']:>7.0f}s{r.get('tokens', 0):>8}"
                  f"{r.get('rate', 0):>7.0f}{str(r.get('finish') or r.get('error')):>10}"
                  f"  {verdict(r)}", flush=True)
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=ocr.CONCURRENCY) as pool:
            rows = list(pool.map(lambda p: read_one(p[1], p[0], cap),
                                 list(enumerate(images, start=1))))
        for r in rows:
            print(f"{r['page']:>5}{r['seconds']:>7.0f}s{r.get('tokens', 0):>8}"
                  f"{r.get('rate', 0):>7.0f}{str(r.get('finish') or r.get('error')):>10}"
                  f"  {verdict(r)}")

    good = [r for r in rows if not r.get("error")]
    slow = [r for r in good if r["seconds"] > 60]
    print("\n" + "=" * 62)
    if not good:
        print("Every page failed. Fix the connection before reading anything into this.")
        return 1

    median = sorted(r["tokens"] for r in good)[len(good) // 2]
    print(f"typical page: {median} tokens   slow pages (>60s): {len(slow)} of {len(good)}")

    if not slow:
        print("\nNothing was slow in this run. Either the fix is working, or the")
        print("gateway is simply having a better day -- run it again under load")
        print("before concluding anything.")
        return 0

    looped = [r for r in slow if verdict(r) in ("MODEL LOOPED", "over-generating")]
    queued = [r for r in slow if verdict(r) == "GATEWAY SLOW"]
    print(f"  of those, {len(looped)} over-generated and {len(queued)} were slow"
          f" while generating a normal amount")

    print()
    if looped and not queued:
        print("VERDICT: the model is not stopping. Capping the output fixes this,")
        print(f"         and costs nothing. Re-run with --capped to confirm.")
    elif queued and not looped:
        print("VERDICT: the gateway is the bottleneck, not the model. Capping the")
        print("         output will NOT help. More concurrency will, up to whatever")
        print("         the gateway allows -- which is worth asking IU directly.")
    else:
        print("VERDICT: both are happening. Cap the output first because it is free,")
        print("         then re-measure before changing any concurrency setting.")

    worst = max(slow, key=lambda r: r["seconds"])
    print(f"\nThe slowest page ({worst['page']}, {worst['seconds']:.0f}s, "
          f"{worst['tokens']} tokens) ended with:")
    print("   ..." + " ".join(worst["text"][-220:].split()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
