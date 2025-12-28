import requests
import time
import statistics
from datetime import datetime, timedelta

# ======================  ВАШИ НАСТРОЙКИ  ======================
TELEGRAM_BOT_TOKEN = "8589700602:AAFgD991-TXG5i7mUEXaW-ATPUrSFfbbZJ4"  # от @BotFather
TELEGRAM_CHAT_ID   = "1951732896"    # ваш chat_id
# =================================================================

CHECK_INTERVAL          = 120
DAILY_LOOKBACK_DAYS     = 90
DAILY_MIN_TOUCHES       = 3
DAILY_ZONE_TOLERANCE    = 0.5
REVERSAL_MIN_DAYS       = 5
CONFIRMATION_WINDOW     = 300
TOUCH_ZONE              = 0.5
BREAKOUT_MIN            = 0.5
TOUCH_MEMORY            = 1800
OI_MIN_CHANGE           = 7
PRICE_MIN_CHANGE        = 2
VOLUME_MIN_INCREASE     = 50
REQUIRE_OI_INCREASE     = True
REQUIRE_PRICE_MOVE      = True
REQUIRE_VOLUME_SPIKE    = True
REQUIRE_DIRECTION_MATCH = True
MIN_VOLUME_24H          = 10_000_000
SEND_CHARTS             = True
CHART_MODE              = "link"
ALERT_COOLDOWN          = 7200


