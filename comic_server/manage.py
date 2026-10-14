import argparse
import getpass
import os
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Manage ComicAI Forge users")
    parser.add_argument("action", choices=["create-user"])
    parser.add_argument("username")
    args = parser.parse_args()
    first = getpass.getpass("Password (at least 12 characters): ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise SystemExit("Passwords did not match")
    Store(os.getenv("COMIC_DATA_DIR", "./data")).create_user(args.username, first)
    print("User created or password reset. Previous sessions for this user were revoked.")


if __name__ == "__main__":
    main()
