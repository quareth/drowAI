"""
Test WebSocket with proper authentication flow
"""
import asyncio
import websockets
import json
import requests

async def test_websocket_with_auth():
    """Test WebSocket connection with real authentication"""
    
    # First, register a test user
    register_data = {
        "username": "testuser",
        "password": "testpass123",
        "email": "test@example.com"
    }
    
    try:
        # Register user first
        print("👤 Registering test user...")
        register_response = requests.post("http://localhost:8000/api/auth/register", json=register_data)
        
        if register_response.status_code == 201:
            print("✅ User registered successfully")
            token_data = register_response.json()
            token = token_data.get("access_token")
        elif register_response.status_code == 409:
            print("👤 User already exists, attempting login...")
            # User exists, try to login
            login_data = {
                "username": "testuser",
                "password": "testpass123"
            }
            response = requests.post("http://localhost:8000/api/auth/login", json=login_data)
            
            if response.status_code != 200:
                print(f"❌ Login failed: {response.status_code} - {response.text}")
                return
            
            token_data = response.json()
            token = token_data.get("access_token")
        else:
            print(f"❌ Registration failed: {register_response.status_code} - {register_response.text}")
            return
        
        if not token:
            print(f"❌ No token in response: {token_data}")
            return
            
        print(f"✅ Got authentication token: {token[:20]}...")
        
        # Now test WebSocket connection
        ws_url = "ws://localhost:8000/ws?type=docker&taskId=1"
        print(f"🔌 Connecting to WebSocket: {ws_url}")

        async with websockets.connect(ws_url, subprotocols=[f"Bearer.{token}"]) as websocket:
            print("✅ WebSocket connected successfully!")
            
            # Listen for initial messages
            try:
                for i in range(3):  # Listen for a few messages
                    message = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                    data = json.loads(message)
                    print(f"📥 Received: {data}")
                    
                    # Send a test message
                    test_msg = {"type": "ping", "timestamp": str(asyncio.get_event_loop().time())}
                    await websocket.send(json.dumps(test_msg))
                    print(f"📤 Sent: {test_msg}")
                    
            except asyncio.TimeoutError:
                print("⏱️ Timeout waiting for messages (this is normal)")
                
        print("✅ WebSocket test completed successfully!")
        
    except Exception as e:
        print(f"❌ WebSocket test failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_websocket_with_auth())
