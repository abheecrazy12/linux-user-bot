"""
Flask application — Linux User Creation Chatbot
Multi-server support: SSH config stored per browser session (Flask session),
not in environment variables. Each user/tab connects to their own server.
"""

import os
import io
import re
import logging
from flask import Flask, render_template, request, jsonify, session, send_file
from flask_cors import CORS
from dotenv import load_dotenv

from core.nlp_parser import parse_user_intent
from core.command_builder import build_command_preview, describe_params
from core.ssh_client import SSHClient
from core.keygen import generate_keypair_on_server, pem_to_ppk

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Secret key encrypts the session cookie — set a strong value in env for production
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-in-production-!!!")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
CORS(app, supports_credentials=True)

# In-memory PPK store keyed by "session_id:username"
# Cleared after download. Acceptable for single-instance deployments.
_ppk_store: dict = {}


# ── Session helpers ───────────────────────────────────────────────────────────

def get_session_cfg() -> dict:
    """Return SSH + AI config from the current browser session."""
    return session.get("cfg", {})

def set_session_cfg(data: dict):
    session["cfg"] = data
    session.modified = True

def build_ssh_client_from_session() -> SSHClient:
    cfg = get_session_cfg()
    return SSHClient(
        host=cfg.get("ssh_host", ""),
        port=int(cfg.get("ssh_port", 22)),
        username=cfg.get("ssh_user", ""),
        password=cfg.get("ssh_password") or None,
        key_content=cfg.get("ssh_key_content") or None,
    )

def session_ssh_configured() -> bool:
    cfg = get_session_cfg()
    return bool(cfg.get("ssh_host") and cfg.get("ssh_user"))

def session_id() -> str:
    """Stable per-browser identifier for keying the PPK store."""
    if "_sid" not in session:
        import uuid
        session["_sid"] = uuid.uuid4().hex
    return session["_sid"]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def api_config():
    """Return current session config (no secrets echoed back)."""
    cfg = get_session_cfg()
    return jsonify({
        "ssh_host":        cfg.get("ssh_host", ""),
        "ssh_port":        cfg.get("ssh_port", "22"),
        "ssh_user":        cfg.get("ssh_user", ""),
        "auth_type":       "key" if cfg.get("ssh_key_content") else "password",
        "key_uploaded":    bool(cfg.get("ssh_key_content")),
        "gemini_key_set":  bool(cfg.get("gemini_api_key") or os.getenv("GEMINI_API_KEY")),
        "configured":      session_ssh_configured(),
    })


@app.route("/api/config", methods=["POST"])
def api_config_update():
    """Save SSH + AI settings into the encrypted session cookie."""
    data = request.get_json(force=True)
    cfg  = get_session_cfg()

    cfg["ssh_host"] = data.get("ssh_host", cfg.get("ssh_host", "")).strip()
    cfg["ssh_port"] = str(data.get("ssh_port", cfg.get("ssh_port", 22)))
    cfg["ssh_user"] = data.get("ssh_user", cfg.get("ssh_user", "")).strip()

    # Store Gemini API key in session only if provided (don't overwrite with empty)
    if data.get("gemini_api_key", "").strip():
        cfg["gemini_api_key"] = data["gemini_api_key"].strip()

    if data.get("auth_type") == "password":
        cfg["ssh_password"]    = data.get("ssh_password", "")
        cfg["ssh_key_content"] = None
    elif data.get("auth_type") == "key":
        cfg["ssh_password"] = None

    set_session_cfg(cfg)
    return jsonify({"status": "ok", "message": "Configuration saved for this session."})


@app.route("/api/config/upload-key", methods=["POST"])
def api_upload_key():
    """
    Accept SSH private key upload (multipart/form-data).
    Stores key content in the encrypted session — never on disk.
    Works from any device including phones.
    """
    if "keyfile" not in request.files:
        return jsonify({"success": False, "message": "No file received."}), 400

    key_file    = request.files["keyfile"]
    key_content = key_file.read().decode("utf-8", errors="replace").strip()

    if not key_content:
        return jsonify({"success": False, "message": "Uploaded file is empty."}), 400

    valid_headers = (
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "-----BEGIN EC PRIVATE KEY-----",
        "PuTTY-User-Key-File",
    )
    if not any(key_content.startswith(h) for h in valid_headers):
        return jsonify({"success": False, "message": "File doesn't look like a valid SSH private key."}), 400

    cfg = get_session_cfg()
    cfg["ssh_key_content"] = key_content
    cfg["ssh_password"]    = None
    set_session_cfg(cfg)

    return jsonify({"success": True, "message": f"Key '{key_file.filename}' loaded for this session."})


@app.route("/api/config/clear", methods=["POST"])
def api_config_clear():
    """Clear all session config — disconnect from current server."""
    session.pop("cfg", None)
    return jsonify({"status": "ok", "message": "Session cleared."})


