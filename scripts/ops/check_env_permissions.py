"""Check (and optionally fix) filesystem permissions on the repo's .env file.

Why: `.env` holds live secrets (API_KEYS, IB credentials, Telegram tokens).
A file world- or group-readable on a shared host lets any other local user
read them. This is a read-only diagnostic by default — it never touches a
file unless --fix is passed, and it never guesses a path other than the one
given (default: `.env` in the current directory).

Usage:
    python -m scripts.ops.check_env_permissions              # report only
    python -m scripts.ops.check_env_permissions --fix         # chmod 600
    python -m scripts.ops.check_env_permissions --path /path/to/.env

Exit code is non-zero when the file is missing or insecurely permissioned,
so this can be wired into a pre-flight/CI check.

Longer term: see docs/operations/api-security.md for the plan to move
secrets out of a plaintext `.env` file entirely (OS keychain / a secrets
manager) — this script only hardens the interim state.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from dataclasses import dataclass

# Only the owner should be able to read/write the secrets file.
_SECURE_MODE = 0o600
# Bits that must NOT be set: any group or other permission.
_INSECURE_MASK = stat.S_IRWXG | stat.S_IRWXO


@dataclass(frozen=True)
class PermissionCheckResult:
    path: str
    secure: bool
    message: str


def check_permissions(path: str) -> PermissionCheckResult:
    """Report whether `path` is readable/writable only by its owner."""
    if not os.path.exists(path):
        return PermissionCheckResult(
            path=path, secure=False, message=f"{path} not found"
        )

    mode = stat.S_IMODE(os.stat(path).st_mode)
    insecure_bits = mode & _INSECURE_MASK
    if insecure_bits:
        problems = []
        if insecure_bits & stat.S_IRWXG:
            problems.append("group")
        if insecure_bits & stat.S_IRWXO:
            problems.append("other")
        return PermissionCheckResult(
            path=path,
            secure=False,
            message=(
                f"{path} is readable/writable by {' and '.join(problems)} "
                f"(mode {oct(mode)}); expected {oct(_SECURE_MODE)} "
                "(owner-only)"
            ),
        )

    return PermissionCheckResult(
        path=path, secure=True, message=f"{path} is owner-only ({oct(mode)})"
    )


def fix_permissions(path: str) -> None:
    """Restrict `path` to owner read/write only. Raises if it doesn't exist.

    This is the only function in this module that mutates a file — callers
    (the CLI's --fix flag) must be an explicit, human-invoked opt-in. Never
    call this against a real .env from an automated agent; the operator
    runs this themselves.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    os.chmod(path, _SECURE_MODE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path", default=".env", help="Path to the env file to check (default: .env)"
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Chmod the file to 600 (owner-only) if it's insecurely permissioned.",
    )
    args = parser.parse_args(argv)

    result = check_permissions(args.path)
    print(result.message)

    if not result.secure and args.fix and os.path.exists(args.path):
        fix_permissions(args.path)
        result = check_permissions(args.path)
        print(f"fixed: {result.message}")

    return 0 if result.secure else 1


if __name__ == "__main__":
    sys.exit(main())
