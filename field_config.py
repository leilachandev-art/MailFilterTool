"""
field_config.py
"AI 从附件里提取哪些字段"这个配置项的解析/序列化工具，被 app.py（存配置、渲染表单、
导出 Excel 表头）、mail_sync.py（决定要不要跑 AI、要不要按 container 分组）、
ai_extract.py（组装发给 AI 的 prompt）三边共用，避免同一份逻辑抄三份、改一处忘了改
另外两处。

user.extract_fields 存的是一个 JSON 字符串，形如：
    [{"name": "金额", "aliases": ["Total Amount", "Grand Total"]},
     {"name": "币种", "aliases": []}]
"name" 是这个字段最终显示/导出用的名字（处理记录表头、Excel 列名、AI 返回 JSON 的 key
都用它），"aliases" 是可选的备选名称列表，按优先级从先到后排——不同供应商的账单，同一个
意思的字段叫法可能不一样（比如"金额"这个字段，有的账单写"Total Amount"，有的写
"Grand Total"，有的写"Amount Due"），AI 提取时会按这个优先级依次去文档里找，找到其中
任意一种叫法对应的值就用，不强求文档里非得出现你填的这个主名称本身。

兼容老数据：这个字段以前存的是纯逗号分隔的字符串（比如"金额, 币种, container号"），
没有 aliases 这个概念。parse_extract_fields() 解析不了 JSON 的话会自动退回按逗号拆分，
每个字段名当成没有备选名称处理，老用户不用重新配置、不用跑数据库迁移。
"""

import json

CONTAINER_FIELD_KEYWORDS = ("container", "集装箱", "柜号", "箱号")


def parse_extract_fields(text_value):
    """返回 [{"name": str, "aliases": [str, ...]}, ...]，解析失败/空值返回 []。"""
    text_value = (text_value or "").strip()
    if not text_value:
        return []
    try:
        data = json.loads(text_value)
    except (ValueError, TypeError):
        data = None

    if isinstance(data, list):
        result = []
        for item in data:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                aliases = [str(a).strip() for a in (item.get("aliases") or []) if str(a).strip()]
                result.append({"name": name, "aliases": aliases})
            elif isinstance(item, str) and item.strip():
                result.append({"name": item.strip(), "aliases": []})
        return result

    # 不是合法的新格式 JSON 数组，当成老版本纯逗号分隔字符串处理。
    return [{"name": t.strip(), "aliases": []} for t in text_value.split(",") if t.strip()]


def field_names_only(text_value):
    """只要字段名列表（表头/Excel 列名/JSON key 用这个，跟 aliases 无关）。"""
    return [f["name"] for f in parse_extract_fields(text_value)]


def serialize_extract_fields(field_defs):
    """[{"name":..., "aliases":[...]}, ...] -> 存进 user.extract_fields 的 JSON 字符串。
    自动丢弃 name 为空的项、去掉 aliases 里的空字符串——不管前端传上来的数据多脏，
    存进数据库之前都先在这里洗一遍。"""
    cleaned = []
    for f in field_defs or []:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "").strip()
        if not name:
            continue
        aliases = [str(a).strip() for a in (f.get("aliases") or []) if str(a).strip()]
        cleaned.append({"name": name, "aliases": aliases})
    return json.dumps(cleaned, ensure_ascii=False)


def field_looks_like_container(field_def_or_name):
    """判断某个字段是不是 container/集装箱相关——可以传字段名字符串，也可以传
    {"name":..., "aliases":[...]} 这种字段定义；名字或者任意一个备选名里带关键词就算。"""
    if isinstance(field_def_or_name, dict):
        candidates = [field_def_or_name.get("name", "")] + list(field_def_or_name.get("aliases") or [])
    else:
        candidates = [field_def_or_name or ""]
    return any(kw in str(c).lower() for c in candidates for kw in CONTAINER_FIELD_KEYWORDS)
