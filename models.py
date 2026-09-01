"""
models.py
数据库模型。用户身份：Zoho OAuth 登录 + 令牌；筛选配置是基于 Zoho Mail 原生搜索语法的
几个结构化条件（主题包含/附件名包含/发件人包含/发件人排除/日期/是否要求带附件）；
另外存已处理邮件记录（去重用）、投递清单（附件溯源信息 + 下载）、运行日志。
"""

import json
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

DEFAULT_EXTRACT_FIELDS = "金额, 币种, container号"


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    # ---- Zoho 身份 + OAuth ----
    zoho_email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    zoho_account_id = db.Column(db.String(64), nullable=True)
    zoho_refresh_token = db.Column(db.Text, nullable=True)
    zoho_accounts_server = db.Column(db.String(255), default="https://accounts.zoho.com")
    zoho_api_domain = db.Column(db.String(255), default="https://mail.zoho.com")

    # ---- 筛选配置：对应 Zoho 邮箱网页里"复合搜索"用的那几个条件 ----
    # 主题/附件名/发件人这三项，会拼成 Zoho Mail 搜索 API 的 searchKey，交给 Zoho 自己在
    # 服务端搜（跟你在网页邮箱搜索框里用 contains / attachment name / from 是同一套语法）。
    # 逗号分隔多个值时，同一个条件内是"满足任意一个"（OR），不同条件之间是"必须同时满足"（AND）。
    search_subject_contains = db.Column(db.Text, default="")
    search_attachment_contains = db.Column(db.Text, default="")
    search_sender_contains = db.Column(db.Text, default="")
    search_content_contains = db.Column(db.Text, default="")  # 正文包含，对应 Zoho 的 content:

    # "发件人不包含"——Zoho 的搜索语法没有"排除/不包含"这种反向条件，没法交给 Zoho 处理，
    # 所以这个是我们自己的代码在拿到 Zoho 搜索结果之后，再筛一遍、把命中排除词的邮件跳过。
    search_sender_excludes = db.Column(db.Text, default="")

    search_since_date = db.Column(db.String(20), default="")  # YYYY-MM-DD，对应 Zoho 的 fromDate
    # YYYY-MM-DD，对应 Zoho 的 toDate。可选——留空的话运行时会自动按"当天"算，不用用户
    # 每次手动填今天的日期，也不影响"完全不填日期范围"这种最常见的用法。
    search_until_date = db.Column(db.String(20), default="")
    # 对应 Zoho 的 has:attachment。这个工具默认不要求带附件——很多时候只是想批量导出符合
    # 条件的邮件标题（比如按发件人域名筛某个供应商的所有邮件），不管有没有附件都要看到。
    search_require_attachment = db.Column(db.Boolean, default=False)

    # ---- AI 提取字段配置：跟 imagetotable.ai 一样"自己定义想要哪几列"，逗号分隔，
    # 比如"发票号, 金额, 币种, container号"。下载 PDF 附件时会调用 Claude 按这几个
    # 字段名去读文档内容提取，不是关键词/表格正则匹配，排版差异大的发票也能读懂。----
    extract_fields = db.Column(db.Text, default=DEFAULT_EXTRACT_FIELDS)
    # 独立的开关，跟"字段填了哪些"分开存——关掉这个开关时字段列表还留着（下次重新
    # 打开不用重新敲一遍），只是运行时不会真的调用 AI，省下这部分 API 费用。
    ai_extract_enabled = db.Column(db.Boolean, default=True)
    # QB Desktop 导入时的全局默认费用科目，供应商预设里没配科目时使用
    global_qb_account = db.Column(db.String(255), default="Uncategorized Expenses")

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # ---- 访问统计：谁在用这个网站，用来在 /admin/users 里查 ----
    last_login_at = db.Column(db.DateTime, nullable=True)
    login_count = db.Column(db.Integer, default=0)


class ProcessedMessage(db.Model):
    """记录某个用户已经检查过的邮件 id，避免重复扫描。"""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    uid = db.Column(db.String(64), nullable=False)

    __table_args__ = (db.UniqueConstraint("user_id", "uid", name="uq_user_uid"),)


class ManifestEntry(db.Model):
    """命中筛选条件的邮件清单：一封邮件如果有附件，每个附件至少一行——如果这份附件（账单）
    同时涉及多个 container，AI 提取时会按 container 拆分，一个附件在这种情况下会对应好几行
    （每行一个 container 各自的字段值），共用同一个 saved_filename/下载链接，方便按 container
    对账，不用自己再从一份合并账单里手动拆分金额；如果没有附件，也会生成一行
    (original_filename/saved_filename 留空)，保证邮件标题不会因为没有附件而漏掉，
    方便批量导出标题到 Excel 做后续处理。"""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    run_id = db.Column(db.String(64), nullable=True, index=True)
    uid = db.Column(db.String(64))  # 邮件 message id，去重/溯源用

    original_filename = db.Column(db.String(500))  # 附件原始文件名，用于显示
    saved_filename = db.Column(db.String(500))  # 实际存在服务器临时目录里的文件名（重名时会加序号后缀）

    # 附件内容的 SHA256，用来判断"内容完全相同的附件"（同一份发票被转发/抄送/重复
    # 发送，虽然是不同邮件、不同 messageId，但内容一样）。按用户维度查重：内容相同的
    # 一组里，只有发送时间最新的那一封会真正保存文件、调用 AI 提取（is_duplicate=False，
    # 也就是"这一组的正主"）；其余发送时间更早的都标成 is_duplicate=True，不重复占用
    # 磁盘、不重复调用 AI，但这些邮件本身还是各自成一行，不会从处理记录里消失——
    # duplicate_of_id 指向正主那一行，前端下载按钮会复用正主的文件。
    # 没有附件的邮件（只记标题那种）这两个字段都是 None/False。
    content_hash = db.Column(db.String(64), nullable=True, index=True)
    is_duplicate = db.Column(db.Boolean, default=False)
    duplicate_of_id = db.Column(db.Integer, nullable=True)

    sender_name = db.Column(db.String(500))  # 发件人显示名
    sender_email = db.Column(db.String(500))  # 发件人邮箱地址
    subject = db.Column(db.String(1000))
    mail_date = db.Column(db.String(200))
    message_id = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # ---- 用 AI(Claude) 从 PDF 附件里按用户自定义字段名提取出来的信息 ----
    # 存成 JSON 字符串，比如 {"金额": "310.00", "币种": "CAD", "container号": "EMCU1561467"}，
    # key 是提取时 user.extract_fields 里的字段名。用 extracted_fields 这个 property 读取，
    # 不要直接读 extracted_fields_json。拿不准的字段值是 None，不会瞎填。
    extracted_fields_json = db.Column(db.Text, nullable=True)

    @property
    def extracted_fields(self):
        if not self.extracted_fields_json:
            return {}
        try:
            return json.loads(self.extracted_fields_json)
        except (ValueError, TypeError):
            return {}

    @extracted_fields.setter
    def extracted_fields(self, value):
        self.extracted_fields_json = json.dumps(value or {}, ensure_ascii=False)


class VendorFieldPreset(db.Model):
    """按发件人给"AI 从附件里提取哪些字段"配一份专属预设：某封邮件的发件人邮箱/域名命中
    了某条预设，这封邮件的附件就按这条预设的字段列表提取，不用全局那一份（user.extract_fields）；
    没有任何预设命中的发件人，仍然照旧用全局默认字段。用途：不同供应商的账单关心的信息、
    字段叫法都不一样，没必要为了照顾所有供应商硬把全局字段列表配得又长又什么都匹配不准——
    比如 sentfrom@ascendtms.com 只关心 A/B/C 这几个字段，info@freightcom.com 只关心
    D/E/F，两边互不干扰。

    这份预设是全站共享的（不挂在具体某个用户名下）：由管理员统一维护"发件人 → 字段列表"
    这套映射，谁的邮箱/域名命中了，运行时都会自动用上，不需要每个普通用户自己配一遍、也不该
    让普通用户随便改字段（改错了会影响所有人）——普通用户在界面上只能看到"发件人是否命中了
    某条预设"这个提示，看不到、改不了字段列表本身；管理员在专门的卡片里维护增删改，
    具体权限判断在 app.py 的路由里做（is_admin_user()），不是靠前端隐藏。

    match_pattern：可以填完整邮箱地址（比如 info@freightcom.com，大小写不敏感，只精确
    匹配这一个地址），也可以只填域名（比如 ascendtms.com，前面多打的 @ 会自动去掉，
    匹配这个域名下的所有发件人）。判断逻辑统一放在 field_config.pick_field_defs_for_sender()。

    extract_fields 存法跟 User.extract_fields 完全一样（同一套 JSON 格式，同样用
    field_config.parse_extract_fields()/serialize_extract_fields() 解析/序列化），
    不另外维护一套格式，改一处两边都生效。"""

    id = db.Column(db.Integer, primary_key=True)
    match_pattern = db.Column(db.String(255), nullable=False)
    extract_fields = db.Column(db.Text, default="")
    qb_account = db.Column(db.String(255), default="")  # QB Desktop 费用科目名称
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
    saved_count = db.Column(db.Integer, default=0)  # 已下载的附件数
    matched_count = db.Column(db.Integer, default=0)  # 命中筛选条件的邮件数(不管有没有附件)

    # 本次运行目前"已知"的 Zoho 搜索结果总数——Zoho 搜索接口是分页拿的，没法提前知道
    # 精确总数，这个值是每拉一页就累加一次，大多数情况（结果不到 200 封，一页就拿完）
    # 从一开始就是准确的总数；结果特别多、要拿好几页的话，这个数会跟着拉页慢慢涨，
    # 不是从头就"准"，但作为进度条的分母已经够用。给非管理员看的简化进度条用。
    total_count = db.Column(db.Integer, default=0)
    # 上一次运行有没有出过错（看有没有记过"[出错]"开头的日志），给非管理员看的简化
    # 状态用——他们看不到详细日志，只显示"运行正常"还是"上次运行出错"这种粗粒度状态。
    last_run_ok = db.Column(db.Boolean, default=True)
