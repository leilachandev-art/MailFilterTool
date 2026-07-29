"""
mail_sync.py
核心同步逻辑：用 Zoho OAuth token 通过 Zoho Mail REST API 拉邮件列表、
按用户配置筛选、把命中的附件直接上传到该用户的 OneDrive，不在服务器上落地任何文件。

用法：run_sync_for_user(flask_app, user_id, run_id) 在后台线程里调用。
"""

import os
import re
import shutil
import time
import uuid
from datetime import datetime

import requests

import oauth_zoho as zoho_oauth
import oauth_microsoft as ms_oauth
import onedrive_client as onedrive
import zoho_mail_api as zmail
import crypto_util as token_crypto
from models import db, User, ProcessedMessage, ManifestEntry, RunLog, RunStatus

PAGE_SIZE = 200
MAX_PAGES = 25  # 安全阀，避免超大邮箱没设日期范围时无限翻页

# "打包下载 ZIP" 模式下，命中的附件先落在服务器这个临时目录里（按用户/run_id 分文件夹），
# 等这次运行跑完，用户点"下载"时再打包发给浏览器。app.py 的 /download/<run_id> 路由会读同一个目录。
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


# ================= 筛选逻辑 =================

def _match_any(text, keywords):
    if not keywords:
        return True
    text_lower = (text or "").lower()
    return any(kw.lower() in text_lower for kw in keywords if kw)


