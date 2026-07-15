#!/usr/bin/env python3
"""Developer smoke test for complete local Docker workspace isolation.

This script verifies direct local-provider workspace content for dev/test
diagnostics only. It is not product task execution proof; product tasks are
expected to use runner placement.
"""

import asyncio
import logging
from backend.services.unified_docker_service import unified_docker_service

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_complete_workspace_isolation():
    """Test that containers see actual task content, not generic directories"""
    print("=" * 60)
    print("Complete Workspace Isolation Test")
    print("=" * 60)
    
    # Use existing task with real content
    test_task_id = 230
    
    print(f"1. Testing container creation for task {test_task_id}...")
    
    try:
        result = await unified_docker_service.create_and_start_container(test_task_id)
        
        if result.get("success"):
            print("   ✓ Container created successfully")
            
            # Test what's actually visible in the container
            print("2. Testing workspace content visibility...")
            
            container = unified_docker_service.containers.get(test_task_id)
            
            if container and hasattr(container, 'exec_run'):
                # Check workspace content - should see scope.md, not generic dirs
                workspace_cmd = "ls -la /workspace/"
                result = container.exec_run(workspace_cmd)
                workspace_content = result.output.decode()
                print("   Workspace content (/workspace/):")
                print(f"   {workspace_content}")
                
                # Verify scope.md is accessible and contains task-specific content
                scope_cmd = "cat /workspace/scope.md"
                result = container.exec_run(scope_cmd)
                if result.exit_code == 0:
                    scope_content = result.output.decode()
                    print("   ✓ scope.md is accessible")
                    print(f"   Content preview: {scope_content[:100]}...")
                    
                    # Check if it's task-specific content (not generic)
                    if "192.168.1.0/24" in scope_content or "Target:" in scope_content:
                        print("   ✓ Contains task-specific content")
                    else:
                        print("   ❌ Content appears generic")
                else:
                    print("   ❌ scope.md not accessible")
                
                # Verify no generic directories are masking content
                generic_dirs = ["data", "logs", "results", "scripts"]
                has_generic_only = True
                
                for dir_name in generic_dirs:
                    dir_check = f"test -d /workspace/{dir_name} && echo 'exists' || echo 'missing'"
                    result = container.exec_run(dir_check)
                    dir_status = result.output.decode().strip()
                    print(f"   Directory {dir_name}: {dir_status}")
                    
                    if dir_status == "missing":
                        has_generic_only = False
                
                if has_generic_only:
                    print("   ⚠ Container shows only generic directories")
                else:
                    print("   ✓ Container shows actual task content")
                
                # Verify backend/agent source passthrough is not mounted.
                source_passthrough = "/" + "agent_src"
                result = container.exec_run(f"test ! -e {source_passthrough}")
                if result.exit_code == 0:
                    print("   ✓ Backend/agent source passthrough is absent")
                else:
                    print("   ❌ Backend/agent source passthrough is mounted")
            
            # Cleanup
            print("3. Cleaning up test container...")
            stop_success, stop_msg = await unified_docker_service.stop_container(test_task_id)
            if stop_success:
                remove_success, remove_msg = await unified_docker_service.remove_container(test_task_id, force=True)
                print("   ✓ Container cleaned up")
            
        else:
            print(f"   ❌ Container creation failed: {result.get('error')}")
            
    except Exception as e:
        print(f"   ❌ Test failed: {e}")
    
    print(f"\n" + "=" * 60)
    print("Complete Workspace Isolation Test Finished")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(test_complete_workspace_isolation())
