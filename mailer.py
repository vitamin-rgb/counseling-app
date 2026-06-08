from __future__ import annotations

import os
import re

from flask import Flask
from flask_mail import Mail, Message

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

mail = Mail()


def init_mail(app: Flask) -> None:
    """Flask-Mail を .env の設定で初期化する。"""
    username = os.environ.get("MAIL_USERNAME", "").strip()
    mail_from = os.environ.get("MAIL_FROM", "").strip() or username

    app.config.update(
        MAIL_SERVER=os.environ.get("MAIL_SERVER", "smtp.gmail.com"),
        MAIL_PORT=int(os.environ.get("MAIL_PORT", "587")),
        MAIL_USE_TLS=os.environ.get("MAIL_USE_TLS", "true").lower() in ("1", "true", "yes"),
        MAIL_USERNAME=username,
        MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD", "").strip(),
        MAIL_DEFAULT_SENDER=mail_from,
    )
    mail.init_app(app)


def is_mail_configured() -> bool:
    username = os.environ.get("MAIL_USERNAME", "").strip()
    password = os.environ.get("MAIL_PASSWORD", "").strip()
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
    return bool(username and password and admin_email)


def validate_email(email: str) -> bool:
    return bool(EMAIL_PATTERN.match(email.strip()))


def format_honorific_name(name: str) -> str:
    name = name.strip()
    if not name:
        return "お客"
    if name.endswith("様"):
        return name
    return f"{name}様"


def build_customer_thanks_body(name: str, slot_label: str) -> str:
    display = format_honorific_name(name)
    return (
        f"{display}\n"
        f"ご予約いただきありがとうございます。\n"
        f"［ご予約日］\n"
        f"{slot_label}\n"
        f"\n"
        f"以上"
    )


def build_admin_notification_body(
    name: str, email: str, counseling_type: str, slot_label: str
) -> str:
    return (
        f"新しいカウンセリング予約が入りました。\n"
        f"\n"
        f"お名前: {name}\n"
        f"メールアドレス: {email}\n"
        f"カウンセリングの種類: {counseling_type}\n"
        f"ご予約日時: {slot_label}\n"
    )


def send_booking_emails(
    name: str,
    customer_email: str,
    counseling_type: str,
    slot_label: str,
) -> None:
    """予約者と管理者（ADMIN_EMAIL）に Flask-Mail で送信する。"""
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
    if not is_mail_configured():
        raise RuntimeError(
            ".env に MAIL_USERNAME / MAIL_PASSWORD / ADMIN_EMAIL を設定してください。"
        )
    if not admin_email:
        raise RuntimeError("ADMIN_EMAIL が設定されていません。")

    customer_msg = Message(
        subject="ご予約ありがとうございます",
        recipients=[customer_email],
        body=build_customer_thanks_body(name, slot_label),
    )
    admin_msg = Message(
        subject=f"【予約通知】{format_honorific_name(name)}",
        recipients=[admin_email],
        body=build_admin_notification_body(name, customer_email, counseling_type, slot_label),
    )

    mail.send(customer_msg)
    mail.send(admin_msg)
