# 基于 helper 微信机器人框架的舞萌 DX 插件

[helper](https://github.com/Prometheus-first/helper) 微信机器人框架的舞萌DX插件：水鱼查分器 OAuth 绑定与 Best50 图片查询。成绩数据由 [水鱼查分器](https://www.diving-fish.com/maimaidx/prober/) 提供。

插件迁移自 [maimaiDX](https://github.com/Yuri-YuzuChaN/maimaiDX) 的基础部分，沿用原版的 PIL 拼图方式绘制 Best50，出图与原版一致。本仓库独立维护，放入 helper 的 `plugins/maimaidx/` 目录即可使用。

## 运行环境

- **框架**：helper（原生 `native_text` 后端），插件遵循框架的插件 API v1。
- **能力**：`plugin.json` 声明 `text.receive`、`text.send`、`image.send`；`native_text` 后端已提供这些能力。
- **框架接口**：用 `ctx.reply` 回复文本、`ctx.reply_image` 发送本地图片，用 `ctx.members` 解析群昵称，用 `ctx.config` 读取配置、`ctx.logger` 记录日志。
- **Python**：与 helper 一致，64 位 Python 3.13/3.14。

本插件放在 helper 的 `plugins/maimaidx/` 目录下，在 `bot_config.json` 的 `plugins` 中启用。出图使用 Pillow（见[依赖](#依赖)），并需要原版静态资源包（见[静态资源包](#静态资源包)）。

## 功能

| 指令 | 功能 |
| --- | --- |
| `绑定水鱼` / `dfbind` / `绑定df` | 发起水鱼 OAuth 授权，回复授权链接与绑定身份 |
| `水鱼授权码 <确认码>` / `dfcode <确认码>`，或 `dfbind <确认码>` | 提交确认码完成绑定 |
| `b50` / `B50` | 查询本人 Best50（图片） |
| `b50 @群成员` | 查询被艾特成员的 Best50（对方需先绑定） |
| `b50 水鱼用户名` | 按水鱼用户名查询公开成绩 |

群聊和私聊都可用，指令前允许一个 `/`。`b50` 始终返回图片；缺少绘图环境时回复错误提示，不会降级为文本。

## 安装到 helper

1. 准备 helper：

   ```bash
   git clone https://github.com/Prometheus-first/helper.git
   cd helper
   ```

2. 把本插件放到 `plugins/maimaidx/`。使用 helper 自带目录即可；若单独获取：

   ```bash
   git clone https://github.com/wmh1024/helper-maimaidx-plugin.git plugins/maimaidx
   ```

3. 安装依赖：

   ```bash
   python -m pip install -r plugins/maimaidx/requirements.txt
   ```

4. 准备原版静态资源包并把 `static` 目录填入 `static_path`（见[静态资源包](#静态资源包)）。

5. 在 `bot_config.json` 的 `plugins` 中启用 `maimaidx` 并填写水鱼 OAuth 凭据（见[配置](#配置)）。

6. 运行 `python main.py --check` 确认插件语法与能力检查通过，再启动 `python main.py`。

## 目录结构

```text
plugins/maimaidx/
  __init__.py     # 指令路由、绑定会话、图片发送
  divingfish.py   # 水鱼 OAuth / 查询客户端（仅标准库 urllib）
  render_pil.py   # 移植原版 maimaiDX 的 PIL 拼图，绘制 Best50 图片
  music_db.py     # 曲目数据：最大 DX 分与定数（水鱼 /music_data，带缓存）
  plugin.json     # 插件清单（api v1，requires text.receive/text.send/image.send）
  requirements.txt # 出图依赖（Pillow）
  README.md
  .gitignore
```

## 绑定流程

1. 用户发送「绑定水鱼」，BOT 带 `handoff=code` 向水鱼账号服务发起绑定，回复授权链接。
2. 用户登录水鱼账号，确认页面上显示的绑定身份后点击「同意授权」。
3. 用户把页面给出的确认码发回给 BOT（形如 `BCDF-GHJK-LMNP`）。
4. BOT 用确认码兑换一次令牌并核对账号，绑定完成。

BOT 只保管 `client_id` 与 `client_secret`，不保存任何用户令牌；绑定关系以 `sha256(client_id:微信ID)` 的形式保存在水鱼服务端。用户可在 <https://auth.diving-fish.com/apps> 撤销授权。建议在私聊中发送确认码，避免被他人看到。

## 依赖

Best50 出图使用 Pillow 拼图，依赖见本插件的 `requirements.txt`。

```bash
python -m pip install -r plugins/maimaidx/requirements.txt
```

原版出图还需要静态资源包（字体、背景、评级图、曲绘）与曲目数据。缺少 `static_path` 时 `b50` 会回复「绘图服务未就绪」，绑定与按用户名查询不受影响。曲目数据首次出图时从水鱼 `/music_data` 获取并缓存到 `plugins/maimaidx/.runtime/music_data.json`；获取失败时退回旧缓存，仍拿不到就跳过 DX 星与定数。

## 静态资源包

Best50 按原版方式用素材拼图，必须使用原项目（Resource CN 1.55）的素材包。把资源包解压到任意目录，将 `static_path` 指向其中的 `static` 目录：

```json
"static_path": "D:/maimaidx/static"
```

期望的目录结构（缺失的评级图、曲绘、牌子会逐项跳过；背景或字体缺失则无法出图）：

```text
static/
  font/                       # 字体，如 Torus SemiBold.otf
  mai/
    pic/<theme>/b50.png       # Best50 背景
    pic/<theme>/logo.png      # 头部 logo
    cover/<song_id>.png       # 曲绘，按 song_id % 10000 查找，缺省用 0.png
```

未配置或目录不存在时无法出图（`b50` 回复「绘图服务未就绪」），绑定与按用户名查询不受影响。曲绘等素材版权归原作者与版权方所有，请遵守原项目的美术声明，勿删除图中署名。

## 配置

在 `bot_config.json` 的 `plugins` 中加入：

```json
{
  "maimaidx": {
    "enabled": true,
    "client_id": "水鱼 OAuth 应用 ID",
    "client_secret": "水鱼 OAuth 应用秘钥",
    "scope": "prober.records.read",
    "auth_url": "https://auth.diving-fish.com",
    "api_url": "https://maimai.diving-fish.com/api/maimaidxprober",
    "bot_name": "helper",
    "timeout_seconds": 20,
    "binding_session_minutes": 20,
    "theme": "prism_plus",
    "static_path": "",
    "image_timeout_seconds": 30
  }
}
```

向水鱼申请 OAuth 应用后填写 `client_id` 与 `client_secret`。`scope` 需要 `prober.records.read`（读取成绩）。未填写凭据时，只有按用户名查询公开成绩可用。

- `theme`：背景主题，可选 `prism_plus` 或 `circle`。
- `static_path`：静态资源包的 `static` 目录，留空则无法出图。
- `image_timeout_seconds`：兼容旧配置保留，PIL 出图不使用。

`b50 用户名` 使用水鱼无需验证的公开查询，取决于对方是否设置隐私或同意用户协议；`b50` 与 `b50 @成员` 使用对应用户的授权令牌，由令牌决定查询对象。

## 说明

- 本插件通过 `image.send` 能力发送本地 PNG，需要后端支持图片发送（helper 的 `native_text` 后端已支持）。
- 出图使用 PIL 拼图，耗时通常在 0.5–1 秒；50 条成绩的图片约 1–2 MiB，画布与原版一致为 1400×1600。
- 曲目数据缓存在 `plugins/maimaidx/.runtime/music_data.json`，默认 7 天有效，过期后下次出图自动刷新。
- `.gitignore` 会忽略本地的 `static/`、`assets/` 资源包与渲染调试产物，避免把大文件或版权素材提交进仓库。
- 本仓库独立维护；放入 helper 的 `plugins/maimaidx/` 目录使用。
