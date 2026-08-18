# 发票邮件筛选工具 - 部署到 Render

同事打开网址 → 点"用 Zoho 登录"（浏览器里授权一次）→ 填筛选条件 → 点"立即运行" → 跑完点"下载本次结果（ZIP）"，附件就下载到自己电脑了。全程不用装任何东西。

你已经有 GitHub 账号，还没有 Render 账号，数据库用 Render 的免费 PostgreSQL（持久保存，服务器重启/重新部署不会丢同事的登录状态和筛选配置）。下面是从"把代码推到 GitHub"到"同事能打开网址用"的完整步骤。

## 一、把代码推到 GitHub

在项目文件夹（`E:\logistic\MailProcessWebsite`）里打开命令行：

```
git init
git add .
git commit -m "发票邮件筛选工具"
```

`.gitignore` 已经配好了，`venv/`、`.env`、本地数据库、临时下载文件都不会被传上去（`.env` 里有你的 Zoho Client Secret，千万不要传到 GitHub，尤其是公开仓库）。

去 GitHub 网站新建一个仓库（Repository），可以设成 Private（私有，推荐，不想让外人看到代码就选这个）。新建后 GitHub 会给你几行命令，大概是：

```
git remote add origin https://github.com/你的用户名/仓库名.git
git branch -M main
git push -u origin main
```

复制粘贴执行，代码就推上去了。

## 二、注册 Render 账号

打开 https://render.com → 右上角 Get Started → 选 **用 GitHub 登录**（推荐，登录后 Render 会自动能访问你的仓库，不用额外配置权限）。

## 三、在 Render 建一个免费 PostgreSQL

1. Render 后台 → New → **PostgreSQL**
2. Name 随便起（比如 `invoice-db`），Region 选新加坡或者离你近的
3. Instance Type 选 **Free**
4. 建好后，进到这个数据库的详情页，找到 **Internal Database URL**（不是 External 那个，同一个 Render 项目内部访问用 Internal 更快），复制下来，后面要填到网站的环境变量里
5. **注意**：免费 PostgreSQL 有效期 90 天，到期前 Render 会提醒你，需要重新建一个并把数据迁移过去（免费额度的限制，正式长期用可以考虑升级付费版，$7/月起，不会到期）

## 四、在 Render 建 Web Service

1. Render 后台 → New → **Web Service** → 选你刚推上去的那个 GitHub 仓库
2. Name 随便起，Region 跟数据库选同一个
3. **Runtime**: Python 3
4. **Build Command**:
   ```
   pip install -r requirements.txt psycopg2-binary
   ```
   （`psycopg2-binary` 是连 PostgreSQL 需要的库，本机用 SQLite 测试不需要装它，所以没写进 `requirements.txt` 里，部署时单独在这行加上）
5. **Start Command**:
   ```
   gunicorn --bind 0.0.0.0:$PORT app:app
   ```
6. Instance Type 选 **Free**

## 五、配置环境变量

还是在这个 Web Service 的设置里，找到 Environment 页面，把下面这些一个个加上（照着 `.env.example` 里的说明填）：

| 变量名 | 填什么 |
|---|---|
| `SECRET_KEY` | 本机执行 `python -c "import secrets; print(secrets.token_hex(32))"`，把输出的一长串粘过来 |
| `TOKEN_ENCRYPTION_KEY` | 本机执行 `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`，把输出粘过来（这一项必须手动设置，不能留空，否则每次重启同事都要重新登录） |
| `ZOHO_CLIENT_ID` | 你之前在 Zoho API Console 注册应用拿到的 Client ID（跟本机 `.env` 里的一样） |
| `ZOHO_CLIENT_SECRET` | 同上，Client Secret |
| `ZOHO_REDIRECT_URI` | 先随便填 `https://placeholder.onrender.com/auth/zoho/callback`，第一次部署成功、拿到真实域名后回来改成真的 |
| `ZOHO_SCOPE` | `ZohoMail.accounts.READ,ZohoMail.messages.READ,ZohoMail.folders.READ` |
| `DATABASE_URL` | 第三步复制的 PostgreSQL Internal Database URL |
| `FORCE_HTTPS_COOKIES` | `1` |

保存后点 **Create Web Service**（或 Manual Deploy → Deploy latest commit），Render 会自动拉代码、装依赖、启动。第一次部署一般要等几分钟，看 Logs 页面确认没有报错，最后出现类似 `Booting worker` 的字样就是成功了。

## 六、回填真实域名

部署成功后，页面顶部会显示 Render 分配的网址，类似 `https://invoice-web-xxxx.onrender.com`。

1. 回到 Render 这个 Web Service 的 Environment 页面，把 `ZOHO_REDIRECT_URI` 改成：
   ```
   https://invoice-web-xxxx.onrender.com/auth/zoho/callback
   ```
2. 去 https://api-console.zoho.com/ → 找到你注册的应用 → 把 Authorized Redirect URI 也改成一样的地址
3. 回 Render 手动触发一次重新部署（改环境变量后它通常会自动重启，没有的话点 Manual Deploy）

## 七、验证

用浏览器打开这个真实域名，应该能看到登录页。点"用 Zoho 登录"，走一遍 Zoho 授权，能进到控制台说明部署成功了。填个筛选条件，点"立即运行"，跑完点"下载本次结果（ZIP）"试一下，能正常下载就说明全流程通了。

## 八、把网址发给同事

同事打开这个网址（`https://invoice-web-xxxx.onrender.com`），流程是：

1. 点"用 Zoho 登录" → 登录自己的 Zoho 邮箱、点同意
2. 填筛选条件（主题包含 / 附件文件名包含 / 发件人包含 / 正文包含 / 发件人排除 / 日期 / 是否要求带附件）→ 保存配置。这几项跟 Zoho 网页邮箱搜索框用的是同一套语法，主题/附件名/发件人/正文会直接交给 Zoho 在服务端搜；"发件人排除"是 Zoho 语法不支持的反向条件，由网站自己在拿到结果后再过滤一遍
3. 点"立即运行"，下面会实时显示进度
4. 跑完后：命中的附件会列在页面下方表格里（发件人名/发件人邮箱/主题/附件标题），可以逐个点击下载；也可以点"下载本次结果（ZIP）"整批下载，或者点"导出 Excel"导出一份带下载链接的表格

每个人的登录状态、筛选配置、处理记录都各自独立存在数据库里，互不影响，也不会被别人看到。

## 九、当前版本的限制

- **免费 Web Service 闲置会休眠**：15 分钟没人访问会自动休眠，下次有人打开网址时需要重新启动，第一次打开可能要等几十秒。如果这个体验对同事来说太糟，可以升级到付费的 Starter 套餐（$7/月起）保持常驻。
- **服务器临时文件 3 天后自动清理**：附件下载链接（单个下载、ZIP、Excel 里的链接）都指向服务器临时目录，超过 3 天没清理会被自动删掉，建议跑完尽快下载，不要把 Excel 导出当长期归档用。
- **发件人排除是客户端过滤**：Zoho 搜索 API 本身不支持"不包含"这种反向条件，"发件人排除"是网站拿到 Zoho 搜索结果后自己再筛一遍，条件设置很宽泛时可能会多拉一些页面来过滤，速度会慢一点。
- **没有自动定时运行**：需要同事手动点"立即运行"。免费版做定时任务不太可靠，暂时没做。
- **同时只能跑一个任务**：一个人的"立即运行"没跑完之前不能再点一次，这是有意设计的，避免重复处理同一封邮件。
- **大邮箱首次全量扫描可能较慢**：建议同事第一次用的时候，先把"只扫描此日期之后"设成最近几天测试，确认筛选条件没问题，再考虑要不要跑全量历史。
- **PostgreSQL 免费版 90 天到期**：到期前 Render 会提醒，需要重新建一个数据库并迁移数据（或者升级付费版）。

## 十、常见问题

- **部署后打开网址显示 502 或者一直转圈**：先看 Render 的 Logs 页面有没有报错。常见原因是环境变量没填全（尤其是 `DATABASE_URL`），或者 Build Command 没装上 `psycopg2-binary`。
- **Zoho 登录后报"没有从 Zoho 拿到账户信息"**：检查 `ZOHO_REDIRECT_URI` 环境变量和 Zoho API Console 里注册的地址是不是一字不差（包括 https、域名、路径）。
- **同事反馈"点登录没反应"或者卡很久**：大概率是免费实例休眠了，正在冷启动，等个几十秒刷新一下页面。
- **重新部署后同事都要重新登录**：说明 `TOKEN_ENCRYPTION_KEY` 没有固定设置成环境变量（用的是自动生成的临时密钥），回到第五步把它显式设置好。
