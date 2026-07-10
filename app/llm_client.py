import os

import requests
from dotenv import load_dotenv

load_dotenv()

REALLM_API_KEY = os.getenv("REALLM_API_KEY")
REALLM_URL = os.getenv("REALLM_URL")
MODEL_NAME = os.getenv("REALLM_MODEL_NAME")

HEADERS = {
    "Authorization": f"Bearer {REALLM_API_KEY}",
    "Content-Type": "application/json",
}


def call_reallm(systemPrompt: str, prompt: str) -> str | None:
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": systemPrompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }

    try:
        response = requests.post(REALLM_URL, headers=HEADERS, json=payload, timeout=180)
    except requests.RequestException as e:
        print(f"ReaLLM request failed: {e}")
        return None

    if response.status_code != 200:
        print(f"ReaLLM API error {response.status_code}: {response.text}")
        return None

    try:
        data = response.json()
        message = data["choices"][0]["message"]

        content = message.get("content", "")
        reasoning = message.get("reasoning_content", "")

        content = content.strip() if isinstance(content, str) else ""
        reasoning = reasoning.strip() if isinstance(reasoning, str) else ""

        if content:
            return content

        if reasoning:
            return reasoning

        return None

    except Exception as e:
        print("Failed to parse ReaLLM response:", e)
        return None
