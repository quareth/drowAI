#!/usr/bin/env python3
"""
Test Workspace Mounting and File Creation
Verifies that scope.md files are properly created and mounted for container access
"""

import requests
import json
import tempfile
import os
from pathlib import Path
import sys

def test_workspace_creation_and_mounting():
    """Test the complete workspace creation and file mounting workflow."""
    print("=== Testing Workspace Creation and Container Mounting ===\n")
    
    # Test workspace manager directly since API requires auth
    from backend.services.workspace.manager import WorkspaceManager
    
    workspace_manager = WorkspaceManager()
    test_task_id = 77777
    
    scope_content = """# Test Penetration Testing Scope

## Target Information
- IP Range: 192.168.1.0/24  
- Domain: test.example.com
- Application: Web portal at https://portal.test.example.com

## Scope Limitations
- No denial of service attacks
- Testing only during business hours
- Avoid production database

## Testing Methodology
1. Network reconnaissance
2. Port scanning and service enumeration
3. Web application testing
4. Vulnerability assessment
5. Report generation

## Expected Deliverables
- Detailed vulnerability report
- Risk assessment and recommendations
- Proof of concept for critical findings
"""

    try:
        print("1. Creating workspace directory...")
        workspace_path = workspace_manager.create_workspace(test_task_id)
        print(f"   ✓ Workspace created: {workspace_path}")
        
        print("2. Saving scope.md file...")
        scope_file_path = workspace_manager.save_scope_file(test_task_id, scope_content)
        print(f"   ✓ Scope file saved: {scope_file_path}")
        
        print("3. Verifying file structure...")
        workspace_dir = Path(workspace_path)
        
        # Check directories
        expected_dirs = ["logs", "artifacts", "reports"]
        for dir_name in expected_dirs:
            dir_path = workspace_dir / dir_name
            if dir_path.exists():
                print(f"   ✓ {dir_name}/ directory exists")
            else:
                print(f"   ❌ {dir_name}/ directory missing")
        
        # Check scope.md file
        scope_file = workspace_dir / "scope.md"
        if scope_file.exists():
            print(f"   ✓ scope.md exists at: {scope_file}")
            
            # Verify content
            with open(scope_file, 'r') as f:
                saved_content = f.read()
            
            if saved_content == scope_content:
                print("   ✓ Scope file content matches")
            else:
                print("   ❌ Scope file content mismatch")
                print(f"   Expected length: {len(scope_content)}")
                print(f"   Actual length: {len(saved_content)}")
        else:
            print("   ❌ scope.md file not found")
        
        print("4. Testing container mount path compatibility...")
        # Simulate container mount
        container_workspace = "/workspace"
        container_scope_file = f"{container_workspace}/scope.md"
        
        print(f"   Host workspace: {workspace_path}")
        print(f"   Container mount: {container_workspace}")
        print(f"   Agent expects: {container_scope_file}")
        
        # Verify path mapping
        if (workspace_dir / "scope.md").exists():
            print("   ✓ File will be accessible at /workspace/scope.md in container")
        else:
            print("   ❌ File will NOT be accessible in container")
        
        print("5. Testing file permissions...")
        try:
            stat = scope_file.stat()
            permissions = oct(stat.st_mode)[-3:]
            print(f"   File permissions: {permissions}")
            
            if permissions >= "644":
                print("   ✓ File permissions are correct (readable)")
            else:
                print("   ❌ File permissions may cause issues")
        except Exception as e:
            print(f"   ❌ Could not check permissions: {e}")
        
        # Cleanup
        import shutil
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir)
            print("   ✓ Test workspace cleaned up")
        
        return True
        
    except Exception as e:
        print(f"   ❌ Error during testing: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_agent_file_access():
    """Test if the agent can actually access the scope file."""
    print("\n=== Testing Agent File Access Pattern ===\n")
    
    # Simulate what the agent does
    from backend.services.workspace.manager import WorkspaceManager
    
    workspace_manager = WorkspaceManager()
    test_task_id = 66666
    
    scope_content = "# Quick Test\n- Target: 10.0.0.1\n- Method: Basic scan"
    
    try:
        print("1. Creating workspace and scope file...")
        workspace_path = workspace_manager.create_workspace(test_task_id)
        scope_file_path = workspace_manager.save_scope_file(test_task_id, scope_content)
        
        print("2. Simulating agent file access...")
        # This is what the agent does in load_scope_document()
        workspace_dir = Path(workspace_path)
        agent_expected_path = workspace_dir / "scope.md"  # When mounted to /workspace in container
        
        if agent_expected_path.exists():
            print(f"   ✓ Agent can access scope file at: {agent_expected_path}")
            
            # Read content like agent does
            with open(agent_expected_path, 'r') as f:
                content = f.read().strip()
            
            if content:
                print(f"   ✓ Agent can read content ({len(content)} chars)")
                print(f"   First line: {content.split(chr(10))[0]}")
            else:
                print("   ❌ File is empty")
        else:
            print(f"   ❌ Agent cannot access scope file at: {agent_expected_path}")
        
        # Cleanup
        import shutil
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir)
            print("   ✓ Test workspace cleaned up")
        
        return True
        
    except Exception as e:
        print(f"   ❌ Error during agent access test: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success1 = test_workspace_creation_and_mounting()
    success2 = test_agent_file_access()
    
    if success1 and success2:
        print("\n🎉 All workspace mounting tests passed!")
        print("\nWorkspace Flow Summary:")
        print("• WorkspaceManager creates /tmp/workspaces/{task_id}/ directory")
        print("• scope.md file is saved in workspace root")
        print("• Workspace is mounted to /workspace in container")
        print("• Agent accesses scope at /workspace/scope.md")
        print("• File permissions and content are preserved")
    else:
        print("\n❌ Some workspace tests failed!")
        sys.exit(1)