"""Run with `python -m scripts.reset_passwords [username ...]` locally."""

import argparse
from getpass import getpass
import json
import os
from pathlib import Path
import tomllib
import tempfile

from utils.credentials import PASSWORD_HASHER


def main():
    parser = argparse.ArgumentParser(description="Reset account passwords without printing credentials")
    parser.add_argument("usernames", nargs="*")
    args = parser.parse_args()
    output = Path("secrets/users.json")
    usernames = args.usernames
    if not usernames:
        if output.exists():
            usernames = list(json.loads(output.read_text(encoding="utf-8")))
        else:
            local = Path(".streamlit/secrets.toml")
            if local.exists():
                usernames = list(tomllib.loads(local.read_text(encoding="utf-8")).get("users", {}))
    if not usernames:
        parser.error("Provide at least one username")
    users = {}
    for username in usernames:
        if not username or len(username) > 128:
            parser.error("Usernames must contain 1-128 characters")
        while True:
            password = getpass(f"New password for {username} (14-256 characters): ")
            confirm = getpass("Confirm password: ")
            if password == confirm and 14 <= len(password) <= 256:
                break
            print("Passwords must match and contain 14-256 characters.")
        users[username] = PASSWORD_HASHER.hash(password)
    output.parent.mkdir(mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="users-", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(users, handle)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print("Password hashes saved to ignored secrets/users.json. Set APP_USERS_FILE=secrets/users.json locally.")
    print("The deployment script mounts these credentials from Secret Manager.")


if __name__ == "__main__":
    main()
