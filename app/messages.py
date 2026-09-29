# Сообщения бота: тексты, кнопки и отправка через maxo.
from datetime import datetime

from maxo import Bot
from maxo.enums.text_format import TextFormat
from maxo.methods import SendMessage
from maxo.types.callback_button import CallbackButton
from maxo.types.inline_keyboard_attachment_request import InlineKeyboardAttachmentRequest
from maxo.types.inline_keyboard_attachment_request_payload import InlineKeyboardAttachmentRequestPayload
from maxo.types.link_button import LinkButton
from maxo.types.open_app_button import OpenAppButton

from app import config
from app.domain import MSK

_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def fmt_dt(value: datetime) -> str:
    local = value.astimezone(MSK)
    return f"{local.day} {_MONTHS[local.month - 1]}, {local:%H:%M}"


def keyboard(rows: list[list]) -> InlineKeyboardAttachmentRequest:
    return InlineKeyboardAttachmentRequest(payload=InlineKeyboardAttachmentRequestPayload(buttons=rows))


def open_app(text: str, payload: str | None = None) -> OpenAppButton:
    return OpenAppButton(text=text, web_app=config.BOT_USERNAME, payload=payload) if payload else OpenAppButton(
        text=text, web_app=config.BOT_USERNAME
    )


# Ссылка на мини-приложение: переживает пересылку, в отличие от кнопок.
def deeplink(payload: str) -> str | None:
    return f"https://max.ru/{config.BOT_USERNAME}?startapp={payload}" if config.BOT_USERNAME else None


def welcome(first_name: str | None) -> dict:
    hello = f"{first_name}, привет!" if first_name else "Привет!"
    return {
        "text": (
            f"{hello} Это Aperio | Бесплатные мероприятия. Сообщу, когда откроется запись на бесплатные "
            "экскурсии, лекции и мастер-классы в Москве, и подскажу, в какие дни вам можно в музей бесплатно по льготе.\n\n"
            "Откройте приложение и выберите категорию льготы.\n\nПроверить уведомление: /demo"
        ),
        "attachments": [keyboard([[open_app("Открыть Aperio")], [CallbackButton(text="Прислать тестовое уведомление", payload="demo")]])],
    }


def help_message() -> dict:
    return {
        "text": ("Команды:\n/start — открыть приложение\n/demo — тестовое уведомление через минуту\n"
                 "/admin — задать вопрос администратору\n/help — эта подсказка"),
        "attachments": [keyboard([[open_app("Открыть Aperio")]])],
    }


def ask_admin() -> dict:
    return {"text": "Напишите свой вопрос одним сообщением — свяжем вас с администратором, он разберётся с проблемой."}


def question_sent() -> dict:
    return {"text": "Вопрос передан администратору. Он ответит вам в этом чате."}


# вопрос уходит главному администратору вместе с кнопкой ответа
def question_for_admin(first_name: str | None, user_id: int, text: str) -> dict:
    who = first_name or "пользователя"
    return {
        "text": f"Вопрос от {who} (ID {user_id}):\n\n{text}",
        "attachments": [keyboard([[CallbackButton(text=f"Ответить {who}", payload=f"reply:{user_id}")]])],
    }


def reply_prompt(first_name: str | None, user_id: int) -> dict:
    return {"text": f"Напишите ответ одним сообщением — перешлю его {first_name or 'пользователю'} (ID {user_id})."}


def reply_for_user(text: str) -> dict:
    return {"text": f"Ответ администратора:\n\n{text}"}


def reply_sent(first_name: str | None) -> dict:
    return {"text": f"Ответ отправлен {first_name or 'пользователю'}."}


def demo_created(delay: int) -> dict:
    return {"text": f"Готово. Через {delay} секунд откроется запись на тестовую экскурсию — и я пришлю уведомление."}


def _window_keyboard(row) -> InlineKeyboardAttachmentRequest:
    rows = [[open_app("Открыть в Aperio", f"w{row['id']}")]]
    if row.get("register_url"):
        rows.append([LinkButton(text="Записаться на сайте", url=row["register_url"])])
    return keyboard(rows)


def reg_opened(row) -> dict:
    link = deeplink(f"w{row['id']}")
    venue = f"{row['venue']}, " if row.get("venue") else ""
    return {
        "text": (
            f"**Открылась запись:** {row['title']}\n{venue}{fmt_dt(row['starts_at'])}\n"
            f"Запись до {fmt_dt(row['reg_closes_at'])} — места обычно разбирают быстро."
            + ("\n\nЭто тестовое уведомление из демо-режима." if row.get("is_demo") else "")
            + (f"\n\n{link}" if link else "")
        ),
        "format": TextFormat.MARKDOWN,
        "attachments": [_window_keyboard(row)],
    }


def reg_closing(row) -> dict:
    return {
        "text": f"**Завтра закрывается запись:** {row['title']}\nУспейте до {fmt_dt(row['reg_closes_at'])}.",
        "format": TextFormat.MARKDOWN,
        "attachments": [_window_keyboard(row)],
    }


def event_tomorrow(row) -> dict:
    place = ", ".join(x for x in [row.get("venue"), row.get("address")] if x)
    docs = f"\nВозьмите: {', '.join(row['documents'])}" if row.get("documents") else ""
    return {
        "text": f"**Завтра:** {row['title']}\n{place}\nНачало: {fmt_dt(row['starts_at'])}{docs}",
        "format": TextFormat.MARKDOWN,
        "attachments": [_window_keyboard(row)],
    }


def benefit_tomorrow(row, day) -> dict:
    docs = f"\nВозьмите: {', '.join(row['documents'])}" if row.get("documents") else ""
    return {
        "text": f"**Завтра бесплатно по вашей льготе:** {row['venue']}\n{row['title']}{docs}",
        "format": TextFormat.MARKDOWN,
        "attachments": [keyboard([[open_app("Подробнее", f"b{row['id']}:{day.isoformat()}")]])],
    }


# Создаёт бота; MAX_API_BASE подменяет адрес API для локальной проверки.
def editor_granted() -> dict:
    return {
        "text": "Вам открыли доступ к админке Aperio: теперь можно вести окна записи и льготные дни.",
        "attachments": [keyboard([[open_app("Открыть админку", "admin")]])],
    }


def make_bot() -> Bot:
    if config.API_BASE:
        import functools

        from maxo.bot import bot as bot_module

        bot_module.MaxApiClient = functools.partial(bot_module.MaxApiClient, base_url=config.API_BASE.rstrip("/") + "/")
    return Bot(config.BOT_TOKEN)


async def send(bot: Bot, user_id: int, message: dict) -> None:
    await bot.call_method(SendMessage(user_id=user_id, **message))


# разовая отправка из веба: там бот не запущен, поднимаем клиент на одно сообщение
async def notify(user_id: int, message: dict) -> None:
    if not config.BOT_TOKEN:
        return
    bot = make_bot()
    try:
        async with bot.context(auto_close=True):
            await send(bot, user_id, message)
    except Exception as exc:
        print(f"[web] уведомление для {user_id} не ушло: {exc}")
