import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

try:
    from agent.context.metrics import ContextMetrics
except Exception:  # pragma: no cover
    from context.metrics import ContextMetrics


def test_metrics_record_and_export():
    metrics = ContextMetrics()
    metrics.record_token_usage("context", 90, 100)
    metrics.record_compression("tool", 200, 100)
    metrics.record_processing_time("build", 50)
    metrics.record_quality_score(0.8)
    exported = metrics.export_metrics()
    assert exported["token_savings"]["context"] == 10
    assert 0.4 <= exported["avg_compression_ratio"]["tool"] <= 0.6
    assert exported["avg_processing_ms"]["build"] == 50
    assert abs(exported["avg_quality_score"] - 0.8) < 1e-6
