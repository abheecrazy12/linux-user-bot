"""
SSH Client module for executing commands on a remote Linux server.
Uses paramiko for secure SSH connections with support for
password, key-path, and in-memory key content authentication.
"""

import io
import paramiko
import logging
from typing import Tuple

logger = logging.getLogger(__name__)


class SSHClient:
    def __init__(self, host: str, port: int, username: str,
                 password: str = None, key_path: str = None,
                 key_content: str = None):
        self.host     = host
        self.port     = port
        self.username = username
        self.password = password
        self.key_path = key_path
        self.key_content = key_content
        self._client: paramiko.SSHClient = None

    def connect(self) -> None:
        """Establish SSH connection to the remote server."""
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname":              self.host,
            "port":                  self.port,
            "username":              self.username,
            "timeout":               30,          # connect timeout
            "banner_timeout":        30,
            "auth_timeout":          30,
            "allow_agent":           False,
            "look_for_keys":         False,
        }

        if self.key_content:
            pkey = paramiko.RSAKey.from_private_key(io.StringIO(self.key_content))
            connect_kwargs["pkey"] = pkey
        elif self.key_path:
            connect_kwargs["key_filename"] = self.key_path
        elif self.password:
            connect_kwargs["password"] = self.password
        else:
            raise ValueError("Provide password, key_path, or key_content.")

        self._client.connect(**connect_kwargs)
        # Keep connection alive — prevents timeout on slow commands
        transport = self._client.get_transport()
        transport.set_keepalive(10)
        logger.info(f"SSH connected to {self.host}:{self.port} as {self.username}")

    def disconnect(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def execute(self, command: str, timeout: int = 120) -> Tuple[int, str, str]:
        """
        Execute a command on the remote server.
        Default timeout is 120s — enough for ssh-keygen 4096 bit.
        Returns (exit_code, stdout, stderr).
        """
        if not self._client:
            self.connect()

        logger.info(f"Executing: {command}")
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)

        # Read output without blocking forever
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        exit_code = stdout.channel.recv_exit_status()

        return exit_code, out, err

    def test_connection(self) -> Tuple[bool, str]:
        try:
            self.connect()
            code, out, err = self.execute("echo connection_ok", timeout=10)
            if code == 0 and "connection_ok" in out:
                return True, "SSH connection successful."
            return False, f"Unexpected response: {out} {err}"
        except Exception as e:
            return False, str(e)
        finally:
            self.disconnect()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()
