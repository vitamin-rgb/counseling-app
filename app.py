from __future__ import annotations

import base64
import json
import os
import re
import unicodedata
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, redirect, render_template, request, session, url_for
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "counseling-reservation-local-dev")

# 権限スコープ：カレンダー操作に加え、Gmailの送信(gmail.send)を追加
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send"
]
SLOT_TITLE = "受付可能"
LOOKAHEAD_DAYS = 14
JST = ZoneInfo("Asia/Tokyo")
WEEKDAYS = "月火水木金土日"

CALENDAR_ID_OVERRIDE = os.environ.get("CALENDAR_ID", "").strip() or None

def get_oauth_client_config():
    return json.loads(os.environ["GOOGLE_APPLICATION_CREDENTIALS_JSON"])

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
    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if not token_json:
        raise Exception("GOOGLE_TOKEN_JSON が環境変数に設定されていません！")
    return Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)

def get_calendar_service():
    return build("calendar", "v3", credentials=get_credentials())

def get_gmail_service():
    return build("gmail", "v1", credentials=get_credentials())

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

# Gmail APIを使ったメール送信関数（Renderのブロックを迂回）
def send_gmail_via_api(to_email: str, subject: str, body_text: str) -> None:
    try:
        service = get_gmail_service()
        message = MIMEText(body_text)
        message["to"] = to_email
        message["subject"] = subject
        
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        service.users().messages().send(userId="me", body={"raw": raw_message}).execute()
        print(f"Email successfully sent to {to_email}")
    except Exception as e:
        print(f"Failed to send email to {to_email}: {e}")
        raise e

def _render_index(**kwargs):
    defaults = {"slots": [], "error": None, "submitted": False, "success": False, "auth_required": False, "authenticated": False, "lookahead_days": LOOKAHEAD_DAYS}
    defaults.update(kwargs)
    return render_template("index.html", **defaults)

def _load_slots_or_error():
    try: return fetch_available_slots(), None, True
    except Exception as exc: return [], str(exc), False

@app.route("/")
def index():
    slots, error, authenticated = _load_slots_or_error()
    return _render_index(slots=slots, error=error, authenticated=authenticated)

@app.route("/confirm", methods=["POST"])
def confirm():
    counseling_type = request.form.get("counseling_type", "カウンセリング")
    slot_id = request.form.get("slot", "")
    slot = _parse_slot_id(slot_id)
    if not slot:
        return "エラー：枠が選択されていません。"
    return render_template("index.html", submitted=True, slot=slot, counseling_type=counseling_type)

@app.route("/book", methods=["POST"])
def book():
    counseling_type = request.form.get("counseling_type", "カウンセリング")
    slot_id = request.form.get("slot", "")
    name = request.form.get("name", "")
    email = request.form.get("email", "")
    slot = _parse_slot_id(slot_id)
    
    if not slot:
        return "エラー：無効な予約枠です。"

    # 1. カレンダーへ登録＆空き枠削除
    book_slot(counseling_type, name, email, slot)
    
    # 2. 予約者向けの確認メール本文
    user_mail_body = (
        f"{name} 様\n\n"
        f"心理カウンセリング ツナグテ へのご予約、誠にありがとうございます。\n"
        f"以下の内容で予約が確定いたしました。\n\n"
        f"----------------------------------------\n"
        f"【日時】{slot['label']}\n"
        f"【メニュー】{counseling_type}\n"
        f"----------------------------------------\n\n"
        f"当日お会いできるのを心よりお待ちしております。\n"
        f"よろしくお願いいたします。\n\n"
        f"心理カウンセリング ツナグテ"
    )
    
    # 3. あなた（管理者）向けの通知メール本文
    admin_email = os.environ.get("ADMIN_EMAIL", email)
    admin_mail_body = (
        f"【自動通知】新しい予約が入りました。\n\n"
        f"お名前: {name} 様\n"
        f"メールアドレス: {email}\n"
        f"日時: {slot['label']}\n"
        f"メニュー: {counseling_type}\n"
    )
    
    # 4. Gmail APIでそれぞれに送信
    send_gmail_via_api(email, "【ツナグテ】ご予約確定のご案内", user_mail_body)
    send_gmail_via_api(admin_email, "【管理用通知】新しい予約が確定しました", admin_mail_body)
    
    return "予約が完了しました。確認メールをお送りしました。"

if __name__ == "__main__":
    app.run(debug=True, port=5000)