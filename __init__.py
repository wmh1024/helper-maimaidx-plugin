"""舞萌DX：水鱼查分器 OAuth 绑定与 Best50 查询。

指令（群聊或私聊均可，允许带一个前导 ``/``）：

- ``绑定水鱼`` / ``dfbind`` / ``绑定df``：发起水鱼 OAuth 绑定，回复授权链接。
- ``水鱼授权码 <码>`` / ``dfcode <码>``，或 ``dfbind <码>``：提交确认码完成绑定。
- ``b50`` / ``B50``：查询本人 Best50；``b50 @群成员`` 查询他人；``b50 用户名``
  按水鱼用户名查询公开成绩。

插件只保管水鱼 OAuth 应用凭据，不保存任何用户令牌；绑定关系由水鱼服务端
按 ``sha256(client_id:微信ID)`` 保存。
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from .divingfish import (
    DEFAULT_API_URL,
    DEFAULT_AUTH_URL,
    DEFAULT_SCOPE,
    BindingMismatchError,
    ConfirmationCodeError,
    DivingFish,
    DivingFishError,
    NetworkError,
    NotAuthorizedError,
    PrivacyError,
    RateLimitError,
    UserNotFoundError,
    binding_label,
    extract_confirmation_code,
)
from .music_db import SongDatabase
from .render_pil import RenderUnavailable, StaticAssets, render_best50_png

BIND_COMMANDS = frozenset({"dfbind", "绑定水鱼", "绑定df"})
CODE_COMMANDS = frozenset({"水鱼授权码", "dfcode"})
B50_COMMANDS = frozenset({"b50"})

DEFAULT_SESSION_MINUTES = 20
REVOKE_PATH = "/apps"

AUTHORIZE_MSG = """请完成水鱼查分器授权：

1. 打开以下链接并登录水鱼账号，授权「{bot_name}」访问您的水鱼查分器数据
=======================
{url}
=======================
2. 确认页面显示的绑定身份为「{label}」后点击「同意授权」
3. 复制页面给出的确认码，直接发送给 BOT（可回复「水鱼授权码 确认码」）

