"""
ANSI Escape Sequence Cleaner Utility
Comprehensive cleaning of Docker container log output for clean display
"""

import re
from typing import Optional


def clean_ansi_codes(text: str) -> str:
    """
    Clean ANSI escape sequences and control characters from text.
    
    This function removes:
    - Color codes (foreground and background)
    - Text formatting (bold, italic, underline, etc.)
    - Cursor movement sequences
    - Terminal control sequences
    - Shell prompt artifacts
    
    Args:
        text: Raw text with potential ANSI escape sequences
        
    Returns:
        Clean text without ANSI escape sequences
    """
    if not text:
        return text
    
    # Comprehensive ANSI escape sequence patterns - order matters!
    patterns = [
        # Real ANSI escape sequences with \x1B
        # Complex sequences first - multi-parameter color codes like \x1B[1;31m
        r'\x1B\[[0-9]+(?:;[0-9]+)*[mK]',
        
        # Simple color codes like \x1B[35m, \x1B[0m
        r'\x1B\[[0-9]*m',
        
        # Cursor movement and control sequences
        r'\x1B\[[0-9]*[A-Za-z]',
        
        # Clear screen and similar: \x1B[2J, \x1B[H, etc.
        r'\x1B\[[0-9]*[JHfABCDsuK]',
        
        # OSC sequences (Operating System Commands): \x1B]<text>\x07 or \x1B]<text>\x1B\\
        r'\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)',
        
        # Simple ESC sequences: \x1B followed by single character
        r'\x1B[@-Z\\-_]',
        
        # Fake ANSI sequences (just bracket notation without escape char)
        # Complex sequences first - multi-parameter color codes like [1;31m
        r'\[[0-9]+(?:;[0-9]+)*[mK]',
        
        # Simple color codes like [35m, [0m
        r'\[[0-9]*m',
        
        # Bell character
        r'\x07',
        
        # Backspace sequences
        r'\x08+',
    ]
    
    # Apply all patterns
    cleaned = text
    for pattern in patterns:
        cleaned = re.sub(pattern, '', cleaned)
    
    # Remove other control characters except newlines and tabs
    cleaned = re.sub(r'[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F-\x9F]', '', cleaned)
    
    # Clean up shell prompt artifacts
    cleaned = re.sub(r'\]0;[^\\]*\\', '', cleaned)
    cleaned = re.sub(r'┌──.*?─\]', '', cleaned)
    
    # Remove excessive whitespace but preserve intentional spacing
    cleaned = re.sub(r'\n\s*\n\s*\n', '\n\n', cleaned)  # Max 2 consecutive newlines
    cleaned = re.sub(r' {4,}', '   ', cleaned)  # Max 3 consecutive spaces
    
    return cleaned.strip()


def clean_docker_log_line(line: str, timestamp: Optional[str] = None) -> dict:
    """
    Clean a single Docker log line and format it for frontend display.
    
    Args:
        line: Raw log line from Docker
        timestamp: Optional timestamp, will extract from line if not provided
        
    Returns:
        Dictionary with cleaned log entry
    """
    if not line.strip():
        return None
    
    # Clean ANSI codes first
    cleaned_line = clean_ansi_codes(line)
    
    if not cleaned_line.strip():
        return None
    
    # Extract timestamp if present in Docker log format
    if not timestamp and ' ' in cleaned_line:
        parts = cleaned_line.split(' ', 1)
        # Check if first part looks like a timestamp
        if len(parts) >= 2 and ('T' in parts[0] or ':' in parts[0]):
            timestamp = parts[0]
            message = parts[1]
        else:
            message = cleaned_line
    else:
        message = cleaned_line
    
    return {
        "timestamp": timestamp or "unknown",
        "service": "docker-container",
        "level": "info",
        "message": message
    }


def test_ansi_cleaning():
    """Test function to verify ANSI cleaning works correctly."""
    test_cases = [
        'File [35m"/opt/drowai/runtime/python/agent_runner.py"[0m, line [35m176[0m, in [35m<module>[0m',
        '[31mAgentRunner[0m[1;31m(task_id)[0m.run()',
        '[31m~~~~~~~~~~~[0m[1;31m^^^^^^^^^[0m',
        '[1;35mValueError[0m: [35mOPENAI_API_KEY environment variable is required[0m'
    ]
    
    expected_results = [
        'File "/opt/drowai/runtime/python/agent_runner.py", line 176, in <module>',
        'AgentRunner(task_id).run()',
        '~~~~~~~~~~~^^^^^^^^^',
        'ValueError: OPENAI_API_KEY environment variable is required'
    ]
    
    print("ANSI Cleaning Test Results:")
    print("=" * 50)
    
    all_passed = True
    for i, (test, expected) in enumerate(zip(test_cases, expected_results)):
        result = clean_ansi_codes(test)
        passed = result == expected
        status = "✓ PASS" if passed else "✗ FAIL"
        
        print(f"Test {i+1}: {status}")
        print(f"  Input:    {test}")
        print(f"  Expected: {expected}")
        print(f"  Got:      {result}")
        print()
        
        if not passed:
            all_passed = False
    
    return all_passed


if __name__ == "__main__":
    test_ansi_cleaning()
