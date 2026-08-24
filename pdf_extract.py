"""
pdf_extract.py —— 已废弃，不再被任何代码调用。

这是早期用正则/关键词规则（写死的 _TOTAL_LABELS 字段名列表）提取 PDF 金额/币种/
container 号的旧实现，已经被 ai_extract.py（调用 Claude、按用户在网页上自己配置的
字段名+备选名称去读文档）完全取代。项目里没有任何地方 import 这个模块了。

我的沙盒工具这次没能正常连接（Workspace unavailable），执行不了 rm 删除这个文件，
先把内容清空成这条说明，你可以直接在文件管理器里把 pdf_extract.py 这个文件删掉，
或者告诉我，等沙盒恢复了我再帮你删。
"""
