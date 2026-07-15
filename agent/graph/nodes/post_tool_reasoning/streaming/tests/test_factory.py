"""Tests for StreamingAdapterFactory."""

import pytest

from ..factory import StreamingAdapterFactory
from ..dr_adapter import DRStreamingAdapter
from ..simple_adapter import SimpleStreamingAdapter


class TestStreamingAdapterFactory:
    """Tests for StreamingAdapterFactory.create method."""
    
    def test_create_dr_adapter(self):
        """Verify factory creates DRStreamingAdapter for deep_reasoning."""
        adapter = StreamingAdapterFactory.create("deep_reasoning")
        assert isinstance(adapter, DRStreamingAdapter)
    
    def test_create_simple_adapter(self):
        """Verify factory creates SimpleStreamingAdapter for simple_tool_execution."""
        adapter = StreamingAdapterFactory.create("simple_tool_execution")
        assert isinstance(adapter, SimpleStreamingAdapter)
    
    def test_create_case_insensitive(self):
        """Verify factory handles case-insensitive capability names."""
        adapter1 = StreamingAdapterFactory.create("DEEP_REASONING")
        adapter2 = StreamingAdapterFactory.create("Simple_Tool_Execution")
        
        assert isinstance(adapter1, DRStreamingAdapter)
        assert isinstance(adapter2, SimpleStreamingAdapter)
    
    def test_create_unsupported_capability(self):
        """Verify factory raises ValueError for unsupported capability."""
        with pytest.raises(ValueError) as exc_info:
            StreamingAdapterFactory.create("unsupported_capability")
        
        assert "Unsupported capability" in str(exc_info.value)
    
    def test_create_empty_capability(self):
        """Verify factory raises ValueError for empty capability."""
        with pytest.raises(ValueError):
            StreamingAdapterFactory.create("")
    
    def test_create_returns_new_instance(self):
        """Verify factory returns new instance each time."""
        adapter1 = StreamingAdapterFactory.create("deep_reasoning")
        adapter2 = StreamingAdapterFactory.create("deep_reasoning")
        
        assert adapter1 is not adapter2

