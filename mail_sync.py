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
from datetime import datetime, timedelta

import requests

import oauth_zoho as zoho_oauth
import zoho_mail_api as zmail
import zoho_search_api as zsearch
import crypto_util as token_crypto
import ai_extract
from models import db, User, ProcessedMessage, ManifestEntry, RunLog, RunStatus
from db_utils import commit_with_retry

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
    """写一条日志。这个函数在同步循环里调用非常频繁（几乎每封邮件都可能触发），
    如果因为一时锁冲突写失败，只打印到控制台兜底，绝不能让"记日志"这个动作本身
    把整个同步任务搞崩——那样用户反而看不到任何有用的错误信息。"""
    with app.app_context():
        try:
            db.session.add(RunLog(user_id=user_id, run_id=run_id, message=message))
            commit_with_retry(db.session)
        except Exception as e:
            db.session.rollback()
            print(f"[写日志失败，仅打印到控制台] user={user_id} run={run_id}: {message} (写库报错: {e})")


def run_sync_for_user(app, user_id, run_id=None, download_attachments=True):
    """download_attachments=False 时是"仅导出标题"快速模式：跳过附件元数据查询和下载，
    只用 Zoho 搜索结果本身自带的 subject/sender/date 记录，命中多少邮件几乎是秒级的，
    适合只是想要批量拿邮件标题（比如从标题里解析 container 号）去做后续处理的场景。"""
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
            status.matched_count = 0
            commit_with_retry(db.session)

        _do_sync(app, user_id, run_id, download_attachments=download_attachments)
    except Exception as e:
        log(app, user_id, run_id, f"[出错] 任务异常终止：{e}")
    finally:
        with app.app_context():
            try:
                status = RunStatus.query.get(user_id)
                if status:
                    status.is_running = False
                    status.stop_requested = False
                    commit_with_retry(db.session)
            except Exception as e:
                db.session.rollback()
                # 这里再失败也不能不管——is_running 卡在 True 会导致页面永远显示"运行中"，
                # 后续手动重跑都会被"已经有任务在运行"拦住。打印出来方便排查，但流程继续走完。
                print(f"[结束状态写库失败] user={user_id} run={run_id}: {e}")
        log(app, user_id, run_id, "---- 本次运行结束 ----")
        _prune_old_logs(app, user_id)


LOG_RETENTION_DAYS = 14


def _prune_old_logs(app, user_id):
    """部署成网站给多人用之后，同一个数据库要长期扛很多人反复点"运行"，run_log 表
    只会越堆越大——尤其 Render 免费 Postgres 只有 1GB 存储，堆满了会影响所有人。
    这里每次运行结束顺手删掉这个用户超过 14 天的旧日志，只影响历史日志能往前翻多久，
    不影响任何功能；删失败了也不能让这次运行本身看起来像是失败了，只打印不抛出。"""
    try:
        with app.app_context():
            cutoff = datetime.utcnow() - timedelta(days=LOG_RETENTION_DAYS)
            RunLog.query.filter(RunLog.user_id == user_id, RunLog.created_at < cutoff).delete()
            commit_with_retry(db.session)
    except Exception as e:
        db.session.rollback()
        print(f"[清理旧日志失败，不影响本次运行结果] user={user_id}: {e}")


def _stop_requested(user_id):
    status = RunStatus.query.get(user_id)
    return bool(status and status.stop_requested)


