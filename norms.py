"""
Нормы сна и времени бодрствования (ВБ) для детей первого и второго года жизни.
Основано на рекомендациях ведущих педиатрических ассоциаций и консультантов по детскому сну.
"""

from datetime import datetime, date, timedelta
from typing import Dict, Any, Optional

# Таблица норм бодрствования и сна по возрасту (в днях)
# [min_days, max_days, min_wb_min, max_wb_min, avg_wb_min, naps_count, total_sleep_h]
NORMS_TABLE = [
    (0, 30, 45, 60, 50, "4-6", "15-18"),       # 0 - 1 месяц
    (31, 60, 60, 80, 70, "4-5", "14.5-16.5"),  # 1 - 2 месяца
    (61, 90, 75, 90, 80, "4-5", "14-16"),      # 2 - 3 месяца (сейчас 67 дней!)
    (91, 120, 80, 105, 90, "3-4", "14-15.5"),  # 3 - 4 месяца
    (121, 150, 90, 120, 105, "3-4", "13.5-15"),# 4 - 5 месяцев
    (151, 180, 115, 140, 125, "3", "13.5-14.5"),# 5 - 6 месяцев
    (181, 240, 135, 170, 150, "2-3", "13-14.5"),# 6 - 8 месяцев
    (241, 300, 165, 210, 180, "2", "12.5-14"), # 8 - 10 месяцев
    (301, 365, 180, 240, 210, "2", "12-14"),   # 10 - 12 месяцев
    (366, 545, 210, 300, 255, "1-2", "11.5-13.5"), # 12 - 18 месяцев
    (546, 1200, 270, 360, 315, "1", "11-13"),  # 1.5 - 3 года
]

def calculate_age_info(birth_date: date, target_date: Optional[date] = None) -> Dict[str, Any]:
    """Вычисляет точный календарный возраст ребенка (месяцы и дни) и возвращает педиатрические нормы."""
    if target_date is None:
        target_date = date.today()
        
    total_days = (target_date - birth_date).days
    if total_days < 0:
        total_days = 0

    # Точный календарный расчет
    years = target_date.year - birth_date.year
    months = target_date.month - birth_date.month
    rem_days = target_date.day - birth_date.day

    if rem_days < 0:
        # Занимаем дни из предыдущего месяца
        first_of_this_month = target_date.replace(day=1)
        last_of_prev_month = first_of_this_month - timedelta(days=1)
        rem_days += last_of_prev_month.day
        months -= 1

    if months < 0:
        months += 12
        years -= 1

    total_months = years * 12 + months
    
    parts = []
    if total_months > 0:
        # Склонение месяцев
        if total_months % 10 == 1 and total_months % 100 != 11:
            parts.append(f"{total_months} месяц")
        elif total_months % 10 in [2, 3, 4] and not (total_months % 100 in [12, 13, 14]):
            parts.append(f"{total_months} месяца")
        else:
            parts.append(f"{total_months} месяцев")
            
    if rem_days > 0 or total_months == 0:
        if rem_days % 10 == 1 and rem_days % 100 != 11:
            parts.append(f"{rem_days} день")
        elif rem_days % 10 in [2, 3, 4] and not (rem_days % 100 in [12, 13, 14]):
            parts.append(f"{rem_days} дня")
        else:
            parts.append(f"{rem_days} дней")
            
    age_text = " ".join(parts)
    
    # Находим норму по total_days
    norm = NORMS_TABLE[-1]
    for row in NORMS_TABLE:
        if row[0] <= total_days <= row[1]:
            norm = row
            break
            
    return {
        "days": total_days,
        "months": total_months,
        "rem_days": rem_days,
        "age_text": age_text,
        "norm_wb_min": norm[2],
        "norm_wb_max": norm[3],
        "norm_wb_avg": norm[4],
        "norm_naps": norm[5],
        "norm_total_sleep": norm[6]
    }

def calculate_next_sleep(
    wake_time: datetime,
    birth_date: date,
    custom_wb_min: Optional[int] = None,
    current_time: Optional[datetime] = None,
    is_first_wake: bool = False,
    is_last_wake: bool = False
) -> Dict[str, Any]:
    """
    Рассчитывает время следующего сна от момента пробуждения.
    Учитывает:
    - Норму по возрасту или персональную настройку
    - Первое утреннее ВБ обычно на 10-15 минут короче
    - Вечернее ВБ перед ночным сном обычно на 10-15 минут длиннее
    """
    if current_time is None:
        current_time = datetime.now()
        
    age_info = calculate_age_info(birth_date, wake_time.date())
    
    # Базовое целевое время бодрствования
    if custom_wb_min and custom_wb_min > 0:
        target_wb = custom_wb_min
    else:
        target_wb = age_info["norm_wb_avg"]
        
    # Корректировка по времени дня
    if is_first_wake:
        target_wb = max(30, target_wb - 10)
    elif is_last_wake:
        target_wb = target_wb + 15
        
    recommended_sleep_time = wake_time + timedelta(minutes=target_wb)
    
    # Сколько минут бодрствует сейчас
    awake_duration_sec = (current_time - wake_time).total_seconds()
    awake_duration_min = int(awake_duration_sec // 60)
    
    # Сколько минут осталось до укладывания
    minutes_left = int((recommended_sleep_time - current_time).total_seconds() // 60)
    
    # Определение статуса
    # > 20 мин - Спокойное бодрствование
    # 5..20 мин - Мостик на сон (время ритуала)
    # 0..5 мин - Пора укладывать
    # < 0 мин - Перегул (нужно скорее уложить)
    if minutes_left > 20:
        status = "calm"
        status_text = "Спокойное бодрствование"
        color = "#10B981" # Green
        stage_desc = "Малыш бодрствует, можно активно играть и общаться."
    elif 5 < minutes_left <= 20:
        status = "wind_down"
        status_text = "Мостик на сон"
        color = "#F59E0B" # Yellow/Amber
        stage_desc = "Пора замедлять активность, приглушить свет и начинать ритуал."
    elif 0 <= minutes_left <= 5:
        status = "sleep_time"
        status_text = "Пора спать"
        color = "#3B82F6" # Blue
        stage_desc = "Идеальный момент для засыпания без переутомления."
    else:
        status = "overtired"
        status_text = "Риск перегула"
        color = "#EF4444" # Red
        overtired_min = abs(minutes_left)
        stage_desc = f"ВБ превышено на {overtired_min} мин! Лучше уложить как можно скорее."

    return {
        "wake_time": wake_time.isoformat(),
        "target_sleep_time": recommended_sleep_time.isoformat(),
        "target_wb_min": target_wb,
        "awake_duration_min": awake_duration_min,
        "minutes_left": minutes_left,
        "status": status,
        "status_text": status_text,
        "color": color,
        "stage_desc": stage_desc,
        "age_info": age_info
    }
