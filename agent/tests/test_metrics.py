try:
    from agent.context.metrics import ContextMetrics
    from agent.logger import AgentLogger
except Exception:  # pragma: no cover
    from context.metrics import ContextMetrics
    from logger import AgentLogger


class DummyLogger:
    def __init__(self):
        self.messages = []

    def warning(self, msg: str, **_: object) -> None:
        self.messages.append(msg)

    def info(self, msg: str, **_: object) -> None:
        self.messages.append(msg)


def test_token_usage_recording_and_alert():
    logger = DummyLogger()
    metrics = ContextMetrics(logger=logger)
    metrics.record_token_usage("ctx", 95, 100)
    assert metrics.token_usage["ctx"][0]["tokens"] == 95
    assert any("approaching" in m for m in logger.messages)


def test_compression_and_processing_metrics():
    metrics = ContextMetrics()
    metrics.record_compression("tool", 1000, 200)
    metrics.record_processing_time("op", 150)
    assert metrics.compression_ratios["tool"][0] == 0.2
    assert metrics.processing_times["op"][0] == 150


def test_export_metrics_calculations():
    metrics = ContextMetrics()
    metrics.record_token_usage("a", 50, 100)
    metrics.record_token_usage("b", 30, 80)
    res = metrics.export_metrics()
    assert res["token_savings"] == 100 - 50 + 80 - 30
    assert res["total_tokens"] == 80
