import requests
import time
import json
from datetime import datetime, timedelta
import statistics

# ============ НАСТРОЙКИ (ИЗМЕНИ ЗДЕСЬ) ============
TELEGRAM_BOT_TOKEN = "8589700602:AAFgD991-TXG5i7mUEXaW-ATPUrSFfbbZJ4"  # Получи у @BotFather
TELEGRAM_CHAT_ID = "1951732896"      # Твой chat ID

# === ОБЩИЕ НАСТРОЙКИ ===
CHECK_INTERVAL = 120  # Проверка каждые 2 минуты (секунды)
# Примечание: Кэш дневных уровней обновляется каждые 2 часа (автоматически)

# === АНАЛИЗ ДНЕВНЫХ УРОВНЕЙ ===
DAILY_LOOKBACK_DAYS = 90        # Сколько дней анализировать (30/60/90/180)
DAILY_MIN_TOUCHES = 3           # Мин. касаний для уровня множественных касаний
DAILY_ZONE_TOLERANCE = 0.5      # Допуск зоны (%) для группировки уровней
REVERSAL_MIN_DAYS = 5           # Мин. дней тренда для определения разворота

# === ОКНО ПОДТВЕРЖДЕНИЙ ===
CONFIRMATION_WINDOW = 300       # За какой период смотреть OI/объём (секунды = 5 минут)

# === ДЕТЕКЦИЯ ПРОБОЯ ===
TOUCH_ZONE = 0.5                # Зона касания уровня (%) ±0.5%
BREAKOUT_MIN = 0.5              # Минимальный выход из уровня (%)
TOUCH_MEMORY = 1800             # Сколько помнить касание (секунды = 30 минут)

# === ПОДТВЕРЖДЕНИЯ ===
OI_MIN_CHANGE = 7               # OI >= X%
PRICE_MIN_CHANGE = 2            # Цена >= X%  
VOLUME_MIN_INCREASE = 50        # Объём >= X%

# === ЖЁСТКИЕ ФИЛЬТРЫ ===
REQUIRE_OI_INCREASE = True      # Обязателен рост OI (не падение)
REQUIRE_PRICE_MOVE = True       # Обязательно движение цены
REQUIRE_VOLUME_SPIKE = True     # Обязателен рост объёма
REQUIRE_DIRECTION_MATCH = True  # Направления совпадают

# === МИНИМАЛЬНЫЕ ТРЕБОВАНИЯ ===
MIN_VOLUME_24H = 10000000       # Мин. объём 24ч ($10M)

# === ГРАФИКИ ===
SEND_CHARTS = True              # Отправлять ли ссылки на графики
CHART_MODE = "link"             # "link" = ссылка на Binance

# === COOLDOWN ===
ALERT_COOLDOWN = 7200           # 2 часа между алертами на одну монету


