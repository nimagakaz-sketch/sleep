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

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

def calculate_weekly_summary(
    daily_summaries: List[Dict[str, Any]],
    end_date: date,
    birth_date: date
) -> Dict[str, Any]:
    """
    Формирует недельную динамику сна (7 дней).
    Рассчитывает средние показатели, пропорцию день/ночь, стабильность режима
    и данные для недельного графика Apple Health.
    """
    days_data = []
    total_sleep_sum = 0
    night_sleep_sum = 0
    day_sleep_sum = 0
    wb_sum = 0
    wb_count = 0
    naps_sum = 0
    days_with_data = 0

    # Максимальная высота шкалы на графике (18 часов = 1080 минут)
    MAX_CHART_MINUTES = 1080

    for d in daily_summaries:
        cur_date = date.fromisoformat(d["date"])
        w_idx = cur_date.weekday()
        weekday_name = WEEKDAYS_RU[w_idx]

        t_min = d["total_sleep_min"]
        n_min = d["night_sleep_min"]
        d_min = d["day_sleep_min"]
        avg_wb = d["avg_wb_min"]

        if t_min > 0:
            days_with_data += 1
            total_sleep_sum += t_min
            night_sleep_sum += n_min
            day_sleep_sum += d_min
            naps_sum += d["day_naps_count"]
            if avg_wb > 0:
                wb_sum += avg_wb
                wb_count += 1

        # Расчет высоты для столбца графика (stacked: ночь внизу, день сверху)
        night_pct = min(100.0, round((n_min / MAX_CHART_MINUTES) * 100, 1))
        day_pct = min(100.0 - night_pct, round((d_min / MAX_CHART_MINUTES) * 100, 1))
        total_pct = round(night_pct + day_pct, 1)

        days_data.append({
            "date": d["date"],
            "date_formatted": d["date_formatted"],
            "day_name": weekday_name,
            "is_selected": (cur_date == end_date),
            "total_sleep_min": t_min,
            "total_sleep_str": d["total_sleep_str"],
            "night_sleep_min": n_min,
            "night_sleep_str": d["night_sleep_str"],
            "day_sleep_min": d_min,
            "day_sleep_str": d["day_sleep_str"],
            "avg_wb_min": avg_wb,
            "avg_wb_str": d["avg_wb_str"],
            "naps_count": d["day_naps_count"],
            "night_bar_pct": night_pct,
            "day_bar_pct": day_pct,
            "total_bar_pct": total_pct
        })

    # Расчет средних
    divisor = max(1, days_with_data)
    avg_total_min = int(total_sleep_sum / divisor)
    avg_night_min = int(night_sleep_sum / divisor)
    avg_day_min = int(day_sleep_sum / divisor)
    avg_wb_min = int(wb_sum / max(1, wb_count)) if wb_count > 0 else 0
    avg_naps = round(naps_sum / divisor, 1)

    age_info = calculate_age_info(birth_date, end_date)

    # Оценка стабильности
    if avg_total_min >= 14 * 60:
        trend_status = "stable"
        trend_title = "Ритм стабилен"
        trend_desc = f"Малыш высыпает суточную норму ({format_duration(avg_total_min)} в среднем). Среднее ВБ {format_duration(avg_wb_min)}."
    elif avg_total_min > 0:
        trend_status = "deficit"
        trend_title = "Небольшой дефицит сна"
        trend_desc = f"В среднем {format_duration(avg_total_min)} сна в сутки (норма {age_info['norm_total_sleep']}ч). Рекомендуется сместить укладывание на 20 мин раньше."
    else:
        trend_status = "nodata"
        trend_title = "Недостаточно данных"
        trend_desc = "Заполняйте трекер регулярно, чтобы видеть точную недельную динамику."

    start_date = end_date - timedelta(days=6)

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "period_formatted": f"{start_date.strftime('%d.%m')} — {end_date.strftime('%d.%m')}",
        "days": days_data,
        "days_with_data": days_with_data,
        "averages": {
            "total_sleep_min": avg_total_min,
            "total_sleep_str": format_duration(avg_total_min),
            "night_sleep_min": avg_night_min,
            "night_sleep_str": format_duration(avg_night_min),
            "day_sleep_min": avg_day_min,
            "day_sleep_str": format_duration(avg_day_min),
            "avg_wb_min": avg_wb_min,
            "avg_wb_str": format_duration(avg_wb_min),
            "avg_naps": avg_naps,
            "norm_total_sleep": age_info["norm_total_sleep"],
            "norm_wb_str": f"{age_info['norm_wb_min']}–{age_info['norm_wb_max']} мин"
        },
        "trend": {
            "status": trend_status,
            "title": trend_title,
            "desc": trend_desc
        }
    }

