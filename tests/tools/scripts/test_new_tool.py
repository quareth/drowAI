#!/usr/bin/env python
"""
Test runner for newly added tools.

Usage:
    # Test a single tool
    python -m tests.tools.scripts.test_new_tool information_gathering.dns.mytool
    
    # Test multiple tools
    python -m tests.tools.scripts.test_new_tool tool1 tool2 tool3
    
    # Test with verbose output
    python -m tests.tools.scripts.test_new_tool -v information_gathering.dns.mytool
    
    # Run all test categories
    python -m tests.tools.scripts.test_new_tool --all information_gathering.dns.mytool
"""

import argparse
import subprocess
import sys
from typing import List


def run_pytest(tool_ids: List[str], test_type: str, verbose: bool = False) -> int:
    """Run pytest for specific tools and test type."""
    
    # Build the -k filter for tool IDs
    tool_filter = " or ".join(tool_ids)
    
    cmd = [
        sys.executable, "-m", "pytest",
        f"tests/tools/contracts/{test_type}",
        "-k", tool_filter,
    ]
    
    if verbose:
        cmd.append("-v")
    else:
        cmd.append("-q")
    
    cmd.append("--tb=short")
    
    print(f"\n{'='*60}")
    print(f"Running: {test_type}")
    print(f"Tools: {', '.join(tool_ids)}")
    print(f"{'='*60}\n")
    
    result = subprocess.run(cmd)
    return result.returncode


def run_all_tests(tool_ids: List[str], verbose: bool = False) -> int:
    """Run all test types for specified tools."""
    
    # Convert tool IDs to pytest filter patterns
    # e.g., "information_gathering.dns.amass" -> "amass"
    tool_names = [tid.split(".")[-1] for tid in tool_ids]
    
    test_types = [
        ("test_information_gathering.py", "Core contracts (information gathering)"),
        ("test_password_attacks.py", "Core contracts (password attacks)"),
        ("test_web_applications.py", "Core contracts (web applications)"),
        ("test_command_correctness.py", "Command correctness"),
        ("test_value_validation.py", "Value validation"),
        ("test_output_accuracy.py", "Output accuracy"),
        ("test_security.py", "Security checks"),
    ]
    
    tool_filter = " or ".join(tool_names)
    total_failures = 0
    results = []
    
    for test_file, description in test_types:
        cmd = [
            sys.executable, "-m", "pytest",
            f"tests/tools/contracts/{test_file}",
            "-k", tool_filter,
        ]
        
        if verbose:
            cmd.append("-v")
        else:
            cmd.append("-q")
        
        cmd.append("--tb=short")
        
        print(f"\n{'='*60}")
        print(f"Running: {description}")
        print(f"{'='*60}")
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        output = result.stdout or ""
        
        # Check if any tests actually ran
        no_tests_ran = "deselected" in output and ("0 passed" in output or "passed" not in output.lower() or all(x not in output for x in ["passed", "failed"]))
        has_failures = "failed" in output.lower() and "0 failed" not in output.lower()
        
        if has_failures:
            total_failures += 1
            results.append((description, "FAILED"))
            if not verbose:
                print(output)
                print(result.stderr or "")
        elif no_tests_ran or "deselected" in output and "passed" not in output:
            results.append((description, "SKIPPED (no matching tests)"))
            if not verbose:
                # Show summary line
                for line in output.split("\n"):
                    if "deselected" in line or "warning" in line.lower():
                        print(line)
                        break
        else:
            results.append((description, "PASSED"))
            if not verbose:
                # Show summary line
                for line in output.split("\n"):
                    if "passed" in line or "failed" in line or "skipped" in line:
                        print(line)
    
    # Print summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    passed_count = 0
    skipped_count = 0
    for desc, status in results:
        if "PASSED" in status:
            icon = "[OK]"
            passed_count += 1
        elif "SKIPPED" in status:
            icon = "[--]"
            skipped_count += 1
        else:
            icon = "[X]"
        print(f"{icon} {desc}: {status}")
    
    tested = len(results) - skipped_count
    print(f"\nTotal: {passed_count}/{tested} passed ({skipped_count} skipped)")
    
    return total_failures