class DailyBreakoutBot:
    def __init__(self):
        self.base_url = "https://fapi.binance.com"
        self.levels_cache = {}      # Кэш найденных уровней
        self.last_level_update = 0  # Время последнего обновления уровней
        self.sent_alerts = {}       # {symbol: timestamp}
        self.oi_history = {}        # История OI для окна подтверждений
        self.price_history = {}     # История цен
        self.volume_history = {}    # История объёмов
        self.level_touches = {}     # Касания уровней: {symbol: {level_price: timestamp}}
        
    def send_telegram_message(self, text):
        """Отправка сообщения в Telegram"""
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
        try:
            response = requests.post(url, data=data, timeout=10)
            return response.json()
        except Exception as e:
            print(f"Ошибка отправки в Telegram: {e}")
            return None
    
    def get_daily_candles(self, symbol, days):
        """Получение дневных свечей"""
        try:
            url = f"{self.base_url}/fapi/v1/klines"
            params = {
                'symbol': symbol,
                'interval': '1d',
                'limit': days
            }
            response = requests.get(url, params=params, timeout=10)
            data = response.json()
            
            # Проверка что API вернул список, а не ошибку
            if not isinstance(data, list):
                print(f"⚠️ API вернул не список для {symbol}: {data}")
                return []
            
            candles = []
            for candle in data:
                candles.append({
                    'time': candle[0],
                    'open': float(candle[1]),
                    'high': float(candle[2]),
                    'low': float(candle[3]),
                    'close': float(candle[4]),
                    'volume': float(candle[5])
                })
            return candles
        except Exception as e:
            print(f"Ошибка получения дневных свечей {symbol}: {e}")
            return []
    
    def detect_trend_reversal(self, candles):
        """Определение разворотов тренда"""
        reversals = []
        
        for i in range(REVERSAL_MIN_DAYS, len(candles) - REVERSAL_MIN_DAYS):
            # Проверяем тренд до точки
            trend_before = []
            for j in range(i - REVERSAL_MIN_DAYS, i):
                trend_before.append(candles[j]['close'])
            
            # Проверяем тренд после точки
            trend_after = []
            for j in range(i + 1, min(i + 1 + REVERSAL_MIN_DAYS, len(candles))):
                trend_after.append(candles[j]['close'])
            
            if len(trend_after) < REVERSAL_MIN_DAYS:
                continue
            
            # Определяем направление до и после
            # Тренд вниз если большинство свечей закрывается ниже предыдущей
            down_before = sum(1 for j in range(len(trend_before)-1) 
                            if trend_before[j+1] < trend_before[j])
            up_after = sum(1 for j in range(len(trend_after)-1) 
                          if trend_after[j+1] > trend_after[j])
            
            # Разворот ВНИЗ → ВВЕРХ (поддержка) - берём LOW свечи разворота
            if down_before >= REVERSAL_MIN_DAYS - 2 and up_after >= REVERSAL_MIN_DAYS - 2:
                reversals.append({
                    'price': candles[i]['low'],  # LOW дня разворота!
                    'type': 'support',
                    'method': 'reversal',
                    'date': datetime.fromtimestamp(candles[i]['time']/1000).strftime('%d.%m.%Y'),
                    'strength': 10,  # Максимальная сила для разворота
                    'touches': 1,
                    'index': i
                })
            
            # Тренд вверх если большинство свечей закрывается выше предыдущей
            up_before = sum(1 for j in range(len(trend_before)-1) 
                           if trend_before[j+1] > trend_before[j])
            down_after = sum(1 for j in range(len(trend_after)-1) 
                            if trend_after[j+1] < trend_after[j])
            
            # Разворот ВВЕРХ → ВНИЗ (сопротивление) - берём HIGH свечи разворота
            if up_before >= REVERSAL_MIN_DAYS - 2 and down_after >= REVERSAL_MIN_DAYS - 2:
                reversals.append({
                    'price': candles[i]['high'],  # HIGH дня разворота!
                    'type': 'resistance',
                    'method': 'reversal',
                    'date': datetime.fromtimestamp(candles[i]['time']/1000).strftime('%d.%m.%Y'),
                    'strength': 10,
                    'touches': 1,
                    'index': i
                })
        
        return reversals
    
    def detect_multiple_touches(self, candles):
        """Определение уровней по множественным касаниям (зеркальные уровни)"""
        all_touches = []
        
        # Собираем все касания High и Low с отскоками
        for i in range(1, len(candles) - 1):
            current = candles[i]
            next_candle = candles[i + 1]
            
            # HIGH касание с отскоком вниз (сопротивление)
            # Проверяем: следующая свеча закрылась ниже текущего High
            if next_candle['close'] < current['high']:
                all_touches.append({
                    'price': current['high'],
                    'type': 'resistance',
                    'touch_type': 'high',
                    'date': datetime.fromtimestamp(current['time']/1000).strftime('%d.%m.%Y'),
                    'index': i
                })
            
            # LOW касание с отскоком вверх (поддержка)
            # Проверяем: следующая свеча закрылась выше текущего Low
            if next_candle['close'] > current['low']:
                all_touches.append({
                    'price': current['low'],
                    'type': 'support',
                    'touch_type': 'low',
                    'date': datetime.fromtimestamp(current['time']/1000).strftime('%d.%m.%Y'),
                    'index': i
                })
        
        if not all_touches:
            return []
        
        # Группируем касания по зонам (зеркальные уровни)
        # High и Low могут быть в одной группе!
        grouped = []
        
        for touch in all_touches:
            added = False
            
            for group in grouped:
                # Средняя цена группы
                avg_price = statistics.mean([t['price'] for t in group])
                
                # Проверяем попадание в зону (±DAILY_ZONE_TOLERANCE%)
                if abs(touch['price'] - avg_price) / avg_price * 100 <= DAILY_ZONE_TOLERANCE:
                    group.append(touch)
                    added = True
                    break
            
            if not added:
                grouped.append([touch])
        
        # Фильтруем группы с достаточным количеством касаний
        levels = []
        
        for group in grouped:
            if len(group) >= DAILY_MIN_TOUCHES:
                # Средняя цена всех касаний (High + Low)
                avg_price = statistics.mean([t['price'] for t in group])
                
                # Считаем сколько High и сколько Low
                high_count = sum(1 for t in group if t['touch_type'] == 'high')
                low_count = sum(1 for t in group if t['touch_type'] == 'low')
                
                # Определяем основной тип (больше High или Low)
                if high_count > low_count:
                    level_type = 'resistance'
                elif low_count > high_count:
                    level_type = 'support'
                else:
                    level_type = 'mirror'  # Равное количество = зеркальный
                
                # Даты первых 5 касаний
                dates = [t['date'] for t in group[:5]]
                
                # Сила по количеству касаний (макс 8)
                strength = min(len(group) * 2, 8)
                
                levels.append({
                    'price': avg_price,
                    'type': level_type,
                    'method': 'touches',
                    'touches': len(group),
                    'high_touches': high_count,
                    'low_touches': low_count,
                    'dates': dates,
                    'strength': strength
                })
        
        return levels
    
    def find_levels(self, symbol):
        """Поиск всех уровней для символа"""
        print(f"  🔍 Анализ уровней {symbol}...")
        
        # Получаем дневные свечи
        candles = self.get_daily_candles(symbol, DAILY_LOOKBACK_DAYS)
        if len(candles) < REVERSAL_MIN_DAYS * 2:
            return []
        
        # Находим развороты тренда
        reversals = self.detect_trend_reversal(candles)
        print(f"    Разворотов найдено: {len(reversals)}")
        
        # Находим множественные касания
        touches = self.detect_multiple_touches(candles)
        print(f"    Касаний найдено: {len(touches)}")
        
        # Объединяем все уровни
        all_levels = reversals + touches
        
        # Группируем близкие уровни (могут совпадать разворот и касания)
        final_levels = []
        for level in all_levels:
            added = False
            for final in final_levels:
                if abs(level['price'] - final['price']) / final['price'] * 100 <= DAILY_ZONE_TOLERANCE:
                    # Объединяем - берём максимальную силу
                    if level['strength'] > final['strength']:
                        final.update(level)
                    added = True
                    break
            if not added:
                final_levels.append(level)
        
        # Сортируем по силе
        final_levels.sort(key=lambda x: x['strength'], reverse=True)
        
        return final_levels
    
    def get_current_price(self, symbol):
        """Получение текущей цены"""
        try:
            url = f"{self.base_url}/fapi/v1/ticker/price"
            params = {'symbol': symbol}
            response = requests.get(url, params=params, timeout=10)
            data = response.json()
            
            # Проверка что есть поле price
            if 'price' not in data:
                print(f"⚠️ Нет поля 'price' в ответе для {symbol}: {data}")
                return None
                
            return float(data['price'])
        except Exception as e:
            print(f"Ошибка получения цены {symbol}: {e}")
            return None
    
    def get_open_interest(self, symbol):
        """Получение Open Interest"""
        try:
            url = f"{self.base_url}/fapi/v1/openInterest"
            params = {'symbol': symbol}
            response = requests.get(url, params=params, timeout=10)
            data = response.json()
            
            # Проверка что есть поле openInterest
            if 'openInterest' not in data:
                print(f"⚠️ Нет поля 'openInterest' в ответе для {symbol}: {data}")
                return None
                
            return float(data['openInterest'])
        except Exception as e:
            print(f"Ошибка получения OI {symbol}: {e}")
            return None
    
    def get_24h_stats(self, symbol):
        """Получение статистики 24ч"""
        try:
            url = f"{self.base_url}/fapi/v1/ticker/24hr"
            params = {'symbol': symbol}
            response = requests.get(url, params=params, timeout=10)
            data = response.json()
            
            # Проверка что есть необходимые поля
            if 'quoteVolume' not in data or 'lastPrice' not in data:
                print(f"⚠️ Нет полей 'quoteVolume'/'lastPrice' в ответе для {symbol}: {data}")
                return None
                
            return {
                'volume': float(data['quoteVolume']),
                'price': float(data['lastPrice'])
            }
        except Exception as e:
            print(f"Ошибка получения 24h stats {symbol}: {e}")
            return None
    
    def check_confirmations(self, symbol, current_time):
        """Проверка подтверждений за окно CONFIRMATION_WINDOW"""
        confirmations = {
            'oi_change': 0,
            'price_change': 0,
            'volume_change': 0,
            'passes': True,
            'info': []
        }
        
        # OI изменение
        if symbol in self.oi_history and len(self.oi_history[symbol]) >= 2:
            recent_oi = [e for e in self.oi_history[symbol] 
                        if current_time - e['time'] <= CONFIRMATION_WINDOW]
            
            if len(recent_oi) >= 2:
                old_oi = recent_oi[0]['oi']
                new_oi = recent_oi[-1]['oi']
                
                if old_oi > 0:
                    oi_change = ((new_oi - old_oi) / old_oi) * 100
                    confirmations['oi_change'] = oi_change
                    
                    # Проверка фильтров OI
                    if REQUIRE_OI_INCREASE and oi_change < 0:
                        confirmations['passes'] = False
                        return confirmations
                    
                    if abs(oi_change) >= OI_MIN_CHANGE:
                        confirmations['info'].append(f"OI {oi_change:+.1f}%")
                    else:
                        if REQUIRE_OI_INCREASE or abs(oi_change) > 0:
                            confirmations['passes'] = False
                            return confirmations
        
        # Цена изменение
        if symbol in self.price_history and len(self.price_history[symbol]) >= 2:
            recent_price = [e for e in self.price_history[symbol]
                           if current_time - e['time'] <= CONFIRMATION_WINDOW]
            
            if len(recent_price) >= 2:
                old_price = recent_price[0]['price']
                new_price = recent_price[-1]['price']
                
                if old_price > 0:
                    price_change = ((new_price - old_price) / old_price) * 100
                    confirmations['price_change'] = price_change
                    
                    if REQUIRE_PRICE_MOVE and abs(price_change) >= PRICE_MIN_CHANGE:
                        confirmations['info'].append(f"Цена {price_change:+.1f}%")
                    elif REQUIRE_PRICE_MOVE:
                        confirmations['passes'] = False
                        return confirmations
        
        # Объём изменение
        if symbol in self.volume_history and len(self.volume_history[symbol]) >= 2:
            recent_vol = [e for e in self.volume_history[symbol]
                         if current_time - e['time'] <= CONFIRMATION_WINDOW]
            
            if len(recent_vol) >= 2:
                old_volumes = [e['volume'] for e in recent_vol[:len(recent_vol)//2]]
                new_volumes = [e['volume'] for e in recent_vol[len(recent_vol)//2:]]
                
                if old_volumes and new_volumes:
                    avg_old = statistics.mean(old_volumes)
                    avg_new = statistics.mean(new_volumes)
                    
                    if avg_old > 0:
                        vol_change = ((avg_new - avg_old) / avg_old) * 100
                        confirmations['volume_change'] = vol_change
                        
                        if REQUIRE_VOLUME_SPIKE and vol_change >= VOLUME_MIN_INCREASE:
                            confirmations['info'].append(f"Объём +{vol_change:.0f}%")
                        elif REQUIRE_VOLUME_SPIKE:
                            confirmations['passes'] = False
                            return confirmations
        
        # Проверка согласованности направлений
        if REQUIRE_DIRECTION_MATCH:
            if confirmations['oi_change'] != 0 and confirmations['price_change'] != 0:
                oi_dir = 1 if confirmations['oi_change'] > 0 else -1
                price_dir = 1 if confirmations['price_change'] > 0 else -1
                
                if oi_dir == price_dir:
                    direction = "вверх" if oi_dir > 0 else "вниз"
                    confirmations['info'].append(f"Направление согласовано ({direction})")
                else:
                    confirmations['passes'] = False
                    return confirmations
        
        return confirmations
    
    def get_chart_link(self, symbol):
        """Получение ссылки на график"""
        if CHART_MODE == "link":
            return f"https://www.binance.com/en/futures/{symbol}?type=um"
        return None
    
    def send_breakout_alert(self, alert_data):
        """Отправка алерта о пробое"""
        chart_link = self.get_chart_link(alert_data['symbol'])
        
        level = alert_data['level']
        confirmations = alert_data['confirmations']
        touch_time = alert_data.get('touch_time', 0)
        
        # Тип уровня и детали
        if level['method'] == 'reversal':
            level_type_text = f"РАЗВОРОТ ТРЕНДА ⭐⭐⭐\n   └─ Разворот: {level['date']} ({level['type']})\n   └─ Тренд изменился: {'вниз → вверх' if level['type'] == 'support' else 'вверх → вниз'}"
        else:
            dates_text = "\n      • ".join(level['dates'])
            high_count = level.get('high_touches', 0)
            low_count = level.get('low_touches', 0)
            
            # Определяем тип уровня по касаниям
            if level['type'] == 'mirror':
                level_name = "ЗЕРКАЛЬНЫЙ УРОВЕНЬ ⭐⭐⭐"
                level_desc = f"Работает как поддержка И сопротивление"
            elif high_count > low_count:
                level_name = "УРОВЕНЬ СОПРОТИВЛЕНИЯ ⭐⭐"
                level_desc = f"Преимущественно сопротивление"
            else:
                level_name = "УРОВЕНЬ ПОДДЕРЖКИ ⭐⭐"
                level_desc = f"Преимущественно поддержка"
            
            level_type_text = f"{level_name}\n   └─ {level_desc}\n   └─ Касаний: {level['touches']} ({high_count} High, {low_count} Low)\n   └─ Даты касаний:\n      • {dates_text}"
        
        # Подтверждения
        conf_text = "\n".join([f"• {c}" for c in confirmations['info']]) if confirmations['info'] else "• Все фильтры пройдены"
        
        # Детали пробоя
        breakout_dir = "ВВЕРХ" if alert_data['direction'] == 'up' else "ВНИЗ"
        breakout_emoji = "⚡" if alert_data['direction'] == 'up' else "📉"
        
        # Время с момента касания
        current_time = time.time()
        time_since_touch = int((current_time - touch_time) / 60)  # минуты
        touch_time_str = datetime.fromtimestamp(touch_time).strftime('%H:%M:%S')
        
        # Ближайшие уровни
        next_levels_text = ""
        if 'next_resistance' in alert_data:
            next_levels_text += f"\n⬆️ Сопротивление: ${alert_data['next_resistance']['price']:.2f} (+{alert_data['next_resistance']['distance']:.1f}%)"
            if alert_data['next_resistance'].get('method') == 'reversal':
                next_levels_text += " - разворот"
            else:
                next_levels_text += f" - {alert_data['next_resistance'].get('touches', 0)} касаний"
        
        if 'next_support' in alert_data:
            next_levels_text += f"\n⬇️ Поддержка: ${alert_data['next_support']['price']:.2f} ({alert_data['next_support']['distance']:.1f}%)"
            if alert_data['next_support'].get('method') == 'reversal':
                next_levels_text += " - разворот"
            else:
                next_levels_text += f" - {alert_data['next_support'].get('touches', 0)} касаний"
        
        # Рекомендации
        recommendations = ""
        if alert_data['direction'] == 'up' and 'next_resistance' in alert_data:
            stop_price = level['price'] * 0.99
            target_price = alert_data['next_resistance']['price']
            risk = abs(alert_data['current_price'] - stop_price)
            reward = abs(target_price - alert_data['current_price'])
            rr = reward / risk if risk > 0 else 0
            
            recommendations = f"""
💡 Рекомендации:
✅ Вход: ${alert_data['current_price']*0.999:.4f}-${alert_data['current_price']*1.001:.4f}
✅ Стоп: ${stop_price:.4f} (под уровнем)
✅ Цель: ${target_price:.4f} (следующее сопротивление)
✅ R/R: 1:{rr:.1f}"""
        
        elif alert_data['direction'] == 'down' and 'next_support' in alert_data:
            stop_price = level['price'] * 1.01
            target_price = alert_data['next_support']['price']
            risk = abs(stop_price - alert_data['current_price'])
            reward = abs(alert_data['current_price'] - target_price)
            rr = reward / risk if risk > 0 else 0
            
            recommendations = f"""
💡 Рекомендации:
✅ Вход: ${alert_data['current_price']*0.999:.4f}-${alert_data['current_price']*1.001:.4f}
✅ Стоп: ${stop_price:.4f} (над уровнем)
✅ Цель: ${target_price:.4f} (следующая поддержка)
✅ R/R: 1:{rr:.1f}"""
        
        message = f"""
🎯 <b>ПРОБОЙ ДНЕВНОГО УРОВНЯ</b>

💎 <b>Монета:</b> {alert_data['coin']}
📊 <b>Уровень:</b> ${level['price']:.4f}
📍 <b>Тип уровня:</b> {level_type_text}

{breakout_emoji} <b>ПРОБОЙ {breakout_dir}!</b>

🎯 <b>Детали пробоя:</b>
📍 Уровень: ${level['price']:.4f}
👉 Касание: {touch_time_str}
💰 Текущая цена: ${alert_data['current_price']:.4f}
📏 Дистанция: {alert_data['distance']:+.2f}% от уровня
⏱️ Время пробоя: {datetime.now().strftime('%H:%M:%S')} ({datetime.now().strftime('%d %b %Y')})
🕐 Через {time_since_touch} мин после касания

✅ <b>Подтверждения (за {CONFIRMATION_WINDOW//60} минут):</b>
{conf_text}

📊 <b>Контекст уровня:</b>
🔹 Найден методом: {level['method']}
🔹 Период анализа: {DAILY_LOOKBACK_DAYS} дней
🔹 Сила: {"ВЫСОКАЯ" if level['strength'] >= 8 else "СРЕДНЯЯ"}
{next_levels_text}
{recommendations}

⏰ <b>Время алерта:</b> {datetime.now().strftime('%H:%M:%S')}
"""
        
        if SEND_CHARTS and chart_link:
            message += f"\n📊 <a href='{chart_link}'>График на Binance (1D)</a>"
        
        self.send_telegram_message(message)

    
    def monitor_breakouts(self):
        """Мониторинг пробоев уровней"""
        current_time = time.time()
        
        # Обновляем уровни каждые 2 часа (дневные уровни редко меняются)
        if current_time - self.last_level_update > 7200 or not self.levels_cache:
            print("\n🔄 Обновление уровней...")
            self.update_levels_cache()
            self.last_level_update = current_time
        
        print(f"\n{'='*60}")
        print(f"🎯 МОНИТОРИНГ ПРОБОЕВ - {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'='*60}")
        
        alerts = []
        checked = 0
        skipped_cooldown = 0
        
        for symbol, levels in self.levels_cache.items():
            if not levels:
                continue
            
            # Проверка cooldown
            if symbol in self.sent_alerts:
                time_passed = current_time - self.sent_alerts[symbol]
                if time_passed < ALERT_COOLDOWN:
                    skipped_cooldown += 1
                    continue
            
            # Получаем текущие данные
            current_price = self.get_current_price(symbol)
            if not current_price:
                continue
            
            current_oi = self.get_open_interest(symbol)
            stats = self.get_24h_stats(symbol)
            
            # Обновляем историю
            if symbol not in self.oi_history:
                self.oi_history[symbol] = []
            if symbol not in self.price_history:
                self.price_history[symbol] = []
            if symbol not in self.volume_history:
                self.volume_history[symbol] = []
            
            self.oi_history[symbol].append({'time': current_time, 'oi': current_oi if current_oi else 0})
            self.price_history[symbol].append({'time': current_time, 'price': current_price})
            if stats:
                self.volume_history[symbol].append({'time': current_time, 'volume': stats['volume']})
            
            # Очищаем старую историю
            cutoff_time = current_time - CONFIRMATION_WINDOW * 2
            self.oi_history[symbol] = [e for e in self.oi_history[symbol] if e['time'] > cutoff_time]
            self.price_history[symbol] = [e for e in self.price_history[symbol] if e['time'] > cutoff_time]
            self.volume_history[symbol] = [e for e in self.volume_history[symbol] if e['time'] > cutoff_time]
            
            # Проверяем касания и пробои уровней
            for level in levels[:5]:  # Топ-5 уровней
                level_price = level['price']
                
                # Инициализация отслеживания касаний для символа
                if symbol not in self.level_touches:
                    self.level_touches[symbol] = {}
                
                # Зона касания (±TOUCH_ZONE%)
                touch_zone_high = level_price * (1 + TOUCH_ZONE / 100)
                touch_zone_low = level_price * (1 - TOUCH_ZONE / 100)
                
                # Зона выхода (для пробоя)
                breakout_up_zone = level_price * (1 + BREAKOUT_MIN / 100)
                breakout_down_zone = level_price * (1 - BREAKOUT_MIN / 100)
                
                # Проверяем: цена в зоне касания?
                if touch_zone_low <= current_price <= touch_zone_high:
                    # Запоминаем касание
                    level_key = f"{level_price:.8f}"
                    if level_key not in self.level_touches[symbol]:
                        self.level_touches[symbol][level_key] = current_time
                        print(f"  👉 {symbol.replace('USDT', '')}: Касание уровня ${level_price:.4f}")
                
                # Проверяем: было ли недавнее касание этого уровня?
                level_key = f"{level_price:.8f}"
                if level_key in self.level_touches[symbol]:
                    touch_time = self.level_touches[symbol][level_key]
                    time_since_touch = current_time - touch_time
                    
                    # Касание ещё актуально?
                    if time_since_touch <= TOUCH_MEMORY:
                        direction = None
                        distance = 0
                        
                        # Пробой вверх (вышла выше зоны касания)
                        if current_price > breakout_up_zone:
                            direction = 'up'
                            distance = ((current_price - level_price) / level_price) * 100
                        
                        # Пробой вниз (вышла ниже зоны касания)
                        elif current_price < breakout_down_zone:
                            direction = 'down'
                            distance = ((current_price - level_price) / level_price) * 100
                        
                        if direction:
                            # Удаляем касание (использовано)
                            del self.level_touches[symbol][level_key]
                            
                            # Проверяем подтверждения
                            confirmations = self.check_confirmations(symbol, current_time)
                            
                            if confirmations['passes']:
                                coin = symbol.replace('USDT', '')
                                
                                # Находим ближайшие уровни
                                next_resistance = None
                                next_support = None
                                
                                for other_level in levels:
                                    if other_level['price'] > current_price and not next_resistance:
                                        next_resistance = other_level.copy()
                                        next_resistance['distance'] = ((other_level['price'] - current_price) / current_price) * 100
                                    elif other_level['price'] < current_price and not next_support:
                                        next_support = other_level.copy()
                                        next_support['distance'] = ((other_level['price'] - current_price) / current_price) * 100
                                
                                alert = {
                                    'coin': coin,
                                    'symbol': symbol,
                                    'level': level,
                                    'current_price': current_price,
                                    'direction': direction,
                                    'distance': distance,
                                    'touch_time': touch_time,
                                    'confirmations': confirmations,
                                    'next_resistance': next_resistance,
                                    'next_support': next_support
                                }
                                
                                alerts.append(alert)
                                print(f"  🎯 {coin}: Пробой {level_price:.4f} → {direction.upper()}")
                                break  # Один алерт на монету
                    else:
                        # Касание устарело, удаляем
                        del self.level_touches[symbol][level_key]
                
                # Очищаем старые касания
                expired_keys = [k for k, t in self.level_touches[symbol].items() 
                               if current_time - t > TOUCH_MEMORY]
                for k in expired_keys:
                    del self.level_touches[symbol][k]
            
            checked += 1
            if checked % 10 == 0:
                time.sleep(0.5)
        
        if skipped_cooldown > 0:
            print(f"⏭️  Пропущено {skipped_cooldown} монет (cooldown)")
        print(f"✅ Проверено {checked} монет, найдено {len(alerts)} пробоев")
        
        # Отправляем алерты
        for alert in alerts:
            self.send_breakout_alert(alert)
            self.sent_alerts[alert['symbol']] = current_time
            time.sleep(1)
        
        return alerts
    
    def update_levels_cache(self):
        """Обновление кэша уровней для всех монет"""
        # Получаем список монет
        try:
            url = f"{self.base_url}/fapi/v1/ticker/24hr"
            response = requests.get(url, timeout=10)
            data = response.json()
            
            # ИСПРАВЛЕНИЕ: Проверяем что data это список
            if not isinstance(data, list):
                print(f"❌ Ошибка: API вернул неожиданный формат: {type(data)}")
                return
            
            # Фильтруем USDT пары с достаточным объёмом
            symbols = []
            for t in data:
                try:
                    if isinstance(t, dict) and t.get('symbol', '').endswith('USDT'):
                        quote_vol = float(t.get('quoteVolume', 0))
                        if quote_vol >= MIN_VOLUME_24H:
                            symbols.append(t['symbol'])
                except (ValueError, KeyError):
                    continue  # Пропускаем проблемные записи
            
            print(f"Найдено {len(symbols)} монет для анализа")
            
            # Анализируем каждую монету
            for i, symbol in enumerate(symbols[:50]):  # Топ-50 по объёму
                levels = self.find_levels(symbol)
                self.levels_cache[symbol] = levels
                
                if (i + 1) % 10 == 0:
                    print(f"  Обработано {i+1}/{len(symbols[:50])}")
                    time.sleep(1)
            
            print(f"✅ Кэш уровней обновлён для {len(self.levels_cache)} монет")
            
        except Exception as e:
            print(f"❌ Ошибка обновления кэша: {e}")
    
    def run(self):
        """Запуск бота"""
        print("\n" + "="*60)
        print("🤖 DAILY BREAKOUT BOT v1.0")
        print("="*60)
        print(f"⚙️  Период анализа: {DAILY_LOOKBACK_DAYS} дней")
        print(f"⚙️  Окно подтверждений: {CONFIRMATION_WINDOW//60} минут")
        print(f"⏱️  Интервал проверки: {CHECK_INTERVAL} секунд")
        print(f"🎯 Зона касания: ±{TOUCH_ZONE}%")
        print(f"🎯 Минимальный пробой: {BREAKOUT_MIN}%")
        print(f"⏰ Память касания: {TOUCH_MEMORY//60} минут")
        print(f"📊 Фильтры: OI {OI_MIN_CHANGE}%, Цена {PRICE_MIN_CHANGE}%, Объём {VOLUME_MIN_INCREASE}%")
        print("="*60 + "\n")
        
        # Стартовое сообщение
        start_msg = (
            "🚀 <b>Daily Breakout Bot запущен!</b>\n\n"
            f"⚙️ Анализ дневных уровней за {DAILY_LOOKBACK_DAYS} дней\n"
            f"⏱️ Проверка каждые {CHECK_INTERVAL//60} минуты\n"
            f"🎯 Касание: ±{TOUCH_ZONE}% от уровня\n"
            f"🎯 Пробой: {BREAKOUT_MIN}% выход из зоны\n"
            f"📊 Окно подтверждений: {CONFIRMATION_WINDOW//60} минут\n\n"
            f"🕐 Запущен: {datetime.now().strftime('%H:%M:%S')}"
        )
        self.send_telegram_message(start_msg)
        
        try:
            while True:
                try:
                    alerts = self.monitor_breakouts()
                    
                    if not alerts:
                        print("\n✨ Пробоев не обнаружено")
                    
                    print(f"\n⏳ Следующая проверка через {CHECK_INTERVAL} секунд...")
                    print("="*60 + "\n")
                    time.sleep(CHECK_INTERVAL)
                    
                except Exception as e:
                    print(f"Ошибка в цикле мониторинга: {e}")
                    time.sleep(60)
                    
        except KeyboardInterrupt:
            print("\n\n👋 Бот остановлен")
            self.send_telegram_message("⏸️ Daily Breakout Bot остановлен")


if __name__ == "__main__":
    bot = DailyBreakoutBot()
    bot.run()
