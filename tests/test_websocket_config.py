#!/usr/bin/env python3
"""
Test Environment-Agnostic WebSocket Configuration
Tests the new centralized WebSocket configuration system
"""
import asyncio
import websockets
import json
import os
import sys

async def test_websocket_config():
    """Test the environment-agnostic WebSocket configuration"""
    print("🔧 Testing Environment-Agnostic WebSocket Configuration")
    
    # Get backend URL from environment
    backend_url = os.getenv('BACKEND_URL', 'http://localhost:8000')
    print(f"📡 Backend URL: {backend_url}")
    
    # Test authentication first
    import requests
    
    # Create test user credentials
    username = "testuser"
    password = "testpass123"
    email = "test@example.com"
    
    print("🔐 Testing authentication flow...")
    
    # Register user
    try:
        register_response = requests.post(f"{backend_url}/api/auth/register", json={
            "username": username,
            "email": email,
            "password": password
        })
        print(f"✅ Registration: {register_response.status_code}")
    except Exception as e:
        print(f"⚠️ Registration failed: {e}")
    
    # Login to get token
    try:
        login_response = requests.post(f"{backend_url}/api/auth/login", json={
            "username": username,
            "password": password
        })
        
        if login_response.status_code == 200:
            token = login_response.json().get('access_token')
            print(f"✅ Login successful, token: {token[:20]}...")
        else:
            print(f"❌ Login failed: {login_response.status_code}")
            return
    except Exception as e:
        print(f"❌ Login error: {e}")
        return
    
    # Test WebSocket connections
    ws_base_url = backend_url.replace('http://', 'ws://').replace('https://', 'wss://')
    
    # Test cases for different connection types
    test_cases = [
        {
            'type': 'agent',
            'task_id': 1,
            'url': f"{ws_base_url}/ws?type=agent&taskId=1&token={token}"
        },
        {
            'type': 'terminal',
            'task_id': 1,
            'url': f"{ws_base_url}/ws?type=terminal&taskId=1&token={token}"
        },
        {
            'type': 'docker',
            'task_id': 1,
            'url': f"{ws_base_url}/ws?type=docker&taskId=1&token={token}"
        }
    ]
    
    print("\n🌐 Testing WebSocket connections...")
    
    for test_case in test_cases:
        print(f"\n📡 Testing {test_case['type']} WebSocket connection...")
        print(f"🔗 URL: {test_case['url']}")
        
        try:
            async with websockets.connect(test_case['url']) as websocket:
                print(f"✅ {test_case['type']} connection established")
                
                # Send test message
                test_message = {
                    "type": "test",
                    "message": f"Hello from {test_case['type']} client"
                }
                await websocket.send(json.dumps(test_message))
                print(f"📤 Sent test message")
                
                # Wait for response
                try:
                    response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                    print(f"📥 Received: {response}")
                except asyncio.TimeoutError:
                    print("⏱️ No response received (timeout)")
                
        except Exception as e:
            print(f"❌ {test_case['type']} connection failed: {e}")
    
    print("\n🎯 Environment-agnostic WebSocket configuration test completed!")

if __name__ == "__main__":
    asyncio.run(test_websocket_config())