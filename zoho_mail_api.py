"""
zoho_mail_api.py
Zoho Mail 官方 REST API 的薄封装：列文件夹、列邮件、看附件信息、下载附件内容。
用这套代替 IMAP，因为 Zoho 的 OAuth 系统本来就是给 REST API 设计的，
不存在能直接登录 IMAP 的 OAuth scope。
"""

import requests


def _headers(access_token):
    return {"Authorization": f"Zoho-oauthtoken {access_token}"}


def get_inbox_folder_id(access_token, api_domain, account_id):
    resp = requests.get(
        f"{api_domain}/api/accounts/{account_id}/folders",
        headers=_headers(access_token),
        timeout=30,
    )
    resp.raise_for_status()
    folders = resp.json().get("data", [])
    for f in folders:
        if f.get("folderType") == "Inbox":
            return f.get("folderId")
    raise RuntimeError(f"没找到 Inbox 文件夹：{folders}")


def list_messages(access_token, api_domain, account_id, folder_id, start, limit=200):
    """按时间倒序（最新的在前）拿一页邮件列表。"""
    resp = requests.get(
        f"{api_domain}/api/accounts/{account_id}/messages/view",
        headers=_headers(access_token),
        params={
            "folderId": folder_id,
            "start": start,
            "limit": limit,
            "sortBy": "date",
            "sortorder": "false",  # false = 倒序，最新的在前
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def get_attachment_info(access_token, api_domain, account_id, folder_id, message_id):
    resp = requests.get(
        f"{api_domain}/api/accounts/{account_id}/folders/{folder_id}/messages/{message_id}/attachmentinfo",
        headers=_headers(access_token),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})
    return data.get("attachments", [])


def download_attachment(access_token, api_domain, account_id, folder_id, message_id, attachment_id):
    resp = requests.get(
        f"{api_domain}/api/accounts/{account_id}/folders/{folder_id}/messages/{message_id}/attachments/{attachment_id}",
        headers=_headers(access_token),
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content
