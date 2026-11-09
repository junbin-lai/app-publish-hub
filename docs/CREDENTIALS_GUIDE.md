# 凭证配置指南（含多应用）

`config/credentials.json`（本地私有，gitignore 排除）集中存放各平台凭证。
**多应用**时用 `apps` 键按包名映射凭证（优先于顶层字段）。

## 通用结构

```json
{
  "google":   { "service_account_json": "/path/to/service-account.json" },
  "apple":    { "country": "cn", "apps": { "com.android.pkg": "1234567890" } },
  "xiaomi":   { "apps": { "com.android.pkg": { "email": "", "password": "", "public_key": "" } } },
  "oppo":     { "apps": { "com.android.pkg": { "client_id": "", "client_secret": "" } } },
  "vivo":     { "apps": { "com.android.pkg": { "access_key": "", "access_secret": "" } } },
  "honor":    { "apps": { "com.android.pkg": { "client_id": "", "client_secret": "" } } },
  "huawei":   { "apps": { "com.android.pkg": { "client_id": "", "client_secret": "", "app_id": "" } } }
}
```

## 各平台详细说明

### Google Play

| 字段 | 说明 |
| --- | --- |
| `google.service_account_json` | Google Cloud Console 服务账号 JSON 文件路径 |

获取：Google Cloud → 创建服务账号并下载 JSON → Play Console → 用户和权限 → 邀请服务账号（授予发布权限）。

### Apple App Store（查询 + 发布）

| 字段 | 说明 |
| --- | --- |
| `apple.country` | 地区（默认 cn） |
| `apple.apps` | 安卓包名 → 数字 App ID（发布必须）或 bundleId（仅查询）；也可写成 `{"app_id": 数字ID, "api_key": {...}}` |
| `apple.api_key` | 可选，**默认密钥**；只有所有应用都属于同一个开发者账号时才配置 |
| `apple.apps.<包名>.api_key` | 该应用专属的 App Store Connect 密钥（issuer_id / key_id / private_key_path） |

**不同开发者账号的 Key 不能混用**：一个 `.p8` 只能访问它所属账号下的应用。
应用分散在多个账号时，请逐个应用配置 `apps.<包名>.api_key`，不要配顶层 `api_key`；
顶层 `api_key` 只在该应用没有单独配置时作为回退。

App Store 链接里的数字 ID：`https://apps.apple.com/cn/app/id1234567890` → `1234567890`。
只查询时用 bundleId（如 `com.example.app`）也可以；**发布必须配数字 ID 和该账号的 api_key**。

```json
"apple": {
  "country": "cn",
  "apps": {
    "com.kaidishi.lock": 1155786023,
    "com.motorola.security": {
      "app_id": 1234567890,
      "api_key": {
        "issuer_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        "key_id": "NPLV7ZY2R5",
        "private_key_path": "/Users/you/keys/app_stores/AuthKey_NPLV7ZY2R5.p8"
      }
    }
  }
}
```

- `.p8` 只在创建 Key 时可下载一次，丢失需吊销重建
- Key 的角色需要 **App Manager**（或 Admin/Account Holder），否则拉构建会 403
- 发布走「选择 TestFlight 已上传构建 → 关联版本 → 提交审核」，本机不需要 ipa
- `apps` 里没有数字 ID 或没有可用密钥的应用，页面不会出现「苹果 App Store 信息」区块

### 小米应用商店

```json
"xiaomi": {
  "apps": {
    "com.example.app": {
      "email": "账号邮箱",
      "password": "账号密码或私钥",
      "public_key": "/绝对路径/dev.api.public.cer"
    }
  }
}
```

- `public_key`：小米开放平台下载的 X509 公钥证书（.cer/.pem），用于 RSA 加密签名
- 不同账号对应不同证书/私钥，按包名分别配置

### OPPO 软件商店

```json
"oppo": { "apps": { "com.android.pkg": { "client_id": "19位", "client_secret": "..." } } }
```

- client_id 为 **19 位数字**（OPPO 开放平台 → 应用 → 密钥）
- 更新版本自动复用现有资料，无需重复传图标/截图

### vivo 应用商店

```json
"vivo": { "apps": { "com.android.pkg": { "access_key": "", "access_secret": "" } } }
```

- 开放平台申请 API 传包服务后获得 access_key/access_secret

### 荣耀应用市场

```json
"honor": { "apps": { "com.android.pkg": { "client_id": "", "client_secret": "" } } }
```

- 荣耀开发者服务平台 → 凭证（client_id / 密钥）

### 华为 AppGallery（重点注意）

```json
"huawei": {
  "apps": {
    "com.android.pkg": {
      "client_id": "19位API客户端ID（project_client_id 类型）",
      "client_secret": "64位十六进制密钥",
      "app_id": "应用数字ID"
    }
  }
}
```

**关键坑**：
- 必须使用 **API 客户端类型（project_client_id）** 凭证：在 AGC 控制台 → 用户与访问 → API 凭证 → 新建，生成 19 位 client_id + 64 位 secret
- **不要**使用「常规」页的 OAuth2.0 client_id（= App ID），那种凭证调发布 API 会 403
- 每个 client 归属于一个项目，只能管理该项目下应用；不同 app 可能需各自建 API 客户端
- 如果要同时管 Harmony 应用（包名与 Android 不同、app_id 不同），必须为 Harmony 包名**单独配一条**，
  并加上 `"app_kind": "harmony"`：否则版本拉取与提审会按 Android APK 流程处理，直接报错
- Harmony 条目里的 `client_id` / `client_secret` 也要写全（不会自动继承顶层配置）
- 同时要在 `apps/catalog.json` 对应应用里配置 `huawei_harmony_package`，版本下拉与提审都用这个包名匹配凭证

### 多应用示例（华为）

```json
"huawei": {
  "apps": {
    "com.example.app": { "client_id": "...1", "client_secret": "...", "app_id": "100000001" },
    "com.kaidishi.lock":       { "client_id": "...2", "client_secret": "...", "app_id": "100038281" },
    "com.kaadas.lock":         { "client_id": "...3", "client_secret": "...", "app_id": "100000002", "app_kind": "harmony" }
  }
}
```

## 校验凭证

```bash
appstore validate --credentials config/credentials.json
```

所有平台凭证均可先 `--dry-run` 发布（仅校验本地字段，不发请求）。