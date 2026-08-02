"""Config loading: defaults, required vars, validation rules."""
from __future__ import annotations

import pytest

from rsc_host.config import load_from_env


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Strip every RSC_HOST_* var; tests set the ones they care about."""
    for var in (
        "RSC_HOST_BIND",
        "RSC_HOST_PORT",
        "RSC_HOST_TOKEN",
        "RSC_HOST_BACKEND",
        "RSC_HOST_TLS_CERT",
        "RSC_HOST_TLS_KEY",
        "RSC_HOST_LOG_LEVEL",
        "RSC_HOST_ADVERTISE",
        "RSC_HOST_ROBOT_NAME",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


class TestRequired:
    def test_missing_token_raises(self, clean_env: pytest.MonkeyPatch) -> None:
        with pytest.raises(RuntimeError, match="RSC_HOST_TOKEN"):
            load_from_env()

    def test_empty_token_raises(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("RSC_HOST_TOKEN", "   ")
        with pytest.raises(RuntimeError, match="RSC_HOST_TOKEN"):
            load_from_env()


class TestDefaults:
    def test_minimal_config(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("RSC_HOST_TOKEN", "dev")
        cfg = load_from_env()
        assert cfg.bind == "127.0.0.1"
        assert cfg.port == 8765
        assert cfg.token == "dev"
        assert cfg.backend == "fake"
        assert cfg.tls_cert is None
        assert cfg.tls_key is None
        assert cfg.tls_enabled is False
        assert cfg.log_level == "INFO"
        assert cfg.advertise is True
        assert cfg.robot_name is None


class TestBackend:
    def test_pi_backend(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("RSC_HOST_TOKEN", "dev")
        clean_env.setenv("RSC_HOST_BACKEND", "pi")
        cfg = load_from_env()
        assert cfg.backend == "pi"

    def test_backend_case_normalised(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("RSC_HOST_TOKEN", "dev")
        clean_env.setenv("RSC_HOST_BACKEND", "FAKE")
        cfg = load_from_env()
        assert cfg.backend == "fake"

    def test_invalid_backend_rejected(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("RSC_HOST_TOKEN", "dev")
        clean_env.setenv("RSC_HOST_BACKEND", "quantum")
        with pytest.raises(ValueError, match="RSC_HOST_BACKEND"):
            load_from_env()


class TestTls:
    def test_both_set_enables_tls(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("RSC_HOST_TOKEN", "dev")
        clean_env.setenv("RSC_HOST_TLS_CERT", "/tmp/cert.pem")
        clean_env.setenv("RSC_HOST_TLS_KEY", "/tmp/key.pem")
        cfg = load_from_env()
        assert cfg.tls_enabled is True
        assert cfg.tls_cert == "/tmp/cert.pem"
        assert cfg.tls_key == "/tmp/key.pem"

    def test_cert_only_rejected(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("RSC_HOST_TOKEN", "dev")
        clean_env.setenv("RSC_HOST_TLS_CERT", "/tmp/cert.pem")
        with pytest.raises(ValueError, match="TLS_CERT and RSC_HOST_TLS_KEY"):
            load_from_env()

    def test_key_only_rejected(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("RSC_HOST_TOKEN", "dev")
        clean_env.setenv("RSC_HOST_TLS_KEY", "/tmp/key.pem")
        with pytest.raises(ValueError, match="TLS_CERT and RSC_HOST_TLS_KEY"):
            load_from_env()


class TestOverrides:
    def test_bind_and_port(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("RSC_HOST_TOKEN", "dev")
        clean_env.setenv("RSC_HOST_BIND", "0.0.0.0")
        clean_env.setenv("RSC_HOST_PORT", "9000")
        cfg = load_from_env()
        assert cfg.bind == "0.0.0.0"
        assert cfg.port == 9000

    def test_log_level_uppercased(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("RSC_HOST_TOKEN", "dev")
        clean_env.setenv("RSC_HOST_LOG_LEVEL", "debug")
        cfg = load_from_env()
        assert cfg.log_level == "DEBUG"


class TestAdvertise:
    def test_advertise_default_true(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("RSC_HOST_TOKEN", "dev")
        assert load_from_env().advertise is True

    def test_advertise_false(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("RSC_HOST_TOKEN", "dev")
        clean_env.setenv("RSC_HOST_ADVERTISE", "false")
        assert load_from_env().advertise is False

    def test_advertise_various_true_values(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("RSC_HOST_TOKEN", "dev")
        for v in ("true", "TRUE", "1", "yes"):
            clean_env.setenv("RSC_HOST_ADVERTISE", v)
            assert load_from_env().advertise is True

    def test_advertise_invalid_rejected(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("RSC_HOST_TOKEN", "dev")
        clean_env.setenv("RSC_HOST_ADVERTISE", "maybe")
        with pytest.raises(ValueError, match="RSC_HOST_ADVERTISE"):
            load_from_env()

    def test_robot_name_override(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("RSC_HOST_TOKEN", "dev")
        clean_env.setenv("RSC_HOST_ROBOT_NAME", "Shiny")
        assert load_from_env().robot_name == "Shiny"

    def test_robot_name_default_none(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("RSC_HOST_TOKEN", "dev")
        assert load_from_env().robot_name is None
