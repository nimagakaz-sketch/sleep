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
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.enums import ParseMode

import database as db
import norms
import analytics

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


# =====================================================================
# API ЭНДПОИНТЫ FASTAPI
# =====================================================================

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Отдает главную страницу Telegram Mini App."""
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return HTMLResponse("<h1>Baby Sleep Tracker</h1>")

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

    next_sleep_info = None
    if not active_sleep:
        # Ребенок сейчас бодрствует
        if last_completed:
            wake_dt = datetime.fromisoformat(last_completed["end_time"])
        else:
            wake_dt = datetime.now() - timedelta(minutes=30)
            
        next_sleep_info = norms.calculate_next_sleep(
            wake_time=wake_dt,
            birth_date=birth_date,
            custom_wb_min=family.get("custom_wake_window_min"),
            current_time=datetime.now()
        )

    return {
        "is_sleeping": bool(active_sleep),
        "active_sleep": active_sleep,
        "last_completed_sleep": last_completed,
        "next_sleep": next_sleep_info,
        "family": family_info,
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
    try:
        target_day = dt_date.fromisoformat(date) if date else datetime.now().date()
    except Exception:
        target_day = datetime.now().date()

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
        now=datetime.now()
    )
    return summary

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

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    """Обработчик команды /start."""
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
    """Отображает текущий статус малыша прямо в чате."""
    user_id = event.from_user.id
    user_info = await db.get_or_create_user(user_id, default_birth_date=DEFAULT_BIRTH_DATE)
    family = user_info["family"]
    active = await db.get_active_sleep(family["id"])
    last_completed = await db.get_last_completed_sleep(family["id"])

    birth_date = dt_date.fromisoformat(family.get("baby_birth_date", DEFAULT_BIRTH_DATE))
    now = datetime.now()

    if active:
        start_dt = datetime.fromisoformat(active["start_time"])
        dur_min = int((now - start_dt).total_seconds() // 60)
        dur_str = analytics.format_duration(dur_min)
        text = (
            f"🌙 <b>Малыш сейчас спит!</b>\n"
            f"Начало сна: {start_dt.strftime('%H:%M')}\n"
            f"Длительность: <b>{dur_str}</b>\n"
            f"Тип: {'Ночной' if active['is_night'] else 'Дневной'}"
        )
    else:
        if last_completed:
            wake_dt = datetime.fromisoformat(last_completed["end_time"])
        else:
            wake_dt = now - timedelta(minutes=45)
            
        next_info = norms.calculate_next_sleep(
            wake_time=wake_dt,
            birth_date=birth_date,
            custom_wb_min=family.get("custom_wake_window_min"),
            current_time=now
        )
        awake_str = analytics.format_duration(next_info["awake_duration_min"])
        target_time_str = datetime.fromisoformat(next_info["target_sleep_time"]).strftime("%H:%M")

        text = (
            f"☀️ <b>Малыш сейчас бодрствует</b>\n"
            f"Бодрствует уже: <b>{awake_str}</b>\n"
            f"Рекомендуемое укладывание: <b>~{target_time_str}</b> ({next_info['status_text']})\n"
            f"Осталось времени: <b>{next_info['minutes_left']} мин</b>\n\n"
            f"💡 <i>{next_info['stage_desc']}</i>"
        )

    if isinstance(event, types.CallbackQuery):
        await event.message.answer(text, reply_markup=get_webapp_keyboard(family["invite_code"]), parse_mode=ParseMode.HTML)
        await event.answer()
    else:
        await event.answer(text, reply_markup=get_webapp_keyboard(family["invite_code"]), parse_mode=ParseMode.HTML)

@dp.message(Command("sleep"))
@dp.callback_query(F.data == "btn_sleep")
async def cmd_sleep(event: types.Message | types.CallbackQuery):
    """Уложить спать прямо из чата Telegram."""
    user_id = event.from_user.id
    user_info = await db.get_or_create_user(user_id, default_birth_date=DEFAULT_BIRTH_DATE)
    family = user_info["family"]

    await db.start_sleep(family["id"], user_id)
    text = f"😴 <b>Сон начался в {datetime.now().strftime('%H:%M')}!</b>\nТаймер сна запущен."

    if isinstance(event, types.CallbackQuery):
        await event.message.answer(text, reply_markup=get_webapp_keyboard(family["invite_code"]), parse_mode=ParseMode.HTML)
        await event.answer("Сон зафиксирован!")
    else:
        await event.answer(text, reply_markup=get_webapp_keyboard(family["invite_code"]), parse_mode=ParseMode.HTML)

@dp.message(Command("wake"))
@dp.callback_query(F.data == "btn_wake")
async def cmd_wake(event: types.Message | types.CallbackQuery):
    """Пробуждение прямо из чата."""
    user_id = event.from_user.id
    user_info = await db.get_or_create_user(user_id, default_birth_date=DEFAULT_BIRTH_DATE)
    family = user_info["family"]

    res = await db.stop_sleep(family["id"], user_id)
    if res:
        start_dt = datetime.fromisoformat(res["start_time"])
        end_dt = datetime.fromisoformat(res["end_time"])
        dur_min = int((end_dt - start_dt).total_seconds() // 60)
        text = (
            f"☀️ <b>Малыш проснулся в {end_dt.strftime('%H:%M')}!</b>\n"
            f"Длительность сна: <b>{analytics.format_duration(dur_min)}</b>.\n"
            f"Таймер бодрствования запущен."
        )
    else:
        text = "ℹ️ Активный сон не был найден, но время бодрствования отсчитывается!"

    if isinstance(event, types.CallbackQuery):
        await event.message.answer(text, reply_markup=get_webapp_keyboard(family["invite_code"]), parse_mode=ParseMode.HTML)
        await event.answer("Доброе утро!")
    else:
        await event.answer(text, reply_markup=get_webapp_keyboard(family["invite_code"]), parse_mode=ParseMode.HTML)

@dp.message(Command("family"))
async def cmd_family(message: types.Message):
    """Показывает код семьи для подключения второго родителя."""
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
            now = datetime.now()

            for fam in families:
                fam_id = fam["id"]
                active = await db.get_active_sleep(fam_id)
                # Уведомляем только когда ребенок бодрствует
                if active:
                    continue

                last_completed = await db.get_last_completed_sleep(fam_id)
                if not last_completed:
                    continue

                wake_dt = datetime.fromisoformat(last_completed["end_time"])
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
                    target_time = datetime.fromisoformat(next_info["target_sleep_time"]).strftime("%H:%M")

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

@app.on_event("startup")
async def on_startup():
    """Инициализация базы данных и запуск Telegram бота."""
    await db.init_db()
    logger.info("Database initialized successfully.")

    if bot:
        # Запуск polling бота в фоновом task
        asyncio.create_task(start_bot_polling())
        # Запуск фонового планировщика уведомлений
        asyncio.create_task(sleep_reminder_worker())
        logger.info("Sleep reminder worker started.")

if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
