#!/usr/bin/env python3
"""Test that completion reports are properly logged for the frontend."""

import os
import json
import tempfile
import shutil
from agent.logger import UnifiedAgentLogger

def test_completion_report_logging():
    """Test that completion reports are properly logged for the frontend."""
    
    # Create a temporary workspace
    temp_dir = tempfile.mkdtemp()
    original_workspace = os.environ.get("WORKSPACE")
    os.environ["WORKSPACE"] = temp_dir
    
    try:
        # Create logger
        logger = UnifiedAgentLogger("test-task-789")
        
        print("🧪 Testing Completion Report Logging...")
        
        # Simulate the completion report logging that was added back
        completion_reasoning = (
            "🎯 TASK COMPLETION DETECTED\n\n"
            "The reconnaissance objectives have been successfully accomplished. "
            "I have discovered sufficient findings to compile a comprehensive security assessment report. "
            "The scope requirements have been met and no further actions are necessary."
        )
        logger.log_reasoning_step("thought", completion_reasoning)
        
        # Log the final report
        md_report = """# Security Assessment Report

## Executive Summary
This penetration testing engagement successfully identified open ports and services on the target system.

## Findings
- Open port 22/tcp (SSH)
- Open port 80/tcp (HTTP)
- Host is up and responsive

## Recommendations
- Review service configurations
- Implement proper access controls
- Monitor for unauthorized access attempts
"""
        logger.log_reasoning_step("report", "📝 FINAL MARKDOWN REPORT")
        logger.log_reasoning_step("report", md_report)
        
        # Log final completion message
        final_completion_msg = (
            "✅ TASK COMPLETED SUCCESSFULLY\n\n"
            "The penetration testing reconnaissance has been completed successfully. "
            "A comprehensive security assessment report has been generated and saved. "
            "All objectives have been met and the agent is terminating gracefully."
        )
        logger.log_reasoning_step("thought", final_completion_msg)
        
        # Log completion signal
        logger.log_reasoning_step("complete", "COMPLETE")
        
        # Log completion summary
        logger.log_reasoning_step("completion", "🎯 TASK COMPLETION SUMMARY", metadata={
            "iterations": 3,
            "findings_count": 3,
            "actions_count": 2,
            "duration_minutes": 5,
            "objectives_met": True,
            "targets_tested": True
        })
        
        summary = """🎯 TASK COMPLETION SUMMARY
================================================================================

📋 TASK OVERVIEW:
   • Targets: scanme.nmap.org
   • Objectives: Network reconnaissance, port scanning
   • Duration: 5 minutes
   • Iterations: 3

🔧 ACTIONS EXECUTED:
   1. scan_ports on scanme.nmap.org
   2. service_enumeration on scanme.nmap.org

🔍 FINDINGS DISCOVERED (3 total):
   1. ℹ️ Open port 22/tcp
   2. ℹ️ Open port 80/tcp
   3. ℹ️ Host is up

✅ COMPLETION STATUS:
   • ✅ Objectives successfully accomplished
   • ✅ All targets tested

🏁 TASK COMPLETED SUCCESSFULLY
   • Agent terminated gracefully after 3 iterations
   • Total execution time: 5 minutes
   • Findings generated: 3
   • Actions performed: 2"""
        
        logger.log_reasoning_step("completion", summary, metadata={
            "iterations": 3,
            "findings_count": 3,
            "actions_count": 2,
            "duration_minutes": 5,
            "objectives_met": True,
            "targets_tested": True
        })
        
        # Also log operational status for console
        logger.log_operation("INFO", "Task completed successfully - report generated and saved")
        
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
        
        # Check for completion report entries
        step_types = [step.get("step_type", "").upper() for step in react_steps]
        print(f"✓ Step types: {step_types}")
        
        # Verify completion report is present
        completion_steps = [step for step in react_steps if step.get("step_type") in ["completion", "report", "complete"]]
        if len(completion_steps) >= 4:  # Should have at least 4 completion-related steps
            print("✓ Completion report properly logged for frontend")
        else:
            print(f"❌ Expected 4+ completion steps, found {len(completion_steps)}")
            return False
        
        # Check for specific completion messages
        completion_messages = [step.get("content", "") for step in completion_steps]
        expected_messages = [
            "🎯 TASK COMPLETION DETECTED",
            "📝 FINAL MARKDOWN REPORT",
            "✅ TASK COMPLETED SUCCESSFULLY",
            "COMPLETE",
            "🎯 TASK COMPLETION SUMMARY"
        ]
        
        found_expected = 0
        for expected in expected_messages:
            if any(expected in msg for msg in completion_messages):
                found_expected += 1
        
        if found_expected >= 4:  # Should find most expected messages
            print(f"✓ Found {found_expected}/5 expected completion messages")
        else:
            print(f"❌ Expected 4+ completion messages, found {found_expected}")
            return False
        
        print("🎉 Completion report logging test completed successfully!")
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
    success = test_completion_report_logging()
    exit(0 if success else 1) 