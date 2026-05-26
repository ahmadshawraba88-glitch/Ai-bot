import asyncio
import aiohttp
import json
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ========== CONFIG ==========
TELEGRAM_TOKEN = "8613392614:AAGWSNJOfuX_5SKLXhiy8S8Z7q36WIin5Js"
TD_KEY         = "b8bbfc1519a247058d7ba780db2ac435"
ANTHROPIC_KEY  = "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"
TD_BASE        = "https://api.twelvedata.com"
CLAUDE_URL     = "https://api.anthropic.com/v1/messages"
SCAN_EVERY     = 180
MIN_CONFIDENCE = 75

LEBANON_TZ = timezone(timedelta(hours=3))

CURRENCIES = {
    "AUD_CAD": "AUD/CAD", "AUD_CHF": "AUD/CHF",
    "AUD_JPY": "AUD/JPY", "AUD_USD": "AUD/USD",
    "CAD_JPY": "CAD/JPY", "CAD_CHF": "CAD/CHF",
    "CHF_JPY": "CHF/JPY", "EUR_AUD": "EUR/AUD",
    "EUR_CAD": "EUR/CAD", "EUR_CHF": "EUR/CHF",
    "EUR_JPY": "EUR/JPY", "EUR_USD": "EUR/USD",
    "USD_CAD": "USD/CAD", "USD_CHF": "USD/CHF",
    "USD_JPY": "USD/JPY",
}

# ========== TIME FILTER ==========

def is_trading_time():
    now     = datetime.now(LEBANON_TZ)
    weekday = now.weekday()
    hour    = now.hour
    if weekday >= 5:
        return False
    return (11 <= hour < 17) or (16 <= hour < 21)

# ========== TWELVE DATA ==========

async def get(session, endpoint, symbol, interval, extra=""):
    url = f"{TD_BASE}/{endpoint}?symbol={symbol}&interval={interval}&apikey={TD_KEY}{extra}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            return await r.json()
    except:
        return {}

async def fetch_data(symbol: str, interval: str) -> dict:
    async with aiohttp.ClientSession() as session:
        candles = await get(session, "time_series", symbol, interval, "&outputsize=10")
        await asyncio.sleep(0.3)
        rsi     = await get(session, "rsi",  symbol, interval, "&outputsize=2")
        await asyncio.sleep(0.3)
        macd    = await get(session, "macd", symbol, interval, "&outputsize=2")
        await asyncio.sleep(0.3)
        ema50   = await get(session, "ema",  symbol, interval, "&time_period=50&outputsize=2")
    return {"candles": candles, "rsi": rsi, "macd": macd, "ema50": ema50}

def safe(data, *keys):
    try:
        v = data
        for k in keys:
            v = v[k]
        return float(v)
    except:
        return None

def prepare_data(raw: dict, symbol: str, interval: str) -> dict:
    """تحضير البيانات لإرسالها لـ Claude"""
    candles = []
    try:
        for c in raw["candles"].get("values", [])[:8]:
            o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
            candles.append({
                "open": round(o, 5), "high": round(h, 5),
                "low": round(l, 5),  "close": round(cl, 5),
                "direction": "UP" if cl > o else "DOWN",
                "body_size": round(abs(cl - o) / (h - l) * 100 if h != l else 0, 1)
            })
    except:
        pass

    return {
        "symbol":   symbol,
        "interval": interval,
        "candles":  candles,
        "rsi":      safe(raw, "rsi",   "values", 0, "rsi"),
        "macd":     safe(raw, "macd",  "values", 0, "macd"),
        "macd_sig": safe(raw, "macd",  "values", 0, "macd_signal"),
        "macd_hist":safe(raw, "macd",  "values", 0, "macd_hist"),
        "ema50":    safe(raw, "ema50", "values", 0, "ema"),
        "price":    candles[0]["close"] if candles else None,
    }

# ========== CLAUDE AI ==========

