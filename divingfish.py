"""水鱼查分器 OAuth 绑定与 Best50 查询。

只使用标准库：插件在自己的线程里同步请求，不需要额外依赖。水鱼 OAuth 的
流程与上游 maimaiDX 插件一致——BOT 只保管应用凭据，用户确认授权后把页面上
的确认码发回来，BOT 凭码兑换一次令牌，之后每次查询用应用凭据换取代该用户
访问的短期令牌。本地不保存任何用户令牌。
"""
from __future__ import annotations

import json
import re
from base64 import urlsafe_b64decode
from hashlib import sha256
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_AUTH_URL = "https://auth.diving-fish.com"
DEFAULT_API_URL = "https://maimai.diving-fish.com/api/maimaidxprober"
DEFAULT_SCOPE = "prober.records.read"

ON_BEHALF_OF_GRANT = "urn:diving-fish:params:oauth:grant-type:on-behalf-of"
CONFIRMATION_CODE_GRANT = "urn:diving-fish:params:oauth:grant-type:confirmation-code"

#: 确认码只用这 20 个字母：去掉了 A/E/I/O/U 和形近的 0/1/L，方便用户手抄。
CONFIRMATION_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"
CONFIRMATION_CODE_LENGTH = 12

#: 提前一点过期，避免令牌在请求途中失效。
EXPIRES_MARGIN = 30
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

_BODY_PATTERN = re.compile(
    rf"^[{CONFIRMATION_ALPHABET}]{{{CONFIRMATION_CODE_LENGTH}}}$"
)
_PREFIX_PATTERN = re.compile(r"^(?:确认码|授权码)\s*[:：]?\s*(\S+)$")


class DivingFishError(Exception):
    """水鱼相关错误的基类。"""


class NetworkError(DivingFishError):
    """网络连接失败或超时。"""


class OAuthError(DivingFishError):
    """OAuth 服务返回了无法归类的错误。"""


class NotAuthorizedError(DivingFishError):
    """用户尚未授权 BOT 访问其水鱼数据。"""


class ConfirmationCodeError(DivingFishError):
    """确认码无效、已过期或已被使用。"""


class BindingMismatchError(DivingFishError):
    """兑换出的授权不属于发起本次绑定的用户。"""


class UserNotFoundError(DivingFishError):
    """水鱼未找到该用户。"""


class PrivacyError(DivingFishError):
    """该用户已设置隐私或未同意用户协议。"""


class RateLimitError(DivingFishError):
    """请求次数超出配额。"""


class ResponseError(DivingFishError):
    """响应结构或状态码不符合预期。"""


def extract_confirmation_code(text: str) -> str | None:
    """从整条消息里认出确认码，归一化成 ``XXXX-XXXX-XXXX``。

    只认「整条消息就是一串码」，不从一句话里抠，避免正常聊天被误判。
    容忍小写、缺少连字符、带「确认码：」前缀和首尾空白。
    """
    value = (text or "").strip()
    prefixed = _PREFIX_PATTERN.fullmatch(value)
    if prefixed:
        value = prefixed.group(1)
    body = re.sub(r"[\s\-—_]", "", value).upper()
    if not _BODY_PATTERN.fullmatch(body):
        return None
    return "-".join(body[i:i + 4] for i in range(0, CONFIRMATION_CODE_LENGTH, 4))


def subject_ref(client_id: str, user_id: str) -> str:
    """用户标识摘要，水鱼服务端用它定位授权过的账号。"""
    return sha256(f"{client_id}:{user_id}".encode("utf-8")).hexdigest()


def token_subject(access_token: str) -> str | None:
    """只解不验地读出访问令牌里的 ``sub``，用于自洽性核对。"""
    try:
        payload = access_token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        return json.loads(urlsafe_b64decode(padded)).get("sub")
    except (IndexError, ValueError):
        return None


def binding_label(user_id: str) -> str:
    """展示在授权页面上的绑定身份，用户凭它确认不是在给别人授权。"""
    value = str(user_id)
    if len(value) <= 4:
        return f"微信 {value}"
    return f"微信 {value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def _default_transport(method, url, headers, body, timeout):
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        return error.code, error.read(MAX_RESPONSE_BYTES + 1)
    except (URLError, TimeoutError, OSError) as error:
        raise NetworkError("水鱼服务连接失败或超时") from error


