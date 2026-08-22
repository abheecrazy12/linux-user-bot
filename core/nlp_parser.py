"""
NLP Parser — Google Gemini API (free tier)
Extracts Linux user-creation parameters from natural language.
Falls back to rule-based parsing if Gemini is unavailable.
"""

import re
import json
import logging
import requests

logger = logging.getLogger(__name__)

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent"
)

SYSTEM_PROMPT = """You are a Linux system administrator assistant.
Extract Linux user account creation parameters from the user message and return ONLY a valid JSON object. No explanation, no markdown, just raw JSON.

Fields to extract (use null if not mentioned):
- username       : string  (Linux username, lowercase letters/digits/underscore/hyphen only)
- full_name      : string  (full display name / GECOS)
- password       : string  (plain text password)
- groups         : array   (e.g. ["sudo","docker"])
- shell          : string  (e.g. /bin/bash, /bin/zsh)
- home_dir       : string  (custom home path or null)
- create_home    : boolean (default true)
- system_account : boolean (true for service/daemon accounts)
- expiry_date    : string  (YYYY-MM-DD or null)
- comment        : string  (any extra notes or null)

Example output:
{"username":"alice","full_name":"Alice Smith","password":"secret123","groups":["sudo"],"shell":"/bin/bash","home_dir":null,"create_home":true,"system_account":false,"expiry_date":null,"comment":null}"""


def _query_gemini(message: str, api_key: str) -> str:
    """Call Gemini API and return the raw text response."""
    url  = f"{GEMINI_URL}?key={api_key}"
    body = {
        "contents": [
            {
                "parts": [
                    {"text": SYSTEM_PROMPT + "\n\nUser message: " + message}
                ]
            }
        ],
        "generationConfig": {
            "temperature":    0.1,
            "maxOutputTokens": 512,
        }
    }
    resp = requests.post(url, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a string."""
    # Strip markdown code fences if present
    text = re.sub(r"```(?:json)?", "", text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _rule_based_fallback(message: str) -> dict:
    """
    Simple regex fallback when Gemini is unavailable.
    Handles the most common patterns.
    """
    text = message.lower()

    params = {
        "username": None, "full_name": None, "password": None,
        "groups": [], "shell": "/bin/bash", "home_dir": None,
        "create_home": True, "system_account": False,
        "expiry_date": None, "comment": None,
    }

    SKIP = {"user","a","an","the","new","system","service","create","add",
            "make","with","for","and","or","account","linux","sudo","admin","me"}

    for pat in [
        r"user(?:name)?\s+['\"]?([a-z][a-z0-9_\-]{1,30})['\"]?",
        r"called\s+['\"]?([a-z][a-z0-9_\-]{1,30})['\"]?",
        r"named\s+['\"]?([a-z][a-z0-9_\-]{1,30})['\"]?",
        r"add\s+([a-z][a-z0-9_\-]{1,30})\b",
        r"create\s+([a-z][a-z0-9_\-]{1,30})\b",
    ]:
        m = re.search(pat, text)
        if m and m.group(1) not in SKIP:
            params["username"] = m.group(1)
            break

    pw = re.search(r"pass(?:word)?\s+['\"]?(\S+)['\"]?", message, re.IGNORECASE)
    if pw:
        params["password"] = pw.group(1)

    sh = re.search(r"shell\s+['\"]?(\S+)['\"]?", text)
    if sh:
        raw = sh.group(1).rstrip(",'\"")
        shells = {"bash":"/bin/bash","zsh":"/bin/zsh","sh":"/bin/sh","fish":"/bin/fish"}
        params["shell"] = shells.get(raw, raw if raw.startswith("/") else "/bin/bash")

    for kw, grp in [("sudo","sudo"),("admin","sudo"),("docker","docker"),("www-data","www-data"),("wheel","wheel")]:
        if kw in text:
            params["groups"].append(grp)

    if any(w in text for w in ["system","service","daemon","nologin"]):
        params["system_account"] = True
        params["create_home"]    = False
        params["shell"]          = "/sbin/nologin"

    exp = re.search(r"expir\w*\s+(\d{4}-\d{2}-\d{2})", text)
    if exp:
        params["expiry_date"] = exp.group(1)

    return params


def parse_user_intent(message: str,
                      api_key: str = "",
                      **kwargs) -> dict:
    """
    Parse natural language and return Linux user-creation parameters.
    Uses Gemini API when api_key is provided, falls back to rule-based parser.
    """
    # Try Gemini first
    if api_key and api_key.strip():
        try:
            raw    = _query_gemini(message, api_key.strip())
            logger.debug(f"Gemini raw: {raw}")
            params = _extract_json(raw)

            if not params:
                logger.warning("Gemini returned non-JSON, falling back to rules")
                params = _rule_based_fallback(message)
            else:
                # Sanitize username
                if params.get("username"):
                    params["username"] = re.sub(r"[^a-z0-9_\-]", "",
                                                params["username"].lower())

            if not params.get("username"):
                return {"parse_error": "No username found. Try: **create user john** or **add alice with sudo**"}

            # Apply defaults
            params.setdefault("create_home",    True)
            params.setdefault("system_account", False)
            params.setdefault("shell",          "/bin/bash")
            params.setdefault("groups",         [])
            return params

        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                return {"parse_error": "Invalid Gemini API key. Check your key in SSH Config."}
            logger.warning(f"Gemini HTTP error: {e}, falling back to rules")
        except requests.ConnectionError:
            logger.warning("Gemini unreachable, falling back to rules")
        except Exception as e:
            logger.warning(f"Gemini error: {e}, falling back to rules")

    # Rule-based fallback (no API key or Gemini failed)
    logger.info("Using rule-based parser")
    params = _rule_based_fallback(message)

    if not params.get("username"):
        return {"parse_error": "No username found. Try: **create user john** or **add alice with sudo**"}

    params.setdefault("create_home",    True)
    params.setdefault("system_account", False)
    params.setdefault("shell",          "/bin/bash")
    params.setdefault("groups",         [])
    return params