本次绑定 {minutes} 分钟内有效，确认码只能使用一次；
超时或失效后请重新发送「绑定水鱼」。
=======================
链接与确认码都仅供您本人使用，请勿转发他人；建议在与 BOT 的私聊中发送确认码。
如需取消授权，请前往 {revoke}"""

NO_SESSION_MSG = "请先发送「绑定水鱼」获取授权链接，完成授权后再发送确认码。"
INVALID_CODE_MSG = (
    "未识别到有效的水鱼确认码。\n"
    "请发送授权完成页面显示的完整确认码，形如 BCDF-GHJK-LMNP。"
)
OAUTH_NOT_CONFIGURED_MSG = "BOT 管理员尚未配置水鱼 OAuth 应用，无法绑定或查询本人成绩。"
BIND_START_FAILED_MSG = "发起水鱼授权失败：水鱼账号服务可能暂时不可用，请稍后再试。"
CODE_FAILED_MSG = (
    "水鱼绑定失败：确认码可能已使用、已过期，或不是本次绑定的确认码。\n"
    "当前绑定会话仍有效，您可以发送新的确认码；"
    "如需重新授权，请再次发送「绑定水鱼」。"
)
MISMATCH_MSG = (
    "水鱼绑定失败：这串确认码对应的授权不属于您的账号。\n"
    "确认码只能由发起绑定的本人使用，请勿使用他人转发给您的确认码。"
)
CODE_TEMP_FAILED_MSG = (
    "水鱼绑定暂时失败：水鱼账号服务或网络出现异常。\n"
    "当前绑定会话仍有效，您可以稍后重新发送确认码。"
)
BIND_SUCCESS_MSG = "水鱼查分器授权完成，现在可以使用「b50」查询 Best50 了。"
NOT_BOUND_MSG = "尚未绑定水鱼查分器，请先发送「绑定水鱼」完成授权。"
DRAW_UNAVAILABLE_MSG = (
    "Best50 绘图服务未就绪：BOT 未配置静态资源包（static_path）。\n"
    "请联系管理员下载 maimaiDX 资源包并把其中的 static 目录填入 static_path 后重试。"
)
DRAW_FAILED_MSG = "Best50 绘图失败，请稍后再试。"


class Plugin:
    def on_load(self, ctx):
        options = ctx.config
        self.bot_name = self._text(options.get("bot_name"), "helper")
        timeout = self._number(options.get("timeout_seconds"), 20, 1, 60, "timeout_seconds")
        minutes = self._number(
            options.get("binding_session_minutes"), DEFAULT_SESSION_MINUTES, 1, 120,
            "binding_session_minutes",
        )
        self.session_ttl = minutes * 60
        self.revoke_url = self._text(options.get("auth_url"), DEFAULT_AUTH_URL).rstrip("/") + REVOKE_PATH
        theme = self._text(options.get("theme"), "prism_plus")
        if theme not in ("prism_plus", "circle"):
            raise ValueError("水鱼插件配置项 theme 只能是 prism_plus 或 circle")
        self.theme = theme
        self.assets = StaticAssets(self._text(options.get("static_path"), ""))
        self.image_timeout = self._number(
            options.get("image_timeout_seconds"), 30, 5, 120, "image_timeout_seconds"
        )
        self.client = DivingFish(
            self._text(options.get("client_id"), ""),
            self._text(options.get("client_secret"), ""),
            scope=self._text(options.get("scope"), DEFAULT_SCOPE),
            auth_url=self._text(options.get("auth_url"), DEFAULT_AUTH_URL),
            api_url=self._text(options.get("api_url"), DEFAULT_API_URL),
            timeout=timeout,
        )
        self.song_db = SongDatabase(
            self.client.music_data,
            cache_path=Path(__file__).resolve().parent / ".runtime" / "music_data.json",
        )
        self.pending: dict[str, float] = {}
        if not self.client.oauth_configured:
            ctx.logger.warning("未配置水鱼 OAuth 应用凭据，绑定与本人查询不可用")
        if not self.assets.available:
            ctx.logger.warning(
                "未配置 static_path，Best50 无法出图；请把静态资源包的 static 目录填入 static_path"
            )

    @staticmethod
    def _text(value, default):
        if value is None:
            return default
        if not isinstance(value, str):
            raise ValueError("水鱼插件配置项必须是字符串")
        value = value.strip()
        return value or default

    @staticmethod
    def _number(value, default, low, high, name):
        if value is None:
            return default
        if type(value) not in (int, float) or not low <= value <= high:
            raise ValueError(f"水鱼插件配置项 {name} 应为 {low}–{high} 的数值")
        return value

    def on_unload(self, ctx):
        self.pending.clear()

    # ------------------------------------------------------------------ 指令

    def on_message(self, message, ctx):
        if message.type != 1:
            return
        text = message.text.strip()
        if not text:
            return
        if text.startswith("/"):
            text = text[1:].lstrip()
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        if command in BIND_COMMANDS:
            self._handle_bind(message, ctx, argument)
        elif command in CODE_COMMANDS:
            self._handle_code(message, ctx, argument)
        elif command in B50_COMMANDS:
            self._handle_b50(message, ctx, argument)

    def _handle_bind(self, message, ctx, argument):
        if argument:
            self._handle_code(message, ctx, argument)
            return
        if not self.client.oauth_configured:
            ctx.reply(message, OAUTH_NOT_CONFIGURED_MSG)
            return
        try:
            authorization = self.client.device_authorization(message.sender_id)
        except DivingFishError as error:
            ctx.logger.warning("发起水鱼授权失败：%s", error)
            ctx.reply(message, BIND_START_FAILED_MSG)
            return
        self._start_session(message.sender_id)
        try:
            expires = int(authorization.get("expires_in", 0))
        except (TypeError, ValueError):
            expires = 0
        expires = max(expires, 60)
        # 本地会话至少和确认码一样长，避免码还有效时被本地闸门拦下。
        if expires > self.session_ttl:
            self._start_session(message.sender_id, ttl=expires)
        minutes = max(expires // 60, 1)
        ctx.reply(message, AUTHORIZE_MSG.format(
            bot_name=self.bot_name,
            url=authorization["verification_uri_complete"],
            label=_safe_label(message.sender_id, ctx, message),
            minutes=minutes,
            revoke=self.revoke_url,
        ))

    def _handle_code(self, message, ctx, argument):
        if not argument:
            ctx.reply(message, INVALID_CODE_MSG)
            return
        code = extract_confirmation_code(argument)
        if code is None:
            ctx.reply(message, INVALID_CODE_MSG)
            return
        if not self.client.oauth_configured:
            ctx.reply(message, OAUTH_NOT_CONFIGURED_MSG)
            return
        if not self._session_active(message.sender_id):
            ctx.reply(message, NO_SESSION_MSG)
            return
        try:
            self.client.complete_binding(message.sender_id, code)
        except ConfirmationCodeError:
            ctx.reply(message, CODE_FAILED_MSG)
            return
        except BindingMismatchError:
            ctx.reply(message, MISMATCH_MSG)
            return
        except NetworkError:
            ctx.reply(message, CODE_TEMP_FAILED_MSG)
            return
        except DivingFishError as error:
            ctx.logger.warning("水鱼绑定失败：%s", error)
            ctx.reply(message, CODE_TEMP_FAILED_MSG)
            return
        self._clear_session(message.sender_id)
        ctx.reply(message, BIND_SUCCESS_MSG)

    def _handle_b50(self, message, ctx, argument):
        data = message.data if message.data is not None else {}
        self_wxid = data.get("self_wxid") or ""
        mentions = [
            member for member in (data.get("at_members") or ())
            if member and member != self_wxid
        ]
        if mentions:
            target = mentions[0]
            display = _member_name(ctx, target, message.chat_id)
            self._query_user(message, ctx, target, own=False, display=display)
        elif argument:
            self._query_username(message, ctx, argument)
        else:
            self._query_user(message, ctx, message.sender_id, own=True, display=None)

    # ------------------------------------------------------------ 查询与回复

    def _query_user(self, message, ctx, user_id, *, own, display):
        if not self.client.oauth_configured:
            ctx.reply(message, OAUTH_NOT_CONFIGURED_MSG)
            return
        try:
            data = self.client.query_b50(user_id=user_id)
        except NotAuthorizedError:
            if own:
                ctx.reply(message, NOT_BOUND_MSG)
            else:
                who = f"{display}（{user_id}）" if display else user_id
                ctx.reply(message, f"{who} 尚未绑定水鱼查分器，无法查询。")
            return
        except PrivacyError:
            ctx.reply(message, "该用户已设置隐私或未同意用户协议，无法查询。")
            return
        except UserNotFoundError:
            ctx.reply(message, "未找到对应的水鱼用户。")
            return
        except RateLimitError:
            ctx.reply(message, "水鱼接口请求过于频繁，请稍后再试。")
            return
        except DivingFishError as error:
            ctx.logger.warning("查询 Best50 失败：%s", error)
            ctx.reply(message, "查询 Best50 失败，请稍后再试。")
            return
        self._send_best50(message, ctx, data)

    def _query_username(self, message, ctx, username):
        try:
            data = self.client.query_b50(username=username)
        except UserNotFoundError:
            ctx.reply(message, f"未找到用户名「{username}」对应的水鱼账号。")
            return
        except PrivacyError:
            ctx.reply(message, "该用户已设置隐私或未同意用户协议，无法查询。")
            return
        except RateLimitError:
            ctx.reply(message, "水鱼接口请求过于频繁，请稍后再试。")
            return
        except DivingFishError as error:
            ctx.logger.warning("查询 Best50 失败：%s", error)
            ctx.reply(message, "查询 Best50 失败，请稍后再试。")
            return
        self._send_best50(message, ctx, data)

    def _send_best50(self, message, ctx, data):
        try:
            png = render_best50_png(
                data, assets=self.assets, theme=self.theme,
                bot_name=self.bot_name, timeout=self.image_timeout,
                song_db=self.song_db,
            )
        except RenderUnavailable as error:
            ctx.logger.warning("Best50 绘图不可用：%s", error)
            ctx.reply(message, DRAW_UNAVAILABLE_MSG)
            return
        except Exception as error:
            ctx.logger.warning("Best50 绘图失败：%s", error)
            ctx.reply(message, DRAW_FAILED_MSG)
            return
        path = _write_temp_image(png)
        if path is None:
            ctx.reply(message, DRAW_FAILED_MSG)
            return
        try:
            ctx.reply_image(message, path)
        except Exception as error:
            ctx.logger.warning("Best50 图片发送失败：%s", error)
            ctx.reply(message, DRAW_FAILED_MSG)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    # -------------------------------------------------------------- 绑定会话

    def _start_session(self, user_id, *, ttl=None):
        self._clear_expired()
        self.pending[user_id] = time.monotonic() + (self.session_ttl if ttl is None else ttl)

    def _session_active(self, user_id):
        self._clear_expired()
        return user_id in self.pending

    def _clear_session(self, user_id):
        self.pending.pop(user_id, None)

    def _clear_expired(self):
        now = time.monotonic()
        for user_id in [key for key, expires in self.pending.items() if expires <= now]:
            del self.pending[user_id]


def _write_temp_image(png: bytes) -> str | None:
    """把渲染结果落到临时文件，供框架按路径发送。"""
    try:
        handle, path = tempfile.mkstemp(suffix=".png")
        with os.fdopen(handle, "wb") as stream:
            stream.write(png)
    except OSError:
        return None
    return path


def _safe_label(user_id, ctx, message):
    """授权页面上展示的身份：优先本群称呼，否则用脱敏微信 ID。"""
    try:
        members = ctx.members
        name = members.display_name(user_id, message.chat_id)
    except Exception:
        name = None
    if isinstance(name, str) and name.strip() and not name.strip().startswith("wxid_"):
        return name.strip()[:32]
    return binding_label(user_id)


def _member_name(ctx, user_id, chat_id):
    try:
        name = ctx.members.display_name(user_id, chat_id)
    except Exception:
        return None
    if isinstance(name, str) and name.strip() and not name.strip().startswith("wxid_"):
        return name.strip()
    return None
