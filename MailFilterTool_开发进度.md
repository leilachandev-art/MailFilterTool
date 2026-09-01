# MailFilterTool 开发进度存档

**存档日期**：2026-08-31  
**项目路径**：`E:\logistic\MailFilterTool`  
**技术栈**：Flask + SQLAlchemy（本地 SQLite / 线上 PostgreSQL on Render）

---

## 一、项目概览

MailFilterTool 是一个邮件自动同步与附件提取工具，连接 Zoho Mail，按用户配置的关键词筛选邮件和附件，并可选用 AI 从附件（PDF/图片）中提取结构化字段，结果展示在 Web 控制台并可导出 Excel。

核心文件：
| 文件 | 职责 |
|------|------|
| `app.py` | Flask 路由、用户配置、处理记录渲染 |
| `mail_sync.py` | Zoho API 拉取邮件、附件过滤、写入数据库 |
| `ai_extract.py` | AI 提取附件字段（调用 LLM） |
| `field_config.py` | 字段定义解析/序列化、供应商预设匹配 |
| `templates/dashboard.html` | 主控制台页面 |
| `static/style.css` | 三页共用样式表 |

---

## 二、已完成的功能与修复

### 1. 附件文件名关键词过滤（客户端二次过滤）
**问题**：Zoho `fileName` 搜索参数只保证"至少有一个附件匹配"，但会把同一封邮件的所有附件都返回，导致不含关键词的附件也被处理/入库。

**修复位置**：`mail_sync.py`

**修复内容**：
- 在附件循环内加客户端过滤：
  ```python
  attachment_kws = _split(user.search_attachment_contains)
  # ...
  if attachment_kws and not _match_any(filename, attachment_kws):
      continue
  ```
- 对无附件的邮件，若附件关键词已设置则整封跳过：
  ```python
  if attachment_kws:
      continue
  ```

---

### 2. 处理记录表格双向滚动 + 表头固定
**问题**：处理记录区域只有内容区域，列多时横向溢出，纵向无法限高，表头不固定。

**修复位置**：`static/style.css` + `templates/dashboard.html`

**修复内容**：
- CSS `.table-scroll` 增加 `overflow-y: auto; max-height: 540px`，支持横/纵双向滚动
- `th` 增加 `position: sticky; top: 0; z-index: 2; background: var(--color-card); box-shadow: 0 1px 0 var(--color-border)` 实现表头固定
- `dashboard.html` 用 `<div class="table-scroll">` 包裹 `<table>`

---

### 3. 未启用 AI 提取时隐藏字段列
**问题**：`ai_extract_enabled` 为 false 时，处理记录仍显示所有 AI 字段列。

**修复位置**：`app.py`（`dashboard` 路由 + `manifest_page` 路由）

**修复内容**：
- 初始渲染：
  ```python
  extract_field_names=(
      (_active_field_names(user) if effective_is_admin else _pick_field_names_for_member(user, user.search_sender_contains))
      if user.ai_extract_enabled else []
  ),
  ```
- AJAX 刷新：
  ```python
  if not user.ai_extract_enabled:
      field_names = []
  elif effective_is_admin:
      field_names = _active_field_names(user)
  else:
      sender = request.args.get("sender", user.search_sender_contains or "")
      field_names = _pick_field_names_for_member(user, sender)
  ```

---

### 4. 普通成员视角无法显示供应商预设字段列
**问题**：管理员视角切换到普通用户视角后，发件人域名匹配的 VendorFieldPreset 字段列不显示。

**根本原因**：`field_config.pick_field_defs_for_sender()` 内部用 `"@" in sender_email` 来提取域名；而用户在"发件人包含"里通常填的是纯域名（如 `ascendtms.com`），没有 `@`，导致 `domain = ""`，域名匹配条件 `and domain` 短路为 False，所有预设匹配失败，退回全局字段。

**修复位置**：`app.py` → `_pick_field_names_for_member()` 函数（约第 289 行）

**修复内容**：
```python
# 修复前
chosen = field_config.pick_field_defs_for_sender(token, global_defs, presets_data)

# 修复后
lookup = token if "@" in token else f"_@{token}"
chosen = field_config.pick_field_defs_for_sender(lookup, global_defs, presets_data)
```
纯域名 token 加 `_@` 占位前缀，让函数能正确提取域名部分并匹配预设；`field_config.py` 本身无需改动。

---

## 三、已知问题 / 待观察

### 运行状态正常但处理进度长时间为 0/0
**现象**：特定筛选条件下任务显示"正在运行 0/0"长时间不动；切换其他筛选条件正常。

**初步判断**：
- 目标邮件已在前次运行中入库，去重逻辑跳过全部，导致计数不增——正常行为，任务最终会自动结束
- 若长时间不结束，可检查日志是否有 Zoho API 超时或 token 过期

**状态**：观察中，未复现稳定复现路径，暂无代码修改。

---

## 四、架构备注

### `field_config.py` 核心逻辑
- `parse_extract_fields(text)` — 解析 JSON 格式或旧版逗号分隔的字段配置
- `pick_field_defs_for_sender(sender_email, global_defs, presets)` — 精确邮箱优先，域名次之，都不匹配退回全局
- `field_looks_like_container(field)` — 判断字段是否为集装箱号相关

### 多用户视角
- `effective_is_admin`：判断当前是管理员视角还是成员视角
- `_active_field_names(user)`：管理员视角，返回所有预设中有数据的字段的并集
- `_pick_field_names_for_member(user, sender_raw)`：成员视角，按发件人 token 匹配预设，返回匹配预设的字段；无匹配退回全局字段

---

## 五、部署信息

- **本地开发**：SQLite，`flask run`
- **线上**：Render，PostgreSQL，环境变量 `DATABASE_URL`
- **Zoho 认证**：OAuth2，access token 存 DB，过期自动刷新

---

*本文档由 Claude (Cowork) 自动生成，记录截至 2026-08-31 的开发状态。*
