from dotenv import load_dotenv  # type: ignore
load_dotenv()

from fastapi import FastAPI, Request, BackgroundTasks  # type: ignore
from fastapi.responses import JSONResponse, PlainTextResponse  # type: ignore
import os
import httpx  # type: ignore
from repositories.database import AsyncSessionLocal # type: ignore
from repositories.models import MessageLog, Media # type: ignore
from sqlalchemy.ext.asyncio import AsyncSession  # type: ignore
import boto3  # type: ignore
from botocore.config import Config  # type: ignore
import asyncio

############################ ENV variables ############################
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v17.0")

ACCOUNT_ID = os.getenv("ACCOUNT_ID")
ACCESS_KEY_ID = os.getenv("ACCESS_KEY_ID")
SECRET_ACCESS_KEY = os.getenv("SECRET_ACCESS_KEY")
BUCKET_NAME = os.getenv("BUCKET_NAME")
ENDPOINT_URL = os.getenv("ENDPOINT_URL")
##############################################################################

async def send_message(to_number: str, message: str):
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {
            "body": message
        }
    }

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(url, headers=headers, json=payload)
        return response.status_code, response.json()

async def save_message(session: AsyncSession, user_id: str, message_text: str, message_id: str = None):
    new_message = MessageLog(
        user_id=user_id,
        message_text=message_text,
        message_id=message_id
    )
    session.add(new_message)
    await session.commit()
    await session.refresh(new_message)
    return new_message

def get_r2_client():
    return boto3.client(
        service_name="s3",
        endpoint_url=ENDPOINT_URL,
        aws_access_key_id=ACCESS_KEY_ID,
        aws_secret_access_key=SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(s3={"addressing_style": "path"})
    )

async def upload_to_r2(key: str, data: bytes, content_type: str | None):
    def _upload():
        s3 = get_r2_client()
        extra = {"ContentType": content_type} if content_type else {}
        s3.put_object(Bucket=BUCKET_NAME, Key=key, Body=data, **extra)
    await asyncio.to_thread(_upload)

async def fetch_media_bytes(media_id: str):
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    meta_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{media_id}?phone_number_id={PHONE_NUMBER_ID}"

    async with httpx.AsyncClient(timeout=20) as client:
        meta_response = await client.get(meta_url, headers=headers)
        meta_response.raise_for_status()
        media_url = meta_response.json().get("url")

        if not media_url:
            raise ValueError("Media URL not found in metadata response")

        media_response = await client.get(media_url, headers=headers)
        media_response.raise_for_status()
        return media_response.content, media_response.headers.get("Content-Type", "application/octet-stream")

async def process_image_message(sender: str, db_message_id: int, whatsapp_message_id: str, media_id: str, mime_type: str | None):
    data, content_type = await fetch_media_bytes(media_id)

    ext = "jpg" if mime_type == "image/jpeg" else "bin"
    r2_key = f"images/{sender}/{whatsapp_message_id}.{ext}"

    await upload_to_r2(r2_key, data, content_type)

    async with AsyncSessionLocal() as session:
        media_record = Media(
            message_id=db_message_id,
            whatsapp_message_id=whatsapp_message_id,
            user_id=sender,
            media_id=media_id,
            media_type="image",
            mime_type=content_type,
            r2_key=r2_key
        )
        session.add(media_record)
        await session.commit()


app = FastAPI()

@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        return PlainTextResponse(content=challenge, status_code=200)

    return PlainTextResponse(content="Verification failed", status_code=403)


@app.post("/webhook")
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()

    try:
        entry = data.get("entry", [])[0]
        changes = entry.get("changes", [])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return JSONResponse(content="No messages to process", status_code=200)

        message = messages[0]
        whatsapp_message_id = message.get("id")
        sender = message.get("from")
        message_text = message.get("text", {}).get("body", "")

        async with AsyncSessionLocal() as session:
            db_message = await save_message(session, sender, message_text, whatsapp_message_id)

        if message.get("type") == "image":
            media_id = message["image"]["id"]
            mime_type = message["image"].get("mime_type")
            background_tasks.add_task(
                process_image_message,
                sender,
                db_message.id,
                whatsapp_message_id,
                media_id,
                mime_type
            )

        if sender:
            status, body = await send_message(sender, "Welcome in AgriBot how can i help you?")

        return JSONResponse(content={"status": "ok"}, status_code=200)

    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)