from __future__ import annotations

import json
import os
import re
import shutil
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

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

# ローカル開発用（http://127.0.0.1）
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
# 以前の readonly 権限などと混ざったときのスコープ差分を許容
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "counseling-reservation-local-dev")
init_mail(app)

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
CREDENTIALS_ALT = BASE_DIR / "credentials.json.json"
TOKEN_FILE = BASE_DIR / "token.json"

SCOPES = ["https://www.googleapis.com/auth/calendar"]
SLOT_TITLE = "受付可能"
LOOKAHEAD_DAYS = 14
JST = ZoneInfo("Asia/Tokyo")
WEEKDAYS = "月火水木金土日"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:5000/oauth2callback"

CALENDAR_ID_OVERRIDE = os.environ.get("CALENDAR_ID", "").strip() or None


class AuthRequired(Exception):
    """Google 認証がまだ完了していない。"""


def ensure_credentials_file() -> Path:
    """credentials.json が無ければ credentials.json.json から自動コピーする。"""
    if CREDENTIALS_FILE.exists():
        return CREDENTIALS_FILE
    if CREDENTIALS_ALT.exists():
        shutil.copy2(CREDENTIALS_ALT, CREDENTIALS_FILE)
        return CREDENTIALS_FILE
    raise FileNotFoundError(
        f"{CREDENTIALS_FILE} が見つかりません。"
        f" Google Cloud から OAuth クライアントの JSON を"
        f" {CREDENTIALS_FILE.name} として保存してください。"
    )


def get_oauth_redirect_uri() -> str:
    with ensure_credentials_file().open(encoding="utf-8") as f:
        data = json.load(f)
    for key in ("web", "installed"):
        block = data.get(key, {})
        uris = block.get("redirect_uris") or []
        if uris:
            return uris[0]
    return DEFAULT_REDIRECT_URI


def create_oauth_flow(
    state: str | None = None,
    code_verifier: str | None = None,
) -> Flow:
    """OAuth フローを生成。コールバック時は state と code_verifier を同じ値で復元する。"""
    return Flow.from_client_secrets_file(
        str(ensure_credentials_file()),
        scopes=SCOPES,
        redirect_uri=get_oauth_redirect_uri(),
        state=state,
        code_verifier=code_verifier,
        autogenerate_code_verifier=code_verifier is None,
    )


def clear_token_file() -> None:
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()


def _has_required_scopes(creds: Credentials) -> bool:
    granted = set(creds.scopes or [])
    return set(SCOPES).issubset(granted)


def save_credentials(creds: Credentials) -> None:
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")


def get_credentials() -> Credentials:
    ensure_credentials_file()
    creds: Credentials | None = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and not _has_required_scopes(creds):
        clear_token_file()
        raise AuthRequired()

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_credentials(creds)
        return creds

    raise AuthRequired()


def get_calendar_service():
    return build("calendar", "v3", credentials=get_credentials())


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
    return (
        f"{start.year}年{start.month}月{start.day}日（{weekday}） "
        f"{start.strftime('%H:%M')}〜{end.strftime('%H:%M')}"
    )


def _list_target_calendar_ids(service) -> list[str]:
    if CALENDAR_ID_OVERRIDE:
        return [CALENDAR_ID_OVERRIDE]

    result = service.calendarList().list().execute()
    ids: list[str] = []
    for item in result.get("items", []):
        if item.get("selected", True):
            ids.append(item["id"])
    return ids or ["primary"]


def _writable_calendar_ids(service) -> set[str]:
    result = service.calendarList().list().execute()
    writable: set[str] = set()
    for item in result.get("items", []):
        if item.get("accessRole") in ("owner", "writer"):
            writable.add(item["id"])
    return writable or {"primary"}


