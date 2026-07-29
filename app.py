"""
webapp_app.py
发票邮件筛选小工具 - 网站版主程序（Flask）。

同事访问网站 -> 点"用 Zoho 登录"完成一次性授权 -> 点"连接 OneDrive"再授权一次 ->
在页面上填筛选条件 -> 点"立即运行" -> 命中的发票附件自动传到各自的 OneDrive。

部署说明见 README_WEBSITE.md。
"""

import io
import os
import secrets
import threading
import zipfile
from datetime import datetime

from flask import Flask, redirect, url_for, session, request, render_template, jsonify, flash, send_file
from sqlalchemy import inspect, text, func
from dotenv import load_dotenv

load_dotenv()

from models import db, User, ManifestEntry, ProcessedMessage, RunLog, RunStatus
import oauth_zoho as zoho_oauth
import oauth_microsoft as ms_oauth
import crypto_util as token_crypto
from mail_sync import run_sync_for_user, DOWNLOADS_ROOT

try:
    from flask_wtf import CSRFProtect

    _CSRF_AVAILABLE = True
except ImportError:
    _CSRF_AVAILABLE = False


def _admin_emails():
    return {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}


def _sqlite_column_ddl(column):
    """把 SQLAlchemy 的 Column 对象转成 SQLite 的 ALTER TABLE ADD COLUMN 类型片段。"""
    col_type = column.type.compile(dialect=db.engine.dialect)
    parts = [col_type]
    default = column.default
    if default is not None and getattr(default, "is_scalar", False):
        default_val = default.arg
        if isinstance(default_val, bool):
            parts.append(f"DEFAULT {1 if default_val else 0}")
        elif isinstance(default_val, (int, float)):
            parts.append(f"DEFAULT {default_val}")
        elif isinstance(default_val, str):
            escaped = default_val.replace("'", "''")
            parts.append(f"DEFAULT '{escaped}'")
    return " ".join(parts)


def _run_lightweight_migrations():
    """SQLite 这边没接 Alembic，db.create_all() 只会建不存在的新表，不会给已经存在的老表加新列。
    这里自动比对 models.py 里声明的列和数据库里实际的列，缺什么自动 ALTER TABLE 补上，
    这样以后再改字段就不用让大家手动删库、丢掉已经保存的处理记录了。"""
    inspector = inspect(db.engine)
    for model in (User, ProcessedMessage, ManifestEntry, RunLog, RunStatus):
        table = model.__table__
        if not inspector.has_table(table.name):
            continue  # 全新的表，db.create_all() 已经按最新结构建好了
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        missing = [c for c in table.columns if c.name not in existing]
        if not missing:
            continue
        with db.engine.begin() as conn:
            for col in missing:
                ddl = _sqlite_column_ddl(col)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}'))
                print(f"[启动迁移] {table.name} 表补上了缺失的列：{col.name}")


def is_admin_user(user):
    """本地保存功能会让人直接读写服务器磁盘，不能对所有登录的同事开放。
    默认只有第一个注册（第一个用 Zoho 登录）的人算管理员；也可以用 ADMIN_EMAILS
    环境变量（逗号分隔的邮箱）显式指定。"""
    if not user:
        return False
    configured = _admin_emails()
    if configured:
        return user.zoho_email.lower() in configured
    first_user = User.query.order_by(User.id.asc()).first()
    return bool(first_user and first_user.id == user.id)


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

    # Render 之类的平台是通过反向代理转发请求的（它们处理 https，转给应用的是内部 http）。
    # 加这个之后 Flask 才能正确识别出外面其实是 https、原始访问域名是什么，
    # 不加的话像 CSRF 的 Referer 校验之类的判断可能会出错。
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db_url = os.environ.get("DATABASE_URL", "sqlite:///app.db")
    # Render 等平台给的 Postgres 连接串有时是 postgres:// 开头，SQLAlchemy 2.x 需要 postgresql://
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # ---- Session / Cookie 安全加固 ----
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # 本机用 http 测试时不能开 Secure（否则 cookie 发不出去导致登录不上）；
    # 部署到有 https 的正式环境时，在 .env 里设 FORCE_HTTPS_COOKIES=1 打开。
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FORCE_HTTPS_COOKIES", "0") == "1"

    db.init_app(app)

    # ---- CSRF 防护：所有会改数据的 POST 表单都强制校验 token ----
    if _CSRF_AVAILABLE:
        CSRFProtect(app)
    else:
        print("[警告] 没装 Flask-WTF，CSRF 防护未启用。建议执行 pip install -r requirements.txt 后重启。")
        # 没装的话模板里的 {{ csrf_token() }} 会报错，先注册一个空实现兜底，不让页面直接崩掉。
        app.jinja_env.globals.setdefault("csrf_token", lambda: "")

    with app.app_context():
        db.create_all()
        _run_lightweight_migrations()
        # 服务器重启前如果有任务还没跑完就被关掉了（比如 Ctrl+C 或崩溃），
        # 对应后台线程已经不存在了，但数据库里 is_running 还残留 True，
        # 不清理的话，网页一打开就会显示"运行中..."，看起来像是自动开始运行了。
        stale = RunStatus.query.filter_by(is_running=True).all()
        if stale:
            for s in stale:
                s.is_running = False
                s.stop_requested = False
            db.session.commit()

    register_routes(app)
    return app


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)


