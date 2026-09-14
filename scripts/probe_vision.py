#!/usr/bin/env python3
"""Ask the configured ReaLLM model whether it can actually see an image.

    python3 scripts/probe_vision.py                 # test the configured model
    python3 scripts/probe_vision.py --model NAME    # test a different model
    python3 scripts/probe_vision.py --list-models   # what does the gateway offer?

Reads REALLM_URL / REALLM_API_KEY / REALLM_MODEL_NAME from .env, exactly as the
app does. Nothing from any student is sent -- the only image is one this script
draws.

WHY A GENERATED IMAGE WITH A CODE IN IT
"Can you see this image?" is a question a model will happily answer "yes" to
without seeing anything. So the image contains a randomly generated code, and
the model is asked to read it back. Only an exact match proves the pixels
actually reached a model that looked at them. Anything else -- a refusal, a
plausible-sounding description, a different code -- is a no.

Two request shapes are tried, because gateways differ:
  1. OpenAI style   content: [{type: image_url, image_url: {url: data:...}}]
  2. Anthropic style content: [{type: image, source: {type: base64, ...}}]

The exit code is 0 if some combination worked, 1 if none did.
"""
import argparse
import base64
import io
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

URL = os.getenv("REALLM_URL")
API_KEY = os.getenv("REALLM_API_KEY")
MODEL = os.getenv("REALLM_MODEL_NAME")


def build_test_image(code):
    """A plain white PNG with the code drawn large and a simple shape."""
    from PIL import Image, ImageDraw  # ships with pdfplumber

    image = Image.new("RGB", (520, 260), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([10, 10, 509, 249], outline="black", width=3)
    # Drawn big so no OCR subtlety is involved: if the model sees at all, it
    # sees this.
    draw.text((40, 60), f"CODE: {code}", fill="black")
    draw.text((40, 110), "SHAPE: TRIANGLE", fill="black")
    draw.polygon([(300, 210), (360, 120), (420, 210)], outline="black", width=3)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def openai_style(prompt, image_b64):
    return [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
    ]}]


def anthropic_style(prompt, image_b64):
    return [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": image_b64}},
    ]}]


def send(messages, model):
    payload = {"model": model, "messages": messages, "temperature": 0}
    try:
        response = requests.post(
            URL,
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
    except requests.RequestException as exc:
        return None, f"request failed: {type(exc).__name__}: {exc}"

    if response.status_code != 200:
        return None, f"HTTP {response.status_code}: {response.text[:400]}"

    try:
        message = response.json()["choices"][0]["message"]
    except (ValueError, KeyError, IndexError) as exc:
        return None, f"unexpected response shape ({exc}): {response.text[:400]}"

    content = message.get("content") or message.get("reasoning_content") or ""
    if isinstance(content, list):  # some gateways return parts
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return (content or "").strip(), None


def list_models():
    """Best-effort: most OpenAI-compatible gateways expose /models."""
    base = URL.rsplit("/chat/completions", 1)[0].rsplit("/completions", 1)[0].rstrip("/")
    for candidate in (f"{base}/models", f"{base}/v1/models"):
        try:
            response = requests.get(
                candidate, headers={"Authorization": f"Bearer {API_KEY}"}, timeout=30
            )
        except requests.RequestException:
            continue
        if response.status_code != 200:
            continue
        try:
            data = response.json()
        except ValueError:
            continue
        entries = data.get("data") if isinstance(data, dict) else data
        names = sorted(
            str(e.get("id") or e.get("name")) for e in entries or [] if isinstance(e, dict)
        )
        if names:
            print(f"\nModels reported by {candidate}:")
            for name in names:
                marker = "  <- configured" if name == MODEL else ""
                print(f"  {name}{marker}")
            print("\nNames are a poor guide: plenty of general-purpose models take images "
                  "without saying so, while some that sound multimodal do not.")
            print("Rule these out without testing: *embedding* and *reranker* (no text "
                  "output at all), *asr* and *whisper* (speech, not images), and anything "
                  "that GENERATES images rather than reading them.")
            print("Test the rest with --model <name>; a code read back out of the picture "
                  "is the only proof that counts.")
            return True
    print("\nThe gateway did not answer a /models listing; ask the ReaLLM team which "
          "models are available.")
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=MODEL, help="model to test (default: REALLM_MODEL_NAME)")
    parser.add_argument("--list-models", action="store_true")
    args = parser.parse_args()

    missing = [n for n, v in (("REALLM_URL", URL), ("REALLM_API_KEY", API_KEY)) if not v]
    if missing:
        print(f"Missing from .env: {', '.join(missing)}")
        return 1

    print(f"Gateway : {URL}")
    print(f"Model   : {args.model}")

    if args.list_models:
        list_models()
        return 0

    code = f"{random.randint(1000, 9999)}-{random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}"
    image_b64 = base64.b64encode(build_test_image(code)).decode()
    print(f"Test image: 520x260 PNG, {len(image_b64) // 1024} KB base64, "
          f"containing the code {code}")

    prompt = ("Read the image. Reply with ONLY the code printed in it and the shape "
              "drawn in it, as JSON: {\"code\": \"...\", \"shape\": \"...\"}. "
              "If you cannot see an image, reply exactly: NO_IMAGE")

    # A text-only control first: proves the key, URL and model work at all, so a
    # failure below is about images specifically and not about the connection.
    control, error = send([{"role": "user", "content": "Reply with the single word OK."}],
                          args.model)
    if error:
        print(f"\nFAIL  the model is not reachable at all: {error}")
        print("      Fix this before drawing any conclusion about images.")
        return 1
    print(f"\nText-only control: {control[:60]!r}  -- connection and model are fine")

    for label, builder in (("OpenAI style (image_url)", openai_style),
                           ("Anthropic style (source.base64)", anthropic_style)):
        print(f"\n--- {label} ---")
        reply, error = send(builder(prompt, image_b64), args.model)
        if error:
            print(f"REJECTED  {error}")
            continue

        print(f"Reply: {reply[:300]!r}")
        if code in reply:
            print(f"\nVISION WORKS. The model read {code} out of the image, so it genuinely "
                  f"saw the pixels.")
            print(f"Use this request shape: {label}.")
            return 0
        if "NO_IMAGE" in reply.upper():
            print("The model answered explicitly that it received no image.")
        else:
            print(f"The model replied without the code {code} -- it did not read the image "
                  f"(this is what a text-only model does when handed image parts it ignores).")

    print("\nNO VISION with this model.")
    print("Next step: run with --list-models to see what else the gateway offers, then "
          "retry with --model <name>. If nothing there is multimodal, screenshots cannot "
          "be graded without either a different model or a local OCR dependency.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