@app.route("/api/test-ssh", methods=["POST"])
def api_test_ssh():
    """Test SSH connectivity using current session config."""
    if not session_ssh_configured():
        return jsonify({"success": False, "message": "SSH not configured. Enter host and username first."})
    client = build_ssh_client_from_session()
    ok, msg = client.test_connection()
    return jsonify({"success": ok, "message": msg})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """
    Main chat endpoint. Three stages:
      parse   — NLP extracts user-creation params from natural language
      confirm — frontend shows preview, waits for user confirmation
      execute — runs useradd + keygen over SSH on the session's server
    """
    data    = request.get_json(force=True)
    message = data.get("message", "").strip()
    stage   = data.get("stage", "parse")
    params  = data.get("params", {})
    cfg     = get_session_cfg()

    api_key = cfg.get("gemini_api_key", os.getenv("GEMINI_API_KEY", ""))

    if not message and stage == "parse":
        return jsonify({"error": "Empty message."}), 400

    # ── Stage 1: Parse ────────────────────────────────────────────────────────
    if stage == "parse":
        parsed = parse_user_intent(message, api_key=api_key)

        if "parse_error" in parsed:
            return jsonify({
                "stage": "error",
                "message": parsed["parse_error"],
                "raw_response": parsed.get("raw_response", "")
            })

        preview = build_command_preview(parsed)
        if not preview["valid"]:
            return jsonify({"stage": "error", "message": preview["error"]})

        return jsonify({
            "stage":       "confirm",
            "message":     "I understood your request. Here's what I'll execute:",
            "description": describe_params(parsed),
            "commands":    preview["commands"],
            "server":      cfg.get("ssh_host", "unknown"),
            "params":      parsed,
        })

    # ── Stage 2: Execute ──────────────────────────────────────────────────────
    elif stage == "execute":
        if not params:
            return jsonify({"stage": "error", "message": "No parameters to execute."}), 400

        if not session_ssh_configured():
            return jsonify({
                "stage": "error",
                "message": "SSH is not configured. Go to SSH Config and enter your server details."
            })

        preview = build_command_preview(params)
        if not preview["valid"]:
            return jsonify({"stage": "error", "message": preview["error"]})

        results  = []
        ppk_data = None
        sid      = session_id()

        try:
            with build_ssh_client_from_session() as ssh:

                # useradd
                code, out, err = ssh.execute(preview["useradd_cmd"])
                if code != 0:
                    if code == 9:
                        return jsonify({
                            "stage": "error",
                            "message": f"User **{params['username']}** already exists on the server."
                        })
                    return jsonify({
                        "stage": "error",
                        "message": f"useradd failed (exit {code}): {err or out}"
                    })
                results.append(f"✅ User `{params['username']}` created on **{cfg.get('ssh_host')}**.")

                # Set password
                if preview["passwd_cmd"]:
                    pcode, _, perr = ssh.execute(preview["passwd_cmd"])
                    if pcode != 0:
                        results.append(f"⚠️ Password set failed: {perr}")
                    else:
                        results.append("✅ Password set successfully.")

                # Generate SSH keypair for non-system accounts
                if not params.get("system_account"):
                    kp = generate_keypair_on_server(ssh, params["username"])
                    if kp["success"]:
                        results.append("✅ SSH keypair generated. Public key installed in authorized_keys.")
                        try:
                            ppk_bytes = pem_to_ppk(kp["private_key_pem"])
                            store_key = f"{sid}:{params['username']}"
                            _ppk_store[store_key] = ppk_bytes
                            ppk_data = params["username"]
                        except Exception as e:
                            logger.warning(f"PPK conversion failed: {e}")
                            results.append(f"⚠️ PPK conversion failed: {e}")
                    else:
                        results.append(f"⚠️ SSH keygen failed: {kp['error']}")

                # Verify
                vcode, vout, _ = ssh.execute(f"id {params['username']}")
                if vcode == 0:
                    results.append(f"✅ Verified: `{vout}`")

        except Exception as e:
            logger.exception("SSH execution error")
            return jsonify({"stage": "error", "message": f"SSH error: {str(e)}"})

        return jsonify({
            "stage":        "success",
            "message":      f"User **{params['username']}** created on **{cfg.get('ssh_host')}**! 🎉",
            "details":      results,
            "ppk_available": ppk_data is not None,
            "ppk_username":  ppk_data,
        })

    return jsonify({"error": "Unknown stage."}), 400


@app.route("/api/download-ppk/<username>")
def download_ppk(username: str):
    """One-time PPK download — removed from store after serving."""
    if not re.match(r'^[a-z0-9_\-]{1,32}$', username):
        return jsonify({"error": "Invalid username"}), 400

    store_key = f"{session_id()}:{username}"
    ppk_bytes = _ppk_store.get(store_key)
    if not ppk_bytes:
        return jsonify({"error": "PPK not found or already downloaded."}), 404

    _ppk_store.pop(store_key, None)
    return send_file(
        io.BytesIO(ppk_bytes),
        as_attachment=True,
        download_name=f"{username}.ppk",
        mimetype="application/octet-stream",
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
