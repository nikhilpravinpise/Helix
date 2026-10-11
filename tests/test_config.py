class TestDotenvIsolation:
    """FOLLOWUPS #20: the unit tier read the developer's .env, so local
    settings reached tests - a 50 ms step-timeout test failed on Vraj's
    machine and passed in a clean checkout. HELIX_ENV_FILE="" (set for the
    unit tier in tests/conftest.py) turns the file off; unset, .env is read
    as always."""

    def test_the_unit_tier_ignores_a_local_env_file(self, tmp_path, monkeypatch):
        from helix.config import get_settings

        (tmp_path / ".env").write_text("HELIX_LOG_LEVEL=DEBUG\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("HELIX_LOG_LEVEL", raising=False)
        assert get_settings().log_level == "INFO"

    def test_outside_tests_env_is_read(self, tmp_path, monkeypatch):
        from helix.config import get_settings

        (tmp_path / ".env").write_text("HELIX_LOG_LEVEL=DEBUG\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("HELIX_LOG_LEVEL", raising=False)
        monkeypatch.delenv("HELIX_ENV_FILE", raising=False)
        assert get_settings().log_level == "DEBUG"
