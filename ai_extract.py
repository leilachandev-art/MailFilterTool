"""
ai_extract.py
用 Claude 的文档理解能力（不是正则/关键词规则）从 PDF 附件里提取用户自己定义的字段，
思路跟 imagetotable.ai 的"定义列名，AI 自动提取"一致：你在网页上填想要哪些字段
（比如"发票号, 金额, 币种, container号"），每份 PDF 附件下载下来后都会调用一次
Claude，把这些字段的值抠出来，不管发票排版长什么样，AI 都是"读懂"了再填，
比之前那套关键词/表格正则准得多。

代价：每处理一份 PDF 附件都会调用一次 Anthropic API，有少量费用和几秒延迟，
需要在 .env 里配置 ANTHROPIC_API_KEY 才能用；没配置的话这个功能自动跳过
（附件本身照常下载，只是提取字段那几列留空)。

用法：extract_line_items_from_pdf(pdf_bytes, field_defs) -> (items_list, error_message_or_None)
field_defs 是 field_config.parse_extract_fields(user.extract_fields) 解析出来的字段定义
列表（每项 {"name":..., "aliases":[...]}），不是单纯的字段名字符串列表——aliases 是这个
字段在不同供应商账单里可能出现的其他叫法，AI 会按优先级依次尝试匹配，具体见 field_config.py
顶部的说明。一份 PDF 可能同时涉及好几个 container（或者除 container 费用外还有别的杂项
费用），所以返回的是一个列表，每一条对应一个 container（或"其他费用"这一组）；最常见的
单 container 场景，列表就只有一条，跟"一份 PDF 一组字段值"是一回事。调用方（mail_sync.py）
应该把 error_message 记到运行日志里——不然调用失败时用户只会看到"提取字段全是空的"，完全
不知道是哪一步出的问题（没装包？Key 不对？模型名不对？配额用完？）。

用法：diagnose() -> str|None，返回"当前配置不满足什么条件"的说明，None 表示配置齐全，
可以在运行前先检查一次，比每份 PDF 都失败一次才发现问题更快。
"""

import base64
import json
import os
import threading

import field_config

try:
    import anthropic

    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

# claude-sonnet-5 是目前的主力模型，文档理解能力强，适合发票这种排版差异很大的场景。
# 如果想省钱可以在 .env 里把 ANTHROPIC_EXTRACT_MODEL 换成更便宜的模型试试效果。
DEFAULT_MODEL = "claude-sonnet-5"

# 部署成网站给多人用之后，可能好几个同事同时点"立即运行"，各自的 PDF 附件会并发
# 调用 Anthropic API——量一大容易撞到账号的限流（rate limit），也可能让服务器
# 短时间内开太多外部请求。这里用一个进程内的信号量把"同时在发起的 API 调用数"
# 卡住，默认最多 3 个同时在跑，多的排队等，不是失败。可以用 AI_EXTRACT_MAX_CONCURRENT
# 环境变量调整。注意：如果 Render 上开了多个 gunicorn worker 进程，这个上限是
# "每个进程"各自的上限，不是跨进程的全局上限（进程之间不共享内存）。
_MAX_CONCURRENT = max(1, int(os.environ.get("AI_EXTRACT_MAX_CONCURRENT", "3") or "3"))
_CONCURRENCY_GATE = threading.Semaphore(_MAX_CONCURRENT)

# 字段名（或者它的备选名称，见 field_config.py）里带这些关键词的，认为它是"container
# (集装箱)号"这一类字段，提取出来以后会顺手用 ISO 6346 校验位算法核对一下格式对不对——
# 不是为了拦截/丢弃 AI 给的答案（万一算法本身有边界情况误判，不该让用户平白少一条数据），
# 而是校验不通过时在日志里提醒一声，让人多留个心眼去核对原件，比什么提示都没有强。
# 关键词列表和判断逻辑统一放在 field_config.py（跟 app.py/mail_sync.py 共用同一份），
# 这里直接复用，不要再自己维护一份，不然改一处忘了改另一处。
_looks_like_container_field = field_config.field_looks_like_container


def _build_letter_values():
    """ISO 6346 字母转数字表：10,12,13,...,38，跳过 11 的倍数（11/22/33）。"""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    result = {}
    v = 10
    for ch in letters:
        if v % 11 == 0:
            v += 1
        result[ch] = v
        v += 1
    return result


_LETTER_VALUES = _build_letter_values()