def broad_qualifies(subject, from_header, body, attach, keywords, sender_domains, specific_senders, require_attachment):
    if sender_domains and _match_any(from_header, sender_domains):
        return True
    if specific_senders and _match_any(from_header, specific_senders):
        return True
    if not keywords:
        return False
    text = subject + " " + body
    if require_attachment:
        return _match_any(text, keywords) and attach
    return _match_any(text, keywords)


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

        sync_target = user.sync_target or "download"

        if sync_target == "onedrive" and not user.onedrive_connected():
            log(app, user_id, run_id, "[错误] 还没有连接 OneDrive，请先在页面上点\"连接 OneDrive\"，或把投递方式改成本地文件夹。")
            return
        if sync_target == "local" and not user.local_folder:
            log(app, user_id, run_id, "[错误] 还没有填写本地保存路径。")
            return

        if sync_target == "download":
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
        zoho_access_token = zoho_token_resp["access_token"]
        if zoho_token_resp.get("refresh_token"):
            user.zoho_refresh_token = token_crypto.encrypt(zoho_token_resp["refresh_token"])

        if not user.zoho_account_id:
            _, account_id = zoho_oauth.get_account_info(zoho_access_token, user.zoho_api_domain)
            user.zoho_account_id = account_id

        # ---- 刷新 Microsoft token（仅当投递目标是 OneDrive 时才需要）----
        ms_access_token = None
        if sync_target == "onedrive":
            log(app, user_id, run_id, "正在刷新 OneDrive 登录状态 ...")
            try:
                ms_refresh_token = token_crypto.decrypt(user.ms_refresh_token)
                ms_token_resp = ms_oauth.refresh_access_token(ms_refresh_token)
            except Exception as e:
                log(app, user_id, run_id, f"[出错] 连接 Microsoft 服务器失败：{e}")
                return
            ms_access_token = ms_token_resp["access_token"]
            if ms_token_resp.get("refresh_token"):
                user.ms_refresh_token = token_crypto.encrypt(ms_token_resp["refresh_token"])

        db.session.commit()

        keywords = _split(user.keywords)
        sender_domains = _split(user.sender_domains)
        specific_senders = _split(user.specific_senders)
        require_attachment = bool(user.require_attachment_for_keyword_match)

        precise_mode = bool(user.precise_mode)
        precise_subject_kw = _split(user.precise_subject)
        precise_sender_kw = _split(user.precise_sender)
        precise_attachment_kw = _split(user.precise_attachment)
        attachment_name_kw = _split(user.attachment_name_filter)

        folder_path = user.onedrive_folder or "INVOICE-SORTING-RESULT"
        local_folder = (user.local_folder or "").strip()
        download_dir = os.path.join(DOWNLOADS_ROOT, str(user_id), run_id)

        since_cutoff_ms = None
        if user.since_date:
            try:
                since_cutoff_ms = int(datetime.strptime(user.since_date, "%Y-%m-%d").timestamp() * 1000)
            except ValueError:
                log(app, user_id, run_id, f"[警告] 日期格式不对，忽略：{user.since_date}")

        processed_uids = {
            row.uid for row in ProcessedMessage.query.filter_by(user_id=user.id).all()
        }

        log(app, user_id, run_id, "正在获取 Zoho 收件箱 ...")
        if sync_target == "onedrive":
            onedrive.ensure_folder(ms_access_token, folder_path)
        elif sync_target == "local":
            os.makedirs(local_folder, exist_ok=True)
            log(app, user_id, run_id, f"命中的附件将保存到本地路径：{local_folder}")
        else:
            os.makedirs(download_dir, exist_ok=True)
            log(app, user_id, run_id, "命中的附件会先存在服务器上，跑完后点\"下载本次结果\"就能打包下载到你自己电脑。")
        inbox_folder_id = zmail.get_inbox_folder_id(zoho_access_token, user.zoho_api_domain, user.zoho_account_id)

        status = RunStatus.query.get(user_id)

        new_saved = 0
        skipped = 0
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
            messages = zmail.list_messages(
                zoho_access_token, user.zoho_api_domain, user.zoho_account_id, inbox_folder_id, start, PAGE_SIZE
            )
            if not messages:
                break

            log(app, user_id, run_id, f"拉取第 {page + 1} 页，共 {len(messages)} 封")

            for msg_index, msg in enumerate(messages):
                if msg_index % 20 == 0 and _stop_requested(user_id):
                    log(app, user_id, run_id, "[已停止] 用户手动停止了本次运行。")
                    stop = True
                    break

                message_id = str(msg.get("messageId"))
                sent_ms_raw = msg.get("sentDateInGMT") or msg.get("receivedTime") or 0
                try:
                    sent_ms = int(sent_ms_raw)
                except (TypeError, ValueError):
                    sent_ms = 0

                if since_cutoff_ms and sent_ms and sent_ms < since_cutoff_ms:
                    stop = True  # 按时间倒序拉的，遇到比截止日期更早的就可以整体停了
                    break

                checked += 1
                if status:
                    status.checked_count = checked
                    if checked % 50 == 0:
                        db.session.commit()  # 定期落库一下，即使这一段全是已处理过的邮件也能看到进度在走
                if message_id in processed_uids:
                    continue

                try:
                    subject = msg.get("subject", "")
                    from_header = msg.get("fromAddress", "")
                    body_preview = msg.get("summary", "")
                    attach = str(msg.get("hasAttachment", "0")) in ("1", "true", "True")

                    if precise_mode:
                        ok = _match_any(subject, precise_subject_kw) and _match_any(from_header, precise_sender_kw)
                    else:
                        ok = broad_qualifies(
                            subject, from_header, body_preview, attach,
                            keywords, sender_domains, specific_senders, require_attachment,
                        )

                    # 邮件主题/正文没命中关键词时，如果填了"附件文件名必须包含"，不要直接放弃这封邮件——
                    # 主题这种关键词，邮箱自带的搜索框就能搜到；这个工具更该靠附件本身的文件名来判断，
                    # 哪怕主题完全没有 "invoice" 字样，只要附件文件名里有，也应该抓出来。
                    check_attachments_anyway = bool(attachment_name_kw) and attach and not ok

                    if not ok and not check_attachments_anyway:
                        db.session.add(ProcessedMessage(user_id=user.id, uid=message_id))
                        db.session.commit()
                        processed_uids.add(message_id)
                        continue

                    msg_folder_id = str(msg.get("folderId", inbox_folder_id))
                    mail_date = msg.get("sentDateInGMT", "")

                    attachments = []
                    if attach:
                        attachments = zmail.get_attachment_info(
                            zoho_access_token, user.zoho_api_domain, user.zoho_account_id, msg_folder_id, message_id
                        )

                    for a in attachments:
                        filename = safe_filename(a.get("attachmentName", "attachment"))
                        if precise_mode and not _match_any(filename, precise_attachment_kw):
                            continue
                        # 不管邮件是靠关键词还是精确匹配命中的，这一层单独按附件文件名再筛一遍——
                        # 同一封邮件里经常混着发票和其他不相关的附件，只留文件名对得上的。
                        if attachment_name_kw and not _match_any(filename, attachment_name_kw):
                            continue

                        content = zmail.download_attachment(
                            zoho_access_token, user.zoho_api_domain, user.zoho_account_id,
                            msg_folder_id, message_id, a.get("attachmentId"),
                        )

                        if sync_target == "onedrive":
                            saved_link = onedrive.upload_bytes(ms_access_token, folder_path, filename, content)
                        elif sync_target == "local":
                            saved_link = _save_local(local_folder, filename, content)
                        else:
                            saved_path = _save_local(download_dir, filename, content)
                            saved_link = os.path.basename(saved_path)  # 只存文件名，不暴露服务器上的绝对路径

                        new_saved += 1
                        if status:
                            status.saved_count = new_saved
                        db.session.add(
                            ManifestEntry(
                                user_id=user.id,
                                run_id=run_id,
                                uid=message_id,
                                original_filename=filename,
                                sender=from_header,
                                subject=subject,
                                mail_date=mail_date,
                                message_id=message_id,
                                onedrive_link=saved_link,
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

        if sync_target == "onedrive":
            target_desc = f"OneDrive/{folder_path}"
        elif sync_target == "local":
            target_desc = local_folder
        else:
            target_desc = "服务器临时目录（点下面的\"下载本次结果\"打包下载）"
        log(app, user_id, run_id, f"完成：检查了 {checked} 封新邮件，保存 {new_saved} 个附件到 {target_desc}")
        if skipped:
            log(app, user_id, run_id, f"有 {skipped} 封邮件处理失败被跳过（下次运行会重试）")
