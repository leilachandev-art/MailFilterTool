"""
mail_sync.py
核心同步逻辑：把用户填的几个筛选条件拼成 Zoho 搜索语法，通过 Zoho 官方"搜索邮件"接口
直接在服务端搜（不用自己把整个邮箱拉下来再筛），命中的邮件再检查"发件人排除"这个
Zoho 语法不支持的反向条件，最后把符合条件的附件下载到服务器临时目录，
运行结束后可以逐个下载、打包 ZIP 下载、或者导出 Excel。

用法：run_sync_for_user(flask_app, user_id, run_id) 在后台线程里调用。
"""

import hashlib
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests

import oauth_zoho as zoho_oauth
import zoho_mail_api as zmail
import zoho_search_api as zsearch
import crypto_util as token_crypto
import ai_extract
import field_config
from models import db, User, ProcessedMessage, ManifestEntry, RunLog, RunStatus, VendorFieldPreset
from db_utils import commit_with_retry

PAGE_SIZE = 200
MAX_PAGES = 25  # 安全阀，避免筛选条件太宽泛时无限翻页

# ---- 进程内停止标志（解决 SQLite 锁竞争时"停止"按钮失效的问题） ----
# 问题背景：stop_run 路由需要往 SQLite 写 stop_requested=True，但后台同步线程会持续
# 频繁写库，两者抢锁时 SQLite 会等最多 30 秒（busy_timeout）——路由迟迟拿不到锁就无法
# 响应，浏览器 fetch 超时报 "TypeError: Failed to fetch"。
# 解决方案：同时在进程内存里维护一个停止标志字典；stop_run 路由先设内存标志（立刻生效、
# 不涉及任何数据库操作），再异步尝试写库；后台线程的 _stop_requested() 先查内存标志，
# 查到就立刻停，不再等数据库——这样"停止"按钮点下去几乎立刻就能响应。
_stop_flags: dict = {}          # user_id -> bool，进程内共享
_stop_flags_lock = threading.Lock()

# ---- 运行期错误计数（解决"运行中无法感知后台出错"的问题）----
# log() 每次写 [出错] 前缀时在内存里累加；/run/status 接口把这个数字下发给前端，
# 前端在运行期实时展示"已有 X 个错误"，不用等运行结束后才能看到 last_run_ok。
_run_error_counts: dict = {}    # (user_id, run_id) -> int
_run_error_lock = threading.Lock()


def get_run_error_count(user_id: int, run_id: str) -> int:
    with _run_error_lock:
        return _run_error_counts.get((user_id, run_id), 0)


def _increment_run_error(user_id: int, run_id: str) -> None:
    with _run_error_lock:
        key = (user_id, run_id)
        _run_error_counts[key] = _run_error_counts.get(key, 0) + 1


def _clear_run_errors(user_id: int, run_id: str) -> None:
    with _run_error_lock:
        _run_error_counts.pop((user_id, run_id), None)


def request_stop(user_id: int) -> None:
    """从 Flask 路由调用，立即在内存里设置停止标志，不依赖数据库写入成功。"""
    with _stop_flags_lock:
        _stop_flags[user_id] = True


def _clear_stop_flag(user_id: int) -> None:
    """新一次运行开始时清掉上一次遗留的停止标志，避免"刚点了停止，再点运行，立刻又被停"。"""
    with _stop_flags_lock:
        _stop_flags.pop(user_id, None)

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
    is_error = message.startswith("[出错]")
    if is_error:
        # 内存计数立即更新，不依赖数据库写入成功——前端轮询 /run/status 就能
        # 实时拿到"运行中已出现 X 个错误"的数字，不用等运行结束。
        _increment_run_error(user_id, run_id)

    with app.app_context():
        try:
            db.session.add(RunLog(user_id=user_id, run_id=run_id, message=message))
            if is_error:
                # 非管理员看不到详细日志，只看得到"运行正常"还是"上次运行出错"这种粗粒度
                # 状态，这里顺手记一下。一次运行里只要出过一次错就标 False，不会被同一次
                # 运行里后面的正常日志重新翻回 True（要等下一次运行开始时才重置）。
                # 用 no_autoflush 包住这个查询：SQLAlchemy 默认在执行任何查询前会先把
                # session 里待提交的修改 flush 到数据库；但此时外层同步循环里可能已经
                # add() 了一些还没 commit 的对象（比如刚下载完某个附件对应的
                # ManifestEntry），autoflush 触发时如果数据库被另一个线程（比如前端的
                # poll 请求）持有写锁，就会直接拿到 "database is locked" 错误，导致
                # 整条日志写库失败、控制台打出 "[写日志失败]"。no_autoflush 只是告诉
                # SQLAlchemy "这个查询之前不要自动 flush"，不影响后面的 commit，
                # 也不会让那些待提交的对象丢失——它们还在 session 里，等正常的
                # commit_with_retry 提交。
                with db.session.no_autoflush:
                    status = RunStatus.query.get(user_id)
                if status:
                    status.last_run_ok = False
            commit_with_retry(db.session)
        except Exception as e:
            db.session.rollback()
            print(f"[写日志失败，仅打印到控制台] user={user_id} run={run_id}: {message} (写库报错: {e})")


