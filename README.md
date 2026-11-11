# App 发布中枢（App Publish Hub）

[![GitHub stars](https://img.shields.io/github/stars/junbin-lai/app-publish-hub?style=social)](https://github.com/junbin-lai/app-publish-hub/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/junbin-lai/app-publish-hub?style=social)](https://github.com/junbin-lai/app-publish-hub/network)

> ⭐ 如果这个项目对你有帮助，可以顺手点个**Star**，非常感谢！🫶

上传一次 APK/AAB，勾选平台即可同时发布到 **华为 / OPPO / vivo / 小米 / 荣耀 / 腾讯应用宝 / Google Play**；
苹果 App Store 则直接从 TestFlight 已上传的构建里选一个提交审核（不需要本机 ipa），
并随时查看各平台**审核进度**与**线上版本号**。

内置 App发布中枢：多应用并行发布、按应用授权（owner / publisher / viewer）、定时上线、状态分享图、飞书群通知；也提供 CLI 便于脚本集成。

## 核心能力

- **8 大平台全覆盖**：发布 + 查询（Apple 走 App Store Connect 提交审核，**尚未真实发布实测**）
- **统一发布模型**：一个应用配置 + 新 APK/AAB + 更新说明 → 选平台 → 一键发布
- **平台多选发布**：App发布中枢可按勾选平台发布，支持「全部平台」（「全部」不含 Apple，发苹果需显式勾选构建）
- **华为 Harmony 构建版本发布**：从 AGC「软件包管理」拉取已上传的 Harmony 版本，选择后直接提交审核；选了版本时华为会**同时**发 Android 包（有 APK）并提交 Harmony，只发 Harmony 时不需要本机安装包
- **苹果 TestFlight 构建发布**：从 App Store Connect 拉取已上传构建（版本号 / 构建号 / 处理状态），选中后关联版本并提交审核，本机不需要 ipa
- **AAB / APK 版本一致性提示**：两个包都上传时自动比对 versionName / versionCode，不一致在版本下方提示（仅提示，不阻止分别发布）
- **更新说明三语言默认文案**：`config/board.json` 的 `release_notes.defaults` 可配置简体 / 繁体 / 英文预填文案，提交时自动映射为 Google 的 `zh-TW` 与 Apple 的 `zh-Hant`
- **定时上线**：小米/OPPO/vivo/荣耀/华为/腾讯应用宝 均支持定时发布（应用宝限提审 6 小时后、30 天内）
- **审核通过后立即上线**：华为定时上架版本可一键改为立即上架（on-shelf-time 接口），App发布中枢「待发布」状态卡提供「立即上线」按钮（其余平台官方未提供该 API）
- **自动复用资料**：OPPO 更新版本时自动沿用现网图标/简介/截图，无需重传
- **三态查询**：Google Play 区分「已上架 / 草稿未送审 / 审核中」
- **状态总览**：Web 界面直观展示每个 App 在各平台的已上架版本，支持深色/浅色主题一键切换（自动记忆选择）；一键生成竖版「发布状态分享图」（自动带应用图标）并复制到剪贴板，可直接粘贴到微信/钉钉
- **登录与权限**：`config/board.json` 配置账号后开启登录，三种角色 owner（全部应用 + 用户管理）/ publisher（发布员，仅授权应用）/ viewer（只读+查询），支持按应用授权（[App发布中枢手册](docs/WEB_GUIDE.md)）
- **多应用并行发布**：不同应用可同时发布、互不打断，切换应用自动恢复各自进度，侧栏「运行中」角标；同应用重复发布会被拦截；全局上传并发上限防带宽打爆（`upload_concurrency`，默认 2）
- **发布历史永久保留**：不按时间/条数自动清理，只能由 owner 主动清空（接口需二次确认，且清空前自动多份备份）；服务运行日志 `tmpdoc/web-8090.log` 保留 60 天，超期归档删除
- **局域网访问**：`./start-dashboard.sh lan` 一键开放给同网段同事（强制先配好登录账号；不配账号拒绝启动）
- **飞书群机器人**：发布完成自动往群里发结果卡片 + 状态分享图；支持全局通知群 + 应用专属通知群（某应用发布后可额外通知到指定群）；群成员 @机器人 即可查任一应用各平台状态（回复分享图）。自建应用 + 长连接，无需公网 IP（接入步骤见 [App发布中枢手册](docs/WEB_GUIDE.md)）
- **发布历史同步飞书电子表格**：每次发布结束自动追加一行到对应应用的工作表（一个应用一个标签页，启动时自动补齐），方便直接看历史/做统计
- **dry-run 安全校验**：不真实提交。注意行为差异：Google 会真实创建 edit 后立即删除、应用宝会调只读接口查线上版本，其余平台纯本地校验（见 [架构说明](docs/ARCHITECTURE.md)）

## 平台能力一览

| 平台 | 查询 | 发布 | 定时上线 | 定时/手动修改立即上线 | 备注 |
| --- | --- | --- | --- | --- | --- |
| Google Play | ✅ | ✅ | ❌ | ❌ | 查询返回已上架 / 审核中 / 草稿三态；发布需 **AAB**（APK 已不被接受）；可自动送审（勾选「Google 自动送审」）或存草稿后手动送审 |
| Apple App Store | ✅ | ✅ | ❌ | ✅ | 查询返回线上版本与审核状态，优先 App Store Connect API（`/v1/appStoreVersions`），未配密钥时回退 iTunes Lookup；发布从 TestFlight 已上传构建里选一个（`/v1/builds` → 关联版本 → 提交审核），本机不需要 ipa；版本为「待开发者发布」时状态卡出现「立即发布」按钮；尚未真实发布实测 |
| 小米应用商店 | ✅ | ✅ | ✅ | ❌ | X509 公钥 RSA 加密签名 |
| OPPO 软件商店 | ✅ | ✅ | ✅ | ❌ | 更新自动复用现有发布资料 |
| vivo 应用商店 | ✅ | ✅ | ✅ | ❌ | 上传 APK → 同步更新 |
| 荣耀应用市场 | ✅ | ✅ | ✅ | ❌ | 审核结果含审核意见 |
| 华为 AppGallery | ✅ | ✅ | ✅ | ✅ | Android：upload-url + multipart 直传 + app-file-info 绑定；Harmony：拉取 AGC 已上传构建 → 选择版本 → app-submit 提审（与 Android 包可在同一任务内一起发）；定时版本可用 on-shelf-time 立即上架 |
| 腾讯应用宝 | ✅ | ✅ | ✅ | ❌ | HmacSHA256 签名；更新走 API（仅已上线应用，每天限 50 次更新）；定时发布限提审 6 小时后、30 天内 |

## 项目结构

```
.
├── app_store/
│   ├── cli.py            # 命令行入口（publish/status/web...）
│   ├── web.py            # App发布中枢服务器（stdlib http.server + 登录鉴权）
│   ├── board_config.py   # App发布中枢配置（登录账号/角色 + 飞书机器人）
│   ├── share_image.py    # 状态分享图服务端渲染（Pillow）
│   ├── feishu_bot.py     # 飞书机器人（发布通知 + @查询，长连接）
│   ├── base.py           # StoreAdapter 抽象基类
│   ├── registry.py       # 平台 -> 适配器注册表
│   ├── config.py         # 凭证 / 应用目录加载
│   ├── catalog.py        # 应用目录 → Release 模型
│   ├── models.py         # 统一数据模型（Platform/AuditState/...）
│   ├── templates/
│   │   └── index.html    # App发布中枢前端（单文件）
│   └── stores/           # 各平台适配器
│       ├── google.py / apple.py / xiaomi.py
│       ├── oppo.py / vivo.py / honor.py / huawei.py
├── apps/catalog.json     # 应用目录（包名、构建产物路径、版本）
├── config/               # 凭证 credentials.json + App发布中枢配置 board.json（均本地私有）
└── docs/                 # 文档
```

## 快速开始

### 1. 环境准备

```bash
cd /path/to/app 上架及查询
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# 依赖：requests、pycryptodome、cryptography（小米签名）、Pillow+lark-oapi（飞书机器人/分享图）、google-api-python-client（Google，可选）
pip install -e .        # 安装 appstore 命令（本文档所有 appstore ... 命令都依赖这步）
```

> 提示：以下命令均需先激活虚拟环境（`source .venv/bin/activate`）；不想安装也可以用
> `python -m app_store.cli web ...` 代替 `appstore web ...`（其余命令同理，需在项目根目录执行）。

### 2. 配置凭证

复制 `config/example.credentials.json` 为 `config/credentials.json`（已 gitignore，不会入库），
按各平台说明填写。**多应用场景**用 `apps` 映射按包名区分凭证（见 [docs/CREDENTIALS_GUIDE.md](docs/CREDENTIALS_GUIDE.md)）。

### 3. 配置应用目录

复制 `apps/catalog.example.json` 为 `apps/catalog.json`，为每个应用配置包名、APK/AAB 路径（本地真实配置不入库）：

```json
{
  "categories": [
    {
      "name": "示例分类",
      "apps": [
        {
          "id": "example-app",
          "name": "Example App",
          "package_name": "com.example.app",
          "apk_build": "/path/to/app-release.apk",
          "aab_build": "/path/to/app-release.aab",
          "version_name": "1.0.0",
          "version_code": 100,
          "track": "production",
          "release_notes": ""
        }
      ]
    }
  ]
}
```

### 4. 校验配置

```bash
appstore platforms                          # 列出平台与状态
appstore validate --credentials config/credentials.json
appstore apps                               # 查看应用目录
```

### 5. 启动 App发布中枢（推荐日常使用）

```bash
appstore web --port 8090 --credentials config/credentials.json --catalog apps/catalog.json
# 或一键脚本：./start-dashboard.sh（bg=后台 / lan=局域网模式 / stop / status）
# 打开 http://127.0.0.1:8090
```

> ⚠️ **安全提示**：默认**无登录、仅本机 127.0.0.1 访问**（零配置可用，行为与旧版一致）。
> 需要给同事用或远程访问时：复制 `config/example.board.json` 为 `config/board.json` 配置账号
> （owner 全部应用 / publisher 仅授权应用可发布 / viewer 只读+查询），再 `./start-dashboard.sh lan` 开放局域网——
> **没配账号时局域网模式会拒绝启动**。不要把端口直接映射到公网（要走 VPN 或 HTTPS 反代，详见
> [App发布中枢手册](docs/WEB_GUIDE.md)）。
>
> 🤖 **飞书群机器人**（可选）：board.json 的 feishu 段填入自建应用凭证后，发布完成自动在群里
> 发结果卡片+状态分享图，群成员 @机器人 即可查询任一应用状态。创建与配置步骤见
> [App发布中枢手册](docs/WEB_GUIDE.md)。

在 App发布中枢上可：查看每个应用各平台已上架版本 / 选择平台一键发布 / 填版本号与更新说明 / 定时上线。

### 6. 命令行发布与查询

```bash
# dry-run 校验（推荐先跑；--platform 每次只接受一个平台）
appstore publish --app example-app --platform xiaomi --dry-run --credentials config/credentials.json

# 真实发布到多个平台：逐平台执行多次命令（CLI 不支持逗号多选，逗号多选仅 App发布中枢/API 支持）
appstore publish --app example-app --platform xiaomi --credentials config/credentials.json
appstore publish --app example-app --platform oppo --credentials config/credentials.json
appstore publish --app example-app --platform vivo --credentials config/credentials.json

# 发布到所有已配置平台（不含 Apple）
appstore publish --app example-app --all --credentials config/credentials.json

# 指定版本信息覆盖目录配置
appstore publish --app example-app --platform xiaomi --version-name "1.0.1" --version-code 101 --release-notes "修复问题" --credentials config/credentials.json

# 定时上线（小米/OPPO/vivo/荣耀/华为/腾讯应用宝）
appstore publish --app example-app --platform xiaomi --online-time "2026-09-01 10:00" --credentials config/credentials.json

# 查询某 App 所有平台状态
appstore status --app example-app --credentials config/credentials.json

# 查询某包名
appstore status --package com.example.app --credentials config/credentials.json

# JSON 输出（脚本/CI 集成，--json 须在子命令前）
appstore --json status --app example-app --credentials config/credentials.json
```

## 安全提醒

- 凭证（client_secret / 服务账号私钥 / 签名密钥）只保存在 `config/credentials.json`（已在 .gitignore），**切勿提交到仓库**
- App发布中枢登录账号与飞书应用凭证保存在 `config/board.json`（同样已 gitignore；模板 `config/example.board.json`）
- 发布会真实消耗平台配额并触发审核，请先 `--dry-run` 验证
- 局域网开放 App发布中枢必须先配置登录账号（脚本与服务端双重强制）；不要把端口直接映射公网
- 上架最终结果以平台后台为准

## 详细文档

- [凭证配置指南（含多应用）](docs/CREDENTIALS_GUIDE.md)
- [App发布中枢使用手册](docs/WEB_GUIDE.md)
- [CLI 参考手册](docs/CLI_REFERENCE.md)
- [各平台接入清单](docs/STORE_ACCESS.md)
- [架构说明](docs/ARCHITECTURE.md)