def _container_checksum_valid(number):
    """按 ISO 6346 标准校验第 11 位校验位。传进来的字符串要先去掉空格/横线，
    长度不是 11 位、或者不是"4 个字母 + 7 位数字"的格式，直接当校验不通过处理。"""
    number = (number or "").upper()
    if len(number) != 11 or not number[:4].isalpha() or not number[4:].isdigit():
        return False
    body, check = number[:10], number[10]
    total = 0
    for i, ch in enumerate(body):
        val = _LETTER_VALUES.get(ch) if ch.isalpha() else int(ch)
        if val is None:
            return False
        total += val * (2 ** i)
    remainder = total % 11
    expected = 0 if remainder == 10 else remainder
    return expected == int(check)


def _get_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not _ANTHROPIC_AVAILABLE:
        return None
    return anthropic.Anthropic(api_key=api_key)


def is_configured():
    """给上层判断要不要在界面上提示"没配置 API Key，这几列会是空的"。"""
    return bool(os.environ.get("ANTHROPIC_API_KEY", "")) and _ANTHROPIC_AVAILABLE


def diagnose():
    """检查当前环境能不能跑 AI 提取，能跑返回 None，不能跑返回具体原因（中文，可直接展示/记日志）。
    常见的两个坑：requirements.txt 更新后没有重新 pip install，或者 .env 改了但没重启进程
    （load_dotenv 只在启动时读一次，改完 .env 必须重新跑 python app.py 才会生效）。"""
    if not _ANTHROPIC_AVAILABLE:
        return "没装 anthropic 这个 Python 包。请在项目的虚拟环境里执行：pip install -r requirements.txt（或者单独 pip install anthropic），然后重新启动 python app.py。"
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "环境变量 ANTHROPIC_API_KEY 是空的。确认 .env 里确实填了这一行，并且改完 .env 之后重新启动了 python app.py（改配置文件不会自动生效，必须重启进程）。"
    return None