def run_sync_for_user(app, user_id, run_id=None, download_attachments=True):
    """download_attachments=False 时是"仅导出标题"快速模式：跳过附件元数据查询和下载，
    只用 Zoho 搜索结果本身自带的 subject/sender/date 记录，命中多少邮件几乎是秒级的，
    适合只是想要批量拿邮件标题（比如从标题里解析 container 号）去做后续处理的场景。"""
    _clear_stop_flag(user_id)  # 每次新运行开始时清掉上一次遗留的停止标志
    run_id = run_id or uuid.uuid4().hex[:12]
    _clear_run_errors(user_id, run_id)  # 清掉同一个 run_id 可能遗留的旧计数（理论上不会，防御性清理）

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
            status.total_count = 0
            status.last_run_ok = True
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
    # 先查内存标志（不涉及数据库，立刻知道结果）；再查数据库做兜底
    with _stop_flags_lock:
        if _stop_flags.get(user_id):
            return True
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
        attachment_kws = _split(user.search_attachment_contains)  # 附件文件名关键词（客户端二次过滤）
        # ai_extract_enabled 是独立于"填了哪些字段"的开关：关掉的话字段列表还留着（下次
        # 重新打开不用重新敲一遍），只是这次运行不会真的调用 AI，也就不会有对应费用。
        # configured_field_defs 是 [{"name":..., "aliases":[...]}, ...] 这种结构化的字段
        # 定义（见 field_config.py）——aliases 是这个字段在不同供应商账单里可能出现的
        # 其他叫法，按优先级排，AI 提取时会依次尝试；只在界面上填了主名称、没填备选名称
        # 的话 aliases 是空列表，效果跟老版本完全一样。
        configured_field_defs = field_config.parse_extract_fields(user.extract_fields)
        configured_field_names = [f["name"] for f in configured_field_defs]
        extract_field_defs = configured_field_defs if user.ai_extract_enabled else []

        # 按发件人配的字段预设：邮箱/域名命中了哪条，这封邮件的附件就按那条预设的字段
        # 提取，不用全局默认的 extract_field_defs——具体匹配规则见
        # field_config.pick_field_defs_for_sender()。这份预设是全站共享的（管理员统一
        # 维护，不分用户），所以这里不按 user_id 过滤。跟全局字段一样受 ai_extract_enabled
        # 这个总开关控制：开关关了，预设也一起停，不会出现"关了 AI 提取，但配了预设的
        # 供应商还在偷偷调用"这种情况。
        vendor_presets = []
        if user.ai_extract_enabled:
            vendor_presets = [
                (p.match_pattern, field_config.parse_extract_fields(p.extract_fields))
                for p in VendorFieldPreset.query.order_by(VendorFieldPreset.id.asc()).all()
            ]

        if download_attachments and (configured_field_defs or vendor_presets):
            if not user.ai_extract_enabled:
                log(
                    app, user_id, run_id,
                    f"[提示] AI 提取字段功能当前是关闭状态（已配置字段：{'、'.join(configured_field_names)}），"
                    f"如需开启去筛选条件里勾选\"启用 AI 提取字段\"。",
                )
            else:
                ai_problem = ai_extract.diagnose()
                if ai_problem:
                    log(app, user_id, run_id, f"[提示] AI 提取字段这个功能现在用不了：{ai_problem}")
                else:
                    log(app, user_id, run_id, f"下载 PDF 附件时会用 AI 提取这些字段（默认）：{'、'.join(configured_field_names) or '（未配置，只有命中下面预设的发件人才会提取字段）'}")
                    fields_with_aliases = [f["name"] for f in configured_field_defs if f["aliases"]]
                    if fields_with_aliases:
                        log(
                            app, user_id, run_id,
                            f"[提示] 这些字段配了备选名称，AI 会按优先级依次尝试匹配文档里实际出现的叫法："
                            f"{'、'.join(fields_with_aliases)}。",
                        )
                    if vendor_presets:
                        preset_desc = "；".join(
                            f"{pattern} → {'、'.join(f['name'] for f in field_defs) or '（未配置字段）'}"
                            for pattern, field_defs in vendor_presets
                        )
                        log(
                            app, user_id, run_id,
                            f"[提示] 已配置 {len(vendor_presets)} 个按发件人的字段预设，发件人邮箱/域名命中时"
                            f"改用预设自己的字段列表（不叠加默认字段）：{preset_desc}",
                        )
                    # 注意：这条提示只是「顺手告诉你有 container 类字段」，不代表只有配了这种
                    # 字段名的用户才会拆行——AI 现在会自己判断配置的字段里有没有哪个是「每笔
                    # 业务各自独有的标识号」（不只是叫 container/集装箱的字段，Customer Ref./
                    # Tracking #/BOL # 这些叫法一样认得出来），只要账单里这个字段的值真的对不上
                    # 同一个值，就会按这个字段拆成多行，不需要字段名里必须出现 container 这个词。
                    log(
                        app, user_id, run_id,
                        "[提示] 如果某份附件的账单里，能标识「是哪一笔业务」的那个字段（比如"
                        "container 号、Customer Ref./参考号、Tracking #、提单号等，不限于字段名"
                        "叫 container 的）出现了好几个不同的值，AI 会自动按这个字段拆成多行分别"
                        "记录金额，不会混在一起；只有一笔业务的话正常输出一行。",
                    )

        download_dir = os.path.join(DOWNLOADS_ROOT, str(user_id), run_id)
        if download_attachments:
            os.makedirs(download_dir, exist_ok=True)

        processed_uids = {
            row.uid for row in ProcessedMessage.query.filter_by(user_id=user.id).all()
        }

        # 内容去重用：hash -> 这一组里"正主"（发送时间最新、真正保存了文件的那一份附件）
        # 对应的所有 ManifestEntry（一份附件如果按 container 拆成了好几行，这里是那几行
        # 的列表，不是单独一行）。先一次性查出来放内存里，后面每个附件算完哈希直接查这个
        # dict，不用每个附件都单独查一次数据库；组内谁是正主发生变化时（发现了发送时间
        # 更新的同内容附件）也在内存里同步更新，不用重新查库。
        canonical_by_hash = {}
        for row in (
            ManifestEntry.query.filter_by(user_id=user.id, is_duplicate=False)
            .filter(ManifestEntry.content_hash.isnot(None))
            .all()
        ):
            canonical_by_hash.setdefault(row.content_hash, []).append(row)

        status = RunStatus.query.get(user_id)

        new_saved = 0
        new_matched = 0
        matched_with_attachment_flag = 0
        skipped = 0
        excluded = 0
        duplicate_attachments = 0
        checked = 0
        stop = False

        def _parse_mail_epoch(value):
            """sentDateInGMT 正常情况下是毫秒级时间戳字符串，转不了数字就当"不确定"，
            返回 None——遇到 None 时，比较发送时间新旧这一步会保守地不去"提正主"，
            避免因为个别邮件的时间格式异常而误判谁是最新版本。"""
            try:
                return int(str(value).strip())
            except (TypeError, ValueError):
                return None

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
            if status:
                # 累加"目前已知的搜索结果总数"，给非管理员看的简化进度条当分母。结果不到
                # 一页（最常见的情况）从这里就是准确总数了；结果特别多要翻好几页的话，
                # 这个数会跟着每次翻页慢慢涨，不是从一开始就"准"，但够用来看大概进度。
                status.total_count += len(messages)
                commit_with_retry(db.session)

            for msg in messages:
                # 以前这里是"每 20 封才查一次"，图省一点数据库查询——但现在每封邮件本身
                # 可能就要下载附件、调 AI 提取字段，一封处理下来就要好几秒，20 封累计
                # 下来能到几分钟，点了"停止"之后要等很久才会真的停，感觉像是没生效。
                # 这里改成每封邮件都查一次：多出来的只是一次按主键查的轻量查询，跟一次
                # 网络下载/AI 调用比起来完全不算什么，但停止的响应速度能从"分钟级"降到
                # "几秒内"。
                if _stop_requested(user_id):
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
                        # 这封邮件的发件人如果命中了某条按 vender 配的字段预设，这封邮件的所有
                        # 附件都按那条预设的字段列表提取，不用全局默认的 extract_field_defs——
                        # 同一封邮件里的附件共用同一个发件人，只用算一次。
                        this_field_defs = field_config.pick_field_defs_for_sender(
                            sender_email, extract_field_defs, vendor_presets
                        )
                        # 有附件：每个附件先算出它对应几"行"（一份账单可能横跨好几个 container，
                        # 每个 container 拆成单独一行，方便后续按 container 对账/筛选/汇总；
                        # 具体拆几行由 ai_extract.extract_line_items_from_pdf 决定，没有 container
                        # 类字段或者本来就只有一个 container 的话，就是最常见的"一份附件一行"）。
                        for a in attachments:
                            filename = safe_filename(a.get("attachmentName", "attachment"))
                            # Zoho 的 fileName 搜索是"这封邮件里至少一个附件的文件名匹配"，
                            # 但返回的是整封邮件的所有附件——这里做客户端二次过滤，
                            # 只保留文件名里包含用户指定关键词的附件，跳过同邮件里不匹配的其他附件。
                            if attachment_kws and not _match_any(filename, attachment_kws):
                                continue
                            content = zmail.download_attachment(
                                access_token, user.zoho_api_domain, user.zoho_account_id,
                                msg_folder_id, message_id, a.get("attachmentId"),
                            )
                            content_hash = hashlib.sha256(content).hexdigest()
                            canonical_group = canonical_by_hash.get(content_hash)

                            # 判断这一份跟已经见过的同内容附件比，谁的发送时间更新——
                            # 只有"目前最新"的那一份才会真正落盘、调用 AI；较旧的那些
                            # 标成重复，不重复占存储、不重复花 AI 调用的钱，但这封邮件
                            # 本身还是单独成行（有几个 container 就还是几行），不会从
                            # 处理记录里彻底消失。
                            this_epoch = _parse_mail_epoch(mail_date)
                            canon_epoch = _parse_mail_epoch(canonical_group[0].mail_date) if canonical_group else None
                            promote = (
                                canonical_group is not None
                                and this_epoch is not None
                                and canon_epoch is not None
                                and this_epoch > canon_epoch
                            )
                            is_new_canonical = canonical_group is None or promote

                            if is_new_canonical:
                                saved_path = _save_local(download_dir, filename, content)
                                saved_filename = os.path.basename(saved_path)

                                if canonical_group is not None:
                                    # 之前那份是"正主"，现在发现了发送时间更新的同内容附件，
                                    # 内容既然完全一样就不用再花钱调一次 AI 重新拆分 container，
                                    # 直接把之前拆好的每一条结果搬过来用；同时把旧正主那一组
                                    # 全部标成重复，指向新正主。
                                    items = [dict(e.extracted_fields) for e in canonical_group]
                                    duplicate_attachments += 1
                                    log(
                                        app, user_id, run_id,
                                        f"[提示] 附件 {filename} 与之前处理过的一份内容相同，但这封邮件发送时间更新，"
                                        f"已改为保留这一份（原来那份改标记为重复）。",
                                    )
                                else:
                                    # 只对 PDF 附件调用 AI 按用户自定义的字段名提取；没配置好的话
                                    # extract_line_items_from_pdf 会带着具体原因回来，记进日志方便
                                    # 排查，不管提取成不成功都不影响附件本身已经下载成功这件事。
                                    items = [{}]
                                    if filename.lower().endswith(".pdf") and this_field_defs:
                                        items, extract_error = ai_extract.extract_line_items_from_pdf(
                                            content, this_field_defs
                                        )
                                        if extract_error:
                                            # 区分两种情况：
                                            # 1. 真正的 AI 调用失败（API Key 无效/模型名错误/网络超时等）：
                                            #    items 里所有字段都是 None——用 [出错] 前缀记日志，
                                            #    这样 RunStatus.last_run_ok 会被标成 False，非管理员
                                            #    用户也能看到"上次运行出错了"的提示，而不是默默
                                            #    看到一堆空字段、不知道为什么。
                                            # 2. 提取成功但有格式校验提示（比如 container 号校验位不对）：
                                            #    items 里有至少一个非空字段——用 [提示] 记日志，
                                            #    不算运行出错，只是建议人工核对一下原件。
                                            all_extracted_empty = all(
                                                v is None
                                                for item in items
                                                for v in item.values()
                                            )
                                            log_prefix = "[出错]" if all_extracted_empty else "[提示]"
                                            log(app, user_id, run_id, f"{log_prefix} 附件 {filename} 的 AI 提取：{extract_error}（附件本身已正常下载）")
                                        elif len(items) > 1:
                                            log(app, user_id, run_id, f"[提示] 附件 {filename} 识别到 {len(items)} 个 container/费用分组，已按 container 拆成 {len(items)} 行。")

                                new_saved += 1
                                new_matched += 1
                                if status:
                                    status.saved_count = new_saved
                                    status.matched_count = new_matched

                                new_group = []
                                for item_fields in items:
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
                                        content_hash=content_hash,
                                        is_duplicate=False,
                                    )
                                    entry.extracted_fields = item_fields
                                    db.session.add(entry)
                                    new_group.append(entry)
                                # 立刻 flush 拿到这一组每一行的 id——不管是不是"顶替"了旧正主，
                                # 万一同一批消息里紧接着还有别的邮件命中同一个哈希，后面马上就要用
                                # 这一组第一行的 id 当 duplicate_of_id，不 flush 的话这里还是 None。
                                db.session.flush()
                                if canonical_group is not None:
                                    for old_entry in canonical_group:
                                        old_entry.is_duplicate = True
                                        old_entry.duplicate_of_id = new_group[0].id
                                canonical_by_hash[content_hash] = new_group
                            else:
                                # 内容跟已有的正主一样，且发送时间没有更新：不重复下载落盘、
                                # 不重复调用 AI，直接照搬正主那一组已经按 container 拆好的每一条，
                                # 这些行只标记为"重复"，下载链接会指向正主那一组的文件。
                                duplicate_attachments += 1
                                new_matched += 1
                                if status:
                                    status.matched_count = new_matched
                                log(
                                    app, user_id, run_id,
                                    f"[提示] 附件 {filename} 与另一封邮件的附件内容相同，已保留发送时间更晚的那份，本行标记为重复。",
                                )
                                for item_fields in [dict(e.extracted_fields) for e in canonical_group]:
                                    entry = ManifestEntry(
                                        user_id=user.id,
                                        run_id=run_id,
                                        uid=message_id,
                                        original_filename=filename,
                                        saved_filename=None,
                                        sender_name=sender_name,
                                        sender_email=sender_email,
                                        subject=subject,
                                        mail_date=mail_date,
                                        message_id=message_id,
                                        content_hash=content_hash,
                                        is_duplicate=True,
                                        duplicate_of_id=canonical_group[0].id,
                                    )
                                    entry.extracted_fields = item_fields
                                    db.session.add(entry)
                    else:
                        # 没有附件：正常情况下也要记一行，方便批量导出邮件标题到 Excel。
                        # 但如果用户设了"附件文件名含"筛选条件，说明他只想看带特定附件的邮件，
                        # 这种没有附件的邮件就不该出现在结果里——跳过，不记录也不标已处理，
                        # 这样下次运行如果换了筛选条件还能重新捡起来。
                        if attachment_kws:
                            continue
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
            if duplicate_attachments:
                log(app, user_id, run_id, f"其中 {duplicate_attachments} 个附件与其他邮件内容重复，已标记为重复行（未重复下载/未重复调用 AI）")
        else:
            log(app, user_id, run_id, f"完成（仅导出标题模式，未下载附件）：检查了 {checked} 封搜索结果，排除 {excluded} 封（命中发件人排除词），命中 {new_matched} 封邮件，其中 {matched_with_attachment_flag} 封本身带附件（未下载）")
        if skipped:
            log(app, user_id, run_id, f"有 {skipped} 封邮件处理失败被跳过（下次运行会重试）")
