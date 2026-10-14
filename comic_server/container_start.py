"""Initialize container storage, then run the API without root privileges."""
import os
import pwd
from pathlib import Path


def prepare_directory(directory):
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        account = pwd.getpwnam("comic")
        # Only the application directory changes owner, never the whole disk.
        os.chown(path, account.pw_uid, account.pw_gid)
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)


def main():
    on_render = os.getenv("RENDER") == "true"
    directory = os.environ.setdefault("COMIC_DATA_DIR", "/var/data/comicai" if on_render else "/data")
    prepare_directory(directory)
    if on_render:
        from .render_start import main as render_main
        render_main()
    else:
        import uvicorn
        uvicorn.run("comic_server.app:create_app", factory=True, host="0.0.0.0",
                    port=int(os.getenv("PORT", "8000")), workers=1, access_log=False,
                    timeout_graceful_shutdown=110)


if __name__ == "__main__":
    main()
