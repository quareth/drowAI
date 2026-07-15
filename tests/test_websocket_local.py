"""
Test WebSocket connection for local development environment
Backend on port 8000, Frontend on port 5000
"""
import asyncio
import websockets
import json
import requests
from datetime import datetime

async def test_local_websocket():
    """Test WebSocket connection in local environment"""
    print("🏠 Testing WebSocket connection for local environment...")
    print("📋 Backend: localhost:8000")
    print("📋 Frontend: localhost:5000")
    
    # Step 1: Register/Login to get authentication token
    auth_url = "http://localhost:8000/api/auth/login"
    auth_data = {
        "username": "testuser",
        "password": "testpass123"
    }
    
    try:
        print("🔐 Attempting login...")
        response = requests.post(auth_url, json=auth_data, timeout=10)
        
        if response.status_code == 200:
            token_data = response.json()
            token = token_data["access_token"]
            print(f"✅ Got authentication token: {token[:20]}...")
        else:
            print(f"❌ Login failed: {response.status_code} - {response.text}")
            return
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Connection error during login: {e}")
        return
    
    # Step 2: Test WebSocket connection
    # Local environment: both backend and WebSocket on localhost:8000
    ws_url = "ws://localhost:8000/ws?type=docker&taskId=1"
    print(f"🔌 Connecting to WebSocket: {ws_url}")
    
    try:
        async with websockets.connect(ws_url, subprotocols=[f"Bearer.{token}"]) as websocket:
            print("✅ WebSocket connected successfully!")
            
            # Listen for initial messages
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                data = json.loads(message)
                print(f"📥 Received: {data}")
            except asyncio.TimeoutError:
                print("⏰ No initial message received (timeout)")
            
            # Send a test message
            test_message = {
                "type": "ping",
                "timestamp": datetime.now().isoformat()
            }
            await websocket.send(json.dumps(test_message))
            print(f"📤 Sent: {test_message}")
            
            # Listen for responses
            for i in range(3):
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=3.0)
                    data = json.loads(message)
                    print(f"📥 Received: {data}")
                except asyncio.TimeoutError:
                    print(f"⏰ No response #{i+1} (timeout)")
                    break
                except Exception as e:
                    print(f"❌ Error receiving message: {e}")
                    break
            
            print("✅ WebSocket test completed successfully!")
            
    except websockets.exceptions.ConnectionClosed as e:
        print(f"❌ WebSocket connection closed: {e}")
    except websockets.exceptions.InvalidURI as e:
        print(f"❌ Invalid WebSocket URI: {e}")
    except Exception as e:
        print(f"❌ WebSocket connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_local_websocket())
