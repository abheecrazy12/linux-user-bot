"""
NLP Parser module.
Uses a local Ollama model to extract structured user-creation
parameters from free-form natural language input.
"""

import json
import re
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/generate"

SYSTEM_PROMPT = """You are a Linux system administrator assistant.
Your ONLY job is to extract user account creation parameters from the user's message
and return them as a strict JSON object. Do NOT add explanations.

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


def query_ollama(model: str, prompt: str) -> str:
    """Send a prompt to the local Ollama model and return the response text."""
    payload = {
        "model": model,
        "prompt": prompt,
        "system": SYSTEM_PROMPT,
        "stream": False,
        "options": {
            "temperature": 0.1,   # Low temperature for deterministic extraction
            "top_p": 0.9,
        }
    }
    response = requests.post(OLLAMA_URL, json=payload, timeout=60)
    response.raise_for_status()
    return response.json().get("response", "")


def extract_json(text: str) -> Optional[dict]:
    """Extract the first JSON object found in a string."""
    # Try to find a JSON block in the response
    match = re.search(r'\{.*?\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    # Fallback: try parsing the whole text
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None


def parse_user_intent(message: str, model: str = "llama3") -> dict:
    """
    Parse a natural language message and extract user creation parameters.

    Returns a dict with extracted params and a 'parse_error' key if parsing failed.
    """
    try:
        raw = query_ollama(model, message)
        logger.debug(f"Ollama raw response: {raw}")
        params = extract_json(raw)

        if not params:
            return {
                "parse_error": "Could not extract structured data from the response.",
                "raw_response": raw
            }

        # Ensure username is present and valid
        if not params.get("username"):
            return {
                "parse_error": "No username found in your message. Please specify a username.",
                "raw_response": raw
            }

        # Sanitize username: lowercase, alphanumeric + underscore + hyphen only
        params["username"] = re.sub(r'[^a-z0-9_\-]', '', params["username"].lower())

        # Defaults
        params.setdefault("create_home", True)
        params.setdefault("system_account", False)
        params.setdefault("shell", "/bin/bash")
        params.setdefault("groups", [])

        return params

    except requests.ConnectionError:
        return {
            "parse_error": "Cannot reach Ollama. Make sure Ollama is running on localhost:11434."
        }
    except Exception as e:
        logger.exception("NLP parsing failed")
        return {"parse_error": str(e)}
