"""
Модуль аналитики и формирования суточной сводки по сну ребенка.
Вычисляет суммарный сон (день/ночь), среднее время бодрствования (ВБ),
интервалы между снами и 24-часовой таймлайн дня.
"""

from datetime import datetime, date, time, timedelta
from typing import List, Dict, Any, Optional
from norms import calculate_age_info

def format_duration(minutes: int) -> str:
    """Форматирует минуты в вид 'Xч Yм' или 'Yм'."""
    if minutes < 0:
        minutes = 0
    h = minutes // 60
    m = minutes % 60
    if h > 0 and m > 0:
        return f"{h}ч {m}м"
    elif h > 0:
        return f"{h}ч"
    else:
        return f"{m}м"

def calculate_daily_summary(
    sleeps: List[Dict[str, Any]],
    target_date: date,
    birth_date: date,
    now: Optional[datetime] = None
) -> Dict[str, Any]:
    """
    Рассчитывает полную сводку за выбранный день (с 00:00 до 23:59:59).
    Корректно обрезает сны, выходящие за границы суток.
    """
    if now is None:
        now = datetime.now()
        
    day_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
    day_end = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)
    
    age_info = calculate_age_info(birth_date, target_date)
    
    total_sleep_sec = 0
    day_sleep_sec = 0
    night_sleep_sec = 0
    
    day_naps_count = 0
    night_sleeps_count = 0
    
    processed_sleeps = []
    
    for s in sleeps:
        start_dt = datetime.fromisoformat(s["start_time"])
        is_active = (s["end_time"] is None)
        end_dt = datetime.fromisoformat(s["end_time"]) if not is_active else now
        
        # Обрезаем границами суток для точного расчета суточной длительности
        effective_start = max(start_dt, day_start)
        effective_end = min(end_dt, day_end)
        
        if effective_end > effective_start:
            duration_sec = (effective_end - effective_start).total_seconds()
            total_sleep_sec += duration_sec
            if s.get("is_night"):
                night_sleep_sec += duration_sec
                night_sleeps_count += 1
            else:
                day_sleep_sec += duration_sec
                day_naps_count += 1
                
        # Полная длительность этого конкретного сна
        full_duration_sec = (end_dt - start_dt).total_seconds()
        full_duration_min = max(1, int(full_duration_sec // 60))
        
        processed_sleeps.append({
            "id": s["id"],
            "start_time": s["start_time"],
            "end_time": s["end_time"],
            "start_formatted": start_dt.strftime("%H:%M"),
            "end_formatted": end_dt.strftime("%H:%M") if not is_active else "Спит сейчас",
            "duration_min": full_duration_min,
            "duration_str": format_duration(full_duration_min),
            "is_night": bool(s.get("is_night")),
            "is_active": is_active,
            "notes": s.get("notes"),
            "started_by": s.get("started_by_name") or "Родитель",
            "ended_by": s.get("ended_by_name")
        })

    # Сортируем сны по началу
    processed_sleeps.sort(key=lambda x: x["start_time"])
    
    # Расчет интервалов бодрствования (ВБ) между снами внутри этого дня
    wake_intervals = []
    for i in range(len(processed_sleeps)):
        if i == 0:
            continue
        prev_end_str = processed_sleeps[i-1]["end_time"]
        curr_start_str = processed_sleeps[i]["start_time"]
        if prev_end_str:
            prev_end = datetime.fromisoformat(prev_end_str)
            curr_start = datetime.fromisoformat(curr_start_str)
            if curr_start > prev_end:
                wb_sec = (curr_start - prev_end).total_seconds()
                wb_min = int(wb_sec // 60)
                wake_intervals.append(wb_min)

    total_sleep_min = int(total_sleep_sec // 60)
    day_sleep_min = int(day_sleep_sec // 60)
    night_sleep_min = int(night_sleep_sec // 60)
    
    avg_wb_min = int(sum(wake_intervals) / len(wake_intervals)) if wake_intervals else 0
    
    # Генерация 24-часового таймлайна (в процентах от 00:00 до 24:00)
    # Сутки = 1440 минут
    timeline_segments = []
    current_time_limit = now if target_date == now.date() else day_end
    
    last_cursor = day_start
    for s in processed_sleeps:
        s_start = max(datetime.fromisoformat(s["start_time"]), day_start)
        s_end_val = s["end_time"]
        s_end = min(datetime.fromisoformat(s_end_val), day_end) if s_end_val else min(now, day_end)
        
        # Отрезок бодрствования перед этим сном
        if s_start > last_cursor:
            wb_dur = (s_start - last_cursor).total_seconds() / 60
            left_pct = ((last_cursor - day_start).total_seconds() / 86400) * 100
            width_pct = ((s_start - last_cursor).total_seconds() / 86400) * 100
            timeline_segments.append({
                "type": "awake",
                "left_pct": round(left_pct, 2),
                "width_pct": round(width_pct, 2),
                "label": f"ВБ {format_duration(int(wb_dur))}",
                "start": last_cursor.strftime("%H:%M"),
                "end": s_start.strftime("%H:%M")
            })
            
        # Отрезок сна
        if s_end > s_start:
            sleep_dur = (s_end - s_start).total_seconds() / 60
            left_pct = ((s_start - day_start).total_seconds() / 86400) * 100
            width_pct = ((s_end - s_start).total_seconds() / 86400) * 100
            timeline_segments.append({
                "type": "night_sleep" if s["is_night"] else "day_sleep",
                "left_pct": round(left_pct, 2),
                "width_pct": round(width_pct, 2),
                "label": f"{'Ночной' if s['is_night'] else 'Дневной'} {format_duration(int(sleep_dur))}",
                "start": s_start.strftime("%H:%M"),
                "end": s_end.strftime("%H:%M")
            })
            last_cursor = s_end

    # Отрезок от последнего сна до текущего момента (если сегодня)
    if last_cursor < current_time_limit:
        wb_dur = (current_time_limit - last_cursor).total_seconds() / 60
        left_pct = ((last_cursor - day_start).total_seconds() / 86400) * 100
        width_pct = ((current_time_limit - last_cursor).total_seconds() / 86400) * 100
        timeline_segments.append({
            "type": "awake",
            "left_pct": round(left_pct, 2),
            "width_pct": round(width_pct, 2),
            "label": f"ВБ {format_duration(int(wb_dur))}",
            "start": last_cursor.strftime("%H:%M"),
            "end": current_time_limit.strftime("%H:%M")
        })

    return {
        "date": target_date.isoformat(),
        "date_formatted": target_date.strftime("%d.%m.%Y"),
        "is_today": target_date == now.date(),
        "total_sleep_min": total_sleep_min,
        "total_sleep_str": format_duration(total_sleep_min),
        "day_sleep_min": day_sleep_min,
        "day_sleep_str": format_duration(day_sleep_min),
        "night_sleep_min": night_sleep_min,
        "night_sleep_str": format_duration(night_sleep_min),
        "day_naps_count": day_naps_count,
        "night_sleeps_count": night_sleeps_count,
        "avg_wb_min": avg_wb_min,
        "avg_wb_str": format_duration(avg_wb_min),
        "wake_intervals": wake_intervals,
        "sleeps": processed_sleeps,
        "timeline_segments": timeline_segments,
        "age_info": age_info
    }
