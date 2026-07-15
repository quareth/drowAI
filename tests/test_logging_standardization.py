#!/usr/bin/env python3
"""Test the standardized logging system to ensure no duplication and proper separation."""

import os
import json
import tempfile
import shutil
from agent.logger import UnifiedAgentLogger

def test_logging_standardization():
    """Test the standardized logging system."""
    
    # Create a temporary workspace
    temp_dir = tempfile.mkdtemp()
    original_workspace = os.environ.get("WORKSPACE")
    os.environ["WORKSPACE"] = temp_dir
    
    try:
        # Create logger
        logger = UnifiedAgentLogger("test-task-456")
        
        print("🧪 Testing Standardized Logging System...")
        
        # Test reasoning step logging (frontend only)
        logger.log_reasoning_step("thought", "I need to scan for open ports")
        logger.log_reasoning_step("action", "scan_ports on target.com")
        logger.log_reasoning_step("observation", "Found 3 open ports: 22, 80, 443")
        logger.log_reasoning_step("report", "Final security assessment report")
        logger.log_reasoning_step("completion", "Task completed successfully")
        
        # Test operational logging (console only)
        logger.log_operation("INFO", "Starting penetration test")
        logger.log_operation("DEBUG", "Command executed: nmap -F target.com")
        logger.log_operation("WARNING", "Rate limit approaching")
        logger.log_operation("ERROR", "Connection timeout")
        
        # Test convenience methods (console only)
        logger.info("Agent started successfully")
        logger.warning("High memory usage detected")
        logger.error("Command execution failed")
        logger.debug("Debug information")
        
        # Test legacy compatibility methods
        logger.conversation("User message received")
        logger.log_command(["nmap", "-F", "target.com"], "stdout", "stderr", 0)
        
        # Check log file
        log_file = os.path.join(temp_dir, "log.txt")
        if not os.path.exists(log_file):
            print("❌ Log file not created")
            return False
        
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        print(f"✓ Log file created with {len(lines)} lines")
        
        # Parse react_step entries
        react_steps = []
        console_logs = []
        
        for line in lines:
            try:
                data = json.loads(line.strip())
                if data.get("type") == "react_step":
                    react_steps.append(data)
                else:
                    console_logs.append(data)
            except json.JSONDecodeError:
                continue
        
        print(f"✓ Found {len(react_steps)} react_step entries (frontend only)")
        print(f"✓ Found {len(console_logs)} console log entries")
        
        # Verify no duplication
        step_types = [step.get("step_type", "").upper() for step in react_steps]
        console_levels = [log.get("level", "").upper() for log in console_logs]
        
        print(f"✓ Step types: {step_types}")
        print(f"✓ Console levels: {console_levels}")
        
        # Verify separation
        react_step_messages = [step.get("content", "") for step in react_steps]
        console_messages = [log.get("message", "") for log in console_logs]
        
        # Check that reasoning steps don't appear in console logs
        for step_msg in react_step_messages:
            if any(step_msg in console_msg for console_msg in console_messages):
                print(f"❌ Duplication found: '{step_msg}' appears in both frontend and console")
                return False
        
        print("✓ No duplication detected - proper separation maintained")
        
        # Test error log file
        error_file = os.path.join(temp_dir, "error.log")
        if os.path.exists(error_file):
            with open(error_file, "r", encoding="utf-8") as f:
                error_lines = f.readlines()
            print(f"✓ Error log file created with {len(error_lines)} lines")
        else:
            print("✓ No error log file (no errors occurred)")
        
        print("🎉 Standardized logging system test completed successfully!")
        return True
        
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        return False
    finally:
        # Cleanup
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        if original_workspace:
            os.environ["WORKSPACE"] = original_workspace

if __name__ == "__main__":
    success = test_logging_standardization()
    exit(0 if success else 1) 