"""
mail_sync.py
核心同步逻辑：把用户填的几个筛选条件拼成 Zoho 搜索语法，通过 Zoho 官方"搜索邮件"接口
直接在服务端搜（不用自己把整个邮箱拉下来再筛），命中的邮件再检查"发件人排除"这个
Zoho 语法不支持的反向条件，最后把符合条件的附件下载到服务器临时目录，
运行结束后可以逐个下载、打包 ZIP 下载、或者导出 Excel。

用法：run_sync_for_user(flask_app, user_id, run_id) 在后台线程里调用。
"""

import os
import re
import shutil
import time
import uuid

import requests

import oauth_zoho as zoho_oauth
import zoho_mail_api as zmail
import zoho_search_api as zsearch
import crypto_util as token_crypto
from models import db, User, ProcessedMessage, ManifestEntry, RunLog, RunStatus

PAGE_SIZE = 200
MAX_PAGES = 25  # 安全阀，避免筛选条件太宽泛时无限翻页

DOWNLOADS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_downloads")
DOWNLOADS_KEEP_DAYS = 3  # 超过这么多天没人下载的旧运行临时文件，下次跑的时候顺手清掉，避免服务器磁盘被占满


def _cleanup_old_downloads(user_id):
    user_root = os.path.join(DOWNLOADS_ROOT, str(user_id))
    if not os.path.isdir(user_root):
        return
    cutoff = time.time() - DOWNLOADS_KEEP_DAYS * 86400
    for name in os.listdir(user_root):
        path = os.path.join(user_root, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def _match_any(text, keywords):
    if not keywords:
        return False
    text_lower = (text or "").lower()
    return any(kw.lower() in text_lower for kw in keywords if kw)


def safe_filename(name):
    name = re.sub(r"[\r\n\t]+", " ", name)
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > 150:
        if "." in name:
            base, ext = name.rsplit(".", 1)
            name = base[:145] + "." + ext
        else:
            name = name[:150]
    return name or "attachment"


def _split(text):
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def _save_local(folder, filename, content):
    """把附件写到本地磁盘，文件名冲突时自动加序号后缀，返回最终文件的完整路径。"""
    base, ext = os.path.splitext(filename)
    target = os.path.join(folder, filename)
    n = 1
    while os.path.exists(target):
        target = os.path.join(folder, f"{base} ({n}){ext}")
        n += 1
    with open(target, "wb") as f:
        f.write(content)
    return target


# ================= 主流程 =================

def log(app, user_id, run_id, message):
    with app.app_context():
        db.session.add(RunLog(user_id=user_id, run_id=run_id, message=message))
        db.session.commit()


def run_sync_for_user(app, user_id, run_id=None):
    run_id = run_id or uuid.uuid4().hex[:12]

    try:
        # 这一步本身也可能失败（比如数据库文件被占用），放进 try 里，
        # 不然一旦这里报错，线程直接死掉，is_running 会永远卡在 True。
        with app.app_context():
            status = RunStatus.query.get(user_id)
            if not status:
                status = RunStatus(user_id=user_id)
                db.session.add(status)
            status.is_running = True
            status.current_run_id = run_id
            status.stop_requested = False
            status.checked_count = 0
            status.saved_count = 0
            db.session.commit()

        _do_sync(app, user_id, run_id)
    except Exception as e:
        log(app, user_id, run_id, f"[出错] 任务异常终止：{e}")
    finally:
        with app.app_context():
            status = RunStatus.query.get(user_id)
            if status:
                status.is_running = False
                status.stop_requested = False
                db.session.commit()
        log(app, user_id, run_id, "---- 本次运行结束 ----")


def _stop_requested(user_id):
    status = RunStatus.query.get(user_id)
    return bool(status and status.stop_requested)


def _do_sync(app, user_id, run_id):
    with app.app_context():
        user = User.query.get(user_id)
        if not user:
            return

        _cleanup_old_downloads(user_id)

        # ---- 刷新 Zoho token ----
        log(app, user_id, run_id, "正在刷新 Zoho 登录状态 ...")
        try:
            zoho_refresh_token = token_crypto.decrypt(user.zoho_refresh_token)
            zoho_token_resp = zoho_oauth.refresh_access_token(zoho_refresh_token, user.zoho_accounts_server)
        except requests.exceptions.Timeout:
            log(app, user_id, run_id, "[出错] 连接 Zoho 服务器超时（超过 30 秒无响应），请检查网络/VPN 后重试。")
            return
        except requests.exceptions.RequestException as e:
            log(app, user_id, run_id, f"[出错] 连接 Zoho 服务器失败：{e}")
            return
        access_token = zoho_token_resp["access_token"]
        if zoho_token_resp.get("refresh_token"):
            user.zoho_refresh_token = token_crypto.encrypt(zoho_token_resp["refresh_token"])

        if not user.zoho_account_id:
            _, account_id = zoho_oauth.get_account_info(access_token, user.zoho_api_domain)
            user.zoho_account_id = account_id

        db.session.commit()

        search_key = zsearch.build_search_key(
            user.search_subject_contains,
            user.search_attachment_contains,
            user.search_sender_contains,
            user.search_content_contains,
            user.search_since_date,
            bool(user.search_require_attachment),
        )
        if not search_key:
            log(app, user_id, run_id, "[出错] 筛选条件全部留空，没法搜索，请先填一个再运行。")
            return
        log(app, user_id, run_id, f"用 Zoho 搜索条件：{search_key}")

        exclude_terms = _split(user.search_sender_excludes)

        download_dir = os.path.join(DOWNLOADS_ROOT, str(user_id), run_id)
        os.makedirs(download_dir, exist_ok=True)

        processed_uids = {
            row.uid for row in ProcessedMessage.query.filter_by(user_id=user.id).all()
        }

        status = RunStatus.query.get(user_id)

        new_saved = 0
        skipped = 0
        excluded = 0
        checked = 0
        stop = False

        for page in range(MAX_PAGES):
            if stop:
                break
            if _stop_requested(user_id):
                log(app, user_id, run_id, "[已停止] 用户手动停止了本次运行。")
                stop = True
                break

            start = page * PAGE_SIZE + 1
            try:
                messages = zsearch.search_messages(
                    access_token, user.zoho_api_domain, user.zoho_account_id, search_key, start=start, limit=PAGE_SIZE
                )
            except requests.exceptions.RequestException as e:
                log(app, user_id, run_id, f"[出错] 调用 Zoho 搜索失败：{e}")
                break
            if not messages:
                break

            log(app, user_id, run_id, f"拉取第 {page + 1} 页搜索结果，共 {len(messages)} 封")

            for msg_index, msg in enumerate(messages):
                if msg_index % 20 == 0 and _stop_requested(user_id):
                    log(app, user_id, run_id, "[已停止] 用户手动停止了本次运行。")
                    stop = True
                    break

                message_id = str(msg.get("messageId"))
                checked += 1
                if status:
                    status.checked_count = checked
                    if checked % 50 == 0:
                        db.session.commit()  # 定期落库一下，即使这一段全是已处理过的邮件也能看到进度在走
                if message_id in processed_uids:
                    continue

                try:
                    sender_name = msg.get("sender", "")
                    sender_email = msg.get("fromAddress", "")
                    subject = msg.get("subject", "")

                    # Zoho 搜索语法本身不支持"发件人不包含"，这里拿到结果之后自己再过滤一遍。
                    if exclude_terms and (_match_any(sender_email, exclude_terms) or _match_any(sender_name, exclude_terms)):
                        excluded += 1
                        db.session.add(ProcessedMessage(user_id=user.id, uid=message_id))
                        db.session.commit()
                        processed_uids.add(message_id)
                        continue

                    msg_folder_id = str(msg.get("folderId", ""))
                    mail_date = msg.get("sentDateInGMT", "")
                    attach = str(msg.get("hasAttachment", "0")) in ("1", "true", "True")

                    attachments = []
                    if attach and msg_folder_id:
                        attachments = zmail.get_attachment_info(
                            access_token, user.zoho_api_domain, user.zoho_account_id, msg_folder_id, message_id
                        )

                    for a in attachments:
                        filename = safe_filename(a.get("attachmentName", "attachment"))
                        content = zmail.download_attachment(
                            access_token, user.zoho_api_domain, user.zoho_account_id,
                            msg_folder_id, message_id, a.get("attachmentId"),
                        )

                        saved_path = _save_local(download_dir, filename, content)
                        saved_filename = os.path.basename(saved_path)

                        new_saved += 1
                        if status:
                            status.saved_count = new_saved
                        db.session.add(
                            ManifestEntry(
                                user_id=user.id,
                                run_id=run_id,
                                uid=message_id,
                                original_filename=filename,
                                saved_filename=saved_filename,
                                sender_name=sender_name,
                                sender_email=sender_email,
                                subject=subject,
                                mail_date=mail_date,
                                message_id=message_id,
                            )
                        )

                    db.session.add(ProcessedMessage(user_id=user.id, uid=message_id))
                    db.session.commit()
                    processed_uids.add(message_id)
                except Exception as e:
                    skipped += 1
                    log(app, user_id, run_id, f"[警告] 邮件 {message_id} 处理失败，跳过：{e}")
                    db.session.rollback()
                    continue

            if stop:
                break
            if len(messages) < PAGE_SIZE:
                break  # 最后一页了

        log(app, user_id, run_id, f"完成：检查了 {checked} 封搜索结果，排除 {excluded} 封（命中发件人排除词），保存 {new_saved} 个附件")
        if skipped:
            log(app, user_id, run_id, f"有 {skipped} 封邮件处理失败被跳过（下次运行会重试）")
