"""
Google ドキュメント API の最小サンプル

事前準備:
  1. https://console.cloud.google.com/ でプロジェクトを作成
  2. 「APIとサービス」→「ライブラリ」で「Google Docs API」「Google Drive API」を有効化
  3. 「認証情報」→「OAuth クライアント ID」→ デスクトップアプリを作成
  4. JSON をダウンロードし、このフォルダに credentials.json として保存
  5. pip install -r requirements.txt
  6. python google_docs_sample.py

初回実行時にブラウザが開き、token.json が作成されます。
"""

from __future__ import annotations

import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ドキュメントの作成・編集・読み取り
SCOPES = ["https://www.googleapis.com/auth/documents"]

SCRIPT_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = SCRIPT_DIR / "credentials.json"
TOKEN_FILE = SCRIPT_DIR / "token.json"


def get_credentials() -> Credentials:
    """OAuth で認証し、再利用可能なトークンを返す。"""
    creds: Credentials | None = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"{CREDENTIALS_FILE} がありません。"
                    " Google Cloud から OAuth クライアントの JSON をダウンロードして配置してください。"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    return creds


def create_document(service, title: str) -> str:
    """新規ドキュメントを作成し、documentId を返す。"""
    doc = service.documents().create(body={"title": title}).execute()
    return doc["documentId"]


def append_text(service, document_id: str, text: str) -> None:
    """ドキュメント末尾にテキストを追加する。"""
    doc = service.documents().get(documentId=document_id).execute()
    end_index = doc["body"]["content"][-1]["endIndex"] - 1

    requests = [
        {
            "insertText": {
                "location": {"index": end_index},
                "text": text,
            }
        }
    ]
    service.documents().batchUpdate(
        documentId=document_id,
        body={"requests": requests},
    ).execute()


def read_plain_text(service, document_id: str) -> str:
    """段落テキストを連結して返す（装飾は無視）。"""
    doc = service.documents().get(documentId=document_id).execute()
    parts: list[str] = []

    for element in doc.get("body", {}).get("content", []):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for elem in paragraph.get("elements", []):
            text_run = elem.get("textRun")
            if text_run and "content" in text_run:
                parts.append(text_run["content"])

    return "".join(parts)


def main() -> None:
    creds = get_credentials()
    service = build("docs", "v1", credentials=creds)

    title = "Python API サンプル"
    document_id = create_document(service, title)
    print(f"作成しました: https://docs.google.com/document/d/{document_id}/edit")

    append_text(service, document_id, "こんにちは、Google ドキュメント API から書き込みました。\n")
    append_text(service, document_id, "2行目のテキストです。\n")

    content = read_plain_text(service, document_id)
    print("--- ドキュメント内容 ---")
    print(content)


if __name__ == "__main__":
    main()