def fetch_available_slots() -> list[dict]:
    service = get_calendar_service()
    now = datetime.now(JST)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=LOOKAHEAD_DAYS)).isoformat()

    slots: list[dict] = []
    seen: set[str] = set()

    for calendar_id in _list_target_calendar_ids(service):
        try:
            events_result = (
                service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    q=SLOT_TITLE,
                )
                .execute()
            )
        except HttpError:
            continue

        for event in events_result.get("items", []):
            if not _is_slot_event(event):
                continue

            start_raw = event.get("start", {}).get("dateTime")
            end_raw = event.get("end", {}).get("dateTime")
            if not start_raw or not end_raw:
                continue

            start = _parse_event_datetime(start_raw)
            end = _parse_event_datetime(end_raw)
            if start < now:
                continue

            event_id = event.get("id", "")
            dedupe_key = f"{calendar_id}:{event_id}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            slot_id = f"{start.isoformat()}|{end.isoformat()}|{event_id}|{calendar_id}"
            slots.append(
                {
                    "id": slot_id,
                    "label": _format_slot_label(start, end),
                    "date": start.strftime("%Y-%m-%d"),
                    "start_time": start.strftime("%H:%M"),
                    "end_time": end.strftime("%H:%M"),
                    "sort_key": start.timestamp(),
                }
            )

    slots.sort(key=lambda s: s["sort_key"])
    return slots


def _parse_slot_id(slot_id: str) -> dict | None:
    parts = slot_id.split("|")
    if len(parts) != 4:
        return None
    start_raw, end_raw, event_id, calendar_id = parts
    try:
        start = datetime.fromisoformat(start_raw)
        end = datetime.fromisoformat(end_raw)
        if start.tzinfo is None:
            start = start.replace(tzinfo=JST)
        if end.tzinfo is None:
            end = end.replace(tzinfo=JST)
    except ValueError:
        return None

    return {
        "id": slot_id,
        "label": _format_slot_label(start, end),
        "date": start.strftime("%Y-%m-%d"),
        "start_time": start.strftime("%H:%M"),
        "end_time": end.strftime("%H:%M"),
        "event_id": event_id,
        "calendar_id": calendar_id,
        "start": start,
        "end": end,
    }


def _slot_still_available(service, slot: dict) -> bool:
    try:
        event = (
            service.events()
            .get(calendarId=slot["calendar_id"], eventId=slot["event_id"])
            .execute()
        )
    except HttpError:
        return False
    return _is_slot_event(event)


def _build_calendar_description(name: str, email: str, counseling_type: str) -> str:
    return (
        "【予約者情報】\n"
        f"お名前: {name}\n"
        f"メールアドレス: {email}\n"
        "\n"
        "【カウンセリング】\n"
        f"種類: {counseling_type}\n"
        "\n"
        f"（元の空き枠: {SLOT_TITLE}）"
    )


def book_slot(counseling_type: str, name: str, email: str, slot: dict) -> None:
    service = get_calendar_service()
    writable = _writable_calendar_ids(service)

    if slot["calendar_id"] not in writable:
        raise PermissionError(
            "この空き枠のカレンダーに予定を書き込む権限がありません。"
            " 認証に使った Google アカウントが calendar/u/1 と同じか確認してください。"
        )

    if not _slot_still_available(service, slot):
        raise ValueError("選択した枠はすでに予約済みか、削除されています。ページを再読み込みしてください。")

    start = slot["start"]
    end = slot["end"]
    start_body = {"dateTime": start.isoformat(), "timeZone": "Asia/Tokyo"}
    end_body = {"dateTime": end.isoformat(), "timeZone": "Asia/Tokyo"}

    service.events().insert(
        calendarId=slot["calendar_id"],
        body={
            "summary": counseling_type,
            "description": _build_calendar_description(name, email, counseling_type),
            "start": start_body,
            "end": end_body,
        },
    ).execute()

    service.events().delete(
        calendarId=slot["calendar_id"],
        eventId=slot["event_id"],
    ).execute()


def _render_index(**kwargs):
    defaults = {
        "slots": [],
        "error": None,
        "submitted": False,
        "success": False,
        "auth_required": False,
        "authenticated": False,
        "lookahead_days": LOOKAHEAD_DAYS,
    }
    defaults.update(kwargs)
    return render_template("index.html", **defaults)


def _load_slots_or_error():
    try:
        return fetch_available_slots(), None, True
    except AuthRequired:
        return [], None, False
    except Exception as exc:
        return [], str(exc), False


@app.route("/auth")
def auth():
    # 以前の readonly トークンと混ざらないよう、再連携時は古い token を削除
    clear_token_file()

    flow = create_oauth_flow()
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    # PKCE: コールバックで同じ code_verifier が必要（新しい Flow では再生成しない）
    session["oauth_state"] = state
    session["code_verifier"] = flow.code_verifier
    session.modified = True
    return redirect(authorization_url)