def _wants_json():
    """运行/停止/清空记录/保存配置这几个按钮改成了 AJAX 提交，不想让页面整个刷新跳回顶部。
    前端 fetch 请求会带这个头，后端据此决定是返回 JSON（给 AJAX 用）还是走老的 flash+redirect（没启用 JS 时的兜底）。"""
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _respond(message, ok=True, **extra):
    if _wants_json():
        payload = {"ok": ok, "message": message}
        payload.update(extra)
        return jsonify(payload)
    flash(message)
    return redirect(url_for("dashboard"))


def register_routes(app):
    @app.route("/")
    def index():
        if current_user():
            return redirect(url_for("dashboard"))
        return render_template("login.html")

    # ---------------- Zoho 登录（同时也是网站的登录方式） ----------------

    @app.route("/login/zoho")
    def login_zoho():
        state = secrets.token_hex(16)
        session["zoho_oauth_state"] = state
        try:
            return redirect(zoho_oauth.build_authorize_url(state))
        except RuntimeError as e:
            flash(str(e))
            return redirect(url_for("index"))

    @app.route("/auth/zoho/callback")
    def auth_zoho_callback():
        error = request.args.get("error")
        if error:
            flash(f"Zoho 授权失败：{error}")
            return redirect(url_for("index"))

        state = request.args.get("state")
        if not state or state != session.get("zoho_oauth_state"):
            flash("授权状态校验失败，请重新登录。")
            return redirect(url_for("index"))

        code = request.args.get("code")
        accounts_server = request.args.get("accounts-server", zoho_oauth.DEFAULT_ACCOUNTS_SERVER)

        try:
            token_resp = zoho_oauth.exchange_code_for_token(code, accounts_server)
            access_token = token_resp["access_token"]
            refresh_token = token_resp.get("refresh_token")
            # 注意：Zoho Mail API 的域名和 token 响应里的 api_domain 字段无关，
            # 要从 accounts_server 推导（accounts.zoho.com -> mail.zoho.com）。
            api_domain = zoho_oauth.derive_mail_api_domain(accounts_server)
            email_addr, account_id = zoho_oauth.get_account_info(access_token, api_domain)
        except Exception as e:
            flash(f"Zoho 授权处理失败：{e}")
            return redirect(url_for("index"))

        user = User.query.filter_by(zoho_email=email_addr).first()
        if not user:
            user = User(zoho_email=email_addr)
            db.session.add(user)

        if refresh_token:
            user.zoho_refresh_token = token_crypto.encrypt(refresh_token)
        user.zoho_accounts_server = accounts_server
        user.zoho_api_domain = api_domain
        user.zoho_account_id = account_id
        db.session.commit()

        session["user_id"] = user.id
        return redirect(url_for("dashboard"))

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    # ---------------- Microsoft / OneDrive 连接 ----------------

    @app.route("/connect/microsoft")
    def connect_microsoft():
        if not current_user():
            return redirect(url_for("index"))
        state = secrets.token_hex(16)
        session["ms_oauth_state"] = state
        try:
            return redirect(ms_oauth.build_authorize_url(state))
        except RuntimeError as e:
            flash(str(e))
            return redirect(url_for("dashboard"))

    @app.route("/auth/microsoft/callback")
    def auth_microsoft_callback():
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        error = request.args.get("error")
        if error:
            flash(f"OneDrive 授权失败：{error}")
            return redirect(url_for("dashboard"))

        state = request.args.get("state")
        if not state or state != session.get("ms_oauth_state"):
            flash("授权状态校验失败，请重新连接。")
            return redirect(url_for("dashboard"))

        code = request.args.get("code")
        try:
            token_resp = ms_oauth.exchange_code_for_token(code)
        except Exception as e:
            flash(f"OneDrive 授权处理失败：{e}")
            return redirect(url_for("dashboard"))

        new_ms_refresh_token = token_resp.get("refresh_token")
        if new_ms_refresh_token:
            user.ms_refresh_token = token_crypto.encrypt(new_ms_refresh_token)
        account = token_resp.get("id_token_claims", {})
        user.ms_account_name = account.get("preferred_username") or account.get("name")
        db.session.commit()

        flash("OneDrive 连接成功。")
        return redirect(url_for("dashboard"))

    # ---------------- 仪表盘 / 配置 / 运行 ----------------

    RECORDS_PAGE_SIZE = 20

    @app.route("/dashboard")
    def dashboard():
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        # 处理记录按"邮件"(uid)分组显示：同一封邮件命中多个附件时合并成一行，
        # 并支持分页，能翻到本次运行/以前所有运行产生的全部记录，不再只截前 50 条。
        page = request.args.get("page", 1, type=int) or 1
        if page < 1:
            page = 1

        grouped_q = (
            db.session.query(ManifestEntry.uid, func.max(ManifestEntry.created_at).label("latest"))
            .filter(ManifestEntry.user_id == user.id)
            .group_by(ManifestEntry.uid)
            .order_by(func.max(ManifestEntry.created_at).desc())
        )
        total_emails = grouped_q.count()
        total_pages = max(1, (total_emails + RECORDS_PAGE_SIZE - 1) // RECORDS_PAGE_SIZE)
        page = min(page, total_pages)
        page_uids = [
            row.uid
            for row in grouped_q.offset((page - 1) * RECORDS_PAGE_SIZE).limit(RECORDS_PAGE_SIZE).all()
        ]

        grouped_entries = []
        if page_uids:
            rows = ManifestEntry.query.filter(
                ManifestEntry.user_id == user.id, ManifestEntry.uid.in_(page_uids)
            ).all()
            by_uid = {}
            for e in rows:
                by_uid.setdefault(e.uid, []).append(e)
            for uid in page_uids:
                items = sorted(by_uid.get(uid, []), key=lambda e: e.created_at)
                if not items:
                    continue
                first = items[0]
                grouped_entries.append(
                    {
                        "sender": first.sender,
                        "subject": first.subject,
                        "mail_date": first.mail_date,
                        "created_at": max(e.created_at for e in items),
                        "attachments": items,
                    }
                )

        status = RunStatus.query.get(user.id)
        return render_template(
            "dashboard.html",
            user=user,
            entries=grouped_entries,
            status=status,
            is_admin=is_admin_user(user),
            page=page,
            total_pages=total_pages,
            total_emails=total_emails,
        )

    @app.route("/config", methods=["POST"])
    def save_config():
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        user.keywords = request.form.get("keywords", "")
        user.sender_domains = request.form.get("sender_domains", "")
        user.specific_senders = request.form.get("specific_senders", "")
        user.require_attachment_for_keyword_match = bool(request.form.get("require_attachment"))
        user.since_date = request.form.get("since_date", "").strip()
        user.attachment_name_filter = request.form.get("attachment_name_filter", "")

        user.precise_mode = bool(request.form.get("precise_mode"))
        user.precise_subject = request.form.get("precise_subject", "")
        user.precise_sender = request.form.get("precise_sender", "")
        user.precise_attachment = request.form.get("precise_attachment", "")

        user.onedrive_folder = request.form.get("onedrive_folder", "INVOICE-SORTING-RESULT").strip() or "INVOICE-SORTING-RESULT"

        # 投递方式现在只保留"打包下载 ZIP"，OneDrive 还在调试、本地路径只适合单机自跑，
        # 都不再通过界面暴露，这里直接固定成 download，不再接受表单传来的值。
        user.sync_target = "download"

        db.session.commit()
        return _respond("配置已保存。")

    @app.route("/run", methods=["POST"])
    def run_now():
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        status = RunStatus.query.get(user.id)
        if status and status.is_running:
            return _respond("已经有一个任务在运行了，请等它跑完。", ok=False)

        app = current_app_ref["app"]
        thread = threading.Thread(target=run_sync_for_user, args=(app, user.id), daemon=True)
        thread.start()

        return _respond("已开始运行，下面的日志会自动刷新。")

    @app.route("/download/<run_id>")
    def download_run(run_id):
        """把某一次运行命中的附件打包成 ZIP，走浏览器正常下载流程发给用户 ——
        不管网站部署在哪台服务器上，下载下来的文件天然就落在用户自己电脑的下载文件夹里。"""
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        exists = ManifestEntry.query.filter_by(user_id=user.id, run_id=run_id).first()
        if not exists:
            flash("找不到这次运行的记录。")
            return redirect(url_for("dashboard"))

        folder = os.path.join(DOWNLOADS_ROOT, str(user.id), run_id)
        if not os.path.isdir(folder) or not os.listdir(folder):
            flash("这次运行的文件已经不在服务器上了（可能是投递方式不是「打包下载」，或者临时文件已被清理），建议重新运行一次。")
            return redirect(url_for("dashboard"))

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in os.listdir(folder):
                fpath = os.path.join(folder, name)
                if os.path.isfile(fpath):
                    zf.write(fpath, arcname=name)
        buf.seek(0)

        return send_file(
            buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"invoices_{run_id}.zip",
        )

    @app.route("/stop", methods=["POST"])
    def stop_run():
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        status = RunStatus.query.get(user.id)
        if status and status.is_running:
            status.stop_requested = True
            db.session.commit()
            return _respond("已发送停止请求，正在停止当前运行 ...")
        return _respond("当前没有正在运行的任务。", ok=False)

    @app.route("/clear_processed", methods=["POST"])
    def clear_processed():
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        status = RunStatus.query.get(user.id)
        if status and status.is_running:
            return _respond("有任务正在运行，请先停止或等它跑完再清空处理记录。", ok=False)

        deleted = ProcessedMessage.query.filter_by(user_id=user.id).delete()
        db.session.commit()
        return _respond(f"已清空处理记录（{deleted} 条）。下次运行会重新扫描所有历史邮件，已经保存过的附件可能会重复保存一份。")

    @app.route("/run/status")
    def run_status():
        user = current_user()
        if not user:
            return jsonify({"error": "not logged in"}), 401

        status = RunStatus.query.get(user.id)
        is_running = bool(status and status.is_running)
        run_id = status.current_run_id if status else None

        logs = []
        if run_id:
            rows = (
                RunLog.query.filter_by(user_id=user.id, run_id=run_id)
                .order_by(RunLog.created_at.asc())
                .limit(500)
                .all()
            )
            logs = [r.message for r in rows]

        download_url = None
        if run_id and user.sync_target == "download" and (status.saved_count if status else 0):
            download_url = url_for("download_run", run_id=run_id)

        return jsonify(
            {
                "is_running": is_running,
                "logs": logs,
                "checked": status.checked_count if status else 0,
                "saved": status.saved_count if status else 0,
                "download_url": download_url,
            }
        )


# 用一个小 dict 存 app 引用，方便在 /run 路由里拿到 app 传给后台线程
# （避免 Flask 的 current_app 代理对象跨线程使用的问题）
current_app_ref = {}

app = create_app()
current_app_ref["app"] = app


if __name__ == "__main__":
    # Werkzeug 的 debug 交互调试器如果暴露在公网上有远程执行代码的风险，
    # 默认关闭；只有显式在 .env 里设 FLASK_DEBUG=1 才会打开（仅本机开发时用）。
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    # 每个同事都在自己电脑上单独跑一份，默认只监听 127.0.0.1（只有这台电脑自己能访问），
    # 不用 0.0.0.0（那样同一个 WiFi/局域网里的其他人也能连进来，等于把自己的邮箱工具暴露给了旁人）。
    # 以后如果真的要集中部署到云端服务器，再在 .env 里设 HOST=0.0.0.0。
    host = os.environ.get("HOST", "127.0.0.1")
    # threaded=True：不然 Flask 自带的开发服务器同一时间只能处理一个请求，界面会卡。
    app.run(host=host, port=int(os.environ.get("PORT", 5000)), debug=debug_mode, threaded=True)
