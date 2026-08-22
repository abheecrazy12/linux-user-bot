"""
Command Builder module.
Translates extracted NLP parameters into safe, validated
useradd and usermod shell commands.
"""

import re
import shlex
import logging
from typing import Tuple, List

logger = logging.getLogger(__name__)

# Allowlist: valid Linux shells
VALID_SHELLS = {"/bin/bash", "/bin/sh", "/bin/zsh", "/usr/bin/zsh",
                "/bin/fish", "/usr/bin/fish", "/bin/dash", "/sbin/nologin",
                "/usr/sbin/nologin", "/bin/false"}

# Allowlist: safe group name pattern
GROUP_PATTERN = re.compile(r'^[a-z][a-z0-9_\-]{0,31}$')

# Username validation pattern (Linux standard)
USERNAME_PATTERN = re.compile(r'^[a-z_][a-z0-9_\-]{0,31}$')

# Home directory path validation
HOME_PATH_PATTERN = re.compile(r'^/[a-zA-Z0-9/_\-\.]+$')

# Date validation
DATE_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}$')


class ValidationError(Exception):
    pass


def validate_params(params: dict) -> List[str]:
    """Validate all parameters. Returns a list of validation error messages."""
    errors = []

    username = params.get("username", "")
    if not USERNAME_PATTERN.match(username):
        errors.append(f"Invalid username '{username}'. Use lowercase letters, digits, underscore, hyphen.")

    shell = params.get("shell")
    if shell and shell not in VALID_SHELLS:
        errors.append(f"Shell '{shell}' is not in the allowed list: {', '.join(sorted(VALID_SHELLS))}")

    for group in (params.get("groups") or []):
        if not GROUP_PATTERN.match(group):
            errors.append(f"Invalid group name '{group}'.")

    home_dir = params.get("home_dir")
    if home_dir and not HOME_PATH_PATTERN.match(home_dir):
        errors.append(f"Invalid home directory path '{home_dir}'.")

    expiry = params.get("expiry_date")
    if expiry and not DATE_PATTERN.match(expiry):
        errors.append(f"Invalid expiry date '{expiry}'. Use YYYY-MM-DD format.")

    return errors


def build_useradd_command(params: dict) -> Tuple[str, str]:
    """
    Build the useradd command from validated params.
    Returns (useradd_command, passwd_command_or_empty).
    Raises ValidationError if params are invalid.
    """
    errors = validate_params(params)
    if errors:
        raise ValidationError("\n".join(errors))

    username = params["username"]
    args = ["sudo", "useradd"]

    # Home directory
    if params.get("create_home", True):
        args.append("-m")
    else:
        args.append("-M")

    # Custom home directory path
    if params.get("home_dir"):
        args += ["-d", params["home_dir"]]

    # Shell
    if params.get("shell"):
        args += ["-s", params["shell"]]

    # Full name / GECOS
    if params.get("full_name"):
        args += ["-c", params["full_name"]]

    # Additional groups
    groups = params.get("groups") or []
    if groups:
        args += ["-G", ",".join(groups)]

    # System account
    if params.get("system_account"):
        args.append("-r")

    # Expiry date
    if params.get("expiry_date"):
        args += ["-e", params["expiry_date"]]

    # Username is last
    args.append(username)

    useradd_cmd = " ".join(shlex.quote(a) for a in args)

    # Build passwd command separately (never embed password in useradd)
    passwd_cmd = ""
    if params.get("password"):
        # Use chpasswd — safer than passing via -p (avoids hashed pw in ps output)
        passwd_cmd = f"echo {shlex.quote(username + ':' + params['password'])} | sudo chpasswd"

    return useradd_cmd, passwd_cmd


def build_command_preview(params: dict) -> dict:
    """
    Returns a human-readable preview of what will be executed.
    Safe to show to the user before confirmation.
    """
    try:
        useradd_cmd, passwd_cmd = build_useradd_command(params)
        commands = [useradd_cmd]
        if passwd_cmd:
            commands.append("echo '<username>:<password>' | sudo chpasswd  (password hidden)")
        return {
            "valid": True,
            "commands": commands,
            "useradd_cmd": useradd_cmd,
            "passwd_cmd": passwd_cmd,
            "params": params
        }
    except ValidationError as e:
        return {"valid": False, "error": str(e)}


def describe_params(params: dict) -> str:
    """Generate a natural language summary of what will be created."""
    lines = [f"Create user **{params.get('username')}**"]

    if params.get("full_name"):
        lines.append(f"Full name: {params['full_name']}")
    if params.get("shell"):
        lines.append(f"Shell: `{params['shell']}`")
    if params.get("home_dir"):
        lines.append(f"Home: `{params['home_dir']}`")
    elif params.get("create_home", True):
        lines.append(f"Home: `/home/{params.get('username')}` (auto-created)")
    if params.get("groups"):
        lines.append(f"Groups: `{', '.join(params['groups'])}`")
    if params.get("password"):
        lines.append("Password: **(set)**")
    if params.get("system_account"):
        lines.append("Type: System account")
    if params.get("expiry_date"):
        lines.append(f"Expires: {params['expiry_date']}")

    return "\n".join(lines)
