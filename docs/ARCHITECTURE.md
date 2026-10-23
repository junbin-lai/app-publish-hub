# 架构说明

## 分层设计

```
appstore CLI (cli.py) ── Web 看板 (web.py)
     |                        |
     v                         v
Registry (registry.py) -- 按平台名惰性加载适配器
     |
     +-- StoreAdapter (base.py) -- 抽象基类
         |
         +-- GoogleAdapter (stores/google.py)  ready  (publish+query)
         +-- AppleAdapter  (stores/apple.py)   ready  (query；publish 已实现但实验性/待实测)
         +-- XiaomiAdapter (stores/xiaomi.py)  ready  (publish+query+定时)
         +-- OPPOAdapter   (stores/oppo.py)    ready  (publish+query+定时)
         +-- VivoAdapter   (stores/vivo.py)    ready  (publish+query+定时)
         +-- HonorAdapter  (stores/honor.py)   ready  (publish+query+定时)
         +-- HuaweiAdapter (stores/huawei.py)  ready  (publish+query+定时)
```

## 核心数据模型（models.py）

- `Platform`：枚举 google/apple/xiaomi/oppo/vivo/honor/huawei
- `AuditState`：DRAFT / SUBMITTED / REVIEWING / REJECTED / PUBLISHED / UNKNOWN
- `Release`：包名、版本名、版本号、APK/AAB 路径、更新说明、metadata（online_time 等）
- `SubmitResult`：平台、成功与否、消息、remote_reference（任务 ID）
- `StoreStatus`：live_version_names/codes、draft_version_names、reviewing_version_names、review_message

## 发布流程（publish）

1. 用户从 CLI 或 Web 发起：应用 ID + 平台选择 + 版本信息
2. `catalog.to_release()` 从 apps/catalog.json 组装 `Release`（可被 CLI 参数覆盖）
3. CLI/Web 为每个目标平台调用 `adapter.publish(release, dry_run)`
4. 适配器依次：校验凭证 → 上传安装包 → 组装版本信息 → 调平台提交审核接口
5. 返回 `SubmitResult`；Web 端单任务内各平台**并行**执行（`ThreadPoolExecutor`），不同应用的发布任务彼此独立

## Web 看板的并发与持久化

- **任务隔离**：`_TASKS` 内存字典 + 每任务后台线程；同应用已有 running 任务时拒绝新发布，不同应用可并行
- **上传闸门**：进程级 `threading.Semaphore`，跨任务/跨应用限制同时上传安装包的数量
  （`board.json` 顶层 `upload_concurrency`，默认 2，范围 1~6，启动时读取，改完需重启）；
  排队中的上传在任务被手动停止时会放弃等待
- **历史落盘**：`config/tasks_history.json` 原子写（tmp + `os.replace` + chmod 600），全局写锁序列化，
  避免多任务并发结束互相覆盖；发布历史永久保留（不按时间/条数自动清理，`/api/tasks` 仅返回最新 500 条）；
  服务运行日志 `tmpdoc/web-8090.log` 按 60 天保留，启动时轮转归档
- **防误清护栏**：未执行过加载历史的进程禁止写盘（防止外部脚本用空列表覆盖历史）
- **权限模型**：`board.json` 的 `users[].role` 为 owner / publisher / viewer（旧 `admin` 自动映射为 publisher），
  `users[].apps` 为应用白名单（缺省 = 全部，`[]` = 无，owner 恒全部）；会话中缓存角色与白名单，
  接口层统一按应用过滤可见数据并拒绝越权操作

## 查询流程（query_status）

1. 用户提供包名（或应用 ID）
2. 为每个已配置凭证的平台调用 `adapter.query_status(package_name)`
3. 适配器查询各平台最新发布 / 审核状态
4. 映射为统一 `StoreStatus`（已上架/草稿/审核中/消息）

## 凭证模型

`config/credentials.json`（本地私有）顶层按平台分键；**多应用**用 `apps` 映射：

```json
{
  "oppo": { "apps": { "com.a.b": {"client_id": "..", "client_secret": ".."} } },
  "huawei": { "apps": { "com.a.b": {"client_id": "..", "client_secret": "..", "app_id": ".."} } }
}
```

## dry-run 行为

各平台 dry-run 行为并不一致，使用时需注意：

- **Google Play**：会调用真实 API **创建 edit**（验证凭证对该应用有权限），随后**立即删除该 edit**；
  不上传安装包、不 commit，对线上无任何影响。
- **腾讯应用宝（qq）**：会调用真实**只读**接口 `query_app_detail` 获取线上版本号，
  用于校验待发布 versionCode 不小于线上版本；不取上传凭证、不上传 COS、不提审。
- **其余平台**（小米/OPPO/vivo/荣耀/华为/Apple）：仅本地校验凭证、安装包、字段，
  **不发起任何真实 API 请求**。

## 扩展新平台

1. 在 `app_store/stores/` 下创建 `xxx.py` 继承 `StoreAdapter`
2. 设置 `platform`、`required_credential_fields`、`availability`
3. 实现 `publish()` 和 `query_status()`
4. 在 `app_store/registry.py` 注册模块路径
5. 在 `models.Platform` 枚举中添加成员
6. 更新 `docs/STORE_ACCESS.md` 接入清单
