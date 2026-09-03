import pytest

from app.secrets import load_deepseek_key, protect, save_deepseek_key, unprotect


def test_dpapi_memory_round_trip():
    source = b"sk-test-only-not-a-real-key"
    try:
        encrypted = protect(source)
    except OSError:
        pytest.skip("当前Windows进程没有可用DPAPI用户上下文")
    assert encrypted != source
    assert unprotect(encrypted) == source


def test_dpapi_file_round_trip(tmp_path):
    path = tmp_path / "secrets.dat"
    mode = save_deepseek_key("  sk-test-only-not-a-real-key  ", path)
    if mode == "dpapi":
        assert b"sk-test-only" not in path.read_bytes()
        assert load_deepseek_key(path) == "sk-test-only-not-a-real-key"
    else:
        assert not path.exists()
        assert load_deepseek_key(path) == "sk-test-only-not-a-real-key"


def test_save_falls_back_to_memory_when_dpapi_is_unavailable(monkeypatch, tmp_path):
    import app.secrets as secrets

    monkeypatch.setattr(secrets, "_session_key", None)
    monkeypatch.setattr(secrets, "_storage_mode", None)
    monkeypatch.setattr(secrets, "protect", lambda _: (_ for _ in ()).throw(OSError("blocked")))
    mode = secrets.save_deepseek_key("sk-memory-only", tmp_path / "secrets.dat")
    assert mode == "memory"
    assert secrets.load_deepseek_key(tmp_path / "secrets.dat") == "sk-memory-only"


def test_hithink_memory_key_can_be_cleared(monkeypatch, tmp_path):
    import app.secrets as secrets

    path = tmp_path / "hithink.dat"
    monkeypatch.setattr(secrets, "_hithink_session_key", None)
    monkeypatch.setattr(secrets, "_hithink_storage_mode", None)
    monkeypatch.setattr(secrets, "protect", lambda _: (_ for _ in ()).throw(OSError("blocked")))
    assert secrets.save_hithink_key("hithink-test-key", path) == "memory"
    assert secrets.load_hithink_key(path) == "hithink-test-key"
    assert secrets.clear_hithink_key(path)["configured"] is False
    assert secrets.load_hithink_key(path) is None
