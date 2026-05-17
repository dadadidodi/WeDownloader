# WeDownloader

本地归档你自己微信公众号里的已发表文章、草稿图文和素材，输出为可离线浏览的 `HTML + assets`。

这个工具优先使用微信公众号官方开发者接口；当已发表文章接口没有权限时，也支持使用你自己扫码登录后的公众号后台会话导出自己后台能看到的文章。不破解、不绕过登录、不抓取他人不可访问内容。

## 准备

本工具只需要 Python 3，不需要先安装第三方依赖。当前已在 Python 3.9 下测试通过。

### 1. 获取公众号开发配置

登录 [微信公众平台](https://mp.weixin.qq.com/) 后，进入：

`设置与开发` -> `基本配置`

你需要拿到：

- `AppID`
- `AppSecret`

如果还没有启用开发者配置，按后台提示启用即可。`AppSecret` 只会显示一次或需要重置，请保存在本机，不要提交到 Git。

### 2. 配置 IP 白名单

同样在公众号后台的 `基本配置` 页面，把当前运行这台机器的公网 IP 加入 `IP 白名单`。

可以用下面命令查看当前公网 IP：

```bash
curl https://ifconfig.me
```

如果你在家用网络、公司网络或代理/VPN 下运行，公网 IP 可能变化。遇到 `invalid ip`、`ip not in whitelist` 这类错误时，优先检查这里。

### 3. 创建 `.env`

在项目目录下执行：

```bash
cp .env.example .env
```

然后填入：

```dotenv
WECHAT_APP_ID=your_app_id
WECHAT_APP_SECRET=your_app_secret
```

例如：

```dotenv
WECHAT_APP_ID=wx1234567890abcdef
WECHAT_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

`.env` 已经写进 `.gitignore`，不会被提交。

## 跑起来

所有命令都在项目目录里执行：

```bash
cd /Users/dadadidodi/Documents/code/inv/WeDownloader
```

### 1. 确认命令可用

```bash
python3 -m wedownloader --help
```

能看到参数说明就表示本地代码可以运行。

### 2. 先 dry-run 探测文章

先只查看能拉到哪些文章：

```bash
python3 -m wedownloader --dry-run
```

这一步会调用微信公众号官方接口，列出能读取到的已发布文章和草稿标题，但不会下载正文和图片。

### 3. 小批量试下载

先下载 3 篇做检查：

```bash
python3 -m wedownloader --limit 3 --progress
```

下载完成后打开：

```text
archive/index.html
```

确认标题、正文、图片能正常离线查看。

### 4. 全量下载

全量归档：

```bash
python3 -m wedownloader --progress
```

默认会同时拉取：

- 已发布内容
- 草稿/未发布图文
- 正文里的图片等资源

### 5. 可选运行方式

只处理已发布内容：

```bash
python3 -m wedownloader --published-only
```

只处理草稿：

```bash
python3 -m wedownloader --drafts-only
```

同时下载永久图片素材：

```bash
python3 -m wedownloader --download-image-materials
```

自定义输出目录：

```bash
python3 -m wedownloader --archive-dir my_archive
```

使用其他 `.env` 文件：

```bash
python3 -m wedownloader --env /path/to/.env
```

查看上一次下载任务的状态，不调用微信接口：

```bash
python3 -m wedownloader --status
```

清空已下载的归档输出，保留后台登录态：

```bash
python3 -m wedownloader --clean-runs
```

如果连后台登录态也要删除：

```bash
python3 -m wedownloader --clean-runs --clean-session
```

## 导出后台已发表文章

如果官方已发表文章接口返回 `48001 api unauthorized`，说明当前 `AppID/AppSecret` 没有调用已发表列表接口的权限。这个时候可以使用“公众号后台登录态模式”导出你自己后台能看到的已发表文章。

这个模式只面向你自己管理的公众号：不搜索他人公众号，不需要抓别人内容。

你当前这个公众号已经验证过：官方草稿接口可用，官方已发布接口无权限；所以已发表文章建议直接走本节的 `mp-login` / `mp-list` / `mp-download` 流程。

### 1. 保存后台登录态

先在浏览器登录：

```text
https://mp.weixin.qq.com/
```

进入你自己的公众号后台首页。然后准备两样东西：

- 当前后台 URL 里的 `token`，例如 URL 里 `token=123456789`。
- 浏览器开发者工具里请求 `mp.weixin.qq.com` 时带的 `Cookie`。

运行：

```bash
python3 -m wedownloader mp-login
```

按提示粘贴 `token` 和 `Cookie`。如果不知道 `fakeid`，直接回车跳过即可。登录态会保存到：

```text
archive/mp_session.json
```

如果 Cookie 太长，不方便粘到终端，可以写到本地文件：

```text
TOKEN=123456789
COOKIE=xxx=yyy; xxx=yyy; ...
FAKEID=
```

例如保存为 `mp_login.txt`，然后运行：

```bash
python3 -m wedownloader mp-login --from-file mp_login.txt
```

也可以把第一行写成完整后台 URL：

```text
URL=https://mp.weixin.qq.com/cgi-bin/home?t=home/index&lang=zh_CN&token=123456789
COOKIE=xxx=yyy; xxx=yyy; ...
```

`mp_login.txt` 已加入 `.gitignore`。登录成功后可以删除这个临时文件，只保留 `archive/mp_session.json`。

注意：`Cookie` 和 `archive/mp_session.json` 都相当于后台登录态，不要发给别人，不要提交到 Git。

### 2. 检查登录态

```bash
python3 -m wedownloader mp-status
```

如果提示 session 可用，就可以继续列文章。

### 3. 只列后台已发表文章

```bash
python3 -m wedownloader mp-list --dry-run
```

这一步只列标题，不下载正文和图片。

### 4. 小批量下载

```bash
python3 -m wedownloader mp-download --limit 3 --progress
```

确认 `archive/index.html` 能打开后，再全量下载：

```bash
python3 -m wedownloader mp-download --progress
```

## 输出内容

输出目录默认是 `archive/`：

- `archive/index.html`：归档首页
- `archive/articles/.../index.html`：单篇文章
- `archive/articles/.../metadata.json`：单篇元数据
- `archive/articles/.../assets/`：本地化图片等资源
- `archive/materials/image/`：可选的永久图片素材
- `archive/manifest.json`：断点和错误记录
- `archive/articles_readable.json`：更适合人看的中文文章清单
- `archive/progress.json`：最近一次 run 的进度记录
- `archive/mp_session.json`：后台登录态，仅本地使用，不要分享给别人

`manifest.json` 会记录每篇文章和资源的下载状态。重新运行时不会删除已有文件，可以作为排查失败资源的清单。

如果只是想看中文标题、文章路径和资源统计，优先打开 `archive/articles_readable.json` 或 `archive/index.html`。`manifest.json` 里的 `MzU4...` 不是乱码，而是微信 URL 里的公众号/文章标识。

## 清理输出

如果只是想删掉上次测试下载的文章、manifest、进度和首页，但保留后台登录态，运行：

```bash
python3 -m wedownloader --clean-runs
```

它会删除：

- `archive/articles/`
- `archive/materials/`
- `archive/index.html`
- `archive/manifest.json`
- `archive/articles_readable.json`
- `archive/progress.json`
- `archive/.access_token.json`
- `mp_login.txt`

它默认保留：

- `archive/mp_session.json`

如果要彻底删除后台登录态：

```bash
python3 -m wedownloader --clean-runs --clean-session
```

## 进度追踪

这里的“一次 run”指的是你执行一次下载命令，例如：

```bash
python3 -m wedownloader --progress
```

一次 run 不是一篇文章，而是一次完整下载任务。它可能包含很多篇文章和很多图片资源，也可能因为你加了 `--limit 3` 而只处理 3 篇文章。

进度里的一个 `item` 有两种：

- `article`：一篇公众号图文文章。多图文消息里的每一篇子文章单独算一篇。
- `material`：一个永久素材，例如通过 `--download-image-materials` 下载的一张图片。

`processed`、`succeeded`、`failed` 统计的是 item 数量。文章正文里的图片资源单独统计为 assets，包括：

- `assets_downloaded`：下载成功的资源
- `assets_failed`：下载失败的资源
- `assets_needs_manual_fetch`：需要登录态或手动处理的资源

运行时加 `--progress` 可以看到实时进度：

```bash
python3 -m wedownloader --progress
```

如果任务中断或跑完后想看最后状态：

```bash
python3 -m wedownloader --status
```

`--status` 只读取 `archive/progress.json`，不会访问微信接口，也不需要 `.env`。

## 推荐流程

第一次使用建议按这个顺序：

```bash
python3 -m wedownloader --help
python3 -m wedownloader --dry-run
python3 -m wedownloader --limit 3 --progress
python3 -m wedownloader --status
python3 -m wedownloader --progress
```

如果你的目标是导出后台已发表文章，推荐流程是：

```bash
python3 -m wedownloader mp-status
python3 -m wedownloader mp-list --dry-run --limit 20
python3 -m wedownloader mp-download --limit 3 --progress
python3 -m wedownloader --status
python3 -m wedownloader mp-download --progress
```

## 常见问题

如果看到 `invalid ip` 或类似错误，通常是公众号后台没有配置当前公网 IP 白名单。

如果某些资源记录为 `needs_manual_fetch`，说明资源需要额外登录态或权限。工具会保留原链接并在 manifest 中记录，不会尝试绕过权限。

如果文章数量和后台不一致，先运行：

```bash
python3 -m wedownloader --dry-run
```

确认官方接口返回的发布记录和草稿数量，再决定是否需要补充素材库导出流程。

如果看到 `48001 api unauthorized`，说明官方已发布文章接口没有权限。草稿和素材仍可继续用官方 API；已发表文章请改用：

```bash
python3 -m wedownloader mp-login
python3 -m wedownloader mp-list --dry-run
python3 -m wedownloader mp-download --progress
```
