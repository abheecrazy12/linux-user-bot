"""
Flask application — Linux User Creation Chatbot
Handles chat API, SSH config, NLP parsing, and command execution.
"""

import os
import logging
import tempfile
import base64
from flask import Flask, render_template, request, jsonify, send_file
from flask_cors import CORS
from dotenv import load_dotenv
import io

from core.nlp_parser import parse_user_intent
from core.command_builder import build_command_preview, describe_params
from core.ssh_client import SSHClient
from core.keygen import generate_keypair_on_server, pem_to_ppk

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
        key_content=os.getenv("SSH_KEY_CONTENT") or None,
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
        "auth_type": "key" if (os.getenv("SSH_KEY_PATH") or os.getenv("SSH_KEY_CONTENT")) else "password",
        "key_uploaded": bool(os.getenv("SSH_KEY_CONTENT")),
        "ollama_model": os.getenv("OLLAMA_MODEL", "llama3"),
        "configured": ssh_configured(),
    })


@app.route("/api/config", methods=["POST"])
def api_config_update():
    """Save SSH settings (runtime only — not persisted to .env)."""
    data = request.get_json(force=True)
    updates = {
        "SSH_HOST":     data.get("ssh_host", ""),
        "SSH_PORT":     str(data.get("ssh_port", 22)),
        "SSH_USER":     str(data.get("ssh_user", "")),
        "SSH_PASSWORD": data.get("ssh_password", ""),
        "SSH_KEY_PATH": data.get("ssh_key_path", ""),
        "OLLAMA_MODEL": data.get("ollama_model", "llama3"),
    }
    for k, v in updates.items():
        if v:
            os.environ[k] = v
        elif k in os.environ:
            del os.environ[k]
    # If switching to password auth, clear any uploaded key
    if data.get("auth_type") == "password":
        os.environ.pop("SSH_KEY_CONTENT", None)
    return jsonify({"status": "ok", "message": "Configuration updated for this session."})


@app.route("/api/config/upload-key", methods=["POST"])
def api_upload_key():
    """
    Accept an SSH private key file upload (multipart/form-data).
    Stores the key content in memory (env var) — no disk write.
    Works from phone browsers too since it's a standard file upload.
    """
    if "keyfile" not in request.files:
        return jsonify({"success": False, "message": "No file received."}), 400

    key_file = request.files["keyfile"]
    key_content = key_file.read().decode("utf-8", errors="replace").strip()

    if not key_content:
        return jsonify({"success": False, "message": "Uploaded file is empty."}), 400

    # Basic sanity check — must look like a PEM or OpenSSH key
    valid_headers = (
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "-----BEGIN EC PRIVATE KEY-----",
        "PuTTY-User-Key-File",   # PPK format — paramiko can read PPK v2
    )
    if not any(key_content.startswith(h) for h in valid_headers):
        return jsonify({"success": False, "message": "File doesn't look like a valid SSH private key."}), 400

    # Store in env — will be picked up by get_ssh_client()
    os.environ["SSH_KEY_CONTENT"] = key_content
    os.environ.pop("SSH_KEY_PATH", None)      # clear path-based key
    os.environ.pop("SSH_PASSWORD", None)      # clear password auth

    return jsonify({"success": True, "message": f"Key '{key_file.filename}' loaded successfully."})


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
        ppk_data = None
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

                # Generate SSH keypair for the new user
                if not params.get("system_account"):
                    kp = generate_keypair_on_server(ssh, params["username"])
                    if kp["success"]:
                        results.append("✅ SSH keypair generated and authorized_keys configured.")
                        # Convert private key PEM → PPK and cache in memory (base64)
                        try:
                            ppk_bytes = pem_to_ppk(kp["private_key_pem"])
                            # Store in app config temporarily keyed by username
                            app.config[f"ppk_{params['username']}"] = ppk_bytes
                            ppk_data = params["username"]
                        except Exception as e:
                            logger.warning(f"PPK conversion failed: {e}")
                            results.append(f"⚠️ PPK conversion failed — key not available for download: {e}")
                    else:
                        results.append(f"⚠️ SSH keygen failed: {kp['error']}")

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
            "details": results,
            "ppk_available": ppk_data is not None,
            "ppk_username": ppk_data,
        })

    return jsonify({"error": "Unknown stage."}), 400


@app.route("/api/download-ppk/<username>")
def download_ppk(username: str):
    """
    Serve the cached PPK file for download.
    The key is held in memory only — disappears on server restart.
    """
    import re
    # Validate username to prevent path traversal
    if not re.match(r'^[a-z0-9_\-]{1,32}$', username):
        return jsonify({"error": "Invalid username"}), 400

    ppk_bytes = app.config.get(f"ppk_{username}")
    if not ppk_bytes:
        return jsonify({"error": "PPK not found. It may have expired or the server restarted."}), 404

    # Remove from memory after download (one-time download)
    app.config.pop(f"ppk_{username}", None)

    return send_file(
        io.BytesIO(ppk_bytes),
        as_attachment=True,
        download_name=f"{username}.ppk",
        mimetype="application/octet-stream",
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
