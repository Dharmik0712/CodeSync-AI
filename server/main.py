from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict
import json
import firebase_admin
from firebase_admin import auth, credentials
import redis
import uuid
from datetime import datetime

cred = credentials.Certificate("./codepilot-live-3dc47-firebase-adminsdk-fbsvc-870fdb59cc.json")
firebase_admin.initialize_app(cred)
redis_client = redis.Redis(host="localhost", port=6379, db=0)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

rooms: Dict[str, Dict[str, WebSocket]] = {}

async def verify_token(token: str) -> str:
    try:
        decoded_token = auth.verify_id_token(token)
        return decoded_token["uid"]
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

@app.websocket("/ws/{room_id}/{client_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, client_id: str):
    await websocket.accept()
    try:
        token = await websocket.receive_text()
        uid = await verify_token(token)
        if uid != client_id:
            await websocket.close(code=1008, reason="Client ID mismatch")
            return
        
        if room_id not in rooms:
            rooms[room_id] = {}
            if not redis_client.exists(f"room:{room_id}:files:main.js"):
                redis_client.set(f"room:{room_id}:files:main.js", "// Start coding here\n")
        
        rooms[room_id][uid] = websocket
        files = {key.decode().split("files:")[1]: redis_client.get(key).decode() 
                 for key in redis_client.keys(f"room:{room_id}:files:*")}
        await websocket.send_text(json.dumps({"type": "init", "files": files}))

    except Exception as e:
        await websocket.close(code=1008, reason="Authentication failed")
        return

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            if message["type"] == "code_update":
                file_key = f"room:{room_id}:files:{message['file']}"
                version_key = f"room:{room_id}:versions:{message['file']}"
                old_content = redis_client.get(file_key).decode() if redis_client.exists(file_key) else ""
                redis_client.set(file_key, message["code"])
                # Store version with timestamp
                version_data = json.dumps({
                    "code": old_content,
                    "timestamp": datetime.utcnow().isoformat()
                })
                redis_client.lpush(version_key, version_data)
                redis_client.ltrim(version_key, 0, 9)  # Keep only last 10 versions
            elif message["type"] == "cursor_update":
                redis_client.hset(f"room:{room_id}:cursors", uid, json.dumps(message["position"]))
            elif message["type"] == "new_file":
                redis_client.set(f"room:{room_id}:files:{message['file']}", "// New file\n")
            elif message["type"] == "rename_file":
                old_key = f"room:{room_id}:files:{message['old_name']}"
                new_key = f"room:{room_id}:files:{message['new_name']}"
                if redis_client.exists(old_key):
                    redis_client.rename(old_key, new_key)
                    # Rename version history too
                    old_version_key = f"room:{room_id}:versions:{message['old_name']}"
                    new_version_key = f"room:{room_id}:versions:{message['new_name']}"
                    if redis_client.exists(old_version_key):
                        redis_client.rename(old_version_key, new_version_key)
            elif message["type"] == "delete_file":
                redis_client.delete(f"room:{room_id}:files:{message['file']}")
                redis_client.delete(f"room:{room_id}:versions:{message['file']}")
            elif message["type"] == "get_versions":
                version_key = f"room:{room_id}:versions:{message['file']}"
                versions = [json.loads(v.decode()) for v in redis_client.lrange(version_key, 0, -1)]
                await websocket.send_text(json.dumps({"type": "versions", "file": message["file"], "versions": versions}))
            elif message["type"] == "revert_version":
                file_key = f"room:{room_id}:files:{message['file']}"
                version_key = f"room:{room_id}:versions:{message['file']}"
                versions = redis_client.lrange(version_key, 0, -1)
                if versions and message["index"] < len(versions):
                    version_data = json.loads(versions[message["index"]].decode())
                    redis_client.set(file_key, version_data["code"])
                    message["code"] = version_data["code"]
            for client in rooms[room_id].values():
                await client.send_text(json.dumps(message))
    except WebSocketDisconnect:
        del rooms[room_id][uid]
        if not rooms[room_id]:
            del rooms[room_id]
        for client in rooms[room_id].values():
            await client.send_text(json.dumps({"type": "user_left", "client_id": uid}))

@app.get("/create-room")
async def create_room():
    room_id = str(uuid.uuid4())
    return {"room_id": room_id}