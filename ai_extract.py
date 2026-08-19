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

用法：extract_fields_from_pdf(pdf_bytes, field_names) -> (result_dict, error_message_or_None)
调用方（mail_sync.py）应该把 error_message 记到运行日志里——不然调用失败时用户只会看到
"提取字段全是空的"，完全不知道是哪一步出的问题（没装包？Key 不对？模型名不对？配额用完？）。

用法：diagnose() -> str|None，返回"当前配置不满足什么条件"的说明，None 表示配置齐全，
可以在运行前先检查一次，比每份 PDF 都失败一次才发现问题更快。
"""

import base64
import json
import os
import threading

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

# 字段名里带这些关键词的，认为它是"container(集装箱)号"这一类字段，提取出来以后会
# 顺手用 ISO 6346 校验位算法核对一下格式对不对——不是为了拦截/丢弃 AI 给的答案
# （万一算法本身有边界情况误判，不该让用户平白少一条数据），而是校验不通过时在日志里
# 提醒一声，让人多留个心眼去核对原件，比什么提示都没有强。
_CONTAINER_FIELD_KEYWORDS = ("container", "集装箱", "柜号", "箱号")


def _looks_like_container_field(field_name):
    lower = (field_name or "").lower()
    return any(kw in lower for kw in _CONTAINER_FIELD_KEYWORDS)


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


def extract_fields_from_pdf(pdf_bytes, field_names):
    """field_names 是字段名列表，比如 ["发票号", "金额", "币种", "container号"]。
    返回 (result, error)：result 是 dict，每个字段的值是字符串或 None（文档里确实没有 /
    模型不确定）；error 是 None（成功）或者一句话错误描述（调用失败/解析失败），
    调用方应该把 error 记到运行日志里，方便排查，而不是默默吞掉。"""
    result = {name: None for name in field_names}
    if not field_names:
        return result, None

    reason = diagnose()
    if reason:
        return result, reason

    client = _get_client()
    if not client:
        return result, "无法创建 Anthropic 客户端（不应该发生，如果看到这条请检查 ANTHROPIC_API_KEY）。"

    field_list_str = "、".join(field_names)
    prompt = (
        f"这是一份物流/发票类 PDF 文档。请从中提取以下字段的值：{field_list_str}。\n"
        f"在给出最终答案前，请先在心里把文档通读一遍、定位每个字段在文档里具体对应哪个数字/哪段文字，\n"
        f"再逐个核对一遍，确认没有认错行、认错列、张冠李戴，然后再输出最终结果。\n"
        f"要求：\n"
        f"1. 只返回一个 JSON 对象，key 必须跟我给的字段名完全一致，value 是提取到的值（字符串）。\n"
        f"2. 如果某个字段在文档里确实找不到，或者你不确定，对应 value 填 null，不要瞎猜、不要编造、"
        f"不要因为「必须给个答案」就选一个不太确定的候选值——错误答案比留空更糟。\n"
        f"3. 金额类字段：只填数字本身（不要带货币符号、不要千分位逗号）；如果字段名没有明确指定是「小计/税金/定金」\n"
        f"这类局部金额，默认取文档里最终应付的总金额（Grand Total / Total Due / Amount Due / 合计 /\n"
        f"应付金额这一类，通常在文档最下方或金额汇总区域），不要跟半路的小计、单价、税额搞混。\n"
        f"4. 币种类字段：填三位标准货币代码（如 USD/CAD/CNY），不要填货币符号或全称。\n"
        f"5. 日期类字段：统一按 YYYY-MM-DD 格式输出，不管文档里原始是什么格式。\n"
        f"6. container(集装箱)号类字段：严格按文档上印刷的原样输出，通常是 4 个大写字母紧跟 7 位数字\n"
        f"（比如 EMCU1234567），字母和数字中间不要加空格/横线，也不要把提单号、订单号等其他编号误当成它。\n"
        f"7. 如果某个字段在文档里有多个值（比如多个 container 号），用英文逗号分隔全部列出，不要遗漏也不要重复。\n"
        f"8. 除了这个 JSON，不要输出任何其他文字、不要加 ```json 代码块标记。"
    )

    error = None
    text = None
    try:
        b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
        model = os.environ.get("ANTHROPIC_EXTRACT_MODEL", DEFAULT_MODEL)
        with _CONCURRENCY_GATE:
            resp = client.messages.create(
                model=model,
                max_tokens=2048,
                # 提取是"照文档抄"的活，不需要创造性，temperature 调到 0 让结果更稳定、
                # 尽量每次读同一份文档都得到一致的答案，而不是偶尔发挥出不一样的解读。
                temperature=0,
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
        return result, error

    # 走到这里说明 API 调用成功了，剩下的是"模型返回的文本能不能解析成 JSON"这一步。
    try:
        # 万一模型还是加了 ```json 代码块标记，简单剥掉，避免 json.loads 报错。
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()

        data = json.loads(text)
        for name in field_names:
            val = data.get(name)
            if val is not None:
                val = str(val).strip()
                if val and val.lower() != "null":
                    result[name] = val
    except Exception as e:
        preview = (text or "")[:200]
        error = f"模型返回的内容不是合法 JSON，解析失败：{e}。原始返回（截断）：{preview!r}"

    if not error:
        # container 号这类字段格式很固定（ISO 6346 有校验位），能用算法客观核实的就顺手核实一下，
        # 不是为了拦截/覆盖 AI 给的答案（万一是算法本身的边界情况误判，不该让用户平白少一条数据），
        # 只是校验不通过时提醒一声，让人多留个心眼去核对原件，比什么提示都没有强。
        bad_values = []
        for name in field_names:
            if not _looks_like_container_field(name) or not result[name]:
                continue
            for v in [p.strip() for p in result[name].split(",") if p.strip()]:
                compact = v.replace(" ", "").replace("-", "")
                if not _container_checksum_valid(compact):
                    bad_values.append(f"{name}={v}")
        if bad_values:
            error = f"提示：{'、'.join(bad_values)} 没通过 container 号的校验位算法，建议核对一下原附件（不影响其他字段，已正常记录）。"

    return result, error
