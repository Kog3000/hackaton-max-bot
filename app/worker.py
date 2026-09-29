import asyncio
import time
from datetime import timedelta

from aiohttp import ClientError
from maxo import Bot

from app import config, db, messages, services
from app.domain import MSK, now_utc, occurrences, today_msk, window_status


async def deliver(bot: Bot, user_id: int, key: str, message: dict) -> bool:
    claimed = await db.run("INSERT INTO notifications (user_id, key) VALUES ($1,$2) ON CONFLICT DO NOTHING", user_id, key)
    if not claimed.endswith("1"):
        return False
    try:
        await messages.send(bot, user_id, message)
        await asyncio.sleep(0.06)  # лимит MAX: не больше двух сообщений в секунду в один диалог
        return True
    except (ClientError, asyncio.TimeoutError):
        # связи не было — снимаем бронь и повторим на следующем проходе
        await db.run("DELETE FROM notifications WHERE user_id = $1 AND key = $2", user_id, key)
        return False
    except Exception as exc:
        # сообщение могло уйти: бронь оставляем, чтобы не прислать дубль
        if "403" in str(exc) or "404" in str(exc):
            await services.block_user(user_id)
        print(f"[worker] {key} → {user_id}: {exc}")
        return False


async def job_reg_opened(bot: Bot) -> int:
    moment = now_utc()
    rows = await db.rows(
        services.WINDOWS + "WHERE w.reg_opens_at BETWEEN $1 AND $2 AND w.reg_closes_at > $2",
        moment - timedelta(hours=24), moment,
    )
    sent = 0
    for row in rows:
        item = services.window_view(row, moment)
        users = await services.recipients("registration", row["topic"], row["district"], list(row["eligibility"]),
                                          row["id"], row["format"])
        for user_id in users:
            if row["demo_for"] and row["demo_for"] != user_id:
                continue
            sent += await deliver(bot, user_id, f"reg_open:w{row['id']}", messages.reg_opened(item))
    return sent


# напоминания о закрытии записи и о самом событии уходят только тем, кто добавил его в избранное
async def job_reminders(bot: Bot) -> int:
    moment = now_utc()
    rows = await db.rows(
        services.WINDOWS + """JOIN reminders rm ON rm.window_id = w.id AND NOT rm.muted
                              JOIN users u ON u.id = rm.user_id AND NOT u.blocked
                              WHERE w.starts_at > $1 AND (w.reg_closes_at <= $2 OR w.starts_at <= $2)""",
        moment, moment + timedelta(hours=24),
    )
    sent = 0
    for row in rows:
        item = services.window_view(row, moment)
        status = window_status(row, moment)
        if status == "open" and row["reg_closes_at"] - row["reg_opens_at"] > timedelta(hours=36):
            sent += await deliver(bot, row["user_id"], f"reg_closing:w{row['id']}", messages.reg_closing(item))
        if status != "soon" and not row["demo_for"] and row["starts_at"] <= moment + timedelta(hours=24):
            sent += await deliver(bot, row["user_id"], f"event_tomorrow:w{row['id']}", messages.event_tomorrow(item))
    return sent


async def job_benefit_tomorrow(bot: Bot) -> int:
    moment = now_utc()
    if not 10 <= moment.astimezone(MSK).hour < 21:  # не пишем ночью
        return 0
    tomorrow = today_msk(moment) + timedelta(days=1)
    sent = 0
    for row in await db.rows(services.RULES):
        if not occurrences(row["weekday"], row["nth"], tomorrow, 1):
            continue
        item = services.benefit_view(row, tomorrow)
        users = await services.recipients("benefit", "museum", row["district"], list(row["categories"]))
        for user_id in users:
            sent += await deliver(bot, user_id, f"benefit:b{row['id']}:{tomorrow}", messages.benefit_tomorrow(item, tomorrow))
    return sent


async def run_once(bot: Bot) -> int:
    total = 0
    for job in (job_reg_opened, job_reminders, job_benefit_tomorrow):
        try:
            total += await job(bot)
        except Exception as exc:
            print(f"[worker] {job.__name__}: {exc}")
    return total


async def _loop() -> None:
    await db.connect()
    bot = messages.make_bot()
    print(f"[worker] сборка {config.BUILD}, интервал {config.WORKER_INTERVAL} с")
    async with bot.context(auto_close=True):
        while True:
            sent = await run_once(bot)
            if sent:
                print(f"[worker] отправлено сообщений: {sent}")
            await asyncio.sleep(config.WORKER_INTERVAL)


def main() -> None:
    # Без токена работать нечем. Но падать нельзя: контейнер с restart уйдёт
    # в плотный цикл перезапуска и зальёт логи. Ждём и повторяем сообщение
    # раз в минуту — видно, что не так, и ничего не крутится впустую.
    while not config.BOT_TOKEN:
        print("[worker] MAX_BOT_TOKEN не задан — задайте переменную в настройках и перезапустите",
              flush=True)
        time.sleep(60)
    asyncio.run(_loop())


if __name__ == "__main__":
    main()
