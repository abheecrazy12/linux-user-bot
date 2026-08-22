"""
SSH Keypair generation and PPK conversion utilities.

Generates an RSA keypair on the remote Linux server for a new user,
installs the public key into their ~/.ssh/authorized_keys,
then downloads the private key and converts it to PuTTY PPK format
so the end-user can load it directly into PuTTY / WinSCP.
"""

import io
import logging
from paramiko import RSAKey

logger = logging.getLogger(__name__)


def generate_keypair_on_server(ssh_client, username: str) -> dict:
    """
    Generates a 4096-bit RSA keypair for `username` on the remote server.
    Sets up ~/.ssh with correct permissions and installs authorized_keys.

    Returns:
        {
            "success": bool,
            "private_key_pem": str,   # OpenSSH PEM text
            "public_key": str,        # authorized_keys line
            "error": str              # only on failure
        }
    """
    home = f"/home/{username}"
    ssh_dir = f"{home}/.ssh"

    commands = [
        # Create .ssh dir owned by the user
        f"sudo mkdir -p {ssh_dir}",
        f"sudo chown {username}:{username} {ssh_dir}",
        f"sudo chmod 700 {ssh_dir}",
        # Generate keypair (no passphrase, RSA 4096)
        f"sudo ssh-keygen -t rsa -b 4096 -N '' -f {ssh_dir}/id_rsa -C '{username}@server'",
        # Install public key as authorized_keys
        f"sudo cp {ssh_dir}/id_rsa.pub {ssh_dir}/authorized_keys",
        f"sudo chown {username}:{username} {ssh_dir}/authorized_keys {ssh_dir}/id_rsa {ssh_dir}/id_rsa.pub",
        f"sudo chmod 600 {ssh_dir}/authorized_keys {ssh_dir}/id_rsa",
        f"sudo chmod 644 {ssh_dir}/id_rsa.pub",
    ]

    for cmd in commands:
        code, out, err = ssh_client.execute(cmd)
        if code != 0:
            return {"success": False, "error": f"Command failed: {cmd}\n{err or out}"}

    # Read the private key back from the server
    code, private_key_pem, err = ssh_client.execute(f"sudo cat {ssh_dir}/id_rsa")
    if code != 0 or not private_key_pem.strip():
        return {"success": False, "error": f"Could not read private key: {err}"}

    code, public_key, err = ssh_client.execute(f"sudo cat {ssh_dir}/id_rsa.pub")
    if code != 0:
        return {"success": False, "error": f"Could not read public key: {err}"}

    # Remove the private key from server — user will use the downloaded PPK
    ssh_client.execute(f"sudo rm -f {ssh_dir}/id_rsa")

    return {
        "success": True,
        "private_key_pem": private_key_pem.strip(),
        "public_key": public_key.strip(),
    }


def pem_to_ppk(pem_text: str) -> bytes:
    """
    Convert an OpenSSH PEM private key string to PuTTY PPK v2 format bytes.
    Uses paramiko's RSAKey which can read PEM and write PPK.
    """
    # Load the PEM key via paramiko
    key_file = io.StringIO(pem_text)
    rsa_key = RSAKey.from_private_key(key_file)

    # Write it out in PPK format to a BytesIO buffer
    ppk_buf = io.StringIO()
    rsa_key.write_private_key(ppk_buf)          # paramiko writes OpenSSH format

    # paramiko doesn't natively write PPK v2 — we build it manually from the key material
    # PPK v2 format used by PuTTY (no passphrase)
    return _build_ppk_v2(rsa_key)


def _build_ppk_v2(rsa_key: RSAKey) -> bytes:
    """
    Build a PuTTY PPK v2 file from a paramiko RSAKey.
    Format reference: https://the.earth.li/~sgtatham/putty/0.81/htmldoc/AppendixC.html
    """
    import base64
    import hashlib
    import hmac
    import struct

    # ── Extract raw key components ───────────────────────────────
    pub_key = rsa_key.public_blob          # SSH wire format of public key
    # Private key in SSH wire format: n, e, d, p, q, iqmp
    # Build public blob: "ssh-rsa" + e + n  (standard SSH format)
    def mpint(n: int) -> bytes:
        """Encode integer as SSH mpint."""
        b = n.to_bytes((n.bit_length() + 7) // 8, 'big')
        if b[0] & 0x80:
            b = b'\x00' + b
        return struct.pack('>I', len(b)) + b

    def string(s: bytes) -> bytes:
        return struct.pack('>I', len(s)) + s

    n = rsa_key.public_numbers.n
    e = rsa_key.public_numbers.e
    d = rsa_key.key.private_numbers().d
    p = rsa_key.key.private_numbers().p
    q = rsa_key.key.private_numbers().q
    # PuTTY uses u = p^-1 mod q  (iqmp in PuTTY convention: inverse of p mod q)
    iqmp = rsa_key.key.private_numbers().iqmp  # = q^-1 mod p in OpenSSH

    # PPK public blob: ssh-rsa wire format
    pub_blob = string(b"ssh-rsa") + mpint(e) + mpint(n)

    # PPK private blob: d, p, q, iqmp  (PuTTY order)
    # Note: PuTTY's iqmp = inverse(q, p), OpenSSH iqmp = inverse(p, q)
    # We recalculate PuTTY's version:
    putty_iqmp = pow(q, -1, p)
    priv_blob = mpint(d) + mpint(p) + mpint(q) + mpint(putty_iqmp)

    # ── MAC computation ──────────────────────────────────────────
    # PPK v2 MAC = HMAC-SHA1 over:
    #   string("ssh-rsa") + string("aes256-cbc" or "none") +
    #   string("") + uint32(pub_len) + pub_blob +
    #   uint32(priv_len) + priv_blob
    algo      = b"ssh-rsa"
    enc       = b"none"
    comment   = b""
    mac_data  = (string(algo) + string(enc) + string(comment) +
                 string(pub_blob) + string(priv_blob))

    # MAC key = SHA1("putty-private-key-file-mac-key")
    mac_key = hashlib.sha1(b"putty-private-key-file-mac-key").digest()
    mac     = hmac.new(mac_key, mac_data, hashlib.sha1).hexdigest()

    # ── Assemble PPK text file ───────────────────────────────────
    pub_b64  = base64.b64encode(pub_blob).decode()
    priv_b64 = base64.b64encode(priv_blob).decode()

    # Wrap base64 at 64 chars per line
    def wrap64(s: str) -> str:
        return '\n'.join(s[i:i+64] for i in range(0, len(s), 64))

    pub_lines  = wrap64(pub_b64)
    priv_lines = wrap64(priv_b64)
    pub_count  = len(pub_lines.splitlines())
    priv_count = len(priv_lines.splitlines())

    ppk = (
        f"PuTTY-User-Key-File-2: ssh-rsa\n"
        f"Encryption: none\n"
        f"Comment: imported-key\n"
        f"Public-Lines: {pub_count}\n"
        f"{pub_lines}\n"
        f"Private-Lines: {priv_count}\n"
        f"{priv_lines}\n"
        f"Private-MAC: {mac}\n"
    )
    return ppk.encode("utf-8")