def extract_line_items_from_pdf(pdf_bytes, field_defs):
    """field_defs 是字段定义列表，每项 {"name": str, "aliases": [str, ...]}，一般用
    field_config.parse_extract_fields(user.extract_fields) 解析出来。"name" 是这个字段
    最终用来出结果的 key（处理记录表头/导出 Excel 列名/JSON key 都用它）；"aliases" 是
    这个字段在不同供应商账单里可能出现的其他叫法，按优先级从先到后排——比如"金额"这个
    字段，有的账单写"Total Amount"，有的写"Grand Total"，AI 会按这个优先级依次去文档里
    找，找到其中任意一种叫法对应的值就用，不强求文档里非得出现"金额"这两个字。
    跟早期版本的区别：不再假设"一份 PDF 附件只对应一组字段值"——实际业务里，供应商发来的
    账单可能只涉及一个 container，也可能同时涉及好几个 container，还可能除了 container 相关
    费用外，另外还有一些不属于任何具体 container 的杂项费用（文件费、操作费之类）。
    如果 field_defs 里配了个"container 号"这一类字段（靠字段名/备选名关键词识别，不用用户
    特意配置别的开关），会让 AI 按 container 分组，每个 container（以及"其他费用"这一组，
    如果有的话）各自作为返回列表里单独的一条；没配 container 类字段、或者文档本身就只有
    一组的话，返回列表就只有一条，跟旧版本单个 dict 的效果一样，调用方不用为"只有一个
    container"这种最常见的情况特殊处理。
    返回 (items, error)：items 是 list[dict]，至少有一条（哪怕全是 None），每条 dict 的 key
    跟各 field_def 的 "name" 完全一致（不是 aliases），value 是字符串或 None；error 是
    None（成功）或者一句话错误描述（调用失败/解析失败/container 校验位不通过的提示），
    调用方应该把 error 记到运行日志里，方便排查，而不是默默吞掉。"""
    field_names = [f["name"] for f in field_defs]
    empty_item = {name: None for name in field_names}
    if not field_defs:
        return [dict(empty_item)], None

    reason = diagnose()
    if reason:
        return [dict(empty_item)], reason

    client = _get_client()
    if not client:
        return [dict(empty_item)], "无法创建 Anthropic 客户端（不应该发生，如果看到这条请检查 ANTHROPIC_API_KEY）。"

    def _describe_field(f):
        if f["aliases"]:
            alias_str = "、".join(f["aliases"])
            return (
                f"- 「{f['name']}」：这个字段在不同账单里可能用不同的名称表示，按优先级从先到后依次尝试\n"
                f"  匹配这些叫法：{alias_str}；只要在文档里找到其中任意一种叫法对应的值就用，不要求文档里\n"
                f"  必须出现「{f['name']}」这几个字本身。最终结果的 JSON key 用「{f['name']}」这个主名称，\n"
                f"  不要用备选名称当 key。"
            )
        return f"- 「{f['name']}」"

    field_list_block = "\n".join(_describe_field(f) for f in field_defs)
    container_field = next((f["name"] for f in field_defs if field_config.field_looks_like_container(f)), None)

    if container_field:
        grouping_instruction = (
            f"\n重要——分组规则：这份文档可能只涉及一个 container（集装箱），也可能同时涉及多个不同的\n"
            f"container，还可能除了这些 container 相关的费用外，另外还有一些不属于任何具体 container 的\n"
            f"费用（比如文件费、操作费、其他附加费等杂项/共同费用）。请按「{container_field}」分组：\n"
            f"- 同一个 container 名下的所有费用行，作为数组里单独一条；\n"
            f"- 所有不属于任何具体 container 的费用，合并成额外的一条，这一条的「{container_field}」填 null\n"
            f"  （不要为了凑数瞎编一个 container 号）；如果文档里所有费用都能归到某个 container 名下，\n"
            f"  没有这种杂项费用，就不要输出这一条。\n"
            f"- 每一条里，跟这个分组直接相关的字段（比如这个 container 名下的金额）只填这一组自己的值，\n"
            f"  不要把别的 container 的金额也算进来、也不要重复计算；跟整份文档相关、不区分 container 的\n"
            f"  字段（比如发票号、开票日期、供应商名称这类文档级信息），每一条都填一样的值。\n"
            f"- 只有一个 container、且没有其他杂项费用的话，数组就只有一条，正常情况。\n"
            f"怎么判断该不该拆：不要死记某几个固定标题词，因为不同供应商、不同格式用词都不一样——请判断\n"
            f"这份文档整体属于哪种版式：(a) 文档里有一个逐笔列出费用明细的表格，表格每一行会带一个能\n"
            f"对应到具体 container 的栏位（不管这个栏位标题实际叫什么，常见的比如 Container No、Customer\n"
            f"Ref #、Ref No、Booking No 等，这些只是举例，不是穷举），这种版式通常是多 container 账单，\n"
            f"应该按这个栏位的值分组；(b) 文档没有这种逐笔费用明细表，只有一个汇总性质的应付总额\n"
            f"（不管标题实际叫 Amount Paid、Amount Due、Total、还是别的说法），这种版式通常是单 container\n"
            f"账单，只有一组。请通读文档实际内容判断属于哪种版式，不要因为标题文字对不上举例就误判。\n"
        )
    else:
        grouping_instruction = "\n请把整份文档当成一组，只输出一个元素的数组。\n"

    prompt = (
        f"这是一份物流/发票类 PDF 文档，可能来自不同的供应商，排版、用词习惯都可能不一样，不要预设\n"
        f"成跟你之前处理过的某一份长得一样。请从中提取以下字段的值：\n"
        f"{field_list_block}\n"
        f"不管某个字段有没有配置备选名称，都请把字段名/备选名称当成这个字段在语义上代表什么意思的提示，\n"
        f"不是要求文档里必须逐字出现这几个字或者刚好匹配某个备选名称——不同供应商对同一个意思的字段\n"
        f"经常会用完全没列出来的其他说法表达（比如各种语言、缩写、行业内部说法），请优先理解每个字段\n"
        f"实际代表什么信息，在文档里找语义上真正对应的内容，而不是做字面文本比对。\n"
        f"在给出最终答案前，请先在心里把文档通读一遍、定位每个字段在文档里具体对应哪个数字/哪段文字，\n"
        f"再逐个核对一遍，确认没有认错行、认错列、张冠李戴，然后再输出最终结果。\n"
        f"{grouping_instruction}"
        f"要求：\n"
        f"1. 返回一个 JSON 数组，数组每一项是一个 JSON 对象，对象的 key 必须是上面每个字段的主名称\n"
        f"（「」里的那个词，不是备选名称，也不是文档里实际印的字样），value 是提取到的值（字符串）。\n"
        f"2. 如果某个字段在文档里确实找不到，或者你不确定，对应 value 填 null，不要瞎猜、不要编造、"
        f"不要因为「必须给个答案」就选一个不太确定的候选值——错误答案比留空更糟。\n"
        f"3. 金额类字段：只填数字本身（不要带货币符号、不要千分位逗号）；如果字段名没有明确指定是「小计/税金/定金」\n"
        f"这类局部金额，默认取这一条自己对应的最终应付金额，不要跟别的组的金额混在一起、也不要跟半路的\n"
        f"小计、单价、税额搞混。\n"
        f"4. 币种类字段：填三位标准货币代码（如 USD/CAD/CNY），不要填货币符号或全称。\n"
        f"5. 日期类字段：统一按 YYYY-MM-DD 格式输出，不管文档里原始是什么格式。\n"
        f"6. container(集装箱)号类字段：严格按文档上印刷的原样输出，通常是 4 个大写字母紧跟 7 位数字\n"
        f"（比如 EMCU1234567），字母和数字中间不要加空格/横线，也不要把提单号、订单号等其他编号误当成它。\n"
        f"7. 除了这个 JSON 数组，不要输出任何其他文字、不要加 ```json 代码块标记。"
    )

    error = None
    text = None
    try:
        b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
        model = os.environ.get("ANTHROPIC_EXTRACT_MODEL", DEFAULT_MODEL)
        with _CONCURRENCY_GATE:
            resp = client.messages.create(
                model=model,
                max_tokens=4096,
                # 注意：claude-sonnet-5 等新模型已经不支持 temperature 参数了——只要请求里
                # 带上这个字段（不管填几），就会直接报 400 "temperature is deprecated for
                # this model"，所以这里改成不传，靠 prompt 本身（"照文档抄，不要发挥"）
                # 来保证提取结果的稳定性。
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": b64,
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        ).strip()
    except anthropic.APIStatusError as e:
        # API 明确拒绝了这次调用（Key 错、模型名不对、没余额、限流等），把它的原话带出去，
        # 比自己猜"哪里错了"靠谱得多。
        detail = ""
        try:
            detail = e.response.json().get("error", {}).get("message", "")
        except Exception:
            detail = str(e)
        error = f"Anthropic API 返回错误（状态码 {e.status_code}）：{detail or e}"
    except Exception as e:
        error = f"调用 Anthropic API 失败：{type(e).__name__}: {e}"

    if error:
        return [dict(empty_item)], error

    # 走到这里说明 API 调用成功了，剩下的是"模型返回的文本能不能解析成 JSON 数组"这一步。
    MAX_ITEMS = 100  # 安全阀，防止模型异常输出（比如把每一行费用都拆成单独一条）炸出太多记录
    try:
        # 万一模型还是加了 ```json 代码块标记，简单剥掉，避免 json.loads 报错。
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()

        data = json.loads(text)
        if isinstance(data, dict):
            # 模型没按数组格式返回（比如只有一个 container 时偷懒直接返回了单个对象），
            # 兼容一下当成只有一条的数组处理，不算错误——这种情况其实也不算少见。
            data = [data]
        if not isinstance(data, list) or not data:
            raise ValueError("返回的不是非空 JSON 数组")

        items = []
        for raw_item in data[:MAX_ITEMS]:
            if not isinstance(raw_item, dict):
                continue
            item = dict(empty_item)
            for name in field_names:
                val = raw_item.get(name)
                if val is not None:
                    val = str(val).strip()
                    if val and val.lower() != "null":
                        item[name] = val
            items.append(item)
        if not items:
            raise ValueError("数组里没有能用的 JSON 对象")
    except Exception as e:
        preview = (text or "")[:200]
        return [dict(empty_item)], f"模型返回的内容不是合法 JSON 数组，解析失败：{e}。原始返回（截断）：{preview!r}"

    # container 号这类字段格式很固定（ISO 6346 有校验位），能用算法客观核实的就顺手核实一下，
    # 不是为了拦截/覆盖 AI 给的答案（万一是算法本身的边界情况误判，不该让用户平白少一条数据），
    # 只是校验不通过时提醒一声，让人多留个心眼去核对原件，比什么提示都没有强。
    bad_values = []
    for item in items:
        for f in field_defs:
            name = f["name"]
            if not field_config.field_looks_like_container(f) or not item.get(name):
                continue
            for v in [p.strip() for p in item[name].split(",") if p.strip()]:
                compact = v.replace(" ", "").replace("-", "")
                if not _container_checksum_valid(compact):
                    bad_values.append(f"{name}={v}")
    warn = None
    if bad_values:
        warn = f"提示：{'、'.join(bad_values)} 没通过 container 号的校验位算法，建议核对一下原附件（不影响其他字段，已正常记录）。"

    return items, warn
