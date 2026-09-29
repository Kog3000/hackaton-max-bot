import asyncio
import hashlib
import hmac
import json
import os
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, unquote_plus

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import config, db, domain, messages, services
from app.seed import seed_if_empty

BASE_DIR = Path(__file__).parent
COOKIE = "aperio_session"
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


DB_ERROR: str | None = None  # последняя причина, по которой база не поднялась


async def _open_database() -> bool:
    global DB_ERROR
    try:
        await db.connect()
        await db.migrate()
        if config.SEED_ON_START:
            await seed_if_empty()
    except Exception as exc:
        DB_ERROR = str(exc)
        print(f"[web] база недоступна: {exc}", flush=True)
        return False
    DB_ERROR = None
    print("[web] база готова", flush=True)
    return True


# Если база не поднялась, процесс больше не падает: он встаёт, отвечает на
# /health и показывает причину на /__diag, а подключение продолжает пробовать
# в фоне. Иначе контейнер уходит в бесконечный перезапуск, и понять, что
# именно не так, можно только по логам, до которых на хостинге ещё надо добраться.
async def _keep_trying() -> None:
    while DB_ERROR:
        await asyncio.sleep(15)
        if await _open_database():
            return


@asynccontextmanager
async def lifespan(_: FastAPI):
    print(f"[web] сборка {config.BUILD}, порт {config.PORT}", flush=True)
    if not await _open_database():
        asyncio.create_task(_keep_trying())
    yield
    await db.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---------- формат ----------

def fmt_dt(value: datetime) -> str:
    local = value.astimezone(domain.MSK)
    return f"{local.day} {MONTHS[local.month - 1][:4]}., {local:%H:%M}"


def fmt_day(value) -> str:
    day = value.date() if isinstance(value, datetime) else value
    return f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}"


def rel_day(value: date) -> str:
    delta = (value - domain.today_msk()).days
    return "Сегодня" if delta == 0 else "Завтра" if delta == 1 else fmt_day(value).capitalize()


templates.env.filters.update(
    dt=fmt_dt,
    day=fmt_day,
    rel=rel_day,
    checked=lambda d: f"{d.day} {MONTHS[d.month - 1]} {d.year}",
    mskinput=lambda v: v.astimezone(domain.MSK).strftime("%Y-%m-%dT%H:%M") if v else "",
    label=domain.window_label,
    topic=domain.topic_label,
)
if config.ALLOW_DEV_AUTH and config.ADMIN_IDS:
    print("[web] ВНИМАНИЕ: ALLOW_DEV_AUTH=true — вход возможен по любому номеру без подписи MAX, "
          "включая номера администраторов. На боевом запуске поставьте false.")

templates.env.globals.update(
    CATEGORIES=domain.CATEGORIES,
    TOPICS=domain.TOPICS,
    FORMATS=domain.FORMATS,
    DISTRICTS=domain.DISTRICTS,
    WEEKDAYS=domain.WEEKDAYS,
    NTH_OPTIONS=domain.NTH_OPTIONS,
    category_label=domain.category_label,
    format_label=domain.format_label,
    describe_filter=domain.describe_filter,
    rule_text=domain.rule_text,
)


# ---------- сессия ----------

# Подпись window.WebApp.initData по алгоритму MAX: HMAC от отсортированных полей.
# Возвращает (данные пользователя, причина отказа) — причина видна в логах и на экране входа.
def check_init_data(init_data: str) -> tuple[tuple[int, str | None, str] | None, str]:
    if not init_data:
        return None, "мини-приложение не передало данные запуска (initData пуст)"
    if not config.BOT_TOKEN:
        return None, "в .env не задан MAX_BOT_TOKEN"

    data = {}
    for part in init_data.split("&"):
        key, _, raw = part.partition("=")
        data[key] = unquote_plus(raw)
    received = data.pop("hash", "")
    if not received:
        return None, "в данных запуска нет подписи hash"

    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), received.lower()):
        return None, "подпись не совпала — вероятно, MAX_BOT_TOKEN в .env от другого бота"

    auth_date = int(data.get("auth_date", 0) or 0)
    auth_date = auth_date // 1000 if auth_date > 10**12 else auth_date
    if time.time() - auth_date > config.INIT_DATA_TTL:
        return None, "данные запуска просрочены — закройте и откройте приложение заново"

    user = json.loads(data.get("user", "{}"))
    if not user.get("id"):
        return None, "в данных запуска нет пользователя"
    return (int(user["id"]), user.get("first_name"), data.get("start_param", "")), ""


