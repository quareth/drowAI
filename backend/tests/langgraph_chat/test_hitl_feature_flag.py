import importlib


def _reload_config():
    import backend.config as config_module

    return importlib.reload(config_module)


def test_hitl_feature_flag_default(monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_HITL_INTERRUPTS", raising=False)
    config = _reload_config()
    assert config.ENABLE_HITL_INTERRUPTS is True


def test_hitl_feature_flag_disabled(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "false")
    config = _reload_config()
    assert config.ENABLE_HITL_INTERRUPTS is False
