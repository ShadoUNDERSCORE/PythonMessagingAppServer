from datetime import datetime
from fastapi import FastAPI, Response, Request, WebSocket, WebSocketDisconnect
from fastapi.websockets import WebSocketState
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from pymongo import MongoClient
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

mgo_client = MongoClient("mongodb://localhost:27017/")
db = mgo_client["database"]
users = db["users"]
user_contacts = db["user_contacts"]
messages = db["messages"]
undelivered = db["undelivered"]
# print("USERS:", [u for u in users.find()])
# print("CONTACT DB: ", [c for c in user_contacts.find()])
# print("MESSAGES:", [m for m in messages.find()])
# print("UNDELIVERED:", [f for f in undelivered.find()])
# Delete ALL messages or users
# users.drop()
# messages.drop()
# undelivered.drop()


class User(BaseModel):
    username: str
    password: str


class ContactList(BaseModel):
    contacts_of: str
    contacts_list: list


class Message(BaseModel):
    sent_by: str
    sent_to: str
    chat_id: str
    message: str
    timestamp: datetime = datetime.now()


class ChatChunk(BaseModel):
    chat_id: str
    chunk_id: int
    messages: list


class Connection(BaseModel):
    username: str
    websocket: WebSocket

    model_config = ConfigDict(arbitrary_types_allowed=True)


class ConnectionManager:

    def __init__(self):
        self.current_connections: dict[str: WebSocket] = {}

    def add_connection(self, new_connection: Connection):
        self.current_connections[new_connection.username] = new_connection.websocket

    async def remove_connection(self, connection: Connection):
        self.current_connections.pop(connection.username)
        if connection.websocket.client_state == WebSocketState.CONNECTED:
            await connection.websocket.close()

    async def forward_message(self, message: Message, recipient: Connection) -> bool:
        try:
            await recipient.websocket.send_json(message.model_dump(mode="json"))
            return True
        except WebSocketDisconnect:
            print("Recipient Unavailable")
            await self.remove_connection(recipient)
            return False
        except RuntimeError as e:
            print("Runtime Error:", e)
            await self.remove_connection(recipient)
            return False
        except Exception as e:
            print("Unexpected Error:", e)
            return False


manager = ConnectionManager()
app = FastAPI()
ph = PasswordHasher()

# noinspection PyTypeChecker
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # for dev
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/add_contact", status_code=201)
def add_contact(for_user: str, contact_name: str, response: Response):
    current_contacts = user_contacts.find_one({"contacts_of": for_user})
    if current_contacts:
        current_contacts = ContactList.model_validate(current_contacts)
        if contact_name in current_contacts.contacts_list:
            response.status_code = 409
            return "Already a Contact"
        else:
            user_contacts.update_one({"contacts_of": for_user}, {"$push": {"contacts_list": contact_name}})
            return "Success"
    else:
        user_contacts.insert_one({"contacts_of": for_user, "contacts_list": [contact_name]})
        return "Success"


@app.get("/get_contacts", status_code=200)
def get_contacts(for_user: str):
    contacts = user_contacts.find_one({"contacts_of": for_user})
    if contacts:
        contacts = ContactList.model_validate(contacts)
        return contacts.contacts_list
    return []


@app.post("/create_account", status_code=201)
def create_account(new_user: User, response: Response):
    if users.find_one({"username": new_user.username}):
        response.status_code = 409
        return "Username Already In Use"
    try:
        new_user.password = ph.hash(new_user.password)
        users.insert_one(new_user.model_dump())
        return "Success"
    except Exception as e:
        response.status_code = 500
        return f"An Unexpected Error Occurred: {e}"


@app.post("/login", status_code=200)
async def login(user: User, response: Response):
    username = user.username
    password = user.password
    profile = users.find_one({"username": username})
    if not profile:
        response.status_code = 404
        return "User Does Not Exist"
    profile = User.model_validate(profile)
    try:
        ph.verify(profile.password, password)
        return "Success"
    except VerifyMismatchError:
        return "Password Incorrect"
    except Exception as e:
        response.status_code = 500
        return f"An Unexpected Error Occurred: {e}"


def insert_message(message: Message):
    chunks = [ChatChunk.model_validate(c) for c in messages.find({"chat_id": message.chat_id})]
    if chunks:
        chunks = sorted(chunks, key=lambda x: x.chunk_id)
        # New Chunk
        if len(chunks[-1].messages) >= 300:
            messages.insert_one(
                dict(ChatChunk(chat_id=message.chat_id,
                               chunk_id=chunks[-1].chunk_id + 1,
                               messages=[message.model_dump()]
                               )))
        else:
            # Standard Process
            messages.update_one({"chat_id": chunks[-1].chat_id, "chunk_id": chunks[-1].chunk_id},
                                {"$push": {"messages": message.model_dump()}})
    else:
        # New Chat
        messages.insert_one(dict(ChatChunk(chat_id=message.chat_id, chunk_id=0, messages=[message.model_dump()])))


@app.websocket("/socket")
async def websocket_endpoint(username: str, websocket: WebSocket):
    await websocket.accept()
    connection = Connection(websocket=websocket, username=username)
    manager.add_connection(connection)
    try:
        if undelivered.find_one({"sent_to": username}):
            need_delivered = undelivered.find({"sent_to": username})
            for msg in need_delivered:
                msg = Message.model_validate(msg)
                delivered = await manager.forward_message(msg, connection)
                if delivered:
                    undelivered.delete_one(msg.model_dump())
                    insert_message(msg)
        while True:
            message = Message.model_validate(await websocket.receive_json())
            recipient_websocket = manager.current_connections.get(message.sent_to)
            if recipient_websocket:
                delivered = await manager.forward_message(message, Connection(
                    username=message.sent_to,
                    websocket=recipient_websocket
                ))
                if delivered:
                    insert_message(message)
                else:
                    undelivered.insert_one(dict(message))
                    print("Failed to Deliver")
            else:
                undelivered.insert_one(dict(message))
    except WebSocketDisconnect:
        await manager.remove_connection(connection)
        print("Socket Disconnected")
