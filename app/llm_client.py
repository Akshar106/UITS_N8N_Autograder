import logging
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

REALLM_API_KEY = os.getenv("REALLM_API_KEY")
REALLM_URL = os.getenv("REALLM_URL")
MODEL_NAME = os.getenv("REALLM_MODEL_NAME")

# Grading must be reproducible. At any temperature above zero the same
# submission can score differently on two runs, and a faculty member who
# re-runs and watches 42 become 45 has every reason to stop trusting the tool.
TEMPERATURE = float(os.getenv("REALLM_TEMPERATURE", "0"))

# Some gateways accept a seed for stronger determinism and others reject unknown
# fields outright, so it is opt-in rather than sent by default.
SEED = os.getenv("REALLM_SEED")

# Transient failures are normal at roster scale: one 429 or one 502 should not
# leave a student permanently marked ungradable when a retry would have worked.
MAX_ATTEMPTS = int(os.getenv("REALLM_MAX_ATTEMPTS", "3"))
BACKOFF_SECONDS = float(os.getenv("REALLM_BACKOFF_SECONDS", "2"))
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

HEADERS = {
    "Authorization": f"Bearer {REALLM_API_KEY}",
    "Content-Type": "application/json",
}


def _sleep_for(attempt):
    time.sleep(BACKOFF_SECONDS * (2 ** (attempt - 1)))


def _extract_message(response):
    data = response.json()
    message = data["choices"][0]["message"]

    content = message.get("content", "")
    reasoning = message.get("reasoning_content", "")
    content = content.strip() if isinstance(content, str) else ""
    reasoning = reasoning.strip() if isinstance(reasoning, str) else ""

    return content or reasoning or None


def call_reallm(systemPrompt: str, prompt: str) -> str | None:
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": systemPrompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": TEMPERATURE,
    }
    if SEED:
        payload["seed"] = int(SEED)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        last = attempt == MAX_ATTEMPTS
        try:
            response = requests.post(REALLM_URL, headers=HEADERS, json=payload, timeout=180)
        except requests.RequestException as exc:
            # Logged, not printed: on Azure a print goes nowhere anyone looks,
            # and a grading run that fails needs a reason in the log stream.
            logger.warning("ReaLLM request failed (attempt %d/%d): %s", attempt, MAX_ATTEMPTS, exc)
            if last:
                return None
            _sleep_for(attempt)
            continue

        if response.status_code == 200:
            try:
                return _extract_message(response)
            except (ValueError, KeyError, IndexError) as exc:
                logger.warning("Could not parse ReaLLM response (attempt %d/%d): %s",
                               attempt, MAX_ATTEMPTS, exc)
                if last:
                    return None
                _sleep_for(attempt)
                continue

        if response.status_code in RETRYABLE_STATUS and not last:
            logger.warning("ReaLLM API error %s (attempt %d/%d), retrying",
                           response.status_code, attempt, MAX_ATTEMPTS)
            _sleep_for(attempt)
            continue

        logger.error("ReaLLM API error %s: %s", response.status_code, response.text[:500])
        return None

    return None
