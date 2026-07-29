"""
webapp_onedrive_client.py
Microsoft Graph API 操作：建文件夹、上传文件（内存里的字节，不落地到服务器磁盘）。
"""

import requests

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


def ensure_folder(access_token, folder_path):
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(f"{GRAPH_ROOT}/me/drive/root:/{folder_path}", headers=headers, timeout=30)
    if resp.status_code == 200:
        return resp.json()["id"]

    resp = requests.post(
        f"{GRAPH_ROOT}/me/drive/root/children",
        headers={**headers, "Content-Type": "application/json"},
        json={"name": folder_path, "folder": {}, "@microsoft.graph.conflictBehavior": "rename"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def upload_bytes(access_token, folder_path, remote_name, data):
    """把内存里的字节直接上传，不经过服务器磁盘。返回 webUrl。"""
    headers = {"Authorization": f"Bearer {access_token}"}
    size = len(data)

    if size <= 4 * 1024 * 1024:
        resp = requests.put(
            f"{GRAPH_ROOT}/me/drive/root:/{folder_path}/{remote_name}:/content",
            headers=headers,
            data=data,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["webUrl"]

    # 大文件用分片上传会话
    resp = requests.post(
        f"{GRAPH_ROOT}/me/drive/root:/{folder_path}/{remote_name}:/createUploadSession",
        headers=headers,
        json={"@microsoft.graph.conflictBehavior": "rename"},
        timeout=30,
    )
    resp.raise_for_status()
    upload_url = resp.json()["uploadUrl"]

    chunk_size = 5 * 1024 * 1024
    offset = 0
    while offset < size:
        chunk = data[offset : offset + chunk_size]
        end = offset + len(chunk) - 1
        r = requests.put(
            upload_url,
            headers={"Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{size}"},
            data=chunk,
            timeout=60,
        )
        offset += len(chunk)
        if r.status_code in (200, 201):
            return r.json()["webUrl"]
    raise RuntimeError("大文件上传失败")
