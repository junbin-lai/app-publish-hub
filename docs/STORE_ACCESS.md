# 各平台接入清单（2026-08 实测）

> 全部平台已实现 publish + query_status。以下为各平台实测用的接口与凭证要点。

## Google Play

| 项目 | 说明 |
| --- | --- |
| API | androidpublisher v3（google-api-python-client） |
| 认证 | 服务账号 OAuth2（Google Cloud 创建服务账号并授予 Play Console 权限） |
| 凭证 | `google.service_account_json` |
| 发布 | ✅ 支持自动送审：status=completed + edits().commit()（勾选「Google 自动送审」）；不勾则 status=draft + changesNotSentForReview=True 存草稿。若应用在 Play Console 开启了「更改自动送审」，Google 会拒绝该参数（400 `changesNotSentForReview must not be set`），此时自动去掉参数改为送审并在任务日志注明 |
| 查询 | ✅ 三态：live（completed）/ draft（未送审）/ reviewing（审核中） |

**注意**：应用若已是 AAB-only，传 APK 会报 `APKs are not allowed for this application`，必须传 AAB。

## Apple App Store

| 项目 | 说明 |
| --- | --- |
| 查询 | 优先 App Store Connect API：`GET /v1/apps/{数字ID}/appStoreVersions`（版本 + 审核状态）；未配密钥时回退 iTunes Lookup + apps.apple.com 页面（只能拿到线上版本） |
| 认证 | App Store Connect API：ES256 JWT（issuer_id + key_id + .p8），令牌有效期 20 分钟 |
| 凭证 | `apple.apps`：包名 → 数字 App ID（可选带该应用专属 `api_key`）；顶层 `apple.api_key` 为默认密钥，应用分属多个开发者账号时必须逐应用配置 |
| 拉构建 | ✅ `GET /v1/builds?filter[app]=<数字ID>&sort=-uploadedDate&include=preReleaseVersion`（返回商店版本号 + 构建号 + processingState） |
| 发布 | ✅ 选 TestFlight 已上传构建 → 创建 `appStoreVersions` → PATCH build 关联 → 写本地化更新说明 → `reviewSubmissions` 提交审核 |
| 前提 | Key 角色需 App Manager；构建需处理完成（`processingState=VALID`） |
| 状态映射 | `READY_FOR_SALE`→已上架；`WAITING_FOR_REVIEW`/`IN_REVIEW`→审核中；`PENDING_APPLE_RELEASE`/`PENDING_DEVELOPER_RELEASE`/`PROCESSING_FOR_APP_STORE`→待发布；`PREPARE_FOR_SUBMISSION`→草稿；`REJECTED`/`DEVELOPER_REJECTED`→已拒绝 |
| 立即发布 | ✅ 状态为 `PENDING_DEVELOPER_RELEASE` 时状态卡出现「立即发布」按钮，调用 `POST /v1/appStoreVersionReleaseRequests` 立即上架；`PENDING_APPLE_RELEASE` 由苹果自动发布，无按钮 |

## 小米应用商店

| 项目 | 说明 |
| --- | --- |
| 域名 | https://api.developer.xiaomi.com/devupload |
| 签名 | X509 公钥 RSA-1024 分段（PKCS1v1.5），证书 `public_key` 字段 |
| 接口 | /`dev/push`（发布，onlineTime 定时）、/`dev/query`（查询） |
| 凭证 | `email` / `password` / `public_key` |
| 定时 | ✅ `onlineTime`（毫秒时间戳） |

**注意**：小米查询不返回「审核中」版本（需后台人工看）；无提前发布接口。

## OPPO 软件商店

| 项目 | 说明 |
| --- | --- |
| 域名 | https://oop-openapi-cn.heytapmobi.com |
| 认证 | OAuth2 token（48h）+ HMAC-SHA256 签名 |
| 接口 | `/resource/v1/upload/get-upload-url` → 上传 → `/resource/v1/app/upd`（发布）→ `/resource/v1/app/info`（查询） |
| 凭证 | `client_id`（**19 位**）/ `client_secret` |
| 定时 | ✅ `online_type=2` + `sche_online_time`（"YYYY-MM-DD HH:MM:SS"） |

**亮点**：更新版本自动读 `app/info` 复用现网资料（图标/简介/截图 URL），无需重新上传。

## vivo 应用商店

| 项目 | 说明 |
| --- | --- |
| 域名 | https://developer-api.vivo.com.cn/router/rest |
| 签名 | HMAC-SHA256，参数 ASCII 排序 key=value& |
| 接口 | `app.upload.apk.app`（multipart 上传→serialnumber）/ `app.sync.update.app`（发布）/ `app.query.details`（查询） |
| 凭证 | `access_key` / `access_secret` |
| 定时 | ✅ `onlineType=2` + `scheOnlineTime`（"yyyy-MM-dd HH:mm:ss"） |

## 荣耀应用市场

| 项目 | 说明 |
| --- | --- |
| Token | POST https://iam.developer.honor.com/auth/token（client_credentials，form） |
| OpenAPI | https://appmarket-openapi-drcn.cloud.honor.com/openapi/v1/publish |
| 接口 | `get-app-id` / `get-app-detail` / `get-app-current-release` / `get-file-upload-url` / `file-upload` / `update-app-info` / `submit-audit` / `get-audit-result` |
| 凭证 | `client_id` / `client_secret`（账号级，可管理账号下所有应用） |
| 定时 | ✅ `releaseType=2` + `releaseTime`（submit-audit） |
| 查询 | `auditResult`：0 审核中 / 1 通过 / 2 不通过 / 4 编辑未提交 |

## 华为 AppGallery

| 项目 | 说明 |
| --- | --- |
| Token | POST https://connect-api.cloud.huawei.com/api/oauth2/v1/token（JSON body） |
| OpenAPI | https://connect-api.cloud.huawei.com/api/publish/v2（**Android**）/ v3（HarmonyOS 5+） |
| 接口 | `appid-list` / `app-info` / `upload-url`(authCode) / multipart 直传 / `app-file-info`(fileType=5 绑定) / `app-info`(newFeatures) / `app-submit` |
| Harmony | ✅ v3 `version/brief-info/list` 拉取 AGC 已上传构建 / `app-info` 核对当前版本 / `app-submit` 提审（全网 `releaseType=1`、`releasePhase=0`；带 `releaseTime` 即定时上架） |
| 凭证 | `client_id`（19 位 **API 客户端 project_client_id**）/ `client_secret` / `app_id`；Harmony 包名条目需加 `app_kind: "harmony"` |
| 定时 | ✅ `releaseTime`（UTC："yyyy-MM-ddTHH:mm:ssZZ"） |

**关键坑（必读）**：
- 必须使用 AGC 控制台 → 用户与访问 → API 凭证 创建的 **project_client_id 类型** API 客户端（19 位 client_id + 64 位 secret）
- 「常规」页的 OAuth2.0 client_id（= App ID）不能调发布 API（一律 403）
- 一个 API client 属于一个项目，只能管理该项目下应用；华为/荣耀/小米等**不同应用可各自建 client**
- 发布绑定文件用 fileType=5（APK）；上传地址接口为 `upload-url`（非 `for-obs`），返回 uploadUrl+authCode 后 multipart 直传