def _do_sync(app, user_id, run_id, download_attachments=True):
    with app.app_context():
        user = User.query.get(user_id)
        if not user:
            return

        if download_attachments:
            _cleanup_old_downloads(user_id)

        # ---- 刷新 Zoho token ----
        log(app, user_id, run_id, "正在刷新 Zoho 登录状态 ...")
        zoho_refresh_token = token_crypto.decrypt(user.zoho_refresh_token)
        if token_crypto.looks_encrypted(zoho_refresh_token):
            # 解密失败了（存的密文用现在这把 TOKEN_ENCRYPTION_KEY 解不开，通常是这个环境变量
            # 没有固定设置、每次重新部署都随机生成了一把新钥匙）。这种情况下不能把这坨密文
            # 当成真的 refresh_token 发给 Zoho——那样只会得到一个"400"这种让人摸不着头脑的
            # 错误。直接在这里拦下来，给一个能照着做的提示。
            log(
                app, user_id, run_id,
                "[出错] 保存的 Zoho 登录凭证解密失败，需要重新登录一次才能修复："
                "退出登录后重新点\"用 Zoho 登录\"走一遍授权即可。"
                "（根因通常是服务器的 TOKEN_ENCRYPTION_KEY 环境变量没有固定设置，"
                "每次重新部署都换了一把新钥匙，之前加密保存的登录凭证就解不开了——"
                "建议在 Render 的 Environment 页面把这一项显式配置成固定值，避免以后再发生。）",
            )
            return
        try:
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

        commit_with_retry(db.session)

        search_key = zsearch.build_search_key(
            user.search_subject_contains,
            user.search_attachment_contains,
            user.search_sender_contains,
            user.search_content_contains,
            user.search_since_date,
            bool(user.search_require_attachment),
            until_date=user.search_until_date,
        )
        if not search_key:
            log(app, user_id, run_id, "[出错] 筛选条件全部留空，没法搜索，请先填一个再运行。")
            return
        log(app, user_id, run_id, f"用 Zoho 搜索条件：{search_key}")

        exclude_terms = _split(user.search_sender_excludes)
        extract_field_names = _split(user.extract_fields)
        if download_attachments and extract_field_names:
            ai_problem = ai_extract.diagnose()
            if ai_problem:
                log(app, user_id, run_id, f"[提示] AI 提取字段这个功能现在用不了：{ai_problem}")
            else:
                log(app, user_id, run_id, f"下载 PDF 附件时会用 AI 提取这些字段：{'、'.join(extract_field_names)}")

        download_dir = os.path.join(DOWNLOADS_ROOT, str(user_id), run_id)
        if download_attachments:
            os.makedirs(download_dir, exist_ok=True)

        processed_uids = {
            row.uid for row in ProcessedMessage.query.filter_by(user_id=user.id).all()
        }

        status = RunStatus.query.get(user_id)

        new_saved = 0
        new_matched = 0
        matched_with_attachment_flag = 0
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
                    if checked % 10 == 0:
                        # 定期落库一下"检查到第几封了"这个进度数字。这一段如果连续遇到很多
                        # 已经处理过的邮件（continue 掉了，不会走到下面那次真正的 commit），
                        # 进度条会看起来卡住不动——间隔调小一点（原来是 50），前端每 2 秒轮询
                        # 一次页面上的"已检查 X 封"数字才能更跟手，不会一跳一大截。
                        commit_with_retry(db.session)
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
                        commit_with_retry(db.session)
                        processed_uids.add(message_id)
                        continue

                    msg_folder_id = str(msg.get("folderId", ""))
                    mail_date = msg.get("sentDateInGMT", "")
                    attach = str(msg.get("hasAttachment", "0")) in ("1", "true", "True")
                    if attach:
                        matched_with_attachment_flag += 1

                    attachments = []
                    # "仅导出标题"模式（download_attachments=False）不查附件元数据、不下载，
                    # 直接跳到下面 else 分支只记标题，这样每封邮件不用多等一轮 API 调用，快很多。
                    if download_attachments and attach and msg_folder_id:
                        attachments = zmail.get_attachment_info(
                            access_token, user.zoho_api_domain, user.zoho_account_id, msg_folder_id, message_id
                        )

                    if attachments:
                        # 有附件：每个附件各生成一行，附带下载/保存信息。
                        for a in attachments:
                            filename = safe_filename(a.get("attachmentName", "attachment"))
                            content = zmail.download_attachment(
                                access_token, user.zoho_api_domain, user.zoho_account_id,
                                msg_folder_id, message_id, a.get("attachmentId"),
                            )

                            saved_path = _save_local(download_dir, filename, content)
                            saved_filename = os.path.basename(saved_path)

                            # 只对 PDF 附件调用 AI 按用户自定义的字段名提取；没配置好的话
                            # extract_fields_from_pdf 会带着具体原因回来，记进日志方便排查，
                            # 不管提取成不成功都不影响附件本身已经下载成功这件事。
                            extracted_fields = {}
                            if filename.lower().endswith(".pdf") and extract_field_names:
                                extracted_fields, extract_error = ai_extract.extract_fields_from_pdf(
                                    content, extract_field_names
                                )
                                if extract_error:
                                    log(app, user_id, run_id, f"[提示] 附件 {filename} AI 提取没成功（附件本身已正常下载）：{extract_error}")

                            new_saved += 1
                            new_matched += 1
                            if status:
                                status.saved_count = new_saved
                                status.matched_count = new_matched
                            entry = ManifestEntry(
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
                            entry.extracted_fields = extracted_fields
                            db.session.add(entry)
                    else:
                        # 没有附件：也要记一行，只填标题/发件人/日期，方便批量导出邮件标题
                        # 到 Excel 做后续处理（比如从标题里解析 container 号）。
                        new_matched += 1
                        if status:
                            status.matched_count = new_matched
                        db.session.add(
                            ManifestEntry(
                                user_id=user.id,
                                run_id=run_id,
                                uid=message_id,
                                original_filename=None,
                                saved_filename=None,
                                sender_name=sender_name,
                                sender_email=sender_email,
                                subject=subject,
                                mail_date=mail_date,
                                message_id=message_id,
                            )
                        )

                    db.session.add(ProcessedMessage(user_id=user.id, uid=message_id))
                    commit_with_retry(db.session)
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

        if download_attachments:
            log(app, user_id, run_id, f"完成：检查了 {checked} 封搜索结果，排除 {excluded} 封（命中发件人排除词），命中 {new_matched} 封邮件，其中下载附件 {new_saved} 个")
        else:
            log(app, user_id, run_id, f"完成（仅导出标题模式，未下载附件）：检查了 {checked} 封搜索结果，排除 {excluded} 封（命中发件人排除词），命中 {new_matched} 封邮件，其中 {matched_with_attachment_flag} 封本身带附件（未下载）")
        if skipped:
            log(app, user_id, run_id, f"有 {skipped} 封邮件处理失败被跳过（下次运行会重试）")
