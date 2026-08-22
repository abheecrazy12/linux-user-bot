"""
SSH Keypair generation and PPK v2 conversion.
Generates RSA keypair on the remote server, installs authorized_keys,
converts private key to PuTTY PPK v2 format for download.
"""

import io
import base64
import hashlib
import hmac
import struct
import logging
from paramiko import RSAKey

logger = logging.getLogger(__name__)


def generate_keypair_on_server(ssh_client, username: str) -> dict:
    """
    Generate a 4096-bit RSA keypair for `username` on the remote server.
    Installs public key into ~/.ssh/authorized_keys.
    Deletes private key from server after reading — user downloads the PPK.

    Returns:
        {"success": True,  "private_key_pem": str, "public_key": str}
        {"success": False, "error": str}
    """
    ssh_dir = f"/home/{username}/.ssh"

    steps = [
        f"sudo mkdir -p {ssh_dir}",
        f"sudo chown {username}:{username} {ssh_dir}",
        f"sudo chmod 700 {ssh_dir}",
        f"sudo ssh-keygen -t rsa -b 4096 -N '' -f {ssh_dir}/id_rsa -C '{username}@server'",
        f"sudo cp {ssh_dir}/id_rsa.pub {ssh_dir}/authorized_keys",
        f"sudo chown {username}:{username} {ssh_dir}/authorized_keys {ssh_dir}/id_rsa {ssh_dir}/id_rsa.pub",
        f"sudo chmod 600 {ssh_dir}/authorized_keys {ssh_dir}/id_rsa",
        f"sudo chmod 644 {ssh_dir}/id_rsa.pub",
    ]

    for cmd in steps:
        code, out, err = ssh_client.execute(cmd)
        if code != 0:
            return {"success": False, "error": f"Failed: {cmd}\n{err or out}"}

    # Read private key
    code, pem, err = ssh_client.execute(f"sudo cat {ssh_dir}/id_rsa")
    if code != 0 or not pem.strip():
        return {"success": False, "error": f"Could not read private key: {err}"}

    # Read public key
    code, pub, _ = ssh_client.execute(f"sudo cat {ssh_dir}/id_rsa.pub")

    # Delete private key from server — only PPK goes to the user
    ssh_client.execute(f"sudo rm -f {ssh_dir}/id_rsa")

    logger.info(f"SSH keypair generated for {username}")
    return {
        "success":         True,
        "private_key_pem": pem.strip(),
        "public_key":      pub.strip(),
    }


# ── PPK v2 helpers ────────────────────────────────────────────────────────────

def _mpint(n: int) -> bytes:
    """Encode a Python int as an SSH mpint (big-endian, length-prefixed)."""
    if n == 0:
        return struct.pack('>I', 0)
    byte_len = (n.bit_length() + 7) // 8
    raw = n.to_bytes(byte_len, 'big')
    if raw[0] & 0x80:          # needs sign byte
        raw = b'\x00' + raw
    return struct.pack('>I', len(raw)) + raw


def _ssh_string(s: bytes) -> bytes:
    """Encode bytes as an SSH string (uint32 length + data)."""
    return struct.pack('>I', len(s)) + s


def _wrap64(s: str) -> str:
    """Wrap a base64 string at 64 characters per line (PuTTY convention)."""
    return '\n'.join(s[i:i + 64] for i in range(0, len(s), 64))


def pem_to_ppk(pem_text: str) -> bytes:
    """
    Convert an OpenSSH PEM private key to PuTTY PPK v2 format.
    The result can be loaded directly into PuTTY, WinSCP, or FileZilla.
    """
    # Load key via paramiko
    rsa_key = RSAKey.from_private_key(io.StringIO(pem_text))

    # Extract key numbers
    pub_nums  = rsa_key.public_numbers                   # n, e
    priv_nums = rsa_key.key.private_numbers()            # d, p, q, dmp1, dmq1, iqmp

    n    = pub_nums.n
    e    = pub_nums.e
    d    = priv_nums.d
    p    = priv_nums.p
    q    = priv_nums.q
    # PuTTY wants: inverse(q, p)  — OpenSSH stores inverse(p, q)
    putty_iqmp = pow(q, -1, p)

    # ── Public blob: ssh-rsa wire format ──────────────────────────
    pub_blob = (
        _ssh_string(b"ssh-rsa") +
        _mpint(e) +
        _mpint(n)
    )

    # ── Private blob: d, p, q, iqmp ──────────────────────────────
    priv_blob = (
        _mpint(d) +
        _mpint(p) +
        _mpint(q) +
        _mpint(putty_iqmp)
    )

    # ── PPK v2 MAC ────────────────────────────────────────────────
    # HMAC-SHA1 over: algo + encryption + comment + pub_blob + priv_blob
    algo    = b"ssh-rsa"
    enc     = b"none"
    comment = b"imported-key"

    mac_data = (
        _ssh_string(algo) +
        _ssh_string(enc) +
        _ssh_string(comment) +
        _ssh_string(pub_blob) +
        _ssh_string(priv_blob)
    )

    mac_key = hashlib.sha1(b"putty-private-key-file-mac-key").digest()
    mac_hex = hmac.new(mac_key, mac_data, hashlib.sha1).hexdigest()

    # ── Assemble PPK file text ─────────────────────────────────────
    pub_b64   = base64.b64encode(pub_blob).decode()
    priv_b64  = base64.b64encode(priv_blob).decode()
    pub_lines = _wrap64(pub_b64).splitlines()
    priv_lines= _wrap64(priv_b64).splitlines()

    ppk = "\r\n".join([
        "PuTTY-User-Key-File-2: ssh-rsa",
        "Encryption: none",
        f"Comment: {comment.decode()}",
        f"Public-Lines: {len(pub_lines)}",
        *pub_lines,
        f"Private-Lines: {len(priv_lines)}",
        *priv_lines,
        f"Private-MAC: {mac_hex}",
        "",    # trailing newline
    ])
    return ppk.encode("utf-8")
