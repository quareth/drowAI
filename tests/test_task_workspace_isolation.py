#!/usr/bin/env python3
"""Developer smoke test for local Docker task workspace isolation.

This script verifies direct local-provider workspace mounts for dev/test
diagnostics only. It is not product task execution proof; product tasks are
expected to use runner placement.
"""

import asyncio
import logging
from backend.services.unified_docker_service import unified_docker_service
from backend.config.workspace_config import WorkspaceConfig

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_workspace_isolation():
    """Test that containers only see their own task workspace"""
    print("=" * 60)
    print("Testing Task-Specific Workspace Isolation")
    print("=" * 60)
    
    # Create test workspaces for multiple tasks
    test_tasks = [266, 267, 268]
    
    print("\n1. Creating test workspaces for multiple tasks...")
    for task_id in test_tasks:
        workspace_path = WorkspaceConfig.get_task_workspace_path(task_id)
        workspace_path.mkdir(parents=True, exist_ok=True)
        
        # Create scope.md file for each task
        scope_file = workspace_path / "scope.md"
        scope_content = f"""# Task {task_id} Scope

## Target
192.168.1.{task_id}

## Objectives
- Test task {task_id} specific content
- Verify workspace isolation

## Constraints
- No access to other task workspaces
"""
        scope_file.write_text(scope_content)
        
        # Create task-specific test file
        test_file = workspace_path / f"task_{task_id}_secret.txt"
        test_file.write_text(f"SECRET DATA FOR TASK {task_id} ONLY")
        
        print(f"   ✓ Created workspace for task {task_id}: {workspace_path}")
    
    # Test container creation with isolation
    test_task_id = 266
    print(f"\n2. Testing container creation for task {test_task_id}...")
    
    try:
        result = await unified_docker_service.create_and_start_container(test_task_id)
        
        if result.get("success"):
            print(f"   ✓ Container created successfully")
            
            # Test workspace isolation by checking what's visible in container
            print(f"\n3. Testing workspace isolation in container...")
            
            # Get container for task 266
            container = unified_docker_service.containers.get(test_task_id)
            
            if container:
                # Check what's in /workspace (should only be task 266 content)
                workspace_check_cmd = "ls -la /workspace/"
                
                if hasattr(container, 'exec_run'):
                    result = container.exec_run(workspace_check_cmd)
                    workspace_content = result.output.decode()
                    print(f"   Workspace content (/workspace/):")
                    print(f"   {workspace_content}")
                    
                    # Check if task-specific file exists
                    task_file_check = f"cat /workspace/task_{test_task_id}_secret.txt"
                    result = container.exec_run(task_file_check)
                    if result.exit_code == 0:
                        print(f"   ✓ Task {test_task_id} specific file accessible")
                    else:
                        print(f"   ❌ Task {test_task_id} file not accessible")
                    
                    # Verify isolation: backend/agent source passthrough must be absent.
                    source_passthrough = "/" + "agent_src"
                    result = container.exec_run(f"test ! -e {source_passthrough}")
                    if result.exit_code == 0:
                        print("   ✓ Backend/agent source passthrough is absent")
                    else:
                        print("   ❌ Backend/agent source passthrough is mounted")
                
                # Clean up container
                print(f"\n4. Cleaning up test container...")
                stop_success, stop_msg = await unified_docker_service.stop_container(test_task_id)
                if stop_success:
                    remove_success, remove_msg = await unified_docker_service.remove_container(test_task_id, force=True)
                    print(f"   ✓ Container cleaned up")
            else:
                print(f"   ❌ Container not found in service registry")
        else:
            print(f"   ❌ Container creation failed: {result.get('error')}")
            
    except Exception as e:
        print(f"   ❌ Test failed with exception: {e}")
    
    # Clean up test workspaces
    print(f"\n5. Cleaning up test workspaces...")
    for task_id in test_tasks:
        workspace_path = WorkspaceConfig.get_task_workspace_path(task_id)
        if workspace_path.exists():
            import shutil
            shutil.rmtree(workspace_path)
            print(f"   ✓ Cleaned up workspace for task {task_id}")
    
    print(f"\n" + "=" * 60)
    print("Task Workspace Isolation Test Complete")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(test_workspace_isolation())