class DailyBreakoutBot:
    def __init__(self):
        # 1. убран пробел в конце
        self.base_url = "https://fapi.binance.com"
        self.levels_cache = {}
        self.last_level_update = 0
        self.sent_alerts = {}
        self.oi_history = {}
        self.price_history = {}
        self.volume_history = {}
        self.level_touches = {}

    # 2. убран пробел в URL
    def send_telegram_message(self, text: str):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
        try:
            r = requests.post(url, data=payload, timeout=10)
            return r.json()
        except Exception as e:
            print(f"❌ Telegram error: {e}")
            return None

    # ---------- свечи ----------
    def get_daily_candles(self, symbol, days):
        url = f"{self.base_url}/fapi/v1/klines"
        params = {"symbol": symbol, "interval": "1d", "limit": days}
        try:
            r = requests.get(url, params=params, timeout=10)
            data = r.json()
            if not isinstance(data, list):
                print(f"⚠️ klines: not a list for {symbol}")
                return []
            return [{"time": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                     "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])}
                    for c in data]
        except Exception as e:
            print(f"❌ get_daily_candles {symbol}: {e}")
            return []

    # ---------- развороты ----------
    def detect_trend_reversal(self, candles):
        reversals = []
        for i in range(REVERSAL_MIN_DAYS, len(candles) - REVERSAL_MIN_DAYS):
            before = [candles[j]["close"] for j in range(i - REVERSAL_MIN_DAYS, i)]
            after  = [candles[j]["close"] for j in range(i + 1, i + 1 + REVERSAL_MIN_DAYS)]
            down_before = sum(before[j + 1] < before[j] for j in range(len(before) - 1))
            up_after    = sum(after[j + 1]  > after[j]  for j in range(len(after)  - 1))
            if down_before >= REVERSAL_MIN_DAYS - 2 and up_after >= REVERSAL_MIN_DAYS - 2:
                reversals.append({
                    "price": candles[i]["low"], "type": "support", "method": "reversal",
                    "date": datetime.fromtimestamp(candles[i]["time"] / 1000).strftime("%d.%m.%Y"),
                    "strength": 10, "touches": 1, "index": i
                })
            up_before   = sum(before[j + 1] > before[j] for j in range(len(before) - 1))
            down_after  = sum(after[j + 1]  < after[j]  for j in range(len(after)  - 1))
            if up_before >= REVERSAL_MIN_DAYS - 2 and down_after >= REVERSAL_MIN_DAYS - 2:
                reversals.append({
                    "price": candles[i]["high"], "type": "resistance", "method": "reversal",
                    "date": datetime.fromtimestamp(candles[i]["time"] / 1000).strftime("%d.%m.%Y"),
                    "strength": 10, "touches": 1, "index": i
                })
        return reversals

    # ---------- множественные касания ----------
    def detect_multiple_touches(self, candles):
        touches = []
        for i in range(1, len(candles) - 1):
            cur, nxt = candles[i], candles[i + 1]
            if nxt["close"] < cur["high"]:
                touches.append({"price": cur["high"], "type": "resistance", "touch_type": "high",
                                "date": datetime.fromtimestamp(cur["time"] / 1000).strftime("%d.%m.%Y"), "index": i})
            if nxt["close"] > cur["low"]:
                touches.append({"price": cur["low"], "type": "support", "touch_type": "low",
                                "date": datetime.fromtimestamp(cur["time"] / 1000).strftime("%d.%m.%Y"), "index": i})
        if not touches:
            return []
        grouped = []
        for t in touches:
            added = False
            for g in grouped:
                avg = statistics.mean([x["price"] for x in g])
                if abs(t["price"] - avg) / avg * 100 <= DAILY_ZONE_TOLERANCE:
                    g.append(t); added = True; break
            if not added:
                grouped.append([t])
        levels = []
        for g in grouped:
            if len(g) >= DAILY_MIN_TOUCHES:
                avg_price = statistics.mean([x["price"] for x in g])
                high_cnt = sum(1 for x in g if x["touch_type"] == "high")
                low_cnt  = sum(1 for x in g if x["touch_type"] == "low")
                lvl_type = "resistance" if high_cnt > low_cnt else "support" if low_cnt > high_cnt else "mirror"
                levels.append({
                    "price": avg_price, "type": lvl_type, "method": "touches",
                    "touches": len(g), "high_touches": high_cnt, "low_touches": low_cnt,
                    "dates": [x["date"] for x in g][:5], "strength": min(len(g) * 2, 8)
                })
        return levels

    # ---------- объединённый поиск уровней ----------
    def find_levels(self, symbol):
        print(f"  🔍 Анализ уровней {symbol}...")
        candles = self.get_daily_candles(symbol, DAILY_LOOKBACK_DAYS)
        if len(candles) < REVERSAL_MIN_DAYS * 2:
            return []
        reversals = self.detect_trend_reversal(candles)
        touches   = self.detect_multiple_touches(candles)
        all_levels = reversals + touches
        final = []
        for lvl in all_levels:
            merged = False
            for f in final:
                if abs(lvl["price"] - f["price"]) / f["price"] * 100 <= DAILY_ZONE_TOLERANCE:
                    if lvl["strength"] > f["strength"]:
                        f.update(lvl)
                    merged = True; break
            if not merged:
                final.append(lvl)
        final.sort(key=lambda x: x["strength"], reverse=True)
        return final

    # ---------- текущие данные ----------
    def get_current_price(self, symbol):
        try:
            r = requests.get(f"{self.base_url}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=10)
            data = r.json()
            return float(data["price"])
        except Exception as e:
            print(f"❌ price {symbol}: {e}")
            return None

    def get_open_interest(self, symbol):
        try:
            r = requests.get(f"{self.base_url}/fapi/v1/openInterest", params={"symbol": symbol}, timeout=10)
            data = r.json()
            return float(data["openInterest"])
        except Exception as e:
            print(f"❌ OI {symbol}: {e}")
            return None

    def get_24h_stats(self, symbol):
        try:
            r = requests.get(f"{self.base_url}/fapi/v1/ticker/24hr", params={"symbol": symbol}, timeout=10)
            data = r.json()
            return {"volume": float(data["quoteVolume"]), "price": float(data["lastPrice"])}
        except Exception as e:
            print(f"❌ 24h stats {symbol}: {e}")
            return None

    # ---------- подтверждения ----------
    def check_confirmations(self, symbol, cur_time):
        conf = {"oi_change": 0, "price_change": 0, "volume_change": 0, "passes": True, "info": []}
        # OI
        if symbol in self.oi_history and len(self.oi_history[symbol]) >= 2:
            recent = [e for e in self.oi_history[symbol] if cur_time - e["time"] <= CONFIRMATION_WINDOW]
            if len(recent) >= 2:
                old, new = recent[0]["oi"], recent[-1]["oi"]
                if old > 0:
                    ch = ((new - old) / old) * 100
                    conf["oi_change"] = ch
                    if REQUIRE_OI_INCREASE and ch < 0:
                        conf["passes"] = False; return conf
                    if abs(ch) >= OI_MIN_CHANGE:
                        conf["info"].append(f"OI {ch:+.1f}%")
                    elif REQUIRE_OI_INCREASE:
                        conf["passes"] = False; return conf
        # Price
        if symbol in self.price_history and len(self.price_history[symbol]) >= 2:
            recent = [e for e in self.price_history[symbol] if cur_time - e["time"] <= CONFIRMATION_WINDOW]
            if len(recent) >= 2:
                old, new = recent[0]["price"], recent[-1]["price"]
                if old > 0:
                    ch = ((new - old) / old) * 100
                    conf["price_change"] = ch
                    if REQUIRE_PRICE_MOVE and abs(ch) >= PRICE_MIN_CHANGE:
                        conf["info"].append(f"Цена {ch:+.1f}%")
                    elif REQUIRE_PRICE_MOVE:
                        conf["passes"] = False; return conf
        # Volume
        if symbol in self.volume_history and len(self.volume_history[symbol]) >= 2:
            recent = [e for e in self.volume_history[symbol] if cur_time - e["time"] <= CONFIRMATION_WINDOW]
            if len(recent) >= 2:
                half = len(recent) // 2
                old_vol = statistics.mean([e["volume"] for e in recent[:half]])
                new_vol = statistics.mean([e["volume"] for e in recent[half:]])
                if old_vol > 0:
                    ch = ((new_vol - old_vol) / old_vol) * 100
                    conf["volume_change"] = ch
                    if REQUIRE_VOLUME_SPIKE and ch >= VOLUME_MIN_INCREASE:
                        conf["info"].append(f"Объём +{ch:.0f}%")
                    elif REQUIRE_VOLUME_SPIKE:
                        conf["passes"] = False; return conf
        # Direction match
        if REQUIRE_DIRECTION_MATCH and conf["oi_change"] != 0 and conf["price_change"] != 0:
            oi_dir = 1 if conf["oi_change"] > 0 else -1
            price_dir = 1 if conf["price_change"] > 0 else -1
            if oi_dir == price_dir:
                conf["info"].append(f"Направление согласовано ({'вверх' if oi_dir>0 else 'вниз'})")
            else:
                conf["passes"] = False
        return conf

    # ---------- ссылка на график ----------
    def get_chart_link(self, symbol):
        if CHART_MODE == "link":
            return f"https://www.binance.com/en/futures/{symbol}?type=um"
        return None

    # ---------- полный красивый алерт ----------
    def send_breakout_alert(self, alert_data):
        chart_link = self.get_chart_link(alert_data['symbol'])
        level = alert_data['level']
        confirmations = alert_data['confirmations']
        touch_time = alert_data.get('touch_time', 0)
        current_time = time.time()
        time_since_touch = int((current_time - touch_time) / 60)
        touch_time_str = datetime.fromtimestamp(touch_time).strftime('%H:%M:%S')

        # ---- описание уровня ----
        if level['method'] == 'reversal':
            level_type_text = (
                f"РАЗВОРОТ ТРЕНДА ⭐⭐⭐\n"
                f"   └─ Разворот: {level['date']} ({level['type']})\n"
                f"   └─ Тренд изменился: {'вниз → вверх' if level['type'] == 'support' else 'вверх → вниз'}"
            )
        else:
            dates_text = "\n      • ".join(level['dates'])
            high_count = level.get('high_touches', 0)
            low_count  = level.get('low_touches', 0)
            if level['type'] == 'mirror':
                level_name, level_desc = "ЗЕРКАЛЬНЫЙ УРОВЕНЬ ⭐⭐⭐", "Работает как поддержка И сопротивление"
            elif high_count > low_count:
                level_name, level_desc = "УРОВЕНЬ СОПРОТИВЛЕНИЯ ⭐⭐", "Преимущественно сопротивление"
            else:
                level_name, level_desc = "УРОВЕНЬ ПОДДЕРЖКИ ⭐⭐", "Преимущественно поддержка"
            level_type_text = (
                f"{level_name}\n"
                f"   └─ {level_desc}\n"
                f"   └─ Касаний: {level['touches']} ({high_count} High, {low_count} Low)\n"
                f"   └─ Даты касаний:\n      • {dates_text}"
            )

        conf_text = "\n".join([f"• {c}" for c in confirmations['info']]) if confirmations['info'] else "• Все фильтры пройдены"
        breakout_dir = "ВВЕРХ" if alert_data['direction'] == 'up' else "ВНИЗ"
        breakout_emoji = "⚡" if alert_data['direction'] == 'up' else "📉"

        # ---- ближайшие уровни ----
        next_levels_text = ""
        if 'next_resistance' in alert_data:
            nr = alert_data['next_resistance']
            next_levels_text += f"\n⬆️ Сопротивление: ${nr['price']:.2f} (+{nr['distance']:.1f}%)"
            next_levels_text += " - разворот" if nr.get('method') == 'reversal' else f" - {nr.get('touches', 0)} касаний"
        if 'next_support' in alert_data:
            ns = alert_data['next_support']
            next_levels_text += f"\n⬇️ Поддержка: ${ns['price']:.2f} ({ns['distance']:.1f}%)"
            next_levels_text += " - разворот" if ns.get('method') == 'reversal' else f" - {ns.get('touches', 0)} касаний"

        # ---- рекомендации R/R ----
        recommendations = ""
        current_price = alert_data['current_price']
        if alert_data['direction'] == 'up' and 'next_resistance' in alert_data:
            stop_price = level['price'] * 0.99
            target_price = alert_data['next_resistance']['price']
            risk, reward = abs(current_price - stop_price), abs(target_price - current_price)
            rr = reward / risk if risk > 0 else 0
            recommendations = f"""
💡 Рекомендации:
✅ Вход: ${current_price*0.999:.4f}-${current_price*1.001:.4f}
✅ Стоп: ${stop_price:.4f} (под уровнем)
✅ Цель: ${target_price:.4f} (следующее сопротивление)
✅ R/R: 1:{rr:.1f}"""
        elif alert_data['direction'] == 'down' and 'next_support' in alert_data:
            stop_price = level['price'] * 1.01
            target_price = alert_data['next_support']['price']
            risk, reward = abs(stop_price - current_price), abs(current_price - target_price)
            rr = reward / risk if risk > 0 else 0
            recommendations = f"""
💡 Рекомендации:
✅ Вход: ${current_price*0.999:.4f}-${current_price*1.001:.4f}
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
💰 Текущая цена: ${current_price:.4f}
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

    # ---------- мониторинг ----------
    def monitor_breakouts(self):
        cur_time = time.time()
        if cur_time - self.last_level_update > 7200 or not self.levels_cache:
            self.update_levels_cache()
            self.last_level_update = cur_time
        print(f"\n{'='*60}\n🎯 МОНИТОРИНГ ПРОБОЕВ - {datetime.now().strftime('%H:%M:%S')}\n{'='*60}")
        alerts, checked, skipped_cd = [], 0, 0
        for symbol, levels in self.levels_cache.items():
            if not levels:
                continue
            if symbol in self.sent_alerts and cur_time - self.sent_alerts[symbol] < ALERT_COOLDOWN:
                skipped_cd += 1
                continue
            price = self.get_current_price(symbol)
            if price is None:
                continue
            oi    = self.get_open_interest(symbol) or 0
            stats = self.get_24h_stats(symbol)
            # история
            for d, lst in ((self.oi_history,   {"time": cur_time, "oi": oi}),
                           (self.price_history,{"time": cur_time, "price": price}),
                           (self.volume_history,{"time": cur_time, "volume": stats["volume"] if stats else 0})):
                if symbol not in d:
                    d[symbol] = []
                d[symbol].append(lst)
                cutoff = cur_time - CONFIRMATION_WINDOW * 2
                d[symbol] = [e for e in d[symbol] if e["time"] > cutoff]
            # касания / пробои
            for level in levels[:5]:
                lp = level["price"]
                touch_high = lp * (1 + TOUCH_ZONE / 100)
                touch_low  = lp * (1 - TOUCH_ZONE / 100)
                brk_up     = lp * (1 + BREAKOUT_MIN / 100)
                brk_down   = lp * (1 - BREAKOUT_MIN / 100)
                if symbol not in self.level_touches:
                    self.level_touches[symbol] = {}
                key = f"{lp:.8f}"
                # касание
                if touch_low <= price <= touch_high:
                    self.level_touches[symbol][key] = cur_time
                # пробой
                if key in self.level_touches[symbol]:
                    touch_t = self.level_touches[symbol][key]
                    if cur_time - touch_t <= TOUCH_MEMORY:
                        direction = None
                        if price > brk_up:
                            direction = "up"
                        elif price < brk_down:
                            direction = "down"
                        if direction:
                            del self.level_touches[symbol][key]
                            conf = self.check_confirmations(symbol, cur_time)
                            if conf["passes"]:
                                # ближайшие уровни
                                next_res, next_sup = None, None
                                for ol in levels:
                                    if ol["price"] > price and not next_res:
                                        next_res = ol.copy()
                                        next_res["distance"] = ((ol["price"] - price) / price) * 100
                                    elif ol["price"] < price and not next_sup:
                                        next_sup = ol.copy()
                                        next_sup["distance"] = ((ol["price"] - price) / price) * 100
                                alerts.append({
                                    "coin": symbol.replace("USDT", ""),
                                    "symbol": symbol,
                                    "level": level,
                                    "current_price": price,
                                    "direction": direction,
                                    "distance": ((price - lp) / lp) * 100,
                                    "touch_time": touch_t,
                                    "confirmations": conf,
                                    "next_resistance": next_res,
                                    "next_support": next_sup
                                })
                                print(f"  🎯 {symbol}: Пробой {lp:.4f} → {direction.upper()}")
                                break  # один алерт на монету
                    else:
                        del self.level_touches[symbol][key]
            checked += 1
            if checked % 10 == 0:
                time.sleep(0.5)
        if skipped_cd:
            print(f"⏭️ Пропущено {skipped_cd} монет (cooldown)")
        print(f"✅ Проверено {checked} монет, найдено {len(alerts)} пробоев")
        for a in alerts:
            self.send_breakout_alert(a)
            self.sent_alerts[a["symbol"]] = cur_time
            time.sleep(1)
        return alerts

    # ---------- обновление кэша ----------
    def update_levels_cache(self):
        print("\n🔄 Обновление уровней...")
        try:
            r = requests.get(f"{self.base_url}/fapi/v1/ticker/24hr", timeout=10)
            data = r.json()
            if not isinstance(data, list):
                print(f"❌ ticker/24hr вернул не список: {type(data)}")
                return
            symbols = []
            for t in data:
                try:
                    if isinstance(t, dict) and t.get("symbol", "").endswith("USDT"):
                        vol = float(t.get("quoteVolume", 0))
                        if vol >= MIN_VOLUME_24H:
                            symbols.append(t["symbol"])
                except (ValueError, KeyError):
                    continue
            print(f"Найдено {len(symbols)} монет для анализа")
            for i, s in enumerate(symbols[:50]):
                self.levels_cache[s] = self.find_levels(s)
                if (i + 1) % 10 == 0:
                    print(f"  Обработано {i+1}/50")
                    time.sleep(1)
            print(f"✅ Кэш уровней обновлён для {len(self.levels_cache)} монет")
        except Exception as e:
            print(f"❌ Ошибка обновления кэша: {e}")

    # ---------- запуск ----------
    def run(self):
        print("\n" + "="*60 + "\n🤖 DAILY BREAKOUT BOT v1.1\n" + "="*60)
        self.send_telegram_message("🚀 <b>Daily Breakout Bot запущен!</b>")
        try:
            while True:
                try:
                    self.monitor_breakouts()
                    print(f"\n⏳ Следующая проверка через {CHECK_INTERVAL} секунд...\n" + "="*60 + "\n")
                    time.sleep(CHECK_INTERVAL)
                except Exception as e:
                    print(f"Ошибка в цикле: {e}")
                    time.sleep(60)
        except KeyboardInterrupt:
            print("\n👋 Бот остановлен")
            self.send_telegram_message("⏸️ Daily Breakout Bot остановлен")


if __name__ == "__main__":
    DailyBreakoutBot().run()
