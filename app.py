"""
Flask application — Linux User Creation Chatbot
Handles chat API, SSH config, NLP parsing, and command execution.
"""

import os
import logging
from flask import Flask, render_template, request, jsonify, session
from flask_cors import CORS
from dotenv import load_dotenv

from core.nlp_parser import parse_user_intent
from core.command_builder import build_command_preview, describe_params
from core.ssh_client import SSHClient

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-in-production-xyz789")
CORS(app)

# ── Helpers ──────────────────────────────────────────────────────────────────

def get_ssh_client() -> SSHClient:
    return SSHClient(
        host=os.getenv("SSH_HOST", ""),
        port=int(os.getenv("SSH_PORT", 22)),
        username=os.getenv("SSH_USER", ""),
        password=os.getenv("SSH_PASSWORD") or None,
        key_path=os.getenv("SSH_KEY_PATH") or None,
    )

def ssh_configured() -> bool:
    return bool(os.getenv("SSH_HOST") and os.getenv("SSH_USER"))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def api_config():
    """Return current SSH config status (no secrets)."""
    return jsonify({
        "ssh_host": os.getenv("SSH_HOST", ""),
        "ssh_port": os.getenv("SSH_PORT", "22"),
        "ssh_user": os.getenv("SSH_USER", ""),
        "auth_type": "key" if os.getenv("SSH_KEY_PATH") else "password",
        "ollama_model": os.getenv("OLLAMA_MODEL", "llama3"),
        "configured": ssh_configured(),
    })


@app.route("/api/config", methods=["POST"])
def api_config_update():
    """Save SSH settings (runtime only — not persisted to .env)."""
    data = request.get_json(force=True)
    updates = {
        "SSH_HOST": data.get("ssh_host", ""),
        "SSH_PORT": str(data.get("ssh_port", 22)),
        "SSH_USER": str(data.get("ssh_user", "")),
        "SSH_PASSWORD": data.get("ssh_password", ""),
        "SSH_KEY_PATH": data.get("ssh_key_path", ""),
        "OLLAMA_MODEL": data.get("ollama_model", "llama3"),
    }
    for k, v in updates.items():
        if v:
            os.environ[k] = v
        elif k in os.environ:
            del os.environ[k]
    return jsonify({"status": "ok", "message": "Configuration updated for this session."})


@app.route("/api/test-ssh", methods=["POST"])
def api_test_ssh():
    """Test SSH connectivity."""
    if not ssh_configured():
        return jsonify({"success": False, "message": "SSH not configured. Set host and username first."})
    client = get_ssh_client()
    ok, msg = client.test_connection()
    return jsonify({"success": ok, "message": msg})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """
    Main chat endpoint.
    Stages:
      1. parse  — NLP extracts params
      2. confirm — user reviews and confirms
      3. execute — run useradd on server
    """
    data = request.get_json(force=True)
    message: str = data.get("message", "").strip()
    stage: str = data.get("stage", "parse")        # parse | confirm | execute
    params: dict = data.get("params", {})
    model: str = os.getenv("OLLAMA_MODEL", "llama3")

    if not message and stage == "parse":
        return jsonify({"error": "Empty message."}), 400

    # ── Stage 1: Parse ────────────────────────────────────────────────────────
    if stage == "parse":
        parsed = parse_user_intent(message, model=model)

        if "parse_error" in parsed:
            return jsonify({
                "stage": "error",
                "message": parsed["parse_error"],
                "raw_response": parsed.get("raw_response", "")
            })

        preview = build_command_preview(parsed)
        if not preview["valid"]:
            return jsonify({
                "stage": "error",
                "message": preview["error"]
            })

        return jsonify({
            "stage": "confirm",
            "message": "I understood your request. Here's what I'll execute:",
            "description": describe_params(parsed),
            "commands": preview["commands"],
            "params": parsed
        })

    # ── Stage 2: Execute ──────────────────────────────────────────────────────
    elif stage == "execute":
        if not params:
            return jsonify({"stage": "error", "message": "No parameters to execute."}), 400

        if not ssh_configured():
            return jsonify({
                "stage": "error",
                "message": "SSH is not configured. Please set up your server connection first."
            })

        preview = build_command_preview(params)
        if not preview["valid"]:
            return jsonify({"stage": "error", "message": preview["error"]})

        results = []
        try:
            with get_ssh_client() as ssh:
                # Run useradd
                code, out, err = ssh.execute(preview["useradd_cmd"])
                if code != 0:
                    # Exit code 9 = user already exists
                    if code == 9:
                        return jsonify({
                            "stage": "error",
                            "message": f"User **{params['username']}** already exists on the server."
                        })
                    return jsonify({
                        "stage": "error",
                        "message": f"useradd failed (exit {code}): {err or out}"
                    })
                results.append(f"✅ useradd: User `{params['username']}` created.")

                # Set password if provided
                if preview["passwd_cmd"]:
                    pcode, pout, perr = ssh.execute(preview["passwd_cmd"])
                    if pcode != 0:
                        results.append(f"⚠️ Password set failed: {perr or pout}")
                    else:
                        results.append("✅ Password set successfully.")

                # Verify user exists
                vcode, vout, _ = ssh.execute(f"id {params['username']}")
                if vcode == 0:
                    results.append(f"✅ Verified: `{vout}`")

        except Exception as e:
            logger.exception("SSH execution error")
            return jsonify({"stage": "error", "message": f"SSH error: {str(e)}"})

        return jsonify({
            "stage": "success",
            "message": f"User **{params['username']}** has been created successfully! 🎉",
            "details": results
        })

    return jsonify({"error": "Unknown stage."}), 400


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
