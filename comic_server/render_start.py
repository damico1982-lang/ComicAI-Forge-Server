"""Render startup: durable data, actual HTTPS URL, and first-use account only."""
import hashlib
import os
import secrets
from urllib.parse import urlsplit

from .store import Store


def public_url(environment):
    value = (environment.get("COMIC_PUBLIC_URL") or environment.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.path and not parsed.query
                 and not parsed.fragment and parsed.port in (None, 443))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Set COMIC_PUBLIC_URL to the server's HTTPS base URL, or deploy as a Render web service.")
    return value


def bootstrap_user(store, username, password):
    """Create an absent account atomically; never change a stored password/session."""
    if not username or username != username.strip() or len(username) > 80:
        raise ValueError("Set COMIC_BOOTSTRAP_USERNAME to a username of 1–80 characters without surrounding spaces.")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return False
        if not password or len(password) < 12:
            raise ValueError("Set COMIC_BOOTSTRAP_PASSWORD to at least 12 characters for the first deployment.")
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000)
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (username, salt, digest))
        return True


def main():
    import uvicorn

    address = public_url(os.environ)
    try:
        port = int(os.getenv("PORT", "10000"))
    except ValueError:
        raise ValueError("PORT must be an integer from 1 to 65535.") from None
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be an integer from 1 to 65535.")
    directory = os.getenv("COMIC_DATA_DIR", "/var/data/comicai")
    os.environ["COMIC_PUBLIC_URL"] = address
    os.environ["COMIC_DATA_DIR"] = directory
    store = Store(directory)
    if bootstrap_user(store, os.getenv("COMIC_BOOTSTRAP_USERNAME", "damico"), os.getenv("COMIC_BOOTSTRAP_PASSWORD")):
        print("Initial sign-in account created. No credentials are printed.", flush=True)
    uvicorn.run("comic_server.app:create_app", factory=True, host="0.0.0.0", port=port,
                workers=1, access_log=False, timeout_graceful_shutdown=110)


if __name__ == "__main__":
    main()