def check_fixtures_exist(tool_id: str) -> dict:
    """Check if required fixtures exist for a tool."""
    import os
    
    # Convert tool_id to fixture filename
    fixture_name = tool_id.replace(".", "_")
    
    results = {
        "params": os.path.exists(f"tests/tools/fixtures/params/{fixture_name}.json"),
        "output": os.path.exists(f"tests/tools/fixtures/outputs/{fixture_name}.txt"),
    }
    
    return results


def get_status_icon(passed: bool) -> str:
    """Get status icon (ASCII-safe for Windows)."""
    return "[OK]" if passed else "[X]"


def main():
    parser = argparse.ArgumentParser(
        description="Run test suite for specific tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Test a single tool
    python -m tests.tools.scripts.test_new_tool information_gathering.dns.amass
    
    # Test multiple tools
    python -m tests.tools.scripts.test_new_tool \\
        information_gathering.dns.amass \\
        information_gathering.dns.dnsrecon
    
    # Run with verbose output
    python -m tests.tools.scripts.test_new_tool -v information_gathering.dns.amass
    
    # Check fixtures before testing
    python -m tests.tools.scripts.test_new_tool --check information_gathering.dns.amass
        """
    )
    
    parser.add_argument(
        "tool_ids",
        nargs="+",
        help="Tool IDs to test (e.g., information_gathering.dns.amass)"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check if fixtures exist before running tests"
    )
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="Run only schema contract tests"
    )
    parser.add_argument(
        "--command-only",
        action="store_true",
        help="Run only command contract tests"
    )
    parser.add_argument(
        "--security-only",
        action="store_true",
        help="Run only security tests"
    )
    
    args = parser.parse_args()
    
    # Check fixtures if requested
    if args.check:
        print("Checking fixtures...")
        all_exist = True
        for tool_id in args.tool_ids:
            fixtures = check_fixtures_exist(tool_id)
            print(f"\n{tool_id}:")
            for fixture_type, exists in fixtures.items():
                icon = get_status_icon(exists)
                print(f"  {icon} {fixture_type} fixture: {'exists' if exists else 'MISSING'}")
                if not exists:
                    all_exist = False
        
        if not all_exist:
            print("\n[!] Some fixtures are missing. Create them before running tests.")
            print("\nTo create fixtures, add:")
            print("  - tests/tools/fixtures/params/{tool_id}.json")
            print("  - tests/tools/fixtures/outputs/{tool_id}.txt")
            return 1
        print("\n[OK] All fixtures exist!")
    
    # Run specific test type if requested
    if args.schema_only:
        # Determine which contract file to use based on tool category
        tool_names = [tid.split(".")[-1] for tid in args.tool_ids]
        tool_filter = " or ".join(tool_names)
        
        cmd = [
            sys.executable, "-m", "pytest",
            "tests/tools/contracts/",
            "-k", f"schema_contract and ({tool_filter})",
            "-v" if args.verbose else "-q",
            "--tb=short"
        ]
        return subprocess.run(cmd).returncode
    
    if args.command_only:
        tool_names = [tid.split(".")[-1] for tid in args.tool_ids]
        tool_filter = " or ".join(tool_names)
        
        cmd = [
            sys.executable, "-m", "pytest",
            "tests/tools/contracts/",
            "-k", f"command_contract and ({tool_filter})",
            "-v" if args.verbose else "-q",
            "--tb=short"
        ]
        return subprocess.run(cmd).returncode
    
    if args.security_only:
        tool_names = [tid.split(".")[-1] for tid in args.tool_ids]
        tool_filter = " or ".join(tool_names)
        
        cmd = [
            sys.executable, "-m", "pytest",
            "tests/tools/contracts/test_security.py",
            "-k", tool_filter,
            "-v" if args.verbose else "-q",
            "--tb=short"
        ]
        return subprocess.run(cmd).returncode
    
    # Run all tests
    return run_all_tests(args.tool_ids, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
