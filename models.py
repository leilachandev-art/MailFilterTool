"""
models.py
数据库模型。用 SQLite 存：用户（Zoho 身份 + OAuth 令牌 + 筛选配置）、
已处理邮件记录（去重用）、投递清单（附件最终去了哪个 OneDrive 链接）、运行日志。
"""

from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    # ---- Zoho 身份 + OAuth ----
    zoho_email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    zoho_account_id = db.Column(db.String(64), nullable=True)
    zoho_refresh_token = db.Column(db.Text, nullable=True)
    zoho_accounts_server = db.Column(db.String(255), default="https://accounts.zoho.com")
    zoho_api_domain = db.Column(db.String(255), default="https://mail.zoho.com")

    # ---- Microsoft OAuth（OneDrive）----
    ms_refresh_token = db.Column(db.Text, nullable=True)
    ms_account_name = db.Column(db.String(255), nullable=True)

    # ---- 筛选配置 ----
    keywords = db.Column(db.Text, default="invoice, 发票, bill, receipt")
    sender_domains = db.Column(db.Text, default="")
    specific_senders = db.Column(db.Text, default="")
    require_attachment_for_keyword_match = db.Column(db.Boolean, default=True)
    since_date = db.Column(db.String(20), default="")

    precise_mode = db.Column(db.Boolean, default=False)
    precise_subject = db.Column(db.Text, default="")
    precise_sender = db.Column(db.Text, default="")
    precise_attachment = db.Column(db.Text, default="")

    onedrive_folder = db.Column(db.String(255), default="INVOICE-SORTING-RESULT")

    # ---- 投递目标：onedrive / local（服务器磁盘路径，适合自己在本机跑）/ download（浏览器下载 ZIP） ----
    # OneDrive 那条链路还在调试中，暂时先让新用户默认走"打包下载 ZIP"——
    # 网站可以照常集中部署，同事不用在自己电脑装环境、跑代码。
    sync_target = db.Column(db.String(20), default="download")
    local_folder = db.Column(db.String(500), default="")

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def onedrive_connected(self):
        return bool(self.ms_refresh_token)


class ProcessedMessage(db.Model):
    """记录某个用户已经检查过的邮件 UID，避免重复扫描。"""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    uid = db.Column(db.String(64), nullable=False)

    __table_args__ = (db.UniqueConstraint("user_id", "uid", name="uq_user_uid"),)


class ManifestEntry(db.Model):
    """已投递的附件清单，用来在网页上展示 + 保留溯源信息。"""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    run_id = db.Column(db.String(64), nullable=True, index=True)
    uid = db.Column(db.String(64))
    original_filename = db.Column(db.String(500))
    sender = db.Column(db.String(500))
    subject = db.Column(db.String(1000))
    mail_date = db.Column(db.String(200))
    message_id = db.Column(db.String(500))
    onedrive_link = db.Column(db.String(1000))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class RunLog(db.Model):
    """每次运行的日志行，前端轮询展示进度。"""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    run_id = db.Column(db.String(64), index=True)
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class RunStatus(db.Model):
    """当前用户是否有任务正在跑（简单的进程内状态记录）。"""

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    is_running = db.Column(db.Boolean, default=False)
    current_run_id = db.Column(db.String(64), nullable=True)
    stop_requested = db.Column(db.Boolean, default=False)
    checked_count = db.Column(db.Integer, default=0)
    saved_count = db.Column(db.Integer, default=0)
