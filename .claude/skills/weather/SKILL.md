---
name: weather
description: Weather queries and location management (天气/气温/温度/下雨/下雪/空气质量/AQI/穿什么). TRIGGER when user asks about weather conditions (天气怎么样/会下雨吗), temperature (多少度/冷不冷), air quality (空气质量/AQI/雾霾), clothing advice (穿什么/要带伞吗), travel weather (那边天气/出行天气), or multi-day forecasts (未来几天/这周天气). Also triggers when user shares a location message and you need to persist it. Used by garmin-health briefing for morning weather data. DO NOT TRIGGER for climate/geography knowledge questions — answer those directly.
---

# Weather

Real-time weather queries with location persistence. Zero API cost (free public APIs).

## Tool

```
python3 .claude/skills/weather/scripts/weather_ctl.py <command> [args]
```

## Commands

```bash
# Current weather (uses saved location, fallback Beijing)
weather_ctl.py current
weather_ctl.py current --location "Shanghai"
weather_ctl.py current --lat 39.967 --lng 116.535

# Multi-day forecast
weather_ctl.py forecast --days 3
weather_ctl.py forecast --days 7 --location "Tokyo"
weather_ctl.py forecast --days 3 --lat 45.5 --lng -73.6

# Location management
weather_ctl.py location --show
weather_ctl.py location --set --name "北京朝阳" --lat 39.967 --lng 116.535
```

## Data Sources

| Source | 用途 | 特点 |
|--------|------|------|
| wttr.in | 当前天气（主） | 免费无 key，中文天气描述，城市名查询 |
| Open-Meteo | 当前天气（备）+ 多日预报 | 免费无 key，坐标精准查询，全球覆盖 |
| Open-Meteo AQI | 空气质量 | PM2.5/PM10/US AQI，best-effort |

## Location Persistence

位置数据持久化在 `data/location.json`：

```json
{"name": "北京朝阳", "lat": 39.967, "lng": 116.535, "updated": "2026-03-11T10:00:00"}
```

**流程**：用户在飞书分享位置 → CC 确认 → 调用 `location --set` 持久化 → 后续查询自动使用

garmin-health 晨间简报调用 `weather_ctl.py current` 获取天气，自动读取持久化位置。

## Behavior Notes

- `--location` 城市名优先走 wttr.in（中文描述好），`--lat/--lng` 坐标优先走 Open-Meteo（精度高）
- 未设置位置且未传参时，默认 Beijing
- forecast 仅支持 Open-Meteo（wttr.in 无结构化多日数据）
- AQI 为 best-effort，失败不影响天气主数据
- 所有输出 JSON 格式，CC 直接解析
