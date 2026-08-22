"""
NLP Parser module.
Sends the user's natural language message to a local Ollama instance
(or any OpenAI-compatible endpoint) and extracts structured Linux
user-creation parameters from the JSON response.

The Ollama base URL is passed in at call time so each browser session
can point to a different Ollama instance.
"""

import json
import re
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"

SYSTEM_PROMPT = """You are a Linux system administrator assistant.
Your ONLY job is to extract user account creation parameters from the user's message
and return them as a strict JSON object. Do NOT add explanations or any extra text.

Extract the following fields (use null if not mentioned):
- username       : string  (Linux username, lowercase, no spaces)
- full_name      : string  (GECOS full name)
- password       : string  (plain text password — will be hashed before use)
- groups         : list    (additional groups e.g. ["sudo","docker"])
- shell          : string  (login shell e.g. /bin/bash, /bin/zsh, /bin/sh)
- home_dir       : string  (custom home directory path)
- create_home    : boolean (true = create home dir, default true)
- system_account : boolean (true if this is a system/service account)
- expiry_date    : string  (account expiry in YYYY-MM-DD format or null)
- comment        : string  (any extra comment)

Return ONLY valid JSON like:
{
  "username": "alice",
  "full_name": "Alice Smith",
  "password": "secret123",
  "groups": ["sudo"],
  "shell": "/bin/bash",
  "home_dir": null,
  "create_home": true,
  "system_account": false,
  "expiry_date": null,
  "comment": null
}
"""


def _build_ollama_url(base_url: str) -> str:
    """Normalize the base URL and append the generate path."""
    base = base_url.rstrip("/")
    # Support both bare base URL and full path
    if base.endswith("/api/generate"):
        return base
    return f"{base}/api/generate"


def query_ollama(model: str, prompt: str, ollama_url: str = DEFAULT_OLLAMA_URL) -> str:
    """Send a prompt to the Ollama instance and return the response text."""
    url = _build_ollama_url(ollama_url)
    payload = {
        "model":  model,
        "prompt": prompt,
        "system": SYSTEM_PROMPT,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "top_p": 0.9,
        }
    }
    response = requests.post(url, json=payload, timeout=90)
    response.raise_for_status()
    return response.json().get("response", "")


def extract_json(text: str) -> Optional[dict]:
    """Extract the first JSON object found in a string."""
    match = re.search(r'\{.*?\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None


def parse_user_intent(message: str,
                      model: str = "llama3",
                      ollama_url: str = DEFAULT_OLLAMA_URL) -> dict:
    """
    Parse natural language and extract Linux user creation parameters.

    Args:
        message:    The user's chat message.
        model:      Ollama model name (e.g. llama3, mistral).
        ollama_url: Base URL of the Ollama instance for this session.

    Returns a dict with extracted params, or {'parse_error': '...'} on failure.
    """
    try:
        raw    = query_ollama(model, message, ollama_url)
        logger.debug(f"Ollama raw response: {raw}")
        params = extract_json(raw)

        if not params:
            return {
                "parse_error":    "Could not extract structured data from the AI response.",
                "raw_response":   raw
            }

        if not params.get("username"):
            return {
                "parse_error":  "No username found in your message. Please specify a username.",
                "raw_response": raw
            }

        # Sanitize username
        params["username"] = re.sub(r'[^a-z0-9_\-]', '', params["username"].lower())
        if not params["username"]:
            return {"parse_error": "Username became empty after sanitization. Use lowercase letters only."}

        # Defaults
        params.setdefault("create_home",    True)
        params.setdefault("system_account", False)
        params.setdefault("shell",          "/bin/bash")
        params.setdefault("groups",         [])

        return params

    except requests.ConnectionError:
        base = ollama_url or DEFAULT_OLLAMA_URL
        return {
            "parse_error": (
                f"Cannot reach Ollama at **{base}**. "
                "Make sure Ollama is running and the URL in SSH Config is correct."
            )
        }
    except requests.HTTPError as e:
        return {"parse_error": f"Ollama returned an error: {e}"}
    except Exception as e:
        logger.exception("NLP parsing failed")
        return {"parse_error": str(e)}