async def ask_claude(data: dict) -> dict:
    prompt = f"""You are an expert Forex trader for Pocket Option binary trading.

Analyze this data for {data['symbol']} on {data['interval']} timeframe:

Current Price: {data['price']}
EMA 50: {data['ema50']}
RSI: {data['rsi']}
MACD: {data['macd']} | Signal: {data['macd_sig']} | Histogram: {data['macd_hist']}

Last 8 candles (newest first):
{json.dumps(data['candles'], indent=2)}

Based on this analysis:
1. Price vs EMA 50 trend
2. RSI momentum
3. MACD crossover
4. Candlestick patterns (engulfing, hammer, shooting star, etc.)
5. Overall trend consistency

Give me ONE clear trading decision.

Respond ONLY in this JSON format (no other text):
{{
  "signal": "BUY" or "SELL" or "WAIT",
  "confidence": 0-100,
  "reason": "One short sentence explaining why in Arabic"
}}"""

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                CLAUDE_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01"
                },
                json={
                    "model": "claude-3-5-sonnet-20241022",
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    raw  = await resp.json()
                    text = raw["content"][0]["text"].strip()
                    if "```" in text:
                        text = text.split("```")[1].replace("json", "").strip()
                    return json.loads(text)
                else:
                    return {"signal": "WAIT", "confidence": 0, "reason": f"خطأ في الاتصال"}
    except Exception as e:
        return {"signal": "WAIT", "confidence": 0, "reason": str(e)}

# ========== MESSAGE ==========

def build_msg(symbol: str, interval: str, price, result: dict) -> tuple:
    signal     = result.get("signal", "WAIT")
    confidence = result.get("confidence", 0)
    reason     = result.get("reason", "—")

    if signal == "BUY":
        emoji = "🟢🚀"
        text  = "شراء (BUY) ⬆️"
    elif signal == "SELL":
        emoji = "🔴📉"
        text  = "بيع (SELL) ⬇️"
    else:
        emoji = "⚪"
        text  = "انتظر — لا تدخل"

    if confidence >= 90:
        conf_label = "🔥🔥🔥 ثقة عالية جداً"
    elif confidence >= 80:
        conf_label = "🔥🔥 ثقة عالية"
    elif confidence >= 70:
        conf_label = "🔥 ثقة جيدة"
    else:
        conf_label = "⚡ ثقة منخفضة"

    price_line = f"💲 السعر: `{price:.5f}`\n" if price else ""

    msg = f"""{emoji} *{symbol} | {interval}*
{price_line}
━━━━━━━━━━━━━━━━━━━━
🎯 *الإشارة:* *{text}*
{conf_label} `({confidence}%)`
━━━━━━━━━━━━━━━━━━━━
🧠 *تحليل Claude AI:*
_{reason}_
━━━━━━━━━━━━━━━━━━━━
⚠️ _إدارة رأس المال مسؤوليتك._""".strip()

    return confidence, msg

# ========== AUTO SCANNER ==========

async def auto_scanner(app):
    await asyncio.sleep(20)
    while True:
        if is_trading_time():
            chat_ids = app.bot_data.get("chat_ids", set())
            if chat_ids:
                for code, symbol in CURRENCIES.items():
                    try:
                        raw    = await fetch_data(symbol, "5min")
                        data   = prepare_data(raw, symbol, "5min")
                        result = await ask_claude(data)
                        conf, msg = build_msg(symbol, "5min", data.get("price"), result)

                        if conf >= MIN_CONFIDENCE and result.get("signal") != "WAIT":
                            alert = f"🔔 *فرصة تداول!*\n\n{msg}"
                            for cid in chat_ids:
                                try:
                                    await app.bot.send_message(chat_id=cid, text=alert, parse_mode="Markdown")
                                except:
                                    pass
                        await asyncio.sleep(4)
                    except Exception as e:
                        print(f"Error {symbol}: {e}")
        await asyncio.sleep(SCAN_EVERY)

# ========== KEYBOARDS ==========

def currencies_keyboard():
    kb, row = [], []
    for code, label in CURRENCIES.items():
        row.append(InlineKeyboardButton(label, callback_data=f"c_{code}"))
        if len(row) == 2:
            kb.append(row); row = []
    if row: kb.append(row)
    return InlineKeyboardMarkup(kb)

def timeframes_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ 1 دقيقة",  callback_data="t_1min"),
         InlineKeyboardButton("🕐 5 دقائق",  callback_data="t_5min")],
        [InlineKeyboardButton("📊 15 دقيقة", callback_data="t_15min"),
         InlineKeyboardButton("📈 30 دقيقة", callback_data="t_30min")],
        [InlineKeyboardButton("🔙 رجوع",     callback_data="new")],
    ])

# ========== HANDLERS ==========

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "chat_ids" not in ctx.application.bot_data:
        ctx.application.bot_data["chat_ids"] = set()
    ctx.application.bot_data["chat_ids"].add(update.effective_chat.id)
    await update.message.reply_text(
        "🤖 *بوت التداول بالذكاء الاصطناعي*\n\n"
        "🧠 يحلل بـ Claude AI\n"
        "📊 بيانات من Twelve Data\n"
        "🔔 إشعار تلقائي عند ثقة +75%\n\n"
        "اختر العملة 👇",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 تحليل", callback_data="new")],
        ]),
        parse_mode="Markdown"
    )

async def button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer()
    except:
        pass

    if q.data == "new":
        await q.message.reply_text(
            "💱 *اختر العملة:*",
            reply_markup=currencies_keyboard(),
            parse_mode="Markdown"
        )
    elif q.data.startswith("c_"):
        code   = q.data.replace("c_", "")
        symbol = CURRENCIES.get(code, code.replace("_", "/"))
        ctx.user_data["symbol"] = symbol
        await q.message.reply_text(
            f"✅ *{symbol}*\n\nاختر الإطار الزمني:",
            reply_markup=timeframes_keyboard(),
            parse_mode="Markdown"
        )
    elif q.data.startswith("t_"):
        tf     = q.data.replace("t_", "")
        symbol = ctx.user_data.get("symbol", "EUR/USD")
        wait   = await q.message.reply_text(
            f"🧠 Claude AI يحلل *{symbol}*...",
            parse_mode="Markdown"
        )
        raw    = await fetch_data(symbol, tf)
        data   = prepare_data(raw, symbol, tf)
        result = await ask_claude(data)
        conf, msg = build_msg(symbol, tf, data.get("price"), result)
        await wait.delete()
        code = symbol.replace("/", "_")
        await q.message.reply_text(
            msg,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 تحليل مجدداً", callback_data=f"c_{code}")],
                [InlineKeyboardButton("🔙 عملة أخرى",    callback_data="new")],
            ])
        )

# ========== MAIN ==========

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))

    async def post_init(application):
        asyncio.create_task(auto_scanner(application))

    app.post_init = post_init
    print("🤖 AI Bot running!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