def generate_csv_data(sleeps: List[Dict[str, Any]]) -> str:
    """Генерирует CSV файл с UTF-8 BOM для экспорта данных о сне."""
    lines = [
        "\ufeffДата,Время начала,Время окончания,Длительность (мин),Длительность,Тип сна,Зафиксировал,Заметки"
    ]
    for s in sleeps:
        start_dt = datetime.fromisoformat(s["start_time"])
        end_dt = datetime.fromisoformat(s["end_time"]) if s.get("end_time") else None
        
        d_str = start_dt.strftime("%Y-%m-%d")
        t_start = start_dt.strftime("%H:%M")
        t_end = end_dt.strftime("%H:%M") if end_dt else "Спит"
        
        dur_min = int((end_dt - start_dt).total_seconds() // 60) if end_dt else 0
        dur_str = format_duration(dur_min)
        sleep_type = "Ночной" if s.get("is_night") else "Дневной"
        user = s.get("started_by_name") or "Родитель"
        notes = (s.get("notes") or "").replace('"', '""').replace(",", ";")

        lines.append(f'{d_str},{t_start},{t_end},{dur_min},"{dur_str}",{sleep_type},"{user}","{notes}"')

    return "\n".join(lines)

def generate_pediatric_text_report(
    baby_name: str,
    age_text: str,
    weekly_summary: Dict[str, Any]
) -> str:
    """Генерирует аккуратный текстовый отчет для педиатра или консультанта по сну."""
    avg = weekly_summary["averages"]
    trend = weekly_summary["trend"]
    
    lines = [
        f"📋 Отчет по сну малыша (для педиатра / невролога)",
        f"👶 Имя: {baby_name} • Возраст: {age_text}",
        f"📅 Период наблюдения: {weekly_summary['period_formatted']}",
        f"",
        f"📊 Средние показатели за неделю:",
        f"• Суммарный сон в сутки: {avg['total_sleep_str']} (возрастная норма: {avg['norm_total_sleep']}ч)",
        f"• Ночной сон: {avg['night_sleep_str']}",
        f"• Дневной сон: {avg['day_sleep_str']} (в среднем {avg['avg_naps']} снов)",
        f"• Среднее время бодрствования: {avg['avg_wb_str']} (норма: {avg['norm_wb_str']})",
        f"• Статус ритма: {trend['title']} ({trend['desc']})",
        f"",
        f"📅 Детализация по дням:"
    ]

    for d in weekly_summary["days"]:
        if d["total_sleep_min"] > 0:
            lines.append(
                f"• {d['day_name']} ({d['date_formatted']}): всего {d['total_sleep_str']} "
                f"[ночь {d['night_sleep_str']}, день {d['day_sleep_str']}, {d['naps_count']} снов, ср.ВБ {d['avg_wb_str']}]"
            )
        else:
            lines.append(f"• {d['day_name']} ({d['date_formatted']}): нет записей")

    lines.append("")
    lines.append("Создано в приложении «Трекер сна Севы»")
    return "\n".join(lines)