@app.route("/oauth2callback")
def oauth2callback():
    state = session.get("oauth_state")
    code_verifier = session.get("code_verifier")
    if not state or not code_verifier:
        return _render_index(
            error="認証セッションが切れました。トップページからもう一度「Googleカレンダーと連携する」を押してください。",
            auth_required=True,
        )

    flow = create_oauth_flow(state=state, code_verifier=code_verifier)
    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception as exc:
        return _render_index(
            error=f"Google 認証に失敗しました: {exc}",
            auth_required=True,
        )

    save_credentials(flow.credentials)
    session.pop("oauth_state", None)
    session.pop("code_verifier", None)
    return redirect(url_for("index"))


@app.route("/")
def index():
    auth_error = request.args.get("error")
    if auth_error:
        return _render_index(error="認証に失敗しました。もう一度お試しください。", auth_required=True)

    slots, error, authenticated = _load_slots_or_error()
    if not authenticated:
        return _render_index(auth_required=True, error=error)

    return _render_index(slots=slots, error=error, authenticated=True)


@app.route("/confirm", methods=["POST"])
def confirm():
    counseling_type = request.form.get("counseling_type", "").strip()
    slot_id = request.form.get("slot", "").strip()
    slot = _parse_slot_id(slot_id)

    slots, error, authenticated = _load_slots_or_error()
    if not authenticated:
        return _render_index(auth_required=True, error=error, counseling_type=counseling_type)

    if not counseling_type or not slot:
        return _render_index(
            error="カウンセリングの種類と空き枠を選択してください。",
            slots=slots,
            counseling_type=counseling_type,
            slot_id=slot_id,
            authenticated=True,
        )

    return _render_index(
        submitted=True,
        counseling_type=counseling_type,
        slot=slot,
        authenticated=True,
    )


@app.route("/book", methods=["POST"])
def book():
    counseling_type = request.form.get("counseling_type", "").strip()
    slot_id = request.form.get("slot", "").strip()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    slot = _parse_slot_id(slot_id)

    if not counseling_type or not slot:
        return _render_index(error="予約内容が不正です。最初からやり直してください.")

    if not name:
        return _render_index(
            error="お名前を入力してください。",
            submitted=True,
            counseling_type=counseling_type,
            slot=slot,
            name=name,
            email=email,
            authenticated=True,
        )

    if not email or not validate_email(email):
        return _render_index(
            error="有効なメールアドレスを入力してください。",
            submitted=True,
            counseling_type=counseling_type,
            slot=slot,
            name=name,
            email=email,
            authenticated=True,
        )

    mail_warning = None
    try:
        book_slot(counseling_type, name, email, slot)
        if is_mail_configured():
            try:
                send_booking_emails(name, email, counseling_type, slot["label"])
            except Exception as mail_exc:
                mail_warning = f"予約は完了しましたが、確認メールの送信に失敗しました: {mail_exc}"
        else:
            mail_warning = (
                "予約は完了しました。.env に MAIL_USERNAME / MAIL_PASSWORD / ADMIN_EMAIL を"
                "設定すると、Flask-Mail で自動返信メールが送信されます。"
            )
    except AuthRequired:
        return _render_index(
            auth_required=True,
            submitted=True,
            counseling_type=counseling_type,
            slot=slot,
            name=name,
            email=email,
        )
    except Exception as exc:
        slots, _, authenticated = _load_slots_or_error()
        return _render_index(
            error=str(exc),
            slots=slots,
            submitted=True,
            counseling_type=counseling_type,
            slot=slot,
            name=name,
            email=email,
            authenticated=authenticated,
        )

    return _render_index(
        success=True,
        customer_name=format_honorific_name(name),
        slot=slot,
        mail_warning=mail_warning,
    )


if __name__ == "__main__":
    try:
        ensure_credentials_file()
        print(f"認証ファイル: {CREDENTIALS_FILE}")
        print(f"リダイレクトURI: {get_oauth_redirect_uri()}")
    except FileNotFoundError as exc:
        print(f"警告: {exc}")
    app.run(debug=True, port=5000)