def _sign(payload: bytes) -> str:
    key = (config.BOT_TOKEN or "dev-secret").encode()
    return urlsafe_b64encode(hmac.new(key, payload, hashlib.sha256).digest()).decode().rstrip("=")


def make_session(user_id: int, first_name: str | None) -> str:
    body = json.dumps({"id": user_id, "n": first_name or "", "t": int(time.time())}).encode()
    return f"{urlsafe_b64encode(body).decode().rstrip('=')}.{_sign(body)}"


def read_session(cookie: str | None) -> dict | None:
    if not cookie or "." not in cookie:
        return None
    raw, _, signature = cookie.partition(".")
    try:
        body = urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        data = json.loads(body)
    except Exception:
        return None
    user_id = int(data["id"])
    ttl = config.ADMIN_SESSION_TTL if user_id in config.ADMIN_IDS else config.SESSION_TTL
    if not hmac.compare_digest(_sign(body), signature) or time.time() - data["t"] > ttl:
        return None
    return {"id": user_id, "first_name": data["n"] or None}


# сессия приходит либо cookie, либо параметром ?s= — во встроенном окне MAX cookie может быть заблокирована
def session_token(request: Request) -> str | None:
    return request.query_params.get("s") or request.cookies.get(COOKIE)


async def current_user(request: Request) -> dict:
    session = read_session(session_token(request))
    if not session:
        raise HTTPException(status_code=307, headers={"Location": f"/?next={quote(request.url.path)}"})
    user = await services.get_user(session["id"])
    return user or await services.upsert_user(session["id"], session["first_name"])


async def admin(user: dict = Depends(current_user)) -> dict:
    if not services.is_staff(user):
        raise HTTPException(status_code=307, headers={"Location": "/home"})
    return user


# выдавать и снимать права может только главный администратор
async def chief(user: dict = Depends(current_user)) -> dict:
    if not services.is_chief(user):
        raise HTTPException(status_code=307, headers={"Location": "/admin"})
    return user


User = Depends(current_user)
Admin = Depends(admin)
Chief = Depends(chief)


@app.exception_handler(HTTPException)
async def redirect_handler(request: Request, exc: HTTPException):
    if exc.status_code == 307 and "Location" in (exc.headers or {}):
        return RedirectResponse(exc.headers["Location"], status_code=303)
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


# ---------- ответы ----------

def page(request: Request, name: str, user: dict, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(
        request, name,
        {"user": user, "is_admin": services.is_staff(user), "is_chief": services.is_chief(user),
         "toast": request.query_params.get("msg"), **ctx},
    )


def back(url: str, msg: str | None = None) -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + msg.replace(" ", "+")
    return RedirectResponse(url, status_code=303)


def query_filter(request: Request) -> dict:
    query = request.query_params
    return domain.clean_filter(query.getlist("topic"), query.getlist("district"), query.getlist("kind"), query.getlist("format"))


def form_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def msk_input(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=domain.MSK)


# ---------- вход ----------

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    next_url = request.query_params.get("next") or "/home"
    session = read_session(session_token(request))
    ready = f"{next_url}?s={quote(session_token(request))}" if session else ""
    return templates.TemplateResponse(request, "auth.html", {"dev": config.ALLOW_DEV_AUTH, "next_url": next_url, "ready": ready})


@app.post("/auth")
async def auth(request: Request, init_data: str = Form(""), dev_id: str = Form(""), start_param: str = Form(""), next_url: str = Form("")):
    checked, reason = check_init_data(init_data)
    if checked:
        user_id, first_name, start_param = checked[0], checked[1], start_param or checked[2]
    elif config.ALLOW_DEV_AUTH and dev_id.isdigit():
        user_id, first_name = int(dev_id), "Тестовый пользователь"
    else:
        print(f"[auth] вход отклонён: {reason}; длина initData = {len(init_data)}")
        return JSONResponse({"error": reason}, status_code=401)

    user = await services.upsert_user(user_id, first_name)
    if start_param[:1] in {"w", "b"}:
        target = f"/item/{start_param}"
    elif start_param == "admin" and services.is_staff(user):
        target = "/admin"
    elif not user["onboarded"]:
        target = "/onboarding"
    else:
        target = next_url if next_url.startswith("/") else "/home"

    token = make_session(user_id, first_name)
    response = JSONResponse({"redirect": f"{target}?s={quote(token)}", "token": token})
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        COOKIE, token, max_age=config.SESSION_TTL, httponly=True,
        samesite="none" if secure else "lax", secure=secure,
    )
    return response


# Пока база не поднялась, вместо ошибки 500 на каждой странице — понятный текст
# и ссылка на диагностику. /health и /__diag должны работать всегда.
@app.middleware("http")
async def database_gate(request: Request, call_next):
    path = request.url.path
    if DB_ERROR and not path.startswith(("/health", "/__diag", "/static")):
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Aperio</title>"
            "<div style=\"font:16px/1.6 system-ui,sans-serif;max-width:640px;margin:15vh auto;padding:0 24px\">"
            "<h1 style='font-size:24px;margin:0 0 12px'>Приложение запущено, но база недоступна</h1>"
            f"<p style='color:#697592'>{DB_ERROR}</p>"
            "<p>Подключение повторяется каждые 15 секунд — страница заработает сама, как только база ответит.</p>"
            "<p><a href='/__diag'>Что именно не так</a></p></div>",
            status_code=503,
        )
    return await call_next(request)


# сессия из ?s= переносится на следующий адрес после формы
@app.middleware("http")
async def keep_session(request: Request, call_next):
    response = await call_next(request)
    token = request.query_params.get("s")
    location = response.headers.get("location")
    if token and location and location.startswith("/") and "s=" not in location:
        response.headers["location"] = location + ("&" if "?" in location else "?") + "s=" + quote(token)
    return response


# Отвечает 200, пока процесс жив, даже если база ещё не поднялась: иначе
# платформа считает контейнер больным и валит весь деплой, не дав посмотреть
# на /__diag и понять причину. Состояние базы — в теле ответа.
@app.get("/health")
async def health():
    if DB_ERROR:
        return JSONResponse({"ok": False, "db": DB_ERROR})
    try:
        await db.value("SELECT 1")
    except Exception as exc:
        return JSONResponse({"ok": False, "db": str(exc)})
    return {"ok": True}


# Страница для разбора полётов на хостинге. Секретов не печатает: только имена
# переменных окружения и адрес базы без пароля.
@app.get("/__diag", response_class=HTMLResponse)
async def diag():
    known = ["DATABASE_URL", "DATABASE_CA_CERT", "MAX_BOT_TOKEN", "MAX_BOT_USERNAME",
             "ADMIN_USER_IDS", "ALLOW_DEV_AUTH", "PORT", "APP_PORT",
             "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "SEED_ON_START"]
    lines = [
        f"сборка           {config.BUILD}",
        f"порт             {config.PORT}",
        f"база             {db.target()}",
        f"состояние базы   {'ОШИБКА: ' + DB_ERROR if DB_ERROR else 'подключена'}",
        f"вход без MAX     {'ВКЛЮЧЁН — на публичном адресе это дыра' if config.ALLOW_DEV_AUTH else 'выключен'}",
        f"токен бота       {'задан' if config.BOT_TOKEN else 'НЕ задан — бот и уведомления не работают'}",
        f"ник бота         {config.BOT_USERNAME or 'НЕ задан'}",
        f"администраторы   {', '.join(str(x) for x in sorted(config.ADMIN_IDS)) or 'нет'}",
        "",
        "переменные окружения (значения не показываем):",
    ]
    for name in known:
        value = os.environ.get(name)
        mark = "не задана" if value is None else ("задана, но пустая" if not value.strip() else "задана")
        lines.append(f"  {name:20} {mark}")
    body = "\n".join(lines)
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>Aperio · диагностика</title>"
                        f"<pre style='font:14px/1.6 ui-monospace,monospace;padding:24px'>{body}</pre>")


# ---------- экраны ----------

@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding(request: Request, user: dict = User):
    back_url = request.query_params.get("from") or "/home"
    return page(request, "onboarding.html", user, mode=request.query_params.get("mode", "first"),
                back_url=back_url if back_url.startswith("/") else "/home")


@app.post("/category")
async def set_category(category: str = Form(""), next_url: str = Form("/home"), skip: str = Form(""), user: dict = User):
    target = next_url if next_url.startswith("/") else "/home"
    # «Пропустить» раньше была ссылкой на /home, а /home отправлял обратно,
    # пока onboarded = false. Отмечаем онбординг пройденным без категории.
    if skip == "1":
        await services.set_category(user["id"], None)
        return back(target)
    await services.set_category(user["id"], category or None)
    return back(target, "Категория сохранена")


@app.get("/home", response_class=HTMLResponse)
async def home(request: Request, user: dict = User):
    if not user["onboarded"]:
        return back("/onboarding")
    query = (request.query_params.get("q") or "").strip().lower()
    topic = request.query_params.get("topic") or "all"
    feed = await services.build_feed(user)

    def fits(item: dict) -> bool:
        text = " ".join(str(item.get(k) or "") for k in ("title", "venue", "district")).lower()
        return (topic in {"all", item["topic"]}) and query in text

    benefits = [i for i in feed["benefits"] if fits(i)]
    registrations = [i for i in feed["registrations"] if fits(i)]
    return page(
        request, "home.html", user,
        benefits=benefits[:3], events=registrations,
        query=query, topic=topic, empty=not benefits and not registrations,
    )


@app.get("/catalog", response_class=HTMLResponse)
async def catalog(request: Request, user: dict = User):
    flt = query_filter(request)
    query = (request.query_params.get("q") or "").strip().lower()
    feed = await services.build_feed(user, flt)
    items = feed["benefits"] + feed["registrations"]
    if query:
        items = [i for i in items
                 if query in " ".join(str(i.get(k) or "") for k in ("title", "venue", "district")).lower()]
    items.sort(key=lambda i: i["date"] if i["kind"] == "benefit" else i["starts_at"].date())
    return page(request, "catalog.html", user, items=items, filter=flt, query=query,
                query_string=str(request.url.query))


@app.get("/filters", response_class=HTMLResponse)
async def filters(request: Request, user: dict = User):
    return page(request, "filters.html", user, filter=query_filter(request))


@app.post("/subscribe")
async def subscribe(topics: str = Form(""), districts: str = Form(""), kinds: str = Form(""), formats: str = Form(""),
                    user: dict = User):
    flt = domain.clean_filter(topics.split(","), districts.split(","), kinds.split(","), formats.split(","))
    try:
        await services.add_subscription(user["id"], flt)
        return back("/profile/alerts", "Подписка сохранена")
    except ValueError as exc:
        return back("/profile/alerts", str(exc))


@app.post("/unsubscribe/{sub_id}")
async def unsubscribe(sub_id: int, user: dict = User):
    await services.delete_subscription(user["id"], sub_id)
    return back("/profile/alerts", "Подписка удалена")


@app.get("/favorites", response_class=HTMLResponse)
async def favorites(request: Request, user: dict = User):
    windows = await services.list_reminders(user["id"])
    benefits = await services.list_benefit_saves(user["id"])

    def when(item: dict):
        return item["date"] if item["kind"] == "benefit" else item["starts_at"].date()

    return page(request, "favorites.html", user, items=sorted(windows + benefits, key=when))


@app.post("/favorite/benefit/{rule_id}")
async def favorite_benefit(rule_id: int, day: str = Form(""), on: str = Form("1"), next_url: str = Form("/home"), user: dict = User):
    target = next_url if next_url.startswith("/") and not next_url.startswith("//") else "/home"
    try:
        chosen = date.fromisoformat(day)
    except ValueError:
        return back(target)
    if not await db.row("SELECT id FROM benefit_rules WHERE id = $1", rule_id):
        return back(target)
    await services.set_benefit_save(user["id"], rule_id, chosen, on == "1")
    return back(target, "Напомню об этом дне" if on == "1" else "Убрал из избранного")


@app.get("/item/{key}", response_class=HTMLResponse)
async def item(request: Request, key: str, user: dict = User):
    if key.startswith("w") and key[1:].isdigit():
        data, template = await services.get_window(int(key[1:]), user), "item_window.html"
    elif key.startswith("b") and key[1:].partition(":")[0].isdigit():
        rule_id, _, day = key[1:].partition(":")
        data, template = await services.get_benefit(int(rule_id), day or None, user), "item_benefit.html"
    else:
        data, template = None, "not_found.html"
    if not data:
        return page(request, "not_found.html", user)
    return page(request, template, user, item=data, bot_username=config.BOT_USERNAME)


@app.post("/reminder/{window_id}")
async def reminder(window_id: int, on: str = Form("1"), next_url: str = Form("/home"), user: dict = User):
    await services.set_reminder(user["id"], window_id, on == "1")
    return back(next_url, "Напомню об этом событии" if on == "1" else "Убрал из избранного")


# запись на событие: отмечаем его у человека и уводим на страницу записи
@app.post("/register/{window_id}")
async def register(window_id: int, next_url: str = Form("/home"), user: dict = User):
    item = await services.get_window(window_id, user)
    if not item:
        return back(next_url)
    await services.set_reminder(user["id"], window_id, True)
    if item.get("register_url"):
        return RedirectResponse(item["register_url"], status_code=303)
    return back(next_url, "Событие добавлено в ваш список")


@app.get("/profile", response_class=HTMLResponse)
async def profile(request: Request, user: dict = User):
    return page(request, "profile.html", user,
                subscriptions=await services.list_subscriptions(user["id"]),
                reminders=await services.list_reminders(user["id"]),
                visited=await services.list_reminders(user["id"], past=True))


@app.get("/profile/help", response_class=HTMLResponse)
async def profile_help(request: Request, user: dict = User):
    return page(request, "help.html", user)


@app.get("/profile/visited", response_class=HTMLResponse)
async def profile_visited(request: Request, user: dict = User):
    return page(request, "visited.html", user, items=await services.list_reminders(user["id"], past=True))


@app.post("/alerts/mute/{window_id}")
async def mute(window_id: int, muted: str = Form("1"), user: dict = User):
    await services.mute_reminder(user["id"], window_id, muted == "1")
    return back("/profile/alerts")


@app.get("/profile/alerts", response_class=HTMLResponse)
async def alerts(request: Request, user: dict = User):
    return page(request, "alerts.html", user, items=await services.list_reminders(user["id"]))


@app.get("/profile/about", response_class=HTMLResponse)
async def about(request: Request, user: dict = User):
    return page(request, "about.html", user)


@app.post("/demo")
async def demo(user: dict = User):
    await services.create_demo_window(user["id"])
    return back("/profile/alerts", f"Уведомление придёт через {config.DEMO_DELAY} секунд")


# ---------- админка ----------

@app.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request, user: dict = Admin):
    section = request.query_params.get("section", "windows")
    scope = request.query_params.get("scope", "active")
    items = await services.admin_windows(scope) if section == "windows" else await services.admin_rules()
    return page(request, "admin.html", user, stats=await services.admin_stats(), section=section, scope=scope, items=items)


@app.get("/admin/log", response_class=HTMLResponse)
async def admin_log(request: Request, user: dict = Admin):
    return page(request, "admin_log.html", user, entries=await services.audit_log())


@app.get("/admin/people", response_class=HTMLResponse)
async def admin_people(request: Request, user: dict = Chief):
    return page(request, "admin_people.html", user, editors=await services.list_editors())


@app.post("/admin/people")
async def admin_people_add(editor_id: str = Form(""), user: dict = Chief):
    if not editor_id.strip().isdigit():
        return back("/admin/people", "Номер состоит только из цифр")
    editor = int(editor_id.strip())
    await services.set_role(editor, True)
    await services.log_action(user, "granted", "editor", editor, None)
    await messages.notify(editor, messages.editor_granted())
    return back("/admin/people", "Доступ выдан")


@app.post("/admin/people/{editor_id}/delete")
async def admin_people_remove(editor_id: int, user: dict = Chief):
    await services.set_role(editor_id, False)
    await services.log_action(user, "revoked", "editor", editor_id, None)
    return back("/admin/people", "Доступ снят")


@app.post("/admin/probe/window")
async def admin_probe_window(user: dict = Admin):
    await services.create_probe_window()
    await services.log_action(user, "created", "probe", None, "Проверка: окно записи")
    return back("/admin", f"Окно создано, запись откроется через {config.DEMO_DELAY} с")


@app.post("/admin/probe/rule")
async def admin_probe_rule(user: dict = Admin):
    await services.create_probe_rule()
    await services.log_action(user, "created", "probe", None, "Проверка: льготный день")
    return back("/admin?section=rules", "Правило создано, льготный день — завтра")


@app.get("/admin/window", response_class=HTMLResponse)
@app.get("/admin/window/{window_id}", response_class=HTMLResponse)
async def admin_window_form(request: Request, window_id: int | None = None, user: dict = Admin):
    current = await services.admin_window(window_id) if window_id else None
    reach, total = await services.reach_counts(current)
    return page(request, "admin_window.html", user, item=current, venues=await services.list_venues(),
                reach=reach, total=total)


@app.post("/admin/window")
async def admin_window_save(
    window_id: str = Form(""), title: str = Form(...), topic: str = Form(...), district: str = Form(...),
    venue_id: str = Form(""), venue_name: str = Form(""), venue_address: str = Form(""),
    format: str = Form("offline"), reg_opens_at: str = Form(...), reg_closes_at: str = Form(...), starts_at: str = Form(...),
    eligibility: list[str] = Form([]), conditions: str = Form(""), documents: str = Form(""),
    register_url: str = Form(""), image_url: str = Form(""), source_name: str = Form(...),
    source_url: str = Form(""), is_model_data: str = Form(""), user: dict = Admin,
):
    data = {
        "title": title.strip(), "topic": topic, "district": district,
        "venue_id": venue_id, "venue_name": venue_name, "venue_address": venue_address,
        "format": format if format in domain.FORMAT_IDS else "offline",
        "reg_opens_at": msk_input(reg_opens_at), "reg_closes_at": msk_input(reg_closes_at), "starts_at": msk_input(starts_at),
        "eligibility": eligibility or ["all"], "conditions": conditions.strip() or None, "documents": form_lines(documents),
        "register_url": register_url.strip() or None, "image_url": image_url.strip() or None,
        "source_name": source_name.strip(), "source_url": source_url.strip() or None, "is_model_data": bool(is_model_data),
    }
    if data["reg_opens_at"] >= data["reg_closes_at"]:
        return back(f"/admin/window/{window_id}" if window_id else "/admin/window", "Запись должна закрываться позже, чем открывается")
    saved_id = await services.save_window(data, int(window_id) if window_id.isdigit() else None)
    await services.log_action(user, "updated" if window_id.isdigit() else "created", "window", saved_id, data["title"])
    return back("/admin", "Окно сохранено")


@app.post("/admin/window/{window_id}/delete")
async def admin_window_delete(window_id: int, user: dict = Admin):
    removed = await services.admin_window(window_id)
    await services.delete_window(window_id)
    await services.log_action(user, "deleted", "window", window_id, removed["title"] if removed else None)
    return back("/admin", "Окно удалено")


@app.get("/admin/rule", response_class=HTMLResponse)
@app.get("/admin/rule/{rule_id}", response_class=HTMLResponse)
async def admin_rule_form(request: Request, rule_id: int | None = None, user: dict = Admin):
    current = await services.admin_rule(rule_id) if rule_id else None
    if current:
        current["next_date"] = next(iter(domain.occurrences(current["weekday"], current["nth"], domain.today_msk(), 62)), None)
    return page(request, "admin_rule.html", user, item=current, venues=await services.list_venues())


@app.post("/admin/rule")
async def admin_rule_save(
    rule_id: str = Form(""), venue_id: str = Form(""), venue_name: str = Form(""), district: str = Form("ЦАО"),
    title: str = Form(...), categories: list[str] = Form([]), weekday: int = Form(...), nth: int = Form(...),
    conditions: str = Form(""), documents: str = Form(""), source_name: str = Form(...), source_url: str = Form(""),
    checked_at: str = Form(""), is_model_data: str = Form(""), user: dict = Admin,
):
    if not categories:
        return back(f"/admin/rule/{rule_id}" if rule_id else "/admin/rule", "Выберите хотя бы одну категорию")
    data = {
        "venue_id": venue_id, "venue_name": venue_name, "district": district, "title": title.strip(),
        "categories": categories, "weekday": weekday, "nth": nth, "conditions": conditions.strip() or None,
        "documents": form_lines(documents), "source_name": source_name.strip(), "source_url": source_url.strip() or None,
        "checked_at": date.fromisoformat(checked_at) if checked_at else None, "is_model_data": bool(is_model_data),
    }
    saved_id = await services.save_rule(data, int(rule_id) if rule_id.isdigit() else None)
    await services.log_action(user, "updated" if rule_id.isdigit() else "created", "rule", saved_id, data["title"])
    return back("/admin?section=rules", "Правило сохранено")


@app.post("/admin/rule/{rule_id}/delete")
async def admin_rule_delete(rule_id: int, user: dict = Admin):
    removed = await services.admin_rule(rule_id)
    await services.delete_rule(rule_id)
    await services.log_action(user, "deleted", "rule", rule_id, removed["title"] if removed else None)
    return back("/admin?section=rules", "Правило удалено")
