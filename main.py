"""
Главный сервис приложения "Baby Sleep Tracker" для Telegram.
Содержит FastAPI backend, раздачу WebApp интерфейса и фоновый процесс Telegram-бота на Aiogram 3.
"""

import os
import asyncio
import logging
from datetime import datetime, date as dt_date, timedelta
from typing import Optional, Dict, Any

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, FSInputFile, MenuButtonWebApp
from aiogram.enums import ParseMode

import database as db
import norms
import analytics
import time_utils
import development

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("baby_tracker")

# Переменные окружения
BOT_TOKEN = os.getenv("BOT_TOKEN", "9093548532:AAF006ZUDHLrctnVewiGzavFD_bSexAIDgU")
WEBAPP_URL = os.getenv("WEBAPP_URL", "") # Если пустой, бот будет слать инструкцию или локальный URL
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
DEFAULT_BIRTH_DATE = os.getenv("BABY_BIRTH_DATE", "2026-09-11")
DEFAULT_BABY_NAME = os.getenv("BABY_NAME", "Сева")

app = FastAPI(title="Baby Sleep Tracker API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Инициализация Aiogram
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

# Кэш для контроля отправки уведомлений (чтобы не спамить каждую минуту)
notified_winddown_cache = set()
notified_overtired_cache = set()

# Pydantic модели
class StartSleepRequest(BaseModel):
    family_id: int
    user_id: int
    start_time: Optional[str] = None
    is_night: Optional[int] = None

class StopSleepRequest(BaseModel):
    family_id: int
    user_id: int
    end_time: Optional[str] = None

class OffsetRequest(BaseModel):
    family_id: int
    user_id: int
    offset_minutes: int

class ManualSleepRequest(BaseModel):
    family_id: int
    user_id: int
    start_time: str
    end_time: str
    is_night: Optional[bool] = False
    notes: Optional[str] = None

class UpdateSleepRequest(BaseModel):
    family_id: int
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    is_night: Optional[bool] = None
    notes: Optional[str] = None

class UpdateBabyRequest(BaseModel):
    family_id: int
    baby_name: Optional[str] = None
    baby_birth_date: Optional[str] = None
    custom_wake_window_min: Optional[int] = None
    notifications_enabled: Optional[int] = None

class JoinFamilyRequest(BaseModel):
    telegram_id: int
    invite_code: str

class WhitelistAddRequest(BaseModel):
    telegram_id: int
    username: Optional[str] = None
    full_name: Optional[str] = None
    notes: Optional[str] = None
    admin_id: int


# =====================================================================
# API ЭНДПОИНТЫ FASTAPI
# =====================================================================

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def serve_index():
    """Отдает главную страницу Telegram Mini App (поддерживает GET и HEAD для UptimeRobot)."""
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return HTMLResponse("<h1>Baby Sleep Tracker</h1>")

@app.api_route("/health", methods=["GET", "HEAD", "POST"])
@app.api_route("/ping", methods=["GET", "HEAD", "POST"])
async def health_check():
    """
    Легковесный эндпоинт проверки работоспособности для UptimeRobot, Render и мониторинга.
    Не нагружает сервер чтением файлов и возвращает 200 OK на любые типы запросов.
    """
    return {"status": "ok", "service": "baby_sleep_tracker"}

@app.get("/api/status")
async def get_status(telegram_id: int, username: Optional[str] = None):
    """Получает текущий статус малыша: спит/бодрствует, таймер, следующий сон и инфо о семье."""
    user_data = await db.get_or_create_user(
        telegram_id=telegram_id,
        username=username if isinstance(username, str) else None,
        default_birth_date=DEFAULT_BIRTH_DATE
    )
    family = user_data["family"]
    family_id = family["id"]

    family_info = await db.get_family_info(family_id)
    active_sleep = await db.get_active_sleep(family_id)
    last_completed = await db.get_last_completed_sleep(family_id)

    # Дата рождения малыша
    try:
        birth_date = dt_date.fromisoformat(family["baby_birth_date"])
    except Exception:
        birth_date = dt_date.fromisoformat(DEFAULT_BIRTH_DATE)

    age_info = norms.calculate_age_info(birth_date)
    expert_advice = norms.get_age_advice(birth_date)
    is_allowed = await db.is_user_allowed(telegram_id)
    is_superadmin = (telegram_id == db.SUPERADMIN_ID)

    next_sleep_info = None
    if not active_sleep:
        # Ребенок сейчас бодрствует
        now_local = time_utils.get_local_now()
        if last_completed:
            wake_dt = time_utils.to_local_datetime(last_completed["end_time"])
        else:
            wake_dt = now_local - timedelta(minutes=30)
            
        next_sleep_info = norms.calculate_next_sleep(
            wake_time=wake_dt,
            birth_date=birth_date,
            custom_wb_min=family.get("custom_wake_window_min"),
            current_time=now_local
        )

    return {
        "is_allowed": is_allowed,
        "is_superadmin": is_superadmin,
        "current_user_id": telegram_id,
        "is_sleeping": bool(active_sleep),
        "active_sleep": active_sleep,
        "last_completed_sleep": last_completed,
        "next_sleep": next_sleep_info,
        "family": family_info,
        "expert_advice": expert_advice,
        "baby": {
            "name": family.get("baby_name", DEFAULT_BABY_NAME),
            "birth_date": family.get("baby_birth_date", DEFAULT_BIRTH_DATE),
            "age_text": age_info["age_text"],
            "age_days": age_info["days"],
            "norm_wb_min": age_info["norm_wb_min"],
            "norm_wb_max": age_info["norm_wb_max"],
            "norm_wb_avg": age_info["norm_wb_avg"],
            "custom_wb_min": family.get("custom_wake_window_min"),
            "notifications_enabled": bool(family.get("notifications_enabled", 1))
        }
    }

@app.post("/api/sleep/start")
async def api_start_sleep(req: StartSleepRequest):
    """Начинает сон."""
    res = await db.start_sleep(
        family_id=req.family_id,
        user_id=req.user_id,
        start_time_iso=req.start_time,
        is_night=req.is_night
    )
    return {"success": True, "sleep": res}

@app.post("/api/sleep/stop")
async def api_stop_sleep(req: StopSleepRequest):
    """Завершает сон."""
    res = await db.stop_sleep(
        family_id=req.family_id,
        user_id=req.user_id,
        end_time_iso=req.end_time
    )
    return {"success": True, "sleep": res}

@app.post("/api/sleep/offset_start")
async def api_offset_start(req: OffsetRequest):
    """Сдвигает время начала текущего активного сна назад на N минут."""
    active = await db.get_active_sleep(req.family_id)
    if not active:
        raise HTTPException(status_code=400, detail="Малыш сейчас не спит")
    cur_start = datetime.fromisoformat(active["start_time"])
    new_start = cur_start - timedelta(minutes=req.offset_minutes)
    updated = await db.update_sleep(
        sleep_id=active["id"],
        family_id=req.family_id,
        start_time_iso=new_start.isoformat()
    )
    return {"success": True, "sleep": updated}

@app.post("/api/sleep/offset_wake")
async def api_offset_wake(req: OffsetRequest):
    """Сдвигает время окончания последнего сна назад на N минут (проснулся раньше)."""
    last = await db.get_last_completed_sleep(req.family_id)
    if not last:
        raise HTTPException(status_code=400, detail="Нет завершенных снов")
    cur_end = datetime.fromisoformat(last["end_time"])
    new_end = cur_end - timedelta(minutes=req.offset_minutes)
    updated = await db.update_sleep(
        sleep_id=last["id"],
        family_id=req.family_id,
        end_time_iso=new_end.isoformat()
    )
    return {"success": True, "sleep": updated}

@app.post("/api/sleep/manual")
async def api_manual_sleep(req: ManualSleepRequest):
    """Добавляет сон вручную."""
    res = await db.add_manual_sleep(
        family_id=req.family_id,
        user_id=req.user_id,
        start_time_iso=req.start_time,
        end_time_iso=req.end_time,
        is_night=1 if req.is_night else 0,
        notes=req.notes
    )
    return {"success": True, "sleep": res}

@app.put("/api/sleep/{sleep_id}")
async def api_update_sleep(sleep_id: int, req: UpdateSleepRequest):
    """Редактирует сон."""
    res = await db.update_sleep(
        sleep_id=sleep_id,
        family_id=req.family_id,
        start_time_iso=req.start_time,
        end_time_iso=req.end_time,
        is_night=1 if req.is_night else 0 if req.is_night is not None else None,
        notes=req.notes
    )
    if not res:
        raise HTTPException(status_code=404, detail="Сон не найден")
    return {"success": True, "sleep": res}

@app.delete("/api/sleep/{sleep_id}")
async def api_delete_sleep(sleep_id: int, family_id: int):
    """Удаляет сон."""
    ok = await db.delete_sleep(sleep_id, family_id)
    return {"success": ok}

@app.get("/api/stats/daily")
async def api_get_daily_stats(family_id: int, date: Optional[str] = Query(None, alias="date")):
    """Возвращает подробную суточную сводку, расчет сна/бодрствования и 24ч таймлайн."""
    now_local = time_utils.get_local_now()
    try:
        target_day = dt_date.fromisoformat(date) if date else now_local.date()
    except Exception:
        target_day = now_local.date()

    family_info = await db.get_family_info(family_id)
    if not family_info:
        raise HTTPException(status_code=404, detail="Семья не найдена")

    try:
        birth_date = dt_date.fromisoformat(family_info["baby_birth_date"])
    except Exception:
        birth_date = dt_date.fromisoformat(DEFAULT_BIRTH_DATE)

    sleeps = await db.get_sleeps_for_day(family_id, target_day)
    summary = analytics.calculate_daily_summary(
        sleeps=sleeps,
        target_date=target_day,
        birth_date=birth_date,
        now=now_local
    )
    return summary

@app.get("/api/advice")
async def api_get_advice(family_id: int):
    """Возвращает экспертные подсказки и советы по текущему возрасту малыша."""
    family_info = await db.get_family_info(family_id)
    if not family_info:
        raise HTTPException(status_code=404, detail="Семья не найдена")
    try:
        birth_date = dt_date.fromisoformat(family_info["baby_birth_date"])
    except Exception:
        birth_date = dt_date.fromisoformat(DEFAULT_BIRTH_DATE)
    advice = norms.get_age_advice(birth_date)
    return advice

@app.get("/api/stats/weekly")
async def api_get_weekly_stats(family_id: int, end_date: Optional[str] = Query(None)):
    """Возвращает недельную статистику (7 дней), динамику и график сна."""
    now_local = time_utils.get_local_now()
    try:
        end_day = dt_date.fromisoformat(end_date) if end_date else now_local.date()
    except Exception:
        end_day = now_local.date()

    family_info = await db.get_family_info(family_id)
    if not family_info:
        raise HTTPException(status_code=404, detail="Семья не найдена")

    try:
        birth_date = dt_date.fromisoformat(family_info["baby_birth_date"])
    except Exception:
        birth_date = dt_date.fromisoformat(DEFAULT_BIRTH_DATE)

    # Собираем данные за каждый из 7 дней
    daily_summaries = []
    for i in range(6, -1, -1):
        target_day = end_day - timedelta(days=i)
        sleeps = await db.get_sleeps_for_day(family_id, target_day)
        summary = analytics.calculate_daily_summary(
            sleeps=sleeps,
            target_date=target_day,
            birth_date=birth_date,
            now=now_local
        )
        daily_summaries.append(summary)

    weekly = analytics.calculate_weekly_summary(
        daily_summaries=daily_summaries,
        end_date=end_day,
        birth_date=birth_date
    )
    return weekly

@app.get("/api/export/csv")
async def api_export_csv(family_id: int, days: int = 14):
    """Экспорт истории сна в CSV для врача/педиатра."""
    from fastapi.responses import Response
    now_local = time_utils.get_local_now()
    end_day = now_local.date()
    start_day = end_day - timedelta(days=days)
    sleeps = await db.get_all_sleeps_for_period(family_id, start_day, end_day)
    csv_content = analytics.generate_csv_data(sleeps)
    
    return Response(
        content=csv_content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="sleep_report_{end_day.isoformat()}.csv"'
        }
    )

@app.get("/api/export/text")
async def api_export_text(family_id: int):
    """Текстовая выжимка для пересылки педиатру в Telegram."""
    now_local = time_utils.get_local_now()
    end_day = now_local.date()
    family_info = await db.get_family_info(family_id)
    if not family_info:
        raise HTTPException(status_code=404, detail="Семья не найдена")

    try:
        birth_date = dt_date.fromisoformat(family_info["baby_birth_date"])
    except Exception:
        birth_date = dt_date.fromisoformat(DEFAULT_BIRTH_DATE)

    age_info = norms.calculate_age_info(birth_date, end_day)

    daily_summaries = []
    for i in range(6, -1, -1):
        target_day = end_day - timedelta(days=i)
        sleeps = await db.get_sleeps_for_day(family_id, target_day)
        summary = analytics.calculate_daily_summary(sleeps, target_day, birth_date, now_local)
        daily_summaries.append(summary)

    weekly = analytics.calculate_weekly_summary(daily_summaries, end_day, birth_date)
    text_report = analytics.generate_pediatric_text_report(
        baby_name=family_info.get("baby_name", "Сева"),
        age_text=age_info["age_text"],
        weekly_summary=weekly
    )
    return {"report_text": text_report}

@app.put("/api/baby")
async def api_update_baby(req: UpdateBabyRequest):
    """Обновляет профиль малыша."""
    res = await db.update_baby_profile(
        family_id=req.family_id,
        baby_name=req.baby_name,
        baby_birth_date=req.baby_birth_date,
        custom_wake_window_min=req.custom_wake_window_min,
        notifications_enabled=req.notifications_enabled
    )
    return {"success": True, "family": res}

@app.post("/api/family/join")
async def api_join_family(req: JoinFamilyRequest):
    """Присоединяет второго родителя к семье по коду."""
    family = await db.join_family(req.telegram_id, req.invite_code)
    if not family:
        return {"success": False, "error": "Код семьи не найден. Проверьте правильность ввода."}
    return {"success": True, "family": family}

@app.get("/api/development")
async def api_get_development(family_id: int, week: Optional[int] = None):
    """Возвращает информацию о развитии, упражнениях по неделям и скачках роста."""
    family_info = await db.get_family_info(family_id)
    if not family_info:
        raise HTTPException(status_code=404, detail="Семья не найдена")
    try:
        birth_date = dt_date.fromisoformat(family_info["baby_birth_date"])
    except Exception:
        birth_date = dt_date.fromisoformat(DEFAULT_BIRTH_DATE)
    
    now_local = time_utils.get_local_now().date()
    week_val = week if isinstance(week, int) else None
    dev_data = development.get_development_info(
        birth_date=birth_date,
        target_date=now_local,
        week_override=week_val
    )
    return dev_data

@app.get("/api/admin/whitelist")
async def api_get_whitelist(admin_id: int = Query(...)):
    """Возвращает список пользователей белого списка (только для владельца бота)."""
    if admin_id != db.SUPERADMIN_ID:
        raise HTTPException(status_code=403, detail="Доступ запрещен. Только для владельца бота.")
    return await db.get_whitelist()

@app.post("/api/admin/whitelist")
async def api_add_whitelist(req: WhitelistAddRequest):
    """Добавляет пользователя в белый список (только для владельца бота)."""
    if req.admin_id != db.SUPERADMIN_ID:
        raise HTTPException(status_code=403, detail="Доступ запрещен. Только для владельца бота.")
    row = await db.add_to_whitelist(
        telegram_id=req.telegram_id,
        username=req.username,
        full_name=req.full_name,
        added_by=req.admin_id,
        notes=req.notes
    )
    return {"success": True, "user": row}

@app.delete("/api/admin/whitelist/{target_id}")
async def api_delete_whitelist(target_id: int, admin_id: int = Query(...)):
    """Удаляет пользователя из белого списка (только для владельца бота)."""
    if admin_id != db.SUPERADMIN_ID:
        raise HTTPException(status_code=403, detail="Доступ запрещен. Только для владельца бота.")
    success = await db.remove_from_whitelist(target_id)
    return {"success": success}


# =====================================================================
# TELEGRAM БОТ (AIOGRAM 3)
# =====================================================================

def get_webapp_keyboard(user_family_code: str = "") -> InlineKeyboardMarkup:
    """Генерирует клавиатуру с кнопкой запуска Telegram Mini App."""
    url = WEBAPP_URL
    buttons = []
    if url:
        buttons.append([InlineKeyboardButton(text="🌙 Открыть трекер сна", web_app=WebAppInfo(url=url))])
    else:
        # Если WebApp URL еще не настроен публично, даем кнопки управления прямо в чате
        buttons.append([
            InlineKeyboardButton(text="💤 Уложить спать", callback_data="btn_sleep"),
            InlineKeyboardButton(text="☀️ Проснулся", callback_data="btn_wake")
        ])
        buttons.append([
            InlineKeyboardButton(text="📊 Текущий статус", callback_data="btn_status")
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def check_access_or_reply(event: types.Message | types.CallbackQuery) -> bool:
    """Проверяет доступ пользователя по белому списку."""
    user_id = event.from_user.id
    if not await db.is_user_allowed(user_id):
        text = (
            "🔒 <b>Доступ ограничен</b>\n\n"
            "Этот бот — приватный семейный трекер сна Севы.\n"
            f"Ваш Telegram ID: <code>{user_id}</code>\n\n"
            "Чтобы получить доступ, напишите владельцу бота: @youngyannniii"
        )
        if isinstance(event, types.CallbackQuery):
            await event.message.answer(text, parse_mode=ParseMode.HTML)
            await event.answer("Доступ ограничен", show_alert=True)
        else:
            await event.answer(text, parse_mode=ParseMode.HTML)
        return False
    return True

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    """Панель администратора (только для владельца бота)."""
    if message.from_user.id != db.SUPERADMIN_ID:
        await message.answer("🔒 Доступ запрещен. Эта команда доступна только владельцу бота (@youngyannniii).")
        return

    whitelist = await db.get_whitelist()
    lines = ["🛡 <b>Панель управления доступом к боту</b>\n"]
    lines.append(f"Всего пользователей в белом списке: <b>{len(whitelist)}</b>\n")
    for u in whitelist:
        tid = u["telegram_id"]
        uname = f"@{u['username']}" if u.get("username") else "без username"
        fname = u.get("full_name") or "Пользователь"
        note = f" ({u['notes']})" if u.get("notes") else ""
        crown = " 👑" if tid == db.SUPERADMIN_ID else ""
        lines.append(f"• <code>{tid}</code> — <b>{fname}</b> [{uname}]{note}{crown}")

    lines.append("\n<b>Команды управления:</b>")
    lines.append("➕ <code>/add_user &lt;id&gt; [имя/ник]</code> — выдать доступ")
    lines.append("➖ <code>/remove_user &lt;id&gt;</code> — закрыть доступ")

    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

@dp.message(Command("add_user"))
async def cmd_add_user(message: types.Message):
    """Добавить пользователя в белый список: /add_user <id> [имя]."""
    if message.from_user.id != db.SUPERADMIN_ID:
        await message.answer("🔒 Доступ запрещен.")
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("ℹ️ Использование: <code>/add_user &lt;telegram_id&gt; [имя или ник]</code>", parse_mode=ParseMode.HTML)
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("❌ Ошибка: Telegram ID должен состоять только из цифр.")
        return

    name_or_note = parts[2] if len(parts) > 2 else f"ID {target_id}"
    username = name_or_note if name_or_note.startswith("@") else None

    await db.add_to_whitelist(
        telegram_id=target_id,
        username=username,
        full_name=name_or_note,
        added_by=message.from_user.id,
        notes="Добавлен через Telegram"
    )

    await message.answer(
        f"✅ <b>Пользователь добавлен в белый список!</b>\n\n"
        f"• Telegram ID: <code>{target_id}</code>\n"
        f"• Имя/Примечание: <b>{name_or_note}</b>\n\n"
        f"Теперь этот пользователь может полноценно открывать бота и WebApp.",
        parse_mode=ParseMode.HTML
    )

@dp.message(Command("remove_user"))
async def cmd_remove_user(message: types.Message):
    """Удалить пользователя из белого списка: /remove_user <id>."""
    if message.from_user.id != db.SUPERADMIN_ID:
        await message.answer("🔒 Доступ запрещен.")
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("ℹ️ Использование: <code>/remove_user &lt;telegram_id&gt;</code>", parse_mode=ParseMode.HTML)
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("❌ Ошибка: Telegram ID должен состоять только из цифр.")
        return

    if target_id == db.SUPERADMIN_ID:
        await message.answer("⚠️ Вы не можете удалить самого себя из списка администраторов.")
        return

    ok = await db.remove_from_whitelist(target_id)
    if ok:
        await message.answer(f"✅ Пользователь <code>{target_id}</code> удален из белого списка. Доступ закрыт.", parse_mode=ParseMode.HTML)
    else:
        await message.answer(f"ℹ️ Пользователь <code>{target_id}</code> не найден в белом списке.", parse_mode=ParseMode.HTML)

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    """Обработчик команды /start."""
    if not await check_access_or_reply(message):
        return
    args = message.text.split()[1] if len(message.text.split()) > 1 else None
    invite_code = None
    if args:
        if args.startswith("join_"):
            invite_code = args.replace("join_", "")
        else:
            invite_code = args

    user_info = await db.get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        invite_code=invite_code,
        default_birth_date=DEFAULT_BIRTH_DATE
    )
    family = user_info["family"]
    code = family["invite_code"]

    welcome_text = (
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        f"👶 Это ваш семейный трекер сна малыша.\n"
        f"📅 Возраст: <b>2 месяца 7 дней</b> (норма бодрствования: <b>75–90 мин</b>).\n\n"
        f"🔑 <b>Код вашей семьи:</b> <code>{code}</code>\n"
        f"Отправьте этот код второму родителю, чтобы отслеживать сон вместе!\n\n"
        f"Нажмите кнопку ниже, чтобы открыть приложение:"
    )

    await message.answer(
        welcome_text,
        reply_markup=get_webapp_keyboard(code),
        parse_mode=ParseMode.HTML
    )

@dp.message(Command("status"))
@dp.callback_query(F.data == "btn_status")
async def cmd_status(event: types.Message | types.CallbackQuery):
    """Отображает текущий статус малыша прямо в чате с разделением сна на день/ночь."""
    if not await check_access_or_reply(event):
        return
    user_id = event.from_user.id
    user_info = await db.get_or_create_user(user_id, default_birth_date=DEFAULT_BIRTH_DATE)
    family = user_info["family"]
    baby_name = family.get("baby_name", "Сева")
    active = await db.get_active_sleep(family["id"])
    last_completed = await db.get_last_completed_sleep(family["id"])

    birth_date = dt_date.fromisoformat(family.get("baby_birth_date", DEFAULT_BIRTH_DATE))
    now = time_utils.get_local_now()

    if active:
        start_dt = time_utils.to_local_datetime(active["start_time"])
        dur_min = int((now - start_dt).total_seconds() // 60)
        dur_str = analytics.format_duration(dur_min)
        sleep_label = time_utils.get_sleep_type_label(bool(active.get("is_night")), active.get("nap_number"))
        status_text = (
            f"🌙 <b>{baby_name} сейчас спит!</b>\n"
            f"• Тип: <b>{sleep_label}</b>\n"
            f"• Уснул в: <b>{time_utils.format_local_hm(start_dt)}</b> (МСК+4 / GMT+7)\n"
            f"• Длительность: <b>{dur_str}</b>"
        )
    else:
        if last_completed:
            wake_dt = time_utils.to_local_datetime(last_completed["end_time"])
            last_sleep_start = time_utils.to_local_datetime(last_completed["start_time"])
            last_dur_min = int((wake_dt - last_sleep_start).total_seconds() // 60)
            last_sleep_info = (
                f"\n• Последний сон: {'Ночной' if last_completed.get('is_night') else 'Дневной'} "
                f"({time_utils.format_local_hm(last_sleep_start)} – {time_utils.format_local_hm(wake_dt)}, "
                f"{analytics.format_duration(last_dur_min)})"
            )
        else:
            wake_dt = now - timedelta(minutes=45)
            last_sleep_info = ""
            
        next_info = norms.calculate_next_sleep(
            wake_time=wake_dt,
            birth_date=birth_date,
            custom_wb_min=family.get("custom_wake_window_min"),
            current_time=now
        )
        awake_str = analytics.format_duration(next_info["awake_duration_min"])
        target_time_str = time_utils.format_local_hm(next_info["target_sleep_time"])

        status_text = (
            f"☀️ <b>{baby_name} сейчас бодрствует</b>\n"
            f"• Проснулся в: <b>{time_utils.format_local_hm(wake_dt)}</b>\n"
            f"• Бодрствует уже: <b>{awake_str}</b>\n"
            f"• Окно укладывания: <b>~{target_time_str}</b> ({next_info['status_text']})\n"
            f"• До сна осталось: <b>{next_info['minutes_left']} мин</b>"
            f"{last_sleep_info}\n\n"
            f"💡 <i>{next_info['stage_desc']}</i>"
        )

    # Добавляем подробную сводку за сегодня с разделением по дневному и ночному сну
    today_local = now.date()
    today_sleeps = await db.get_sleeps_for_day(family["id"], today_local)
    daily_summary = analytics.calculate_daily_summary(
        sleeps=today_sleeps,
        target_date=today_local,
        birth_date=birth_date,
        now=now
    )

    day_naps_count = daily_summary["day_naps_count"]
    day_naps_text = f"{day_naps_count} снов" if day_naps_count != 1 else "1 сон"
    summary_block = (
        f"\n\n📊 <b>Сводка за сегодня ({daily_summary['date_formatted']}):</b>\n"
        f"• 🌙 Ночной сон: <b>{daily_summary['night_sleep_str']}</b>\n"
        f"• ☀️ Дневной сон: <b>{daily_summary['day_sleep_str']}</b> ({day_naps_text})\n"
        f"• 💤 Всего сна: <b>{daily_summary['total_sleep_str']}</b> (норма {daily_summary['age_info']['norm_total_sleep']}ч)\n"
        f"• ⏳ Ср. бодрствование: <b>{daily_summary['avg_wb_str']}</b>"
    )

    full_text = status_text + summary_block

    if isinstance(event, types.CallbackQuery):
        await event.message.answer(full_text, reply_markup=get_webapp_keyboard(family["invite_code"]), parse_mode=ParseMode.HTML)
        await event.answer()
    else:
        await event.answer(full_text, reply_markup=get_webapp_keyboard(family["invite_code"]), parse_mode=ParseMode.HTML)

@dp.message(Command("sleep"))
@dp.callback_query(F.data == "btn_sleep")
async def cmd_sleep(event: types.Message | types.CallbackQuery):
    """Уложить спать прямо из чата Telegram с автоопределением дневного/ночного сна."""
    if not await check_access_or_reply(event):
        return
    user_id = event.from_user.id
    user_info = await db.get_or_create_user(user_id, default_birth_date=DEFAULT_BIRTH_DATE)
    family = user_info["family"]
    baby_name = family.get("baby_name", "Сева")

    res = await db.start_sleep(family["id"], user_id)
    sleep_label = time_utils.get_sleep_type_label(bool(res.get("is_night")), res.get("nap_number"))
    start_hm = time_utils.format_local_hm(res["start_time"])

    text = (
        f"😴 <b>{sleep_label} начался в {start_hm}!</b>\n\n"
        f"👶 {baby_name} сладко спит. Таймер сна запущен.\n"
        f"Когда малыш проснется, нажмите <b>«☀️ Проснулся»</b> или отправьте /wake."
    )

    if isinstance(event, types.CallbackQuery):
        await event.message.answer(text, reply_markup=get_webapp_keyboard(family["invite_code"]), parse_mode=ParseMode.HTML)
        await event.answer("Сон зафиксирован!")
    else:
        await event.answer(text, reply_markup=get_webapp_keyboard(family["invite_code"]), parse_mode=ParseMode.HTML)

@dp.message(Command("wake"))
@dp.callback_query(F.data == "btn_wake")
async def cmd_wake(event: types.Message | types.CallbackQuery):
    """Пробуждение прямо из чата с фиксацией длительности и типа сна."""
    if not await check_access_or_reply(event):
        return
    user_id = event.from_user.id
    user_info = await db.get_or_create_user(user_id, default_birth_date=DEFAULT_BIRTH_DATE)
    family = user_info["family"]
    baby_name = family.get("baby_name", "Сева")

    res = await db.stop_sleep(family["id"], user_id)
    if res:
        start_dt = time_utils.to_local_datetime(res["start_time"])
        end_dt = time_utils.to_local_datetime(res["end_time"])
        dur_min = int((end_dt - start_dt).total_seconds() // 60)
        sleep_label = "Ночной сон" if res.get("is_night") else "Дневной сон"
        text = (
            f"☀️ <b>{baby_name} проснулся в {time_utils.format_local_hm(end_dt)}!</b>\n\n"
            f"• Тип: <b>{sleep_label}</b>\n"
            f"• Длительность сна: <b>{analytics.format_duration(dur_min)}</b>\n"
            f"• Начало: {time_utils.format_local_hm(start_dt)} → Конец: {time_utils.format_local_hm(end_dt)}\n\n"
            f"⏱ Таймер бодрствования (ВБ) запущен. Норма для возраста: <b>75–90 мин</b>."
        )
    else:
        text = "ℹ️ Активный сон не был найден, но время бодрствования зафиксировано!"

    if isinstance(event, types.CallbackQuery):
        await event.message.answer(text, reply_markup=get_webapp_keyboard(family["invite_code"]), parse_mode=ParseMode.HTML)
        await event.answer("С добрым утром!")
    else:
        await event.answer(text, reply_markup=get_webapp_keyboard(family["invite_code"]), parse_mode=ParseMode.HTML)

@dp.message(Command("backup"))
async def cmd_backup(message: types.Message):
    """Отправляет резервную копию базы данных прямо в чат Telegram."""
    if not await check_access_or_reply(message):
        return
    now_str = time_utils.get_local_now().strftime("%Y%m%d_%H%M")
    backup_file = f"sleep_tracker_backup_{now_str}.db"
    try:
        if db.is_postgres():
            await db.export_neon_to_sqlite(backup_file)
        elif os.path.exists("sleep_tracker.db"):
            import shutil
            shutil.copyfile("sleep_tracker.db", backup_file)
        else:
            await message.answer("⚠️ Файл базы данных пока пуст.")
            return

        doc = FSInputFile(backup_file, filename=backup_file)
        await message.answer_document(
            doc,
            caption="💾 <b>Резервная копия базы данных трекера сна.</b>\n"
                    "Все ваши данные сохранены! Если хостинг перезапустится, "
                    "вы можете просто отправить этот файл боту для мгновенного восстановления.",
            parse_mode=ParseMode.HTML
        )
    finally:
        if os.path.exists(backup_file):
            try:
                os.remove(backup_file)
            except Exception:
                pass

@dp.message(F.document)
async def handle_document_restore(message: types.Message):
    """Позволяет восстановить базу данных из отправленного .db файла (сохраняет в Neon PostgreSQL)."""
    if not await check_access_or_reply(message):
        return
    doc = message.document
    if doc.file_name and doc.file_name.endswith(".db"):
        try:
            file_info = await bot.get_file(doc.file_id)
            tmp_db = f"temp_restore_{message.from_user.id}.db"
            await bot.download_file(file_info.file_path, destination=tmp_db)
            if db.is_postgres():
                await db.migrate_sqlite_to_postgres(tmp_db)
            else:
                import shutil
                shutil.copyfile(tmp_db, "sleep_tracker.db")
            if os.path.exists(tmp_db):
                os.remove(tmp_db)
            await message.answer(
                "✅ <b>База данных успешно восстановлена и сохранена в Neon!</b>\nВсе записи снов снова активны.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            await message.answer(f"❌ Ошибка при восстановлении: {e}")

@dp.message(Command("family"))
async def cmd_family(message: types.Message):
    """Показывает код семьи для подключения второго родителя."""
    if not await check_access_or_reply(message):
        return
    user_info = await db.get_or_create_user(message.from_user.id, default_birth_date=DEFAULT_BIRTH_DATE)
    family = user_info["family"]
    code = family["invite_code"]
    bot_info = await bot.get_me()

    share_link = f"https://t.me/{bot_info.username}?start=join_{code}"
    text = (
        f"👨‍👩‍👧 <b>Семейный доступ для мамы и папы</b>\n\n"
        f"Код вашей семьи: <code>{code}</code>\n\n"
        f"Ссылка для быстрого подключения второго родителя:\n"
        f"{share_link}\n\n"
        f"Просто перешлите это сообщение второму родителю!"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


# =====================================================================
# ФОНОВЫЕ НАПОМИНАНИЯ О СНЕ (УВЕДОМЛЕНИЯ РОДИТЕЛЯМ)
# =====================================================================

async def sleep_reminder_worker():
    """Каждую минуту проверяет время бодрствования и отправляет мягкие уведомления родителям."""
    while True:
        try:
            families = await db.get_all_families_for_reminders()
            now = time_utils.get_local_now()

            for fam in families:
                fam_id = fam["id"]
                active = await db.get_active_sleep(fam_id)
                # Уведомляем только когда ребенок бодрствует
                if active:
                    continue

                last_completed = await db.get_last_completed_sleep(fam_id)
                if not last_completed:
                    continue

                wake_dt = time_utils.to_local_datetime(last_completed["end_time"])
                birth_date = dt_date.fromisoformat(fam.get("baby_birth_date", DEFAULT_BIRTH_DATE))

                next_info = norms.calculate_next_sleep(
                    wake_time=wake_dt,
                    birth_date=birth_date,
                    custom_wb_min=fam.get("custom_wake_window_min"),
                    current_time=now
                )

                min_left = next_info["minutes_left"]
                wake_key = f"{fam_id}_{last_completed['id']}"

                # 1. Напоминание за 10-15 минут ("Мостик на сон / пора замедляться")
                if 5 <= min_left <= 15 and wake_key not in notified_winddown_cache:
                    notified_winddown_cache.add(wake_key)
                    members = (await db.get_family_info(fam_id)).get("members", [])
                    awake_str = analytics.format_duration(next_info["awake_duration_min"])
                    target_time = time_utils.format_local_hm(next_info["target_sleep_time"])

                    msg = (
                        f"⏰ <b>Время готовиться ко сну!</b>\n\n"
                        f"👶 Малыш бодрствует уже <b>{awake_str}</b>.\n"
                        f"Рекомендуемое время засыпания: <b>~{target_time}</b> (через {min_left} мин).\n\n"
                        f"💡 <i>Рекомендуем приглушить свет, включить белый шум и начать спокойный ритуал.</i>"
                    )
                    for m in members:
                        try:
                            await bot.send_message(m["telegram_id"], msg, reply_markup=get_webapp_keyboard(fam["invite_code"]), parse_mode=ParseMode.HTML)
                        except Exception:
                            pass

                # 2. Предупреждение о перегуле (если прошло более 10 минут от нормы)
                elif min_left <= -10 and wake_key not in notified_overtired_cache:
                    notified_overtired_cache.add(wake_key)
                    members = (await db.get_family_info(fam_id)).get("members", [])
                    overtired_m = abs(min_left)

                    msg = (
                        f"⚠️ <b>Внимание: риск перегула!</b>\n\n"
                        f"👶 Время бодрствования превышено на <b>{overtired_m} мин</b>.\n"
                        f"Постарайтесь уложить малыша как можно скорее, чтобы избежать перевозбуждения и слез."
                    )
                    for m in members:
                        try:
                            await bot.send_message(m["telegram_id"], msg, reply_markup=get_webapp_keyboard(fam["invite_code"]), parse_mode=ParseMode.HTML)
                        except Exception:
                            pass

        except Exception as e:
            logger.error(f"Error in sleep reminder worker: {e}")

        await asyncio.sleep(60)


# =====================================================================
# СТАРТ ПРИЛОЖЕНИЯ И БОТА
# =====================================================================

async def start_bot_polling():
    """Фоновый запуск polling бота с автоматическим переподключением."""
    while True:
        try:
            logger.info("Aiogram 3 bot starting polling...")
            await dp.start_polling(bot, handle_signals=False)
            break
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Telegram bot polling warning: {e}. Retrying in 10s...")
            await asyncio.sleep(10)

IS_RENDER = os.getenv("RENDER") == "true" or os.getenv("RENDER_SERVICE_ID") is not None
DEPLOY_SECRET = os.getenv("DEPLOY_SECRET", "aeza_seva_deploy_secret_2026")

@app.api_route("/api/deploy-webhook", methods=["GET", "POST"])
async def deploy_webhook(secret: Optional[str] = Query(None)):
    """Эндпоинт для автодеплоя при push в репозиторий."""
    if secret != DEPLOY_SECRET:
        raise HTTPException(status_code=403, detail="Invalid deploy secret")
    
    logger.info("Auto-deploy webhook triggered! Pulling latest code and restarting...")
    
    async def _async_deploy():
        await asyncio.sleep(1)
        os.system("cd /opt/baby_sleep_tracker && git pull origin main && venv/bin/pip install -r requirements.txt && systemctl restart seva-bot.service &")
        
    asyncio.create_task(_async_deploy())
    return {"status": "ok", "message": "Deploy initiated"}

@app.on_event("startup")
async def on_startup():
    """Инициализация базы данных и запуск Telegram бота."""
    await db.init_db()
    logger.info("Database initialized successfully.")

    if IS_RENDER:
        logger.info("Running on Render environment: Telegram polling disabled in favor of dedicated VPS.")
        return

    if bot:
        # Автоматическая настройка кнопки меню внизу чата Telegram
        if WEBAPP_URL:
            try:
                await bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(text="🌙 Трекер сна", web_app=WebAppInfo(url=WEBAPP_URL))
                )
                logger.info(f"Chat menu button automatically configured: {WEBAPP_URL}")
            except Exception as e:
                logger.warning(f"Failed to set chat menu button: {e}")

        # Запуск polling бота в фоновом task
        asyncio.create_task(start_bot_polling())
        # Запуск фонового планировщика уведомлений
        asyncio.create_task(sleep_reminder_worker())
        logger.info("Sleep reminder worker started.")

@app.on_event("shutdown")
async def on_shutdown():
    """Корректное закрытие пула соединений базы данных при завершении приложения."""
    await db.close_db()
    logger.info("Database connections closed.")

if __name__ == "__main__":
    reload_mode = os.getenv("RELOAD", "false").lower() == "true"
    uvicorn.run("main:app", host=HOST, port=PORT, reload=reload_mode)
