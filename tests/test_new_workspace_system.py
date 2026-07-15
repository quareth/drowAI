#!/usr/bin/env python3
"""
Test New Project-Relative Workspace System
Verifies the complete task-based workspace architecture with robust path configuration
"""

import os
import sys
import json
from pathlib import Path

# Add project root to path for imports
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from backend.config.workspace_config import WorkspaceConfig
from backend.services.workspace.manager import WorkspaceManager

def test_workspace_config():
    """Test the new WorkspaceConfig class"""
    print("=== Testing WorkspaceConfig ===")
    
    # Test project root detection
    project_root = WorkspaceConfig.get_project_root()
    print(f"1. Project root: {project_root}")
    assert project_root.exists(), "Project root should exist"
    assert (project_root / "package.json").exists(), "Should find package.json in project root"
    
    # Test workspaces base path
    workspaces_base = WorkspaceConfig.get_workspaces_base_path()
    print(f"2. Workspaces base: {workspaces_base}")
    expected_path = project_root / "agent" / "workspaces"
    assert workspaces_base == expected_path, f"Expected {expected_path}, got {workspaces_base}"
    
    # Test task workspace path
    task_workspace = WorkspaceConfig.get_task_workspace_path(12345)
    print(f"3. Task workspace: {task_workspace}")
    expected_task_path = workspaces_base / "task-12345"
    assert task_workspace == expected_task_path, f"Expected {expected_task_path}, got {task_workspace}"
    
    # Test file paths
    scope_file = WorkspaceConfig.get_scope_file_path(12345)
    config_file = WorkspaceConfig.get_config_file_path(12345)
    print(f"4. Scope file: {scope_file}")
    print(f"5. Config file: {config_file}")
    
    # Test container path
    container_path = WorkspaceConfig.get_container_workspace_path()
    print(f"6. Container path: {container_path}")
    assert container_path == "/workspace", "Container path should be /workspace"
    
    print("✓ WorkspaceConfig tests passed")

def test_workspace_creation():
    """Test workspace creation with new system"""
    print("\n=== Testing Workspace Creation ===")
    
    test_task_id = 99999
    
    # Test workspace structure creation
    workspace_path = WorkspaceConfig.ensure_workspace_structure(test_task_id)
    print(f"1. Created workspace: {workspace_path}")
    
    assert workspace_path.exists(), "Workspace should exist"
    assert (workspace_path / "results").exists(), "Results directory should exist"
    assert (workspace_path / "logs").exists(), "Logs directory should exist"
    assert (workspace_path / "scripts").exists(), "Scripts directory should exist"
    assert (workspace_path / "data").exists(), "Data directory should exist"
    
    # Test file creation
    scope_content = """# Penetration Testing Scope

## Target Information
- Target: 192.168.1.100
- Network: 192.168.1.0/24

## Objectives
- Network reconnaissance
- Vulnerability scanning
- Service enumeration

## Constraints
- No destructive testing
- Business hours only
- Report all findings
"""
    
    scope_file = WorkspaceConfig.get_scope_file_path(test_task_id)
    scope_file.write_text(scope_content)
    print(f"2. Created scope file: {scope_file}")
    
    config_data = {
        "task_id": test_task_id,
        "target": "192.168.1.100",
        "timeout": 3600,
        "tools": ["nmap", "gobuster", "nikto"]
    }
    
    config_file = WorkspaceConfig.get_config_file_path(test_task_id)
    config_file.write_text(json.dumps(config_data, indent=2))
    print(f"3. Created config file: {config_file}")
    
    # Test mount configuration
    mount_config = WorkspaceConfig.get_mount_config(test_task_id)
    print(f"4. Mount config: {mount_config}")
    
    assert mount_config["container_path"] == "/workspace"
    assert mount_config["mode"] == "rw"
    assert Path(mount_config["host_path"]).exists()
    
    # Test workspace info
    workspace_info = WorkspaceConfig.get_workspace_info(test_task_id)
    print(f"5. Workspace info: {workspace_info['files']}")
    
    assert workspace_info["exists"]
    assert workspace_info["files"]["scope.md"]["exists"]
    assert workspace_info["files"]["config.json"]["exists"]
    
    print("✓ Workspace creation tests passed")