class DivingFish:
    """水鱼 OAuth 与查分客户端；同步调用，线程内使用。"""

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        *,
        scope: str = DEFAULT_SCOPE,
        auth_url: str = DEFAULT_AUTH_URL,
        api_url: str = DEFAULT_API_URL,
        timeout: float = 20,
        transport=None,
    ) -> None:
        self.client_id = client_id or ""
        self.client_secret = client_secret or ""
        self.scope = scope or DEFAULT_SCOPE
        self.auth_url = (auth_url or DEFAULT_AUTH_URL).rstrip("/")
        self.api_url = (api_url or DEFAULT_API_URL).rstrip("/")
        self.timeout = timeout
        self._transport = transport or _default_transport
        self._tokens: dict[str, tuple[str, float]] = {}

    @property
    def oauth_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _require_credentials(self) -> None:
        if not self.oauth_configured:
            raise OAuthError("未配置水鱼 OAuth 应用凭据")

    def _request(self, method, url, *, form=None, json_body=None, headers=None):
        body = None
        request_headers = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        if form is not None:
            body = urlencode(form).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        status, raw = self._transport(method, url, request_headers, body, self.timeout)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ResponseError("水鱼响应过大")
        data: dict = {}
        if raw:
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        return status, data

    @staticmethod
    def _raise_oauth_error(status, data):
        error = data.get("error") if isinstance(data, dict) else None
        if error == "consent_required":
            raise NotAuthorizedError("用户尚未授权 BOT 访问其水鱼数据")
        if error == "subject_mismatch":
            raise BindingMismatchError("确认码不属于发起绑定的用户")
        if error == "invalid_grant":
            raise ConfirmationCodeError("确认码无效、已过期或已被使用")
        raise OAuthError(f"水鱼授权服务返回 HTTP {status}")

    def device_authorization(self, user_id: str) -> dict:
        """发起绑定，返回给用户点开的授权链接。"""
        self._require_credentials()
        ref = subject_ref(self.client_id, user_id)
        self._tokens.pop(ref, None)
        status, data = self._request(
            "POST",
            self.auth_url + "/oauth/device_authorization",
            form={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": self.scope,
                "subject_ref": ref,
                "binding_label": binding_label(user_id),
                "handoff": "code",
            },
        )
        if status != 200 or not data.get("verification_uri_complete"):
            self._raise_oauth_error(status, data)
        return data

    def redeem(self, user_id: str, code: str) -> dict:
        """用用户发回来的确认码兑换一次令牌。"""
        self._require_credentials()
        status, data = self._request(
            "POST",
            self.auth_url + "/oauth/token",
            form={
                "grant_type": CONFIRMATION_CODE_GRANT,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "confirmation_code": code,
                "subject_ref": subject_ref(self.client_id, user_id),
            },
        )
        if status != 200 or not data.get("access_token"):
            self._raise_oauth_error(status, data)
        return data

    def fetch_token(self, user_id: str, *, refresh: bool = False) -> str:
        """换取代该用户访问的令牌，命中缓存则直接复用。"""
        self._require_credentials()
        ref = subject_ref(self.client_id, user_id)
        if refresh:
            self._tokens.pop(ref, None)
        else:
            cached = self._tokens.get(ref)
            if cached is not None and cached[1] > monotonic():
                return cached[0]
            self._tokens.pop(ref, None)
        status, data = self._request(
            "POST",
            self.auth_url + "/oauth/token",
            form={
                "grant_type": ON_BEHALF_OF_GRANT,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "subject": f"ref:{ref}",
                "scope": self.scope,
            },
        )
        if status != 200 or not data.get("access_token"):
            self._raise_oauth_error(status, data)
        token = data["access_token"]
        expires_in = data.get("expires_in")
        if type(expires_in) is not int:
            expires_in = 300
        self._tokens[ref] = (token, monotonic() + max(expires_in - EXPIRES_MARGIN, 0))
        return token

    def complete_binding(self, user_id: str, code: str) -> None:
        """兑换确认码并核对这次绑定确实落在该用户上。"""
        redeemed = self.redeem(user_id, code)
        try:
            access_token = self.fetch_token(user_id, refresh=True)
        except NotAuthorizedError as error:
            raise BindingMismatchError("授权账号与绑定身份不一致") from error
        subject = token_subject(access_token)
        if redeemed.get("sub") and subject != redeemed["sub"]:
            raise BindingMismatchError("授权账号与绑定身份不一致")

    @staticmethod
    def _raise_api_error(status, data, *, oauth=False):
        message = (data.get("message") or data.get("msg")) if isinstance(data, dict) else ""
        if status in (400, 404) and message in ("no such user", "user not exists"):
            raise UserNotFoundError("未找到该水鱼用户")
        if status == 403:
            # 授权查询返回 403，说明令牌缺少成绩读取权限，需要重新绑定。
            if oauth:
                raise NotAuthorizedError("该用户的授权已失效，请重新绑定")
            raise PrivacyError("该用户已设置隐私或未同意用户协议")
        if status == 429:
            raise RateLimitError("请求过于频繁，请稍后再试")
        raise ResponseError(f"水鱼接口返回 HTTP {status}")

    def query_b50(self, *, user_id: str | None = None, username: str | None = None) -> dict:
        """查询简略成绩信息，返回 ``charts`` 结构的原始 JSON。

        传 ``username`` 时走无需验证的公开查询；传 ``user_id`` 时用该用户的
        Bearer 令牌查询，由令牌决定查询对象。
        """
        body: dict = {"b50": True}
        headers = None
        if username:
            body["username"] = username
        elif user_id is not None:
            headers = {"Authorization": f"Bearer {self.fetch_token(user_id)}"}
        else:
            raise ValueError("必须指定查询对象")

        status, data = self._request(
            "POST", self.api_url + "/query/player", json_body=body, headers=headers
        )
        if status == 401 and user_id is not None:
            headers = {"Authorization": f"Bearer {self.fetch_token(user_id, refresh=True)}"}
            status, data = self._request(
                "POST", self.api_url + "/query/player", json_body=body, headers=headers
            )
        if status == 200:
            return data
        self._raise_api_error(status, data, oauth=user_id is not None)
        raise ResponseError(f"水鱼接口返回 HTTP {status}")

    def music_data(self) -> list:
        """获取水鱼公开曲目数据，用于计算最大 DX 分与谱面定数。"""
        status, raw = self._transport(
            "GET", self.api_url + "/music_data", {"Accept": "application/json"}, None, self.timeout
        )
        if status != 200:
            raise ResponseError(f"水鱼曲目数据返回 HTTP {status}")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ResponseError("水鱼曲目数据过大")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as error:
            raise ResponseError("水鱼曲目数据不是合法 JSON") from error
        if not isinstance(data, list):
            raise ResponseError("水鱼曲目数据结构异常")
        return data
