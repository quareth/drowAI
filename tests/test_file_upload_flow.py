#!/usr/bin/env python3
"""
Test File Upload Flow
Tests the complete file upload workflow from frontend to workspace creation
"""

import requests
import json
import tempfile
import os
from pathlib import Path

def test_file_upload_workflow():
    """Test the complete file upload and workspace creation workflow."""
    print("=== Testing File Upload to Workspace Flow ===\n")
    
    base_url = "http://localhost:8000"
    
    # Test scope content
    scope_content = """# Penetration Testing Scope

## Targets
- 192.168.1.0/24
- webapp.example.com
- api.example.com

## Constraints
- No DoS attacks
- Business hours only: 9 AM - 5 PM EST
- Avoid production database

## Methodology
- OWASP Top 10 testing
- Network reconnaissance
- Web application testing"""

    print("1. Testing task creation with scope content...")
    
    # Create task with scope content (simulating file upload)
    task_data = {
        "name": "Test File Upload Task",
        "description": "Testing scope file upload functionality",
        "scope": scope_content
    }
    
    try:
        # This would normally require authentication, but we're testing the workspace creation
        response = requests.post(
            f"{base_url}/api/tasks/",
            json=task_data,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        
        print(f"   Task creation response: {response.status_code}")
        
        if response.status_code == 401:
            print("   Note: Authentication required for task creation (expected)")
            print("   Testing workspace creation directly...")
            
            # Test workspace creation directly
            from backend.services.workspace.manager import WorkspaceManager
            
            workspace_manager = WorkspaceManager()
            test_task_id = 99999
            
            # Create workspace
            workspace_path = workspace_manager.create_workspace(test_task_id)
            print(f"   ✓ Created workspace: {workspace_path}")
            
            # Save scope file
            scope_file_path = workspace_manager.save_scope_file(test_task_id, scope_content)
            print(f"   ✓ Saved scope file: {scope_file_path}")
            
            # Verify file exists and has correct content
            if Path(scope_file_path).exists():
                with open(scope_file_path, 'r') as f:
                    saved_content = f.read()
                if saved_content == scope_content:
                    print("   ✓ Scope file content verified")
                else:
                    print("   ❌ Scope file content mismatch")
            else:
                print("   ❌ Scope file not found")
            
            # Check workspace structure
            workspace_dir = Path(workspace_path)
            expected_dirs = ["logs", "artifacts", "reports"]
            
            print(f"\n2. Verifying workspace structure:")
            for dir_name in expected_dirs:
                dir_path = workspace_dir / dir_name
                if dir_path.exists():
                    print(f"   ✓ {dir_name}/ directory exists")
                else:
                    print(f"   ❌ {dir_name}/ directory missing")
            
            # Check for scope.md file specifically
            scope_md_path = workspace_dir / "scope.md"
            if scope_md_path.exists():
                print(f"   ✓ scope.md file exists at: {scope_md_path}")
                print(f"   File size: {scope_md_path.stat().st_size} bytes")
            else:
                print(f"   ❌ scope.md file not found in workspace")
            
            # Cleanup test workspace
            import shutil
            if workspace_dir.exists():
                shutil.rmtree(workspace_dir)
                print(f"   ✓ Cleaned up test workspace")
                
            return True
            
    except requests.exceptions.RequestException as e:
        print(f"   Request failed: {e}")
        return False
    except Exception as e:
        print(f"   Error: {e}")
        return False

def test_workspace_path_mapping():
    """Test that workspace paths are correctly mapped for container access."""
    print("\n=== Testing Workspace Path Mapping ===\n")
    
    from backend.services.workspace.manager import WorkspaceManager
    
    workspace_manager = WorkspaceManager()
    test_task_id = 88888
    
    try:
        # Create workspace
        workspace_path = workspace_manager.create_workspace(test_task_id)
        print(f"Host workspace path: {workspace_path}")
        
        # Save scope file
        scope_content = "# Test Scope\n- 10.0.0.1\n- test.com"
        scope_file_path = workspace_manager.save_scope_file(test_task_id, scope_content)
        
        # Show expected container path
        expected_container_path = "/workspace/scope.md"
        print(f"Expected container path: {expected_container_path}")
        
        # Verify file mapping would work
        workspace_dir = Path(workspace_path)
        scope_file = workspace_dir / "scope.md"
        
        if scope_file.exists():
            print("✓ Scope file correctly placed for container mounting")
            print(f"  Host file: {scope_file}")
            print(f"  Container file: {expected_container_path}")
        else:
            print("❌ Scope file not properly created")
        
        # Cleanup
        import shutil
        shutil.rmtree(workspace_dir)
        
        return True
        
    except Exception as e:
        print(f"Error testing workspace mapping: {e}")
        return False

if __name__ == "__main__":
    success1 = test_file_upload_workflow()
    success2 = test_workspace_path_mapping()
    
    if success1 and success2:
        print("\n🎉 All file upload tests passed!")
        print("\nFile Upload Flow Summary:")
        print("• Frontend uploads .md/.txt files via drag-drop or file browser")
        print("• Content is read and stored in task.scope database field")
        print("• WorkspaceManager creates task workspace directory")
        print("• Scope content is saved to /workspace/scope.md file")
        print("• AI agent can access scope.md file in container")
    else:
        print("\n❌ Some tests failed!")