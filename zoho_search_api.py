"""
zoho_search_api.py
调用 Zoho Mail 官方"搜索邮件"接口（跟你在网页邮箱搜索框里用的是同一套语法），
以及把网站表单里几个独立输入框拼成 Zoho 要求的 searchKey 字符串。

Zoho 搜索语法参考：https://www.zoho.com/mail/help/search-syntax.html
    subject:发票                主题包含"发票"
    fileName:invoice            附件文件名包含"invoice"
    sender:vendor.com           发件人包含"vendor.com"（邮箱或域名都行）
    content:关键词               正文包含"关键词"
    has:attachment               只看带附件的邮件
    fromDate:17-Aug-2026         只看这个日期之后的邮件
    toDate:17-Aug-2026           只看这个日期（含当天）之前的邮件
    条件之间用 :: 连接 = AND，用 :or: 连接 = OR

注意：Zoho 的这套语法没有"不包含/排除"这种反向条件，所以"发件人排除"这个条件
没法拼进 searchKey 里交给 Zoho 处理，只能在拿到 Zoho 返回的结果之后，
由 mail_sync.py 自己再过滤一遍。
"""

from datetime import datetime

import requests


def _headers(access_token):
    return {"Authorization": f"Zoho-oauthtoken {access_token}"}


def _split(text):
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def _dimension(param, values):
    """同一个条件里填了多个逗号分隔的值时，用 Zoho 的 :or: 操作符连起来，
    表示"这些值里满足任意一个就算命中"。"""
    if not values:
        return None
    return ":or:".join(f"{param}:{v}" for v in values)


def build_search_key(
    subject_contains, attachment_contains, sender_contains, content_contains,
    since_date, require_attachment, until_date=None,
):
    """把网站表单里的几个独立输入框拼成 Zoho 要求的 searchKey 字符串。
    不同条件（主题/附件名/发件人/正文/日期/是否带附件）之间用 :: 连接，即必须同时满足。

    until_date（"范围结束"）是可选的：用户没填的话，自动按"运行当天"算，不用每次手动
    填今天的日期；同时也不影响完全没设日期范围（fromDate/toDate 都不填）这种最常见的
    用法——那种情况下这个函数根本不会走到这段逻辑，因为下面只在 since_date 或
    until_date 至少填了一个的时候才会加日期条件。"""
    clauses = []

    subj = _dimension("subject", _split(subject_contains))
    if subj:
        clauses.append(subj)

    fname = _dimension("fileName", _split(attachment_contains))
    if fname:
        clauses.append(fname)

    sender = _dimension("sender", _split(sender_contains))
    if sender:
        clauses.append(sender)

    content = _dimension("content", _split(content_contains))
    if content:
        clauses.append(content)

    if require_attachment:
        clauses.append("has:attachment")

    since_set = False
    if since_date:
        try:
            d = datetime.strptime(since_date, "%Y-%m-%d")
            # Zoho 的 fromDate/toDate 要求 DD-MMM-YYYY 这种格式，比如 17-Aug-2026
            clauses.append(f"fromDate:{d.strftime('%d-%b-%Y')}")
            since_set = True
        except ValueError:
            pass  # 格式不对当没填

    until_raw = (until_date or "").strip()
    if since_set or until_raw:
        # 只有用户设置了日期范围（开始或结束至少填了一个）才需要一个"结束"边界；
        # 完全没设日期范围的话不加 toDate，避免给"随便搜搜看"这种最简单的用法平白加条件。
        until_set = False
        if until_raw:
            try:
                d = datetime.strptime(until_raw, "%Y-%m-%d")
                clauses.append(f"toDate:{d.strftime('%d-%b-%Y')}")
                until_set = True
            except ValueError:
                pass  # 格式不对就当没填，交给下面的"默认今天"兜底
        if not until_set:
            # 没填，或者填了但格式不对——默认按运行当天算。
            clauses.append(f"toDate:{datetime.utcnow().strftime('%d-%b-%Y')}")

    return "::".join(clauses)


def search_messages(access_token, api_domain, account_id, search_key, start=1, limit=200):
    """按 searchKey 拿一页搜索结果（Zoho 按时间倒序返回，最新的在前）。"""
    resp = requests.get(
        f"{api_domain}/api/accounts/{account_id}/messages/search",
        headers=_headers(access_token),
        params={
            "searchKey": search_key,
            "start": start,
            "limit": limit,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])
