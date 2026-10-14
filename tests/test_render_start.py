import pytest

from comic_server.render_start import bootstrap_user, main, public_url
from comic_server.store import Store


def test_bootstrap_survives_restart_without_resetting_password_or_session(tmp_path):
    store = Store(tmp_path)
    assert bootstrap_user(store, "damico", "first-password-123")
    session = store.login("damico", "first-password-123")
    assert not bootstrap_user(Store(tmp_path), "damico", "different-password-456")
    assert store.user_for_token(session["accessToken"]) == "damico"
    assert store.login("damico", "first-password-123")
    assert store.login("damico", "different-password-456") is None
    assert not bootstrap_user(store, "damico", None)


@pytest.mark.parametrize("password", [None, "", "too-short"])
def test_initial_account_requires_strong_password(tmp_path, password):
    store = Store(tmp_path)
    with pytest.raises(ValueError, match="at least 12"):
        bootstrap_user(store, "damico", password)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_url_uses_actual_render_host_or_explicit_custom_domain():
    assert public_url({"RENDER_EXTERNAL_URL": "https://assigned-host.onrender.com/"}) == "https://assigned-host.onrender.com"
    assert public_url({"COMIC_PUBLIC_URL": "https://comic.example.com", "RENDER_EXTERNAL_URL": "https://assigned-host.onrender.com"}) == "https://comic.example.com"


@pytest.mark.parametrize("url", ["", "http://example.com", "https://u:p@example.com", "https://example.com/api", "https://example.com?token=x", "https://example.com#x"])
def test_invalid_public_urls_are_rejected(url):
    with pytest.raises(ValueError):
        public_url({"COMIC_PUBLIC_URL": url})


def test_native_startup_binds_render_port_and_preserves_account(tmp_path, monkeypatch):
    import uvicorn
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://assigned-host.onrender.com")
    monkeypatch.delenv("COMIC_PUBLIC_URL", raising=False)
    monkeypatch.setenv("COMIC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMIC_BOOTSTRAP_USERNAME", "damico")
    monkeypatch.setenv("COMIC_BOOTSTRAP_PASSWORD", "test-start-password")
    monkeypatch.setenv("PORT", "15432")
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    main()
    assert calls[0][1]["port"] == 15432
    assert calls[0][1]["workers"] == 1
    assert calls[0][1]["access_log"] is False
    store = Store(tmp_path)
    session = store.login("damico", "test-start-password")
    monkeypatch.delenv("COMIC_BOOTSTRAP_PASSWORD")
    main()
    assert store.user_for_token(session["accessToken"]) == "damico"
