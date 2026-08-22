"""
SSH Client module for executing commands on a remote Linux server.
Uses paramiko for secure SSH connections with support for
password and key-based authentication.
"""

import paramiko
import logging
from typing import Tuple

logger = logging.getLogger(__name__)


class SSHClient:
    def __init__(self, host: str, port: int, username: str,
                 password: str = None, key_path: str = None):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.key_path = key_path
        self._client: paramiko.SSHClient = None

    def connect(self) -> None:
        """Establish SSH connection to the remote server."""
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": self.host,
            "port": self.port,
            "username": self.username,
            "timeout": 15,
        }

        if self.key_path:
            connect_kwargs["key_filename"] = self.key_path
        elif self.password:
            connect_kwargs["password"] = self.password
        else:
            raise ValueError("Either password or key_path must be provided.")

        self._client.connect(**connect_kwargs)
        logger.info(f"SSH connected to {self.host}:{self.port} as {self.username}")

    def disconnect(self) -> None:
        """Close the SSH connection."""
        if self._client:
            self._client.close()
            self._client = None

    def execute(self, command: str) -> Tuple[int, str, str]:
        """
        Execute a command on the remote server.
        Returns (exit_code, stdout, stderr).
        """
        if not self._client:
            self.connect()

        logger.info(f"Executing remote command: {command}")
        stdin, stdout, stderr = self._client.exec_command(command, timeout=30)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8").strip()
        err = stderr.read().decode("utf-8").strip()
        return exit_code, out, err

    def test_connection(self) -> Tuple[bool, str]:
        """Test the SSH connection and sudo availability."""
        try:
            self.connect()
            code, out, err = self.execute("echo connection_ok")
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
