# Чат-бот Aperio на maxo: команды, кнопка демо, long polling.
import asyncio
import time

from maxo import Bot, Dispatcher
from maxo.methods import AnswerOnCallback, EditMyCommands
from maxo.transport.long_polling import LongPolling
from maxo.types.bot_command import BotCommand
from maxo.types.bot_started import BotStarted
from maxo.types.message_callback import MessageCallback
from maxo.types.message_created import MessageCreated

from app import config, db, messages, services

dp = Dispatcher()


async def _start(bot: Bot, user_id: int, first_name: str | None) -> None:
    await services.upsert_user(user_id, first_name)
    await messages.send(bot, user_id, messages.welcome(first_name))


async def _demo(bot: Bot, user_id: int, first_name: str | None) -> None:
    await services.upsert_user(user_id, first_name)
    await services.create_demo_window(user_id)
    await messages.send(bot, user_id, messages.demo_created(config.DEMO_DELAY))


# вопрос уходит всем главным администраторам, ответят они в этом же чате
async def _forward_question(bot: Bot, user_id: int, first_name: str | None, text: str) -> None:
    await services.set_asking(user_id, False)
    for admin_id in config.ADMIN_IDS:
        try:
            await messages.send(bot, admin_id, messages.question_for_admin(first_name, user_id, text))
        except Exception as exc:
            print(f"[bot] вопрос не дошёл до {admin_id}: {exc}")
    await messages.send(bot, user_id, messages.question_sent())


# ответ администратора уходит тому, кто задал вопрос
async def _deliver_reply(bot: Bot, admin_id: int, target_id: int, text: str) -> None:
    # без этой проверки любой мог бы разослать сообщение кому угодно от имени бота
    admin = await services.get_user(admin_id)
    if not admin or not services.is_chief(admin):
        await services.set_replying(admin_id, None)
        await messages.send(bot, admin_id, {"text": "Команда доступна только администраторам."})
        return
    await services.set_replying(admin_id, None)
    target = await services.get_user(target_id)
    try:
        await messages.send(bot, target_id, messages.reply_for_user(text))
    except Exception as exc:
        print(f"[bot] ответ не дошёл до {target_id}: {exc}")
        await messages.send(bot, admin_id, {"text": "Не получилось отправить ответ — возможно, человек заблокировал бота."})
        return
    await messages.send(bot, admin_id, messages.reply_sent(target["first_name"] if target else None))


@dp.bot_started()
async def on_bot_started(event: BotStarted, bot: Bot) -> None:
    await _start(bot, event.user.user_id, event.user.first_name)


# Текстовые команды в личном диалоге.
@dp.message_created()
async def on_message(event: MessageCreated, bot: Bot) -> None:
    sender = event.message.sender
    if getattr(sender, "is_bot", False):
        return
    chat_type = getattr(event.message.recipient, "chat_type", None)
    if chat_type and str(chat_type) not in {"dialog", "ChatType.DIALOG"}:
        return
    raw = (getattr(event.message.body, "text", "") or "").strip()
    text = raw.lower()
    user_id, first_name = sender.user_id, sender.first_name
    if not text.startswith("/"):
        user = await services.upsert_user(user_id, first_name)
        if user.get("replying_to"):
            await _deliver_reply(bot, user_id, int(user["replying_to"]), raw)
            return
        if user.get("asking"):
            await _forward_question(bot, user_id, first_name, raw)
            return
    if text.startswith("/reply"):
        parts = raw.split(maxsplit=2)
        if len(parts) < 3 or not parts[1].isdigit():
            await messages.send(bot, user_id, {"text": "Формат: /reply ID текст ответа"})
        else:
            await _deliver_reply(bot, user_id, int(parts[1]), parts[2])
    elif text.startswith("/start"):
        await _start(bot, user_id, first_name)
    elif text.startswith("/demo"):
        await _demo(bot, user_id, first_name)
    elif text.startswith("/admin"):
        await services.upsert_user(user_id, first_name)
        await services.set_asking(user_id, True)
        await messages.send(bot, user_id, messages.ask_admin())
    else:
        await services.upsert_user(user_id, first_name)
        await messages.send(bot, user_id, messages.help_message())


# Нажатие кнопки «Прислать тестовое уведомление».
@dp.message_callback()
async def on_callback(event: MessageCallback, bot: Bot) -> None:
    callback = event.callback
    print(f"[bot] нажата кнопка: {callback.payload!r} от {callback.user.user_id}")
    if callback.payload == "demo":
        await _demo(bot, callback.user.user_id, callback.user.first_name)
        await bot.call_method(AnswerOnCallback(callback_id=callback.callback_id, notification="Уведомление придёт через минуту"))
    elif (callback.payload or "").startswith("reply:"):
        admin_id = callback.user.user_id
        try:
            target_id = int(callback.payload.split(":", 1)[1])
            admin = await services.upsert_user(admin_id, callback.user.first_name)
            if not services.is_chief(admin):
                await bot.call_method(AnswerOnCallback(callback_id=callback.callback_id, notification="Доступно только администраторам"))
                return
            await services.set_replying(admin_id, target_id)
            target = await services.get_user(target_id)
            await messages.send(bot, admin_id, messages.reply_prompt(target["first_name"] if target else None, target_id))
            await bot.call_method(AnswerOnCallback(callback_id=callback.callback_id, notification="Напишите ответ сообщением"))
        except Exception as exc:
            print(f"[bot] кнопка «Ответить» не сработала: {exc}")
            await messages.send(bot, admin_id, {"text": "Не получилось открыть ответ. Ответьте командой: /reply ID текст"})


async def _run() -> None:
    await db.connect()
    bot = messages.make_bot()
    async with bot.context(auto_close=True):
        try:
            await bot.call_method(
                EditMyCommands(
                    commands=[
                        BotCommand(name="start", description="Открыть приложение"),
                        BotCommand(name="demo", description="Тестовое уведомление через минуту"),
                        BotCommand(name="admin", description="Задать вопрос администратору"),
                        BotCommand(name="reply", description="Ответить пользователю: /reply ID текст"),
                        BotCommand(name="help", description="Подсказка"),
                    ]
                )
            )
        except Exception as exc:  # команды не критичны для работы бота
            print(f"[bot] не удалось обновить команды: {exc}")
        print(f"[bot] сборка {config.BUILD}, ожидаю события")
        await LongPolling(dp).start(bot, types=["bot_started", "message_created", "message_callback"], auto_close_bot=False)
    await db.close()


def main() -> None:
    # Без токена работать нечем. Но падать нельзя: контейнер с restart уйдёт
    # в плотный цикл перезапуска и зальёт логи. Ждём и повторяем сообщение
    # раз в минуту — видно, что не так, и ничего не крутится впустую.
    while not config.BOT_TOKEN:
        print("[bot] MAX_BOT_TOKEN не задан — задайте переменную в настройках и перезапустите",
              flush=True)
        time.sleep(60)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
