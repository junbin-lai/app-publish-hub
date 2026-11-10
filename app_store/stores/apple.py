
"""App Store（苹果）适配器：查询 + 发布（通过 App Store Connect API）。

查询：iTunes Lookup 公开接口为主，版本号再抓 apps.apple.com 页面校准（Lookup CDN 缓存可能滞后）。
发布（提交审核）：使用 App Store Connect API（基于 .p8 私钥 JWT 认证），
参考 GitHub 上普遍使用的 ES256 JWT + REST API 方案。

凭证配置（config/credentials.json）：
{
  "apple": {
    "country": "cn",
    "apps": {
      "com.conex.philips": 1671595666
    },
    "api_key": {
      "issuer_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "key_id": "XXXXXXXXXX",
      "private_key_path": "/path/to/AuthKey_XXXXXXXXXX.p8"
    }
  }
}

发布流程（提交已上传的构建版本供审核）：
1. 创建 App Store 版本（POST /v1/apps/{id}/appStoreVersions）
2. 查找构建并关联到版本（PATCH …/relationships/build）
3. 创建本地化信息（版本描述、更新说明）
4. 创建审核提交（POST /v1/reviewSubmissions）
5. 添加版本项目（POST /v1/reviewSubmissions/{id}/items）
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..base import StoreAdapter, StoreError
from ..models import AuditState, Platform, Release, SubmitResult, StoreStatus, utcnow_iso

_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# App Store Connect API 端点
_ASC_BASE = "https://api.appstoreconnect.apple.com/v1"

# apps.apple.com 页面版本解析缓存：App发布中枢/飞书反复查询时避免每次抓大页面。
# key=package_name, value=(expire_monotonic, version)。失败结果也缓存（短 TTL），
# 防止网络抖动时每次都等 30s 超时。
_PAGE_VERSION_CACHE: Dict[str, tuple] = {}

# JWT 令牌缓存：(issuer_id, key_id) -> (token, exp_ts)
# Apple 对令牌生成有频次限制，短时间内大量生成新令牌会被判 401 NOT_AUTHORIZED；
# 官方建议复用同一个令牌（有效期 20 分钟），这里在过期前 5 分钟才重新生成。
_TOKEN_CACHE: Dict[tuple, tuple] = {}
_TOKEN_LOCK = threading.Lock()
_PAGE_VERSION_TTL_SECONDS = 300
_PAGE_VERSION_FAIL_TTL_SECONDS = 60


def _der_ecdsa_to_raw(der: bytes) -> bytes:
    """把 DER 编码的 ECDSA 签名（SEQUENCE{INTEGER r, INTEGER s}）转成裸 R||S（各 32 字节）。

    JWT ES256 要求裸 R||S，而 cryptography 的 sign() 返回 DER。
    这里手工解析 DER 而不使用 decode_dss_signature：后者在不同
    cryptography/OpenSSL 版本（如 Windows 上的 50.0.2）上对带前导 0x00
    的 INTEGER 处理存在差异，会偶发产出 64 字节但无效的签名，
    表现为 Apple 返回 401 NOT_AUTHORIZED。
    """
    if len(der) < 8 or der[0] != 0x30:
        raise ValueError("ECDSA 签名不是 DER SEQUENCE")
    idx = 1
    first_len = der[idx]
    if first_len & 0x80:  # 长格式长度
        idx += 1 + (first_len & 0x7F)
    else:
        idx += 1
    parts = []
    for _ in range(2):
        if idx + 1 >= len(der) or der[idx] != 0x02:
            raise ValueError("ECDSA 签名不是 DER INTEGER")
        ln = der[idx + 1]
        val = der[idx + 2: idx + 2 + ln].lstrip(b"\x00")
        if len(val) > 32:
            raise ValueError("ECDSA 签名分量超过 32 字节")
        parts.append(val.rjust(32, b"\x00"))
        idx += 2 + ln
    return parts[0] + parts[1]


class AppleAdapter(StoreAdapter):
    platform = Platform.APPLE
    display_name = "Apple App Store"
    availability = "ready"
    required_credential_fields = ("country",)  # 查询只需 country；发布额外需要 api_key

    def __init__(self, credentials: Dict[str, Any]) -> None:
        super().__init__(credentials)
        self._api_key = credentials.get("api_key") or {}
        self._apps_map = self.credentials.get("apps") or {}
        # 当前请求/任务对应的包名：按应用取各自的 api_key（不同开发者账号的 p8）
        self._active_pkg = ""

    def _country(self):
        return self.credentials.get("country") or "cn"

    def _app_entry(self, package_name: str) -> Any:
        return self._apps_map.get(package_name) if package_name else None

    def _key_for(self, package_name: str = "") -> Dict[str, Any]:
        """按应用取 App Store Connect 密钥；应用未单独配置时回落到顶层 api_key。

        apps 支持两种写法：
          "com.x": 1234567890                                  -> 沿用顶层 api_key
          "com.x": {"app_id": 1234567890, "api_key": {...}}    -> 用该账号自己的 p8
        """
        entry = self._app_entry(package_name)
        if isinstance(entry, dict) and isinstance(entry.get("api_key"), dict):
            return entry["api_key"]
        return self._api_key

    def _app_id(self, package_name: str = "") -> str:
        """返回 Apple 数字 App ID（如 1671595666）或 bundleId。"""
        entry = self._app_entry(package_name)
        if isinstance(entry, dict):
            aid = entry.get("app_id") or ""
        else:
            aid = entry or ""
        if aid: return str(aid)
        aid = self.credentials.get("app_id") or ""
        if aid: return str(aid)
        if package_name:
            return package_name
        raise StoreError("苹果商店缺少 app_id 或 apps 映射")

    def _apple_app_id(self, package_name: str) -> str:
        """获取 Apple 数字 App ID（必须为数字，用于 ASC API）。"""
        sid = self._app_id(package_name).strip()
        if sid.isdigit():
            return sid
        raise StoreError(f"Apple 需要数字 App Store ID（当前: {sid}）。请在 credentials 中配置数字 app_id")

    # ========== 查询（网页解析，已稳定） ==========

    def _http_get(self, url: str, headers: Dict[str, str] = None, timeout: int = 30) -> str:
        hdrs = {"User-Agent": _BROWSER_UA}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def _lookup(self, package_name: str) -> Dict[str, Any]:
        param = {"country": self._country(), "entity": "software"}
        appid_ = self._app_id(package_name)
        sid = appid_.strip()
        param["id" if sid.isdigit() else "bundleId"] = sid
        url = "https://itunes.apple.com/lookup?" + urllib.parse.urlencode(param)
        data = json.loads(self._http_get(url, headers={"User-Agent": ""}))
        if data.get("resultCount") != 1 or not data.get("results"):
            raise StoreError(f"Apple 查询无结果: 包 {package_name!r} id {appid_!r}")
        return data["results"][0]

    def fetch_icon(self, package_name: str) -> Tuple[bytes, str]:
        """iTunes Lookup 的 artworkUrl 取应用图标（PNG），供分享图使用。"""
        r0 = self._lookup(package_name)
        url = r0.get("artworkUrl512") or r0.get("artworkUrl100") or r0.get("artworkUrl60") or ""
        if not url:
            raise StoreError("iTunes Lookup 无 artwork 字段")
        req = urllib.request.Request(url, headers={"User-Agent": ""})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        if not data:
            raise StoreError("artwork 下载为空")
        return data, "image/png"

    def _parse_page_version(self, html: str) -> str:
        m = re.search(r'"primarySubtitle":"版本\s*([0-9]+(?:\.[0-9]+){1,3})"', html)
        return m.group(1) if m else ""

    def _page_version_cached(self, package_name: str, track_url: str) -> str:
        """带缓存地抓 apps.apple.com 页面版本。

        Lookup 的 CDN 缓存可能滞后，页面版本更准；但页面抓取较慢，按包名缓存，
        成功 5 分钟、失败 1 分钟，避免 App发布中枢/飞书反复查询时每次都等页面。
        """
        now = time.monotonic()
        hit = _PAGE_VERSION_CACHE.get(package_name)
        if hit is not None and hit[0] > now:
            return hit[1]
        value = ""
        if track_url:
            try:
                html = self._http_get(track_url)
                value = self._parse_page_version(html)
            except Exception:
                value = ""
        ttl = _PAGE_VERSION_TTL_SECONDS if value else _PAGE_VERSION_FAIL_TTL_SECONDS
        _PAGE_VERSION_CACHE[package_name] = (now + ttl, value)
        return value

    def query_status(self, package_name: str) -> StoreStatus:
        """商店状态：优先用 ASC API（能拿到审核状态），无密钥时回退网页/Lookup。"""
        api_err = ""
        if self._key_for(package_name):
            try:
                return self._query_status_api(package_name)
            except StoreError as e:
                api_err = str(e)
        try:
            r0 = self._lookup(package_name)
        except StoreError as e:
            # 接口和网页都失败：抛给上层，由状态卡显示「查询失败」+ 具体原因
            raise StoreError(api_err or str(e))
        version = ""
        page_used = False
        track_url = r0.get("trackViewUrl") or ""
        v = self._page_version_cached(package_name, track_url)
        if v:
            version = v
            page_used = True
        if not version:
            version = r0.get("version") or ""
        state = AuditState.PUBLISHED if version else AuditState.UNKNOWN
        note = ""  # 已上架由徽章/已上架版本行表达，不进审核状态
        return StoreStatus(
            platform=self.platform, package_name=package_name, state=state,
            live_version_names=[version] if version else [],
            audit_note=note,
            review_message="",
            raw={"lookup": r0, "page_used": page_used, "api_error": api_err},
            checked_at=utcnow_iso(),
        )

    # ASC appStoreState → 统一状态归类
    # READY_FOR_DISTRIBUTION 是 Apple 新枚举值（替代已弃用的 READY_FOR_SALE），
    # 部分应用开始返回该值，两种都要当作"已上架"。
    _LIVE_STATES = ("READY_FOR_SALE", "READY_FOR_DISTRIBUTION")
    # READY_FOR_REVIEW：提交单已建好但还没真正送审（旧版本卡住时的常见状态）
    _REVIEW_STATES = ("READY_FOR_REVIEW", "WAITING_FOR_REVIEW", "IN_REVIEW")
    _PENDING_STATES = ("PENDING_APPLE_RELEASE", "PENDING_DEVELOPER_RELEASE",
                       "PROCESSING_FOR_APP_STORE")
    _DRAFT_STATES = ("PREPARE_FOR_SUBMISSION",)
    _REJECTED_STATES = ("REJECTED", "DEVELOPER_REJECTED", "METADATA_REJECTED")

    def _query_status_api(self, package_name: str) -> StoreStatus:
        """用 App Store Connect API 查商店版本及审核状态（不抓网页，速度快）。"""
        self._active_pkg = package_name
        apple_app_id = self._apple_app_id(package_name)
        data = self._asc_request(
            "GET",
            f"/apps/{apple_app_id}/appStoreVersions?limit=20"
            f"&fields[appStoreVersions]=versionString,appStoreState,createdDate,platform",
        )
        versions: List[Tuple[str, str, str]] = []
        for v in data.get("data") or []:
            at = v.get("attributes") or {}
            if (at.get("platform") or "IOS") != "IOS":
                continue
            versions.append((
                str(at.get("versionString") or ""),
                str(at.get("appStoreState") or ""),
                str(at.get("createdDate") or ""),
            ))
        if not versions:
            raise StoreError(f"Apple 应用 {package_name}（App ID {apple_app_id}）没有商店版本记录")
        versions.sort(key=lambda x: x[2], reverse=True)  # 新版本在前

        # Apple 会为每个曾经上架的版本保留 READY_FOR_SALE 记录，
        # 直接全量收集会把 6.21.0、6.20.0…… 全部当成"已上架版本"显示，
        # 这里只取最新的一条（versions 已按 createdDate 降序）。
        live_all = [(v, d) for v, st, d in versions if st in self._LIVE_STATES]
        live = [live_all[0][0]] if live_all else []
        live_date = live_all[0][1] if live_all else ""

        def _after_live(created: str) -> bool:
            """只保留晚于当前线上版本的记录，过滤掉历史遗留状态。

            审核中/待发布/被拒都只应反映"本次新版本"的情况；
            历史版本的 REJECTED 记录会一直留在 Apple 后台，
            不过滤会导致应用早已重新上架后仍提示"某版本被拒绝"。
            """
            return not live_date or created > live_date

        review = [(v, st) for v, st, d in versions
                  if st in self._REVIEW_STATES and _after_live(d)]
        pending = [(v, st) for v, st, d in versions
                   if st in self._PENDING_STATES and _after_live(d)]
        draft_all = [v for v, st, _ in versions if st in self._DRAFT_STATES]
        draft = draft_all[:1]
        rejected = [(v, st) for v, st, d in versions
                    if st in self._REJECTED_STATES and _after_live(d)]

        if review:
            state = AuditState.REVIEWING
        elif pending:
            state = AuditState.PENDING
        elif rejected and not live:
            state = AuditState.REJECTED
        elif live:
            state = AuditState.PUBLISHED
        elif draft:
            state = AuditState.DRAFT
        else:
            state = AuditState.UNKNOWN

        note = ""
        if review:
            ver, st = review[0]
            if st == "READY_FOR_REVIEW":
                note = f"{ver} 待送审（提交单未提交）"
            elif st == "WAITING_FOR_REVIEW":
                note = f"{ver} 等待审核"
            else:
                note = f"{ver} 审核中"
        elif pending:
            ver, st = pending[0]
            note = {
                "PENDING_APPLE_RELEASE": f"{ver} 审核通过，等待苹果发布",
                "PENDING_DEVELOPER_RELEASE": f"{ver} 审核通过，待开发者发布",
                "PROCESSING_FOR_APP_STORE": f"{ver} 审核通过，处理中",
            }.get(st, "")
        elif rejected:
            note = f"{rejected[0][0]} 被拒绝"

        return StoreStatus(
            platform=self.platform, package_name=package_name, state=state,
            live_version_names=live,
            reviewing_version_names=[v for v, _ in review],
            draft_version_names=draft,
            audit_note=note,
            # 只有「审核通过、待开发者发布」可以手动一键上线；
            # PENDING_APPLE_RELEASE 由苹果自动发布，PROCESSING_FOR_APP_STORE 正在处理，均无操作。
            apple_release_now=any(st == "PENDING_DEVELOPER_RELEASE" for _, st in pending),
            review_message="",
            raw={"api_versions": versions},
            checked_at=utcnow_iso(),
        )

    # ========== 状态卡操作：审核通过后立即发布 ==========

    def release_now(self, package_name: str) -> Dict[str, Any]:
        """把「审核通过、待开发者发布」的版本立即发布上线。

        只有 PENDING_DEVELOPER_RELEASE（版本发布方式选「手动发布」）可以主动
        触发，通过创建 appStoreVersionReleaseRequest 让苹果立即上架；
        PENDING_APPLE_RELEASE 由苹果自动发布，PROCESSING_FOR_APP_STORE 正在
        处理，两者都没有可调用的发布接口。
        """
        if not self._key_for(package_name):
            raise StoreError(
                f"Apple 应用 {package_name} 立即发布需要配置 api_key"
                "（issuer_id / key_id / private_key_path）"
            )
        self._active_pkg = package_name
        apple_app_id = self._apple_app_id(package_name)
        data = self._asc_request(
            "GET",
            f"/apps/{apple_app_id}/appStoreVersions?limit=20"
            f"&fields[appStoreVersions]=versionString,appStoreState,createdDate,platform",
        )
        target: Optional[Dict[str, Any]] = None
        for v in data.get("data") or []:
            at = v.get("attributes") or {}
            if (at.get("platform") or "IOS") != "IOS":
                continue
            if at.get("appStoreState") == "PENDING_DEVELOPER_RELEASE":
                target = v
                break
        if not target:
            raise StoreError("没有「审核通过、待开发者发布」的版本，无需执行立即发布")

        version_id = target.get("id") or ""
        version_name = (target.get("attributes") or {}).get("versionString") or ""
        self._asc_request("POST", "/appStoreVersionReleaseRequests", {
            "data": {
                "type": "appStoreVersionReleaseRequests",
                "relationships": {
                    "appStoreVersion": {
                        "data": {"type": "appStoreVersions", "id": version_id},
                    },
                },
            },
        })
        return {
            "version": version_name,
            "message": f"{version_name} 已提交立即发布，通常几分钟内在 App Store 生效",
        }

    # ========== 发布（App Store Connect API，框架待测） ==========

    def check(self) -> List[str]:
        """检查发布依赖：校验顶层及各应用单独配置的 .p8 文件是否存在。"""
        problems: List[str] = []
        keys = [self._api_key] + [
            e.get("api_key") for e in self._apps_map.values()
            if isinstance(e, dict) and isinstance(e.get("api_key"), dict)
        ]
        for key_info in keys:
            p8_path = (key_info or {}).get("private_key_path") or ""
            if p8_path and not Path(p8_path).expanduser().is_file():
                problems.append(f"Apple API 私钥 .p8 文件不存在: {p8_path}")
        return problems

    def _jwt_token(self) -> str:
        """生成 App Store Connect API JWT 令牌（ES256 签名）。

        参考 GitHub 普遍使用的 JWT + ECDSA 方案：
        - 用 cryptography 的 ECDSA 签名
        - 有效期 20 分钟（Apple 最大限制）
        - kid header 指向生成的 API Key ID
        """
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.backends import default_backend

        key = self._key_for(self._active_pkg)
        issuer_id = key.get("issuer_id") or ""
        key_id = key.get("key_id") or ""
        p8_path = key.get("private_key_path") or ""

        if not issuer_id or not key_id or not p8_path:
            who = f"应用 {self._active_pkg} " if self._active_pkg else ""
            raise StoreError(f"Apple {who}发布需要配置 api_key.issuer_id / key_id / private_key_path")

        p8_path = Path(p8_path).expanduser()
        if not p8_path.is_file():
            raise StoreError(f"Apple API 私钥文件不存在: {p8_path}")

        # 复用未过期的令牌（Apple 限制令牌生成频次，频繁生成会返回 401）
        now = int(time.time())
        cache_key = (issuer_id, key_id)
        with _TOKEN_LOCK:
            hit = _TOKEN_CACHE.get(cache_key)
        if hit and int(hit[1]) - 300 > now:
            return hit[0]

        # 加载 .p8 私钥（PKCS#8 格式）
        p8_bytes = p8_path.read_bytes()
        private_key = serialization.load_pem_private_key(
            p8_bytes, password=None, backend=default_backend(),
        )

        now = int(time.time())
        payload = {
            "iss": issuer_id,
            "iat": now,
            "exp": now + 1200,  # 20 分钟
            "aud": "appstoreconnect-v1",
        }
        header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}

        # JWT base64url 编码
        def _b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

        header_b64 = _b64(json.dumps(header, separators=(",", ":")).encode())
        payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode())
        message = (header_b64 + "." + payload_b64).encode()

        # ES256 签名（P-256 + SHA-256）
        # 注意：cryptography 输出的是 DER 编码，JWT ES256 要求裸 R||S（各 32 字节）。
        # 直接 base64 DER 会被 Apple 判为 401 NOT_AUTHORIZED；
        # 转换用 _der_ecdsa_to_raw() 手工解析，避免 decode_dss_signature 的版本差异。
        der_sig = private_key.sign(message, ec.ECDSA(hashes.SHA256()))
        raw_sig = _der_ecdsa_to_raw(der_sig)

        # 本地自检：R||S 还原成 DER 后必须能被公钥验签通过，
        # 避免签名转换异常时把无效 token 发出去（只会得到远端 401）。
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        try:
            private_key.public_key().verify(
                encode_dss_signature(int.from_bytes(raw_sig[:32], "big"),
                                     int.from_bytes(raw_sig[32:], "big")),
                message, ec.ECDSA(hashes.SHA256()),
            )
        except Exception as exc:
            raise StoreError(f"Apple JWT 签名自检失败（cryptography/OpenSSL 异常）: {exc}")

        token = f"{header_b64}.{payload_b64}.{_b64(raw_sig)}"
        with _TOKEN_LOCK:
            _TOKEN_CACHE[cache_key] = (token, now + 1200)
        return token

    def _asc_request(self, method: str, path: str, body: Any = None) -> Dict[str, Any]:
        """发送 App Store Connect API 请求，401 NOT_AUTHORIZED 自动重试。

        App Store Connect 的 daiquiri 网关是多实例部署，部分实例的 API Key
        缓存同步存在延迟：同一个有效 token 连续请求会出现 401/200 交替
        （实测连续 10 次为 3 次 401 + 7 次 200）。401 表示请求未被处理，
        重试是安全的，且通常下一次就会落到可用实例。

        Args:
            method: GET / POST / PATCH
            path: 如 /apps/123/appStoreVersions
            body: 请求体 dict

        Returns:
            解析后的 JSON 响应 data
        """
        attempts = 3
        last_error: Optional[StoreError] = None
        for attempt in range(attempts):
            try:
                return self._asc_request_once(method, path, body)
            except StoreError as exc:
                if "HTTP 401" not in str(exc) or attempt == attempts - 1:
                    raise
                last_error = exc
                if attempt == 1:
                    # 再试前强制重签令牌，避开密钥缓存未同步的实例
                    with _TOKEN_LOCK:
                        _TOKEN_CACHE.clear()
                time.sleep(0.6 * (attempt + 1))
        raise last_error if last_error is not None else StoreError("App Store Connect API 请求失败")

    def _asc_request_once(self, method: str, path: str, body: Any = None) -> Dict[str, Any]:
        """发送一次 App Store Connect API 请求（不重试）。"""
        token = self._jwt_token()
        if path.startswith("http://") or path.startswith("https://"):
            url = path  # links.next 返回的是完整地址
        else:
            url = _ASC_BASE + (path if path.startswith("/") else "/" + path)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AppPublishHub/1.0",
        }
        data_bytes = json.dumps(body, ensure_ascii=False).encode() if body else None

        # 使用 http.client 而非 urllib.request.urlopen：Windows 上 urllib 会在请求中
        # 追加 Accept-Encoding: identity / Connection: close，被 App Store Connect 的
        # 边缘节点判为 401 NOT_AUTHORIZED（同样的 token 用 http.client / curl 均正常）。
        parts = urllib.parse.urlsplit(url)
        path_q = parts.path + (("?" + parts.query) if parts.query else "")
        conn = http.client.HTTPSConnection(parts.netloc, timeout=60, context=ssl.create_default_context())
        try:
            conn.request(method, path_q, body=data_bytes, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            if resp.status >= 400:
                try:
                    detail: Any = json.loads(raw)
                except Exception:
                    detail = raw[:300]
                raise StoreError(
                    f"App Store Connect API {method} {path} 失败: HTTP {resp.status} {detail}",
                )
            return json.loads(raw) if raw else {}
        except StoreError:
            raise
        except Exception as e:
            raise StoreError(f"App Store Connect API 请求失败: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _get_app_store_version_id(self, apple_app_id: str, version_string: str) -> Optional[str]:
        """查找已有 App Store 版本（用于更新已有版本而非创建新版本）。

        分页跟随 links.next 翻页（limit=200）；确实不存在返回 None，
        StoreError / 网络错误向上抛，不静默吞掉。
        """
        path: Optional[str] = f"/apps/{apple_app_id}/appStoreVersions?limit=200"
        while path:
            data = self._asc_request("GET", path)
            for v in data.get("data", []):
                attrs = v.get("attributes", {})
                if attrs.get("versionString") == version_string:
                    return v["id"]
            nxt = (data.get("links") or {}).get("next") or ""
            # links.next 为绝对 URL，转回 _asc_request 接受的相对路径
            path = nxt[len(_ASC_BASE):] if nxt.startswith(_ASC_BASE) else None
        return None

    def list_builds(self, package_name: str) -> List[Dict[str, Any]]:
        """TestFlight 里已上传的构建（供发布前勾选，不参与打包）。

        返回 [{build_id, version_name, build_number, processing_state,
               uploaded_date}]，按上传时间倒序，已过期构建不返回。
        """
        if not self._key_for(package_name):
            raise StoreError(
                f"Apple 应用 {package_name} 拉取构建需要配置 api_key"
                "（issuer_id / key_id / private_key_path）"
            )
        self._active_pkg = package_name
        apple_app_id = self._apple_app_id(package_name)
        data = self._asc_request(
            "GET",
            f"/builds?filter[app]={apple_app_id}&sort=-uploadedDate&limit=50"
            f"&include=preReleaseVersion",
        )
        # preReleaseVersion.version 才是商店版本号（4.19.1）；build.version 是构建号（219）
        # 注意：JSON:API 的 type 是复数 "preReleaseVersions"
        pre: Dict[str, str] = {}
        for it in data.get("included") or []:
            if it.get("type") == "preReleaseVersions":
                pre[it.get("id") or ""] = (it.get("attributes") or {}).get("version") or ""
        rows: List[Dict[str, Any]] = []
        for b in data.get("data") or []:
            attrs = b.get("attributes") or {}
            if attrs.get("expired"):
                continue
            rel = ((b.get("relationships") or {}).get("preReleaseVersion") or {}).get("data") or {}
            rows.append({
                "build_id": b.get("id") or "",
                "version_name": pre.get(rel.get("id") or "", ""),
                "build_number": attrs.get("version") or "",
                "processing_state": attrs.get("processingState") or "",
                "uploaded_date": attrs.get("uploadedDate") or "",
            })
        rows.sort(key=lambda r: r.get("uploaded_date") or "", reverse=True)
        return rows

    def publish(self, release: Release, dry_run: bool = False) -> SubmitResult:
        """提交已上传的构建版本以进行审核（App Store Connect API）。

        前提：
        1. 构建已通过 Xcode / Transporter 上传至 App Store Connect
        2. 构建在 TestFlight 中已处理出口合规
        3. 凭证配置了 api_key（issuer_id, key_id, private_key_path）

        流程（参考 GitHub 主流实现）：
        1. 创建 App Store 版本 -> 2. 关联构建 -> 3. 填本地化信息（更新说明） -> 4. 提交审核
        """
        if not self._key_for(release.package_name):
            raise StoreError(
                f"Apple 应用 {release.package_name} 发布需要配置 api_key"
                "（issuer_id / key_id / private_key_path）。"
                "生成路径：App Store Connect → 用户和访问 → 集成 → App Store Connect API"
            )
        self._active_pkg = release.package_name

        apple_app_id = self._apple_app_id(release.package_name)
        # 前端勾选的 TestFlight 构建（可选）；没有则按版本号自动匹配
        sel = (release.metadata or {}).get("apple_build") or {}
        sel_build_id = str(sel.get("build_id") or "")
        version_str = str(sel.get("version_name") or "") or release.version_name or ""
        if not version_str:
            raise StoreError("Apple 发布需要提供 version_name（版本号，如 4.19.1）")
        release_notes = release.release_notes or ""

        if dry_run:
            extra = f"，构建 {sel.get('build_number') or sel_build_id}" if sel_build_id else "（按版本号匹配构建）"
            return SubmitResult(self.platform, True,
                                f"Apple: dry-run 通过（将发布 {version_str} 给 App ID {apple_app_id}{extra}）",
                                state=AuditState.DRAFT)

        step_cb = (release.metadata or {}).get("_step_cb")

        # 1) 创建或获取 App Store 版本
        if step_cb: step_cb("创建 App Store 版本…")
        existing = self._get_app_store_version_id(apple_app_id, version_str)
        if existing:
            version_id = existing
            if step_cb: step_cb(f"版本 {version_str} 已存在，复用 ID: {version_id[:8]}…")
        else:
            body = {
                "data": {
                    "type": "appStoreVersions",
                    "attributes": {
                        "platform": "IOS",
                        "versionString": version_str,
                    },
                    "relationships": {
                        "app": {"data": {"type": "apps", "id": apple_app_id}},
                    },
                }
            }
            resp = self._asc_request("POST", "/appStoreVersions", body=body)
            version_id = resp.get("data", {}).get("id", "")
            if step_cb: step_cb(f"版本 {version_str} 已创建 ({version_id[:8]}…)")
            time.sleep(0.5)

        if not version_id:
            raise StoreError(f"Apple 版本创建失败（versionString: {version_str}）")

        # 2) 查找构建并关联到版本
        if sel_build_id:
            build_id = sel_build_id
            if step_cb:
                step_cb(f"使用已选构建 {sel.get('build_number') or build_id}（{build_id[:8]}…）")
        else:
            if step_cb: step_cb("查找匹配的构建版本…")
            build_id = self._find_build(apple_app_id, version_str)
        if not build_id:
            raise StoreError(
                f"Apple 未找到版本 {version_str} 的构建。请先用 Xcode / Transporter 上传构建，"
                f"并在 TestFlight 中完成出口合规处理"
            )
        # 关联构建
        self._asc_request("PATCH", f"/appStoreVersions/{version_id}/relationships/build", body={
            "data": {"type": "builds", "id": build_id},
        })
        if step_cb: step_cb(f"构建 {build_id[:8]}… 已关联到版本")

        # 3) 填写本地化信息（至少一条）
        if step_cb: step_cb("填写版本本地化（更新说明）…")
        self._set_version_localizations(apple_app_id, version_id, release)

        # 4) 提交审核
        if step_cb: step_cb("创建审核提交…")
        self._submit_for_review(apple_app_id, version_id, version_str)

        if step_cb: step_cb("审核已提交")
        return SubmitResult(
            platform=self.platform, ok=True,
            message=f"Apple: 已提交 {version_str} 审核（App ID {apple_app_id}）",
            remote_reference=version_id,
            state=AuditState.SUBMITTED,
            raw={"version_id": version_id, "build_id": build_id, "version": version_str},
        )

    # ---- ASC API 辅助方法 ----

    def _find_build(self, apple_app_id: str, version_str: str) -> Optional[str]:
        """按版本号查找匹配的构建。

        先精确匹配 versionString；失败则尝试 preReleaseVersion 匹配。
        仅「确实不存在」返回 None；StoreError / 网络错误向上抛，不静默吞掉。
        """
        # 精确匹配
        build_data = self._asc_request(
            "GET",
            f"/builds?filter[app]={apple_app_id}&filter[version]={version_str}"
            f"&filter[processingState]=VALID&limit=5",
        )
        builds = build_data.get("data", [])
        if builds:
            return builds[0]["id"]
        # 试试前缀匹配（如 "4.19" 匹配 "4.19.1"）
        data2 = self._asc_request(
            "GET",
            f"/builds?filter[app]={apple_app_id}&filter[preReleaseVersion.version]={version_str}"
            f"&limit=5",
        )
        builds = data2.get("data", [])
        if builds:
            return builds[0]["id"]
        return None

    # 提交审核时 ASC 要求每个本地化必须带值的属性（新建版本不会自动继承）
    _L10N_REQUIRED = ("description", "keywords", "supportUrl")
    # 可从历史版本复制的元数据属性（whatsNew 仅用于纯同步语言，本次有文案时会被覆盖）
    _L10N_COPYABLE = ("description", "keywords", "supportUrl", "marketingUrl",
                      "promotionalText", "whatsNew")

    def _l10n_template(self, apple_app_id: str, version_id: str) -> Dict[str, Dict[str, str]]:
        """取历史版本（当前版本除外）的本地化作为模板，返回 locale -> {属性: 值}。

        新建的 appStoreVersion 其本地化默认是空的，Apple 在提交审核时会要求
        description / keywords / supportUrl 必须有值，因此需要从上一次发布过的
        版本复制这些元数据。
        """
        try:
            res = self._asc_request(
                "GET", f"/apps/{apple_app_id}/appStoreVersions?limit=200"
                       f"&fields[appStoreVersions]=createdDate,versionString")
        except StoreError:
            return {}
        items = [it for it in (res.get("data") or []) if it.get("id") != version_id]
        items.sort(key=lambda it: (it.get("attributes") or {}).get("createdDate") or "", reverse=True)
        for it in items:
            vid = it.get("id") or ""
            if not vid:
                continue
            try:
                locs = self._asc_request(
                    "GET", f"/appStoreVersions/{vid}/appStoreVersionLocalizations?limit=200")
            except StoreError:
                continue
            tpl: Dict[str, Dict[str, str]] = {}
            for lo in locs.get("data") or []:
                attrs = lo.get("attributes") or {}
                loc = attrs.get("locale") or ""
                if not loc:
                    continue
                vals = {k: (attrs.get(k) or "") for k in self._L10N_COPYABLE}
                if any(vals.values()):
                    tpl[loc] = vals
            if tpl:
                return tpl
        return {}

    @staticmethod
    def _pick_template(tpl: Dict[str, Dict[str, str]], locale: str) -> Dict[str, str]:
        """为某个语言选模板：优先同语言，其次中文互转，最后回退英文/任意语言。

        常见情况：应用只配了 zh-Hant 和 en-US，但更新说明传的是简体。
        此时若直接新建 zh-Hans，Apple 会因 description/keywords/supportUrl
        为空而拒绝提交，所以用最接近的语言补齐这些必填元数据。
        """
        if locale in tpl:
            return tpl[locale]
        prefer = {"zh-Hans": ("zh-Hant",), "zh-Hant": ("zh-Hans",)}
        for p in prefer.get(locale, ()):
            if p in tpl:
                return tpl[p]
        for p in ("en-US", "zh-Hant", "zh-Hans"):
            if p in tpl:
                return tpl[p]
        return next(iter(tpl.values())) if tpl else {}

    def _set_version_localizations(self, apple_app_id: str, version_id: str,
                                   release: Release) -> None:
        """按语言写入更新说明，并补齐 Apple 要求的必填元数据。

        Apple 的语言码与 Google 不同：简体 zh-Hans、繁体 zh-Hant、英文 en-US；
        前端统一按 zh-CN / zh-TW / en-US 传，这里做映射。
        已存在的本地化走 PATCH，不存在才 POST（ASC 对同一 locale 重复创建会报 409）。

        description / keywords / supportUrl 是该版本本地化的必填属性，新建版本
        不会从上一版本自动继承，缺失时提交审核会报 409 ENTITY_ERROR.ATTRIBUTE.REQUIRED，
        因此这里从历史版本复制补齐；历史版本也没有时给出明确提示。
        """
        l10n = (release.metadata or {}).get("release_notes_l10n") or {}
        base = release.release_notes or ""
        notes: Dict[str, str] = {}
        zh = (l10n.get("zh-CN") or "").strip() or base
        if zh:
            notes["zh-Hans"] = zh
        tw = (l10n.get("zh-TW") or "").strip()
        if tw:
            notes["zh-Hant"] = tw
        en = (l10n.get("en-US") or "").strip()
        if en:
            notes["en-US"] = en
        if not notes:
            return

        existing: Dict[str, Dict[str, Any]] = {}
        try:
            res = self._asc_request(
                "GET", f"/appStoreVersions/{version_id}/appStoreVersionLocalizations?limit=200")
            for it in res.get("data") or []:
                loc = (it.get("attributes") or {}).get("locale") or ""
                if loc:
                    existing[loc] = {"id": it.get("id") or "", "attrs": it.get("attributes") or {}}
        except StoreError:
            existing = {}

        tpl = self._l10n_template(apple_app_id, version_id)

        # 目标语言 = 本次要写更新说明的语言 + 历史版本里已有的全部语言。
        # 历史版本可能有十几二十种语言，新建版本时这些语言不会自动出现，
        # 需要按历史元数据逐个补建，否则商店对应语种的页面会退回主语言。
        targets: List[str] = list(notes.keys())
        for loc in list(tpl.keys()) + list(existing.keys()):
            if loc not in targets:
                targets.append(loc)

        # 第一阶段：算出每个语言要写的属性，顺带收集无法补齐的必填项
        plan: List[Tuple[str, str, Dict[str, Any]]] = []  # (locale, 现有 id, 属性)
        missing: List[str] = []
        for locale in targets:
            cur = existing.get(locale)
            src = self._pick_template(tpl, locale)
            has_note = locale in notes
            if cur:
                attrs: Dict[str, Any] = {}
                if has_note:
                    attrs["whatsNew"] = notes[locale]
                for k in self._L10N_REQUIRED:
                    if not (cur["attrs"].get(k) or "").strip():
                        if src.get(k):
                            attrs[k] = src[k]
                        elif has_note:
                            # 本次要写更新说明却补不齐必填 → 提交必然 409，提前报错
                            missing.append(f"{locale}.{k}")
                if attrs:
                    plan.append((locale, cur["id"] or "", attrs))
                continue
            # 新版本里没有该语言：用历史元数据创建
            attrs = {"locale": locale}
            for k in self._L10N_COPYABLE:
                if src.get(k):
                    attrs[k] = src[k]
            if has_note:
                attrs["whatsNew"] = notes[locale]
            lack = [k for k in self._L10N_REQUIRED if not attrs.get(k)]
            if lack:
                if has_note:
                    missing.extend(f"{locale}.{k}" for k in lack)
                # 纯同步的历史语言若元数据本身不全就跳过，避免阻塞发布
                continue
            plan.append((locale, "", attrs))

        if missing:
            raise StoreError(
                "Apple 本地化元数据缺失：" + "、".join(missing) +
                "。历史版本中没有可复制的值，请先在 App Store Connect 网页端补全"
                "版本描述、关键词、技术支持网址后再提交审核"
            )

        # 第二阶段：落库
        for locale, loc_id, attrs in plan:
            if loc_id:
                self._asc_request("PATCH", f"/appStoreVersionLocalizations/{loc_id}", body={
                    "data": {
                        "type": "appStoreVersionLocalizations",
                        "id": loc_id,
                        "attributes": attrs,
                    }
                })
                continue
            body = {
                "data": {
                    "type": "appStoreVersionLocalizations",
                    "attributes": attrs,
                    "relationships": {
                        "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}},
                    },
                }
            }
            self._asc_request("POST", "/appStoreVersionLocalizations", body=body)

    def _open_review_submission(self, apple_app_id: str) -> str:
        """返回该 App 尚未提交的审核提交单 id（Apple 同时只允许一个）。

        历史上失败的发布可能留下只建单、没送审的残留提交单，
        直接新建会触发 409，所以优先复用。
        """
        try:
            res = self._asc_request(
                "GET",
                f"/apps/{apple_app_id}/reviewSubmissions"
                f"?filter[state]=READY_FOR_REVIEW&limit=10",
            )
        except StoreError:
            return ""
        for it in res.get("data") or []:
            sid = it.get("id") or ""
            if sid:
                return sid
        return ""

    def _submission_has_version(self, sub_id: str, version_id: str) -> bool:
        """提交单里是否已包含该版本（重复添加会 409）。"""
        try:
            res = self._asc_request(
                "GET",
                f"/reviewSubmissions/{sub_id}/items?limit=50&include=appStoreVersion",
            )
        except StoreError:
            return False
        ids = set()
        for it in res.get("data") or []:
            rel = ((it.get("relationships") or {}).get("appStoreVersion") or {}).get("data") or {}
            if rel.get("id"):
                ids.add(rel["id"])
        if not ids:
            # 部分情况下 items 不返回 relationship linkage，只能从 included 反推
            for inc in res.get("included") or []:
                if inc.get("type") == "appStoreVersions" and inc.get("id"):
                    ids.add(inc["id"])
        return version_id in ids

    def _submit_for_review(self, apple_app_id: str, version_id: str, version_str: str) -> None:
        """创建/复用审核提交，添加版本项目，并正式提交审核。

        流程（Apple 现行方案）：
        1. 复用或 POST /v1/reviewSubmissions 创建审核提交
        2. POST /v1/reviewSubmissionItems 添加 APP_STORE_VERSION 项目
        3. PATCH /v1/reviewSubmissions/{id} submitted=true  <- 缺少这步版本会一直停在
           READY_FOR_REVIEW，Apple 侧根本收不到送审请求
        """
        newly_created = False
        sub_id = self._open_review_submission(apple_app_id)
        if not sub_id:
            sub_body = {
                "data": {
                    "type": "reviewSubmissions",
                    "attributes": {"platform": "IOS"},
                    "relationships": {
                        "app": {"data": {"type": "apps", "id": apple_app_id}},
                    },
                }
            }
            sub_resp = self._asc_request("POST", "/reviewSubmissions", body=sub_body)
            sub_id = sub_resp.get("data", {}).get("id", "")
            if not sub_id:
                raise StoreError("Apple 审核提交创建失败")
            newly_created = True
            time.sleep(0.3)

        # 添加版本项目（已存在则跳过，避免 409）
        if not self._submission_has_version(sub_id, version_id):
            item_body = {
                "data": {
                    "type": "reviewSubmissionItems",
                    "relationships": {
                        "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": sub_id}},
                        "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}},
                    },
                }
            }
            try:
                self._asc_request("POST", "/reviewSubmissionItems", body=item_body)
            except Exception as e:
                # 仅清理本次新建的空提交单，复用旧单时不能删（里面可能有已有项目）
                if newly_created:
                    try:
                        self._asc_request("DELETE", f"/reviewSubmissions/{sub_id}")
                    except Exception as de:
                        print(f"Apple 清理 reviewSubmission {sub_id} 失败: {de}", file=sys.stderr)
                raise StoreError(f"Apple 添加审核项目失败: {e}")

        # 正式提交审核
        self._asc_request(
            "PATCH", f"/reviewSubmissions/{sub_id}",
            body={
                "data": {
                    "type": "reviewSubmissions",
                    "id": sub_id,
                    "attributes": {"submitted": True},
                }
            },
        )
