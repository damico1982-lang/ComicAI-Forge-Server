import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import threading
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, directory):
        self.resource_lock = threading.Lock()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "comics.sqlite3"
        self.assets = self.directory / "images"
        self.assets.mkdir(exist_ok=True)
        self.uploads = self.directory / "uploads"
        self.uploads.mkdir(exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY, salt BLOB NOT NULL, password_hash BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES users(username), expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS login_attempts (
                    username TEXT PRIMARY KEY, count INTEGER NOT NULL, reset_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES users(username),
                    name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, disk_name TEXT NOT NULL, kind TEXT NOT NULL, notice TEXT);
                CREATE INDEX IF NOT EXISTS uploads_owner ON uploads(username);
                CREATE TABLE IF NOT EXISTS artwork_approvals (
                    job_id TEXT PRIMARY KEY, revision TEXT NOT NULL, estimate TEXT NOT NULL, approved_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES users(username),
                    request_id TEXT NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
                    total INTEGER NOT NULL DEFAULT 0, plan TEXT, error TEXT,
                    UNIQUE(username, request_id));
            """)
        secret_file = self.directory / "image-signing.key"
        try:
            descriptor = secret_file.open("xb")
        except FileExistsError:
            pass
        else:
            with descriptor:
                secret_file.chmod(0o600)
                descriptor.write(secrets.token_bytes(32))
        self.secret = secret_file.read_bytes()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_user(self, username, password):
        if not username.strip() or len(username) > 80 or len(password) < 12:
            raise ValueError("Use a username of 1–80 characters and a password of at least 12 characters.")
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000)
        with self.connect() as db:
            db.execute("INSERT INTO users VALUES (?, ?, ?) ON CONFLICT(username) DO UPDATE SET salt=excluded.salt, password_hash=excluded.password_hash",
                (username, salt, digest))
            db.execute("DELETE FROM sessions WHERE username=?", (username,))

    def login(self, username, password):
        now = time.time()
        with self.connect() as db:
            attempts = db.execute("SELECT * FROM login_attempts WHERE username=?", (username,)).fetchone()
            if attempts and attempts["reset_at"] > now and attempts["count"] >= 10:
                raise PermissionError("Too many login attempts. Try again in 15 minutes.")
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            salt = row["salt"] if row else b"0" * 16
            supplied = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000)
            valid = row is not None and hmac.compare_digest(supplied, row["password_hash"])
            if not valid:
                count = attempts["count"] + 1 if attempts and attempts["reset_at"] > now else 1
                db.execute("INSERT OR REPLACE INTO login_attempts VALUES (?, ?, ?)", (username, count, now + 900))
                return None
            db.execute("DELETE FROM login_attempts WHERE username=?", (username,))
            db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            token = secrets.token_urlsafe(40)
            expires = now + 86400
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (hashlib.sha256(token.encode()).hexdigest(), username, expires))
            return {"accessToken": token, "expiresAt": int(expires)}

    def user_for_token(self, token):
        with self.connect() as db:
            row = db.execute("SELECT username FROM sessions WHERE token_hash=? AND expires>?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
            return row["username"] if row else None

    def logout(self, token):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def create_job(self, username, request):
        import uuid
        # Omit empty new fields to preserve idempotency for jobs created by version 1 clients.
        payload = request.model_dump_json(exclude_defaults=True)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM jobs WHERE username=? AND request_id=?",
                (username, str(request.requestId))).fetchone()
            if existing:
                if existing["payload_hash"] != fingerprint:
                    raise ValueError("This request ID was already used for different content.")
                return dict(existing)
            for ref in request.attachments:
                if db.execute("SELECT 1 FROM uploads WHERE id=? AND username=?", (str(ref.id), username)).fetchone() is None:
                    raise ValueError("An attachment is missing or belongs to another account.")
            active = db.execute("SELECT COUNT(*) FROM jobs WHERE username=? AND status IN ('queued','running')", (username,)).fetchone()[0]
            if active >= 3:
                raise PermissionError("You already have three active generation jobs.")
            job_id = str(uuid.uuid4())
            db.execute("INSERT INTO jobs (id,username,request_id,payload_hash,payload,status,created,total) VALUES (?,?,?,?,?,'queued',?,?)",
                (job_id, username, str(request.requestId), fingerprint, payload, time.time(), request.targetPageCount * request.panelsPerPage))
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def job(self, job_id, username=None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?" + (" AND username=?" if username else ""),
                (job_id, username) if username else (job_id,)).fetchone()
            return dict(row) if row else None

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running', error=NULL WHERE id=?", (row["id"],))
            return dict(row)

    def update(self, job_id, **values):
        if not set(values) <= {"status", "completed", "plan", "error", "total"}:
            raise ValueError("Unknown job update")
        with self.connect() as db:
            db.execute("UPDATE jobs SET " + ",".join(key + "=?" for key in values) + " WHERE id=?", (*values.values(), job_id))

    def interrupt_running(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='interrupted', error='Server stopped during generation. Resume to continue from saved panels.' WHERE status='running'")

    def resume(self, job_id, username):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=? AND username=?", (job_id, username)).fetchone()
            if not row:
                return False
            if row["status"] not in ("failed", "interrupted", "cancelled"):
                raise ValueError("Only stopped jobs can be resumed.")
            active = db.execute("SELECT COUNT(*) FROM jobs WHERE username=? AND status IN ('queued','running')", (username,)).fetchone()[0]
            if active >= 3:
                raise PermissionError("You already have three active generation jobs.")
            db.execute("UPDATE jobs SET status='queued', error=NULL WHERE id=?", (job_id,))
            return True

    def cancel(self, job_id, username):
        with self.connect() as db:
            cursor = db.execute("UPDATE jobs SET status='cancelled', error='Cancelled by user.' WHERE id=? AND username=? AND status IN ('queued','running','awaiting_approval')", (job_id, username))
            return cursor.rowcount > 0

    def image_signature(self, job_id, index, expires):
        return hmac.new(self.secret, f"{job_id}:{index}:{expires}".encode(), hashlib.sha256).hexdigest()

    def upload(self, upload_id, username):
        with self.connect() as db:
            row = db.execute("SELECT * FROM uploads WHERE id=? AND username=?", (upload_id, username)).fetchone()
            return dict(row) if row else None


    def approval(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM artwork_approvals WHERE job_id=?", (job_id,)).fetchone()
            return dict(row) if row else None