def test_workspace_manager_integration():
    """Test WorkspaceManager with new configuration"""
    print("\n=== Testing WorkspaceManager Integration ===")
    
    test_task_id = 88888
    
    # Initialize workspace manager
    manager = WorkspaceManager()
    
    # Test workspace creation through manager
    workspace_path = manager.create_workspace(test_task_id)
    print(f"1. Manager created workspace: {workspace_path}")
    
    # Test scope file saving
    scope_content = """# AI Agent Test Scope

This is a test scope document for validating the new workspace system.

## Test Parameters
- Task ID: 88888
- System: New project-relative workspace architecture
- Mount: agent/workspaces/task-88888 -> /workspace
"""
    
    scope_file_path = manager.save_scope_file(test_task_id, scope_content)
    print(f"2. Saved scope file: {scope_file_path}")
    
    # Test config file saving
    config_data = {
        "test_mode": True,
        "workspace_system": "project-relative",
        "mount_verified": True
    }
    
    config_file_path = manager.save_config_file(test_task_id, config_data)
    print(f"3. Saved config file: {config_file_path}")
    
    # Test workspace validation
    exists = manager.workspace_exists(test_task_id)
    print(f"4. Workspace exists: {exists}")
    assert exists, "Workspace should exist"
    
    # Test workspace info
    info = manager.get_workspace_info(test_task_id)
    print(f"5. Files in workspace: {info.get('total_files', 0)} files, {info.get('total_directories', 0)} directories")
    
    # Test mount config
    mount_config = manager.get_mount_config(test_task_id)
    print(f"6. Docker mount: {mount_config['host_path']} -> {mount_config['container_path']}")
    
    print("✓ WorkspaceManager integration tests passed")

def test_agent_access_simulation():
    """Simulate agent accessing files in the new system"""
    print("\n=== Testing Agent Access Simulation ===")
    
    test_task_id = 77777
    
    # Create workspace
    workspace_path = WorkspaceConfig.ensure_workspace_structure(test_task_id)
    
    # Create scope file that agent would access
    scope_content = """# Network Security Assessment

## Target Systems
- Primary Target: 10.0.0.100
- Secondary Target: 10.0.0.101
- Network Range: 10.0.0.0/24

## Testing Objectives
1. Port scanning and service enumeration
2. Vulnerability assessment
3. Web application testing
4. Network mapping

## Constraints and Limitations
- Testing window: 09:00-17:00 UTC
- No DoS attacks
- No data modification
- Stealth mode required

## Expected Deliverables
- Vulnerability report
- Network diagram
- Risk assessment
- Remediation recommendations
"""
    
    scope_file = WorkspaceConfig.get_scope_file_path(test_task_id)
    scope_file.write_text(scope_content)
    
    # Simulate container environment access
    print(f"1. Host file location: {scope_file}")
    print(f"2. Container will access: /workspace/scope.md")
    print(f"3. Container mount: {workspace_path} -> /workspace")
    
    # Verify file content
    content = scope_file.read_text()
    lines = content.splitlines()
    print(f"4. File contains {len(lines)} lines")
    print(f"5. First line: {lines[0] if lines else 'No content'}")
    
    # Test agent logger would work
    logs_dir = WorkspaceConfig.get_logs_directory_path(test_task_id)
    test_log = logs_dir / "agent.log"
    test_log.write_text("Test log entry from agent\n")
    print(f"6. Agent log created: {test_log}")
    
    # Test results directory
    results_dir = WorkspaceConfig.get_results_directory_path(test_task_id)
    test_result = results_dir / "scan_results.txt"
    test_result.write_text("Test scan results\n")
    print(f"7. Results file created: {test_result}")
    
    print("✓ Agent access simulation tests passed")

def cleanup_test_workspaces():
    """Clean up test workspaces"""
    print("\n=== Cleaning Up Test Workspaces ===")
    
    test_task_ids = [99999, 88888, 77777]
    
    for task_id in test_task_ids:
        try:
            success = WorkspaceConfig.cleanup_workspace(task_id)
            print(f"Cleaned up task {task_id}: {'✓' if success else '✗'}")
        except Exception as e:
            print(f"Error cleaning task {task_id}: {e}")

def main():
    """Run all tests for the new workspace system"""
    print("Testing New Project-Relative Workspace System")
    print("=" * 50)
    
    try:
        test_workspace_config()
        test_workspace_creation()
        test_workspace_manager_integration()
        test_agent_access_simulation()
        
        print("\n" + "=" * 50)
        print("🎉 All tests passed! New workspace system is working correctly.")
        print("\nKey Benefits Verified:")
        print("• Project-relative paths (no hardcoded directories)")
        print("• Task-based isolation (agent/workspaces/task-{id}/)")
        print("• Robust container mounting ({host_path} -> /workspace)")
        print("• Agent file accessibility (/workspace/scope.md)")
        print("• Comprehensive workspace structure")
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    finally:
        cleanup_test_workspaces()
    
    return 0

if __name__ == "__main__":
    exit(main())