"""
Модуль для работы со временем и часовыми поясами.
Гарантирует правильное отображение времени и автоматическое определение
дневного и ночного сна вне зависимости от того, на каком сервере (Render, VPS, локально) запущен бот.
"""

import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

# Часовой пояс по умолчанию: Asia/Novosibirsk (GMT+7, Красноярск/Новосибирск/Томск/Бангкок)
# Можно переопределить через переменную окружения TIMEZONE=Europe/Moscow или TIMEZONE=Asia/Krasnoyarsk
DEFAULT_TZ_NAME = os.getenv("TIMEZONE", "Asia/Novosibirsk")

def get_timezone() -> ZoneInfo:
    """Возвращает объект ZoneInfo для часового пояса пользователя."""
    tz_name = os.getenv("TIMEZONE", DEFAULT_TZ_NAME)
    try:
        return ZoneInfo(tz_name)
    except Exception:
        # Резервный вариант: фиксированное смещение +7 часов (GMT+7)
        try:
            return ZoneInfo("Asia/Novosibirsk")
        except Exception:
            return timezone(timedelta(hours=7))

def get_local_now() -> datetime:
    """Возвращает текущую дату и время в локальном часовом поясе пользователя."""
    tz = get_timezone()
    # Возвращаем "наивный" datetime, но со значениями локального времени,
    # чтобы совместимость с SQLite ISO строками оставалась идеальной
    now_aware = datetime.now(tz)
    return now_aware.replace(tzinfo=None)

def to_local_datetime(dt_or_iso: datetime | str) -> datetime:
    """Преобразует дату/время или ISO-строку в локальное время."""
    if isinstance(dt_or_iso, str):
        # Удаляем лишние смещения если есть, берем базу
        try:
            dt = datetime.fromisoformat(dt_or_iso)
        except Exception:
            return get_local_now()
    else:
        dt = dt_or_iso

    if dt.tzinfo is not None:
        tz = get_timezone()
        dt = dt.astimezone(tz).replace(tzinfo=None)
    return dt

def format_local_hm(dt_or_iso: datetime | str) -> str:
    """Форматирует время в виде 'ЧЧ:ММ' в часовом поясе пользователя."""
    dt = to_local_datetime(dt_or_iso)
    return dt.strftime("%H:%M")

def is_night_sleep(dt_or_iso: datetime | str) -> bool:
    """
    Автоматически определяет, ночной ли это сон:
    - Ночной сон начинается с 20:30 вечера до 07:30 утра.
    - Дневные сны — в интервале с 07:30 до 20:30.
    """
    dt = to_local_datetime(dt_or_iso)
    total_minutes = dt.hour * 60 + dt.minute
    # 20:30 = 1230 минут, 07:30 = 450 минут
    return total_minutes >= 1230 or total_minutes < 450

def get_sleep_type_label(is_night: bool, nap_index: Optional[int] = None) -> str:
    """Возвращает красивое название типа сна."""
    if is_night:
        return "Ночной сон"
    if nap_index is not None and nap_index > 0:
        return f"Дневной сон #{nap_index}"
    return "Дневной сон"
