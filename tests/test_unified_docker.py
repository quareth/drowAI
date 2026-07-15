#!/usr/bin/env python3
"""Developer smoke test for Management-owned local Docker service behavior.

This script exercises direct local Docker container management for dev/test
diagnostics only. It is not product task execution proof; product tasks are
expected to use runner placement.
"""

import asyncio
import logging
import sys
from backend.services.unified_docker_service import unified_docker_service

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_unified_docker():
    """Test the unified Docker service functionality"""
    print("=" * 60)
    print("Testing Unified Docker Service")
    print("=" * 60)
    
    test_task_id = 999
    
    try:
        # Test 1: Container creation and startup
        print(f"\n1. Testing container creation for task {test_task_id}")
        result = await unified_docker_service.create_and_start_container(test_task_id)
        
        print(f"   Success: {result.get('success')}")
        print(f"   Container ID: {result.get('container_id')}")
        print(f"   Container Name: {result.get('container_name')}")
        
        if result.get('logs'):
            print("   Logs:")
            for log in result.get('logs', [])[-3:]:  # Show last 3 logs
                print(f"     {log.get('timestamp', '')}: {log.get('message', '')}")
        
        # Test 2: Container status check
        print(f"\n2. Testing container status check")
        status = await unified_docker_service.get_container_status(test_task_id)
        print(f"   Status: {status}")
        
        # Test 3: Container stop (if not simulated)
        if result.get('success') and status != "simulated":
            print(f"\n3. Testing container stop")
            stop_success, stop_message = await unified_docker_service.stop_container(test_task_id)
            print(f"   Stop success: {stop_success}")
            print(f"   Stop message: {stop_message}")
            
            # Test 4: Container removal
            print(f"\n4. Testing container removal")
            remove_success, remove_message = await unified_docker_service.remove_container(test_task_id, force=True)
            print(f"   Remove success: {remove_success}")
            print(f"   Remove message: {remove_message}")
        else:
            print(f"\n3-4. Skipping stop/remove tests (simulation mode or creation failed)")
        
        print(f"\n" + "=" * 60)
        print("Unified Docker Service Test Complete")
        print(f"Overall result: {'PASS' if result.get('success') else 'FAIL'}")
        print("=" * 60)
        
    except Exception as e:
        logger.error(f"Test failed with exception: {e}")
        print(f"\nTest FAILED: {e}")

if __name__ == "__main__":
    asyncio.run(test_unified_docker())
