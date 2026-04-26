"""Audit-логирование взаимодействий с ботом в отдельный Telegram-чат.

Если в `.env` задан `LOG_CHAT_ID` (канал/группа/личка) — бот будет
автоматически копировать туда:

* **Входящие** сообщения юзеров — через `forward_message` (так в логе
  виден оригинальный отправитель и медиа).
* **Исходящие** ответы бота — через `copy_message` после успешного
  отправления (не повторяем рендер картинки/документа второй раз).

Реализация — два middleware:

1. :class:`AuditIncomingMiddleware` — outer-middleware aiogram-диспетчера,
   срабатывает на каждое входящее `Message`-событие.
2. :class:`AuditOutgoingMiddleware` — middleware на HTTP-сессии бота,
   видит каждый API-вызов перед уходом в Telegram. Когда вызов
   возвращает `Message` (т.е. бот что-то отправил пользователю), мы
   копируем это сообщение в лог-чат.

Защита от зацикливания: вызовы, у которых `chat_id == LOG_CHAT_ID`,
игнорируются (наш собственный copy/forward в лог-чат не дублирует
себя). Дополнительно edit-/delete-/answer-методы пропущены, иначе
редактирование progress-сообщений плодит шум.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import TelegramMethod
from aiogram.methods.base import Response
from aiogram.types import Message, TelegramObject

logger = logging.getLogger(__name__)

# Имена методов, которые НЕ копируем в лог-чат:
# - edit-/delete- — иначе каждое редактирование progress-сообщения будет
#   дублироваться в логах;
# - answer/get/set — это служебные вызовы, которые не отправляют
#   пользователю сообщения;
# - ForwardMessage/CopyMessage — наши же audit-вызовы, иначе
#   зациклимся.
SKIP_OUTGOING_METHODS: frozenset[str] = frozenset(
    {
        "EditMessageText",
        "EditMessageCaption",
        "EditMessageMedia",
        "EditMessageReplyMarkup",
        "EditMessageLiveLocation",
        "StopMessageLiveLocation",
        "DeleteMessage",
        "DeleteMessages",
        "AnswerCallbackQuery",
        "AnswerInlineQuery",
        "AnswerWebAppQuery",
        "AnswerPreCheckoutQuery",
        "AnswerShippingQuery",
        "SendChatAction",
        "SetMessageReaction",
        "SetMyCommands",
        "GetMe",
        "GetMyCommands",
        "GetChat",
        "GetFile",
        "GetUpdates",
        "ForwardMessage",
        "CopyMessage",
    }
)


class AuditOutgoingMiddleware(BaseRequestMiddleware):
    """Копирует каждое отправленное ботом сообщение в LOG_CHAT_ID."""

    def __init__(self, log_chat_id: int) -> None:
        self.log_chat_id = log_chat_id

    async def __call__(
        self,
        make_request: Callable[..., Awaitable[Response[Any]]],
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> Response[Any]:
        result = await make_request(bot, method)
        method_name = type(method).__name__
        if method_name in SKIP_OUTGOING_METHODS:
            return result
        chat_id = getattr(method, "chat_id", None)
        if chat_id is None or chat_id == self.log_chat_id:
            return result
        try:
            data = getattr(result, "result", None)
            if isinstance(data, Message):
                await bot.copy_message(
                    chat_id=self.log_chat_id,
                    from_chat_id=chat_id,
                    message_id=data.message_id,
                    disable_notification=True,
                )
        except Exception:  # noqa: BLE001
            # Если не удалось скопировать в лог-чат — это не должно ломать
            # пользовательский поток, просто пишем в обычные логи.
            logger.exception("audit copy_message failed for %s", method_name)
        return result


class AuditIncomingMiddleware(BaseMiddleware):
    """Forward-ит каждое входящее сообщение от юзера в LOG_CHAT_ID."""

    def __init__(self, log_chat_id: int) -> None:
        self.log_chat_id = log_chat_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if (
            isinstance(event, Message)
            and event.from_user is not None
            and event.chat.id != self.log_chat_id
        ):
            bot: Bot | None = data.get("bot")
            if bot is not None:
                u = event.from_user
                handle = f"@{u.username}" if u.username else f"id{u.id}"
                full_name = " ".join(
                    filter(None, [u.first_name, u.last_name])
                ) or "?"
                try:
                    await bot.send_message(
                        chat_id=self.log_chat_id,
                        text=(
                            f"📥 <b>{handle}</b> "
                            f"<i>({full_name}, id=<code>{u.id}</code>)</i>"
                        ),
                        parse_mode="HTML",
                        disable_notification=True,
                    )
                    await bot.forward_message(
                        chat_id=self.log_chat_id,
                        from_chat_id=event.chat.id,
                        message_id=event.message_id,
                        disable_notification=True,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("audit forward failed")
        return await handler(event, data)


def install_audit(dp: Dispatcher, bot: Bot, log_chat_id: int) -> None:
    """Включает audit-логирование, если задан `log_chat_id`.

    Вызывать после создания Bot и Dispatcher, до `start_polling`.
    """
    if not log_chat_id:
        logger.info("Audit logging выключен (LOG_CHAT_ID=0).")
        return
    dp.message.outer_middleware(AuditIncomingMiddleware(log_chat_id))
    bot.session.middleware(AuditOutgoingMiddleware(log_chat_id))
    logger.info("Audit logging включен → chat_id=%s", log_chat_id)
