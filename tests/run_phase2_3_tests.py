#!/usr/bin/env python3
"""Test runner for and tests."""

import sys
import os
import subprocess
import pytest
from pathlib import Path

def main():
    """Run all Phase 2 and Phase 3 tests."""
    # Add the project root to the Python path
    project_root = Path(__file__).parent.parent
    sys.path.insert(0, str(project_root))
    
    # Define test files for Phase 2 and Phase 3
    phase2_tests = [
        "tests/test_phase2_reasoning_engine.py",
        "tests/test_phase2_completion_cleanup.py",
        "tests/test_phase2_integration.py"
    ]
    
    phase3_tests = [
        "tests/test_phase3_interactive_providers.py",
        "tests/test_phase3_user_input_manager.py",
        "tests/test_phase3_chat_handler.py",
        "tests/test_phase3_integration.py"
    ]
    
    all_tests = phase2_tests + phase3_tests
    
    print("=" * 80)
    print("RUNNING PHASE 2 AND PHASE 3 TESTS")
    print("=" * 80)
    print()
    
    # Run Phase 2 tests
    print("PHASE 2: Clean Up Reasoning Engine")
    print("-" * 40)
    phase2_passed = 0
    phase2_failed = 0
    
    for test_file in phase2_tests:
        if os.path.exists(test_file):
            print(f"Running {test_file}...")
            result = pytest.main([test_file, "-v", "--tb=short"])
            if result == 0:
                phase2_passed += 1
                print(f"✅ {test_file} - PASSED")
            else:
                phase2_failed += 1
                print(f"❌ {test_file} - FAILED")
        else:
            print(f"⚠️  {test_file} - NOT FOUND")
        print()
    
    # Run Phase 3 tests
    print("PHASE 3: Interactive Mode Implementation")
    print("-" * 40)
    phase3_passed = 0
    phase3_failed = 0
    
    for test_file in phase3_tests:
        if os.path.exists(test_file):
            print(f"Running {test_file}...")
            result = pytest.main([test_file, "-v", "--tb=short"])
            if result == 0:
                phase3_passed += 1
                print(f"✅ {test_file} - PASSED")
            else:
                phase3_failed += 1
                print(f"❌ {test_file} - FAILED")
        else:
            print(f"⚠️  {test_file} - NOT FOUND")
        print()
    
    # Summary
    print("=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    print(f"Phase 2 Tests: {phase2_passed} passed, {phase2_failed} failed")
    print(f"Phase 3 Tests: {phase3_passed} passed, {phase3_failed} failed")
    print(f"Total Tests: {phase2_passed + phase3_passed} passed, {phase2_failed + phase3_failed} failed")
    
    if phase2_failed == 0 and phase3_failed == 0:
        print("\n🎉 ALL TESTS PASSED!")
        return 0
    else:
        print(f"\n❌ {phase2_failed + phase3_failed} TESTS FAILED")
        return 1

if __name__ == "__main__":
    sys.exit(main())
