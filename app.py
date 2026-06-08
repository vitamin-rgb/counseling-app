from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, redirect, render_template, request, session, url_for
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from mailer import (
    format_honorific_name,
    init_mail,
    is_mail_configured,
    send_booking_emails,
    validate_email,
)

# Render環境では環境変数から直接設定を読み込む
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "counseling-reservation-local-dev")
init_mail(app)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
SLOT_TITLE = "受付可能"
LOOKAHEAD_DAYS = 14
JST = ZoneInfo("Asia/Tokyo")
WEEKDAYS = "月火水木金土日"

CALENDAR_ID_OVERRIDE = os.environ.get("CALENDAR_ID", "").strip() or None

class AuthRequired(Exception):
    """Google 認証がまだ完了していない。"""

def get_oauth_client_config():
    """環境変数からGoogle OAuth情報を取得"""
    creds_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if not creds_json:
        raise ValueError("環境変数 GOOGLE_APPLICATION_CREDENTIALS_JSON が設定されていません。")
    return json.loads(creds_json)

def create_oauth_flow(state=None, code_verifier=None) -> Flow:
    client_config = get_oauth_client_config()
    return Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=client_config['web']['redirect_uris'][0],
        state=state,
        code_verifier=code_verifier,
        autogenerate_code_verifier=code_verifier is None,
    )

def get_credentials() -> Credentials:
    """環境変数 GOOGLE_TOKEN_JSON からトークンを取得"""
    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if not token_json:
        raise AuthRequired()
    
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # 注: Render環境ではここで更新されたトークンを永続化できません。
        # 本来はDBや外部ストアに保存する必要があります。
        return creds
        
    raise AuthRequired()

def get_calendar_service():
    return build("calendar", "v3", credentials=get_credentials())

# ---------------------------------------------------------
# 以降の関数は変更不要です（そのまま貼り付けてください）
# ---------------------------------------------------------

def _normalize_title(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", "", text)

def _is_slot_event(event: dict) -> bool:
    if event.get("status") == "cancelled":
        return False
    summary = _normalize_title(event.get("summary", ""))
    return summary == _normalize_title(SLOT_TITLE)

def _parse_event_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(JST)

def _format_slot_label(start: datetime, end: datetime) -> str:
    weekday = WEEKDAYS[start.weekday()]
    return f"{start.year}年{start.month}月{start.day}日（{weekday}） {start.strftime('%H:%M')}〜{end.strftime('%H:%M')}"

def _list_target_calendar_ids(service) -> list[str]:
    if CALENDAR_ID_OVERRIDE:
        return [CALENDAR_ID_OVERRIDE]
    result = service.calendarList().list().execute()
    ids = [item["id"] for item in result.get("items", []) if item.get("selected", True)]
    return ids or ["primary"]

def _writable_calendar_ids(service) -> set[str]:
    result = service.calendarList().list().execute()
    return {item["id"] for item in result.get("items", []) if item.get("accessRole") in ("owner", "writer")} or {"primary"}

def fetch_available_slots() -> list[dict]:
    service = get_calendar_service()
    now = datetime.now(JST)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=LOOKAHEAD_DAYS)).isoformat()
    slots = []
    seen = set()
    for calendar_id in _list_target_calendar_ids(service):
        try:
            events_result = service.events().list(calendarId=calendar_id, timeMin=time_min, timeMax=time_max, singleEvents=True, orderBy="startTime", q=SLOT_TITLE).execute()
        except HttpError: continue
        for event in events_result.get("items", []):
            if not _is_slot_event(event): continue
            start_raw, end_raw = event.get("start", {}).get("dateTime"), event.get("end", {}).get("dateTime")
            if not start_raw or not end_raw: continue
            start, end = _parse_event_datetime(start_raw), _parse_event_datetime(end_raw)
            if start < now: continue
            event_id = event.get("id", "")
            dedupe_key = f"{calendar_id}:{event_id}"
            if dedupe_key in seen: continue
            seen.add(dedupe_key)
            slots.append({"id": f"{start.isoformat()}|{end.isoformat()}|{event_id}|{calendar_id}", "label": _format_slot_label(start, end), "date": start.strftime("%Y-%m-%d"), "start_time": start.strftime("%H:%M"), "end_time": end.strftime("%H:%M"), "sort_key": start.timestamp()})
    slots.sort(key=lambda s: s["sort_key"])
    return slots

def _parse_slot_id(slot_id: str) -> dict | None:
    parts = slot_id.split("|")
    if len(parts) != 4: return None
    start_raw, end_raw, event_id, calendar_id = parts
    start, end = datetime.fromisoformat(start_raw).astimezone(JST), datetime.fromisoformat(end_raw).astimezone(JST)
    return {"id": slot_id, "label": _format_slot_label(start, end), "date": start.strftime("%Y-%m-%d"), "start_time": start.strftime("%H:%M"), "end_time": end.strftime("%H:%M"), "event_id": event_id, "calendar_id": calendar_id, "start": start, "end": end}

def _slot_still_available(service, slot: dict) -> bool:
    try:
        event = service.events().get(calendarId=slot["calendar_id"], eventId=slot["event_id"]).execute()
        return _is_slot_event(event)
    except HttpError: return False

def _build_calendar_description(name: str, email: str, counseling_type: str) -> str:
    return f"【予約者情報】\nお名前: {name}\nメールアドレス: {email}\n\n【カウンセリング】\n種類: {counseling_type}\n\n（元の空き枠: {SLOT_TITLE}）"

def book_slot(counseling_type: str, name: str, email: str, slot: dict) -> None:
    service = get_calendar_service()
    if slot["calendar_id"] not in _writable_calendar_ids(service):
        raise PermissionError("権限がありません。")
    if not _slot_still_available(service, slot):
        raise ValueError("予約済みか削除されています。")
    service.events().insert(calendarId=slot["calendar_id"], body={"summary": counseling_type, "description": _build_calendar_description(name, email, counseling_type), "start": {"dateTime": slot["start"].isoformat()}, "end": {"dateTime": slot["end"].isoformat()}}).execute()
    service.events().delete(calendarId=slot["calendar_id"], eventId=slot["event_id"]).execute()

def _render_index(**kwargs):
    defaults = {"slots": [], "error": None, "submitted": False, "success": False, "auth_required": False, "authenticated": False, "lookahead_days": LOOKAHEAD_DAYS}
    defaults.update(kwargs)
    return render_template("index.html", **defaults)

def _load_slots_or_error():
    try: return fetch_available_slots(), None, True
    except AuthRequired: return [], None, False
    except Exception as exc: return [], str(exc), False

@app.route("/auth")
def auth():
    flow = create_oauth_flow()
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    session["oauth_state"] = state
    session["code_verifier"] = flow.code_verifier
    return redirect(url)

@app.route("/oauth2callback")
def oauth2callback():
    state = session.get("oauth_state")
    code_verifier = session.get("code_verifier")
    flow = create_oauth_flow(state=state, code_verifier=code_verifier)
    flow.fetch_token(authorization_response=request.url)
    # 実際にはここに取得したクレデンシャルを表示させ、GOOGLE_TOKEN_JSON に設定させる導線が必要
    return redirect(url_for("index"))

@app.route("/")
def index():
    slots, error, authenticated = _load_slots_or_error()
    return _render_index(slots=slots, error=error, authenticated=authenticated)

# ... (confirm, book 等のルートはそのまま継続)
