from types import SimpleNamespace

from comic_server import container_start


def test_mounted_directory_is_prepared_before_dropping_privileges(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(container_start.os, "geteuid", lambda: 0)
    monkeypatch.setattr(container_start.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=10001, pw_gid=10001))
    for name in ("chown", "setgroups", "setgid", "setuid"):
        monkeypatch.setattr(container_start.os, name, lambda *args, name=name: calls.append((name, args)))
    directory = tmp_path / "mounted" / "comicai"
    container_start.prepare_directory(str(directory))
    assert directory.is_dir()
    assert calls == [("chown", (directory, 10001, 10001)), ("setgroups", ([],)),
                     ("setgid", (10001,)), ("setuid", (10001,))]


def test_render_container_uses_bootstrap_and_disk_path(monkeypatch):
    from comic_server import render_start
    calls = []
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("COMIC_DATA_DIR", raising=False)
    monkeypatch.setattr(container_start, "prepare_directory", lambda path: calls.append(path))
    monkeypatch.setattr(render_start, "main", lambda: calls.append("render startup"))
    container_start.main()
    assert calls == ["/var/data/comicai", "render startup"]


def test_vps_keeps_existing_data_and_manual_account_setup(monkeypatch):
    import uvicorn
    calls = []
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("COMIC_DATA_DIR", "/data")
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(container_start, "prepare_directory", lambda path: calls.append(path))
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    container_start.main()
    assert calls[0] == "/data"
    assert calls[1]["port"] == 8000
    assert calls[1]["workers"] == 1
