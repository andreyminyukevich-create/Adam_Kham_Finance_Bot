import os
import re
import random
import logging
import hashlib
from typing import Optional, Dict, Any, List

import aiohttp
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("finance-bot")


# =========================
# CONFIG from ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SCRIPT_URL = os.getenv("SCRIPT_URL", "").strip()

# Список разрешенных user_id через запятую
USER_TG_IDS_STR = os.getenv("USER_TG_IDS", "").strip()
if USER_TG_IDS_STR:
    USER_TG_IDS = [int(x.strip()) for x in USER_TG_IDS_STR.split(",") if x.strip()]
else:
    USER_TG_IDS = []

# Для backward compatibility - если указан старый USER_TG_ID
USER_TG_ID_SINGLE = os.getenv("USER_TG_ID", "").strip()
if USER_TG_ID_SINGLE and int(USER_TG_ID_SINGLE) not in USER_TG_IDS:
    USER_TG_IDS.append(int(USER_TG_ID_SINGLE))

# Для GAS запросов берем первого из списка (главный пользователь)
USER_TG_ID = USER_TG_IDS[0] if USER_TG_IDS else 0

# Для webhook (Railway)
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not SCRIPT_URL:
    raise RuntimeError("SCRIPT_URL is missing")
if not USER_TG_IDS:
    raise RuntimeError("USER_TG_IDS is missing")


def _default_webhook_path() -> str:
    h = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest()
    return f"tg/{h[:24]}"


# =========================
# Phrases
# =========================
PH_EXP_CAT = [
    "На что потратил? 💪",
    "Куда ушли?",
    "Что оплатил? Выбирай категорию.",
    "Окей, что за трата?",
    "Фиксируем: какая категория?",
]
PH_EXP_SUB = [
    "*{cat}*, а точнее?",
    "Внутри *{cat}* — что именно?",
    "Что конкретно в *{cat}*?",
]
PH_AMOUNT_EXP = [
    "Сколько?",
    "Какая сумма?",
    "На сколько вышло?",
    "Запишем сколько?",
]
PH_COMMENT_EXP = [
    "Добавишь коммент?",
    "Коммент или пропускаем?",
    "Уточнение нужно? (необязательно)",
]
PH_SAVED_EXP = [
    "Записал ✅",
    "Готово ✅",
    "Зафиксировал ✅",
    "Есть ✅",
    "Принял ✅",
]

# Специальные фразы для транспорта (тачки)
PH_SAVED_EXP_CAR = [
    "БМВ сила! ✅ Записал.",
    "Тачка это святое! ✅ Зафиксировал.",
    "Машина должна быть в порядке! ✅ Готово.",
    "На здоровье! ✅ Записал.",
    "Правильное дело! ✅ Есть.",
]

PH_INC_CAT = [
    "Денежки! Откуда?",
    "Доход пришёл 💪 Источник?",
    "Поступление. Кто источник?",
]
PH_AMOUNT_INC = [
    "Сколько?",
    "Сколько пришло?",
    "Какая сумма?",
]
PH_COMMENT_INC = [
    "Коммент оставишь?",
    "Добавишь коммент? (необязательно)",
]
PH_SAVED_INC = [
    "Отлично! ✅ Записал поступление.",
    "Красава! ✅ Зафиксировал.",
    "Есть! ✅ Сохранил.",
    "Принял ✅",
    "Готово ✅",
]

DENY_TEXT = "Извини, доступ закрыт 🙂"


# =========================
# Conversation states
# =========================
(
    ST_MENU,
    ST_ADD_CHOOSE_TYPE,
    ST_EXP_CATEGORY,
    ST_EXP_SUBCATEGORY,
    ST_AMOUNT,
    ST_COMMENT,
    ST_INC_CATEGORY,
    ST_ANALYSIS_KIND,
    ST_ANALYSIS_PERIOD,
    ST_SET_BALANCE,
    ST_SET_DEBTS,
) = range(11)


# =========================
# Helpers: temp messages
# =========================
async def delete_working_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Удалить текущее рабочее сообщение"""
    msg_id = context.user_data.get("working_message_id")
    if msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logger.debug(f"Couldn't delete message {msg_id}: {e}")
    context.user_data["working_message_id"] = None


# =========================
# Helpers: keyboards
# =========================
def is_allowed(update: Update) -> bool:
    """Проверка доступа - разрешен ли пользователь"""
    user = update.effective_user
    if not user:
        return False
    return user.id in USER_TG_IDS


def kb_main(account_type: str = "personal") -> InlineKeyboardMarkup:
    """Главное меню с переключателем типа счета"""
    account_label = "💼 Бизнес" if account_type == "personal" else "👤 Личное"
    switch_to = "business" if account_type == "personal" else "personal"
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Внести транзакцию", callback_data="menu:add")],
        [InlineKeyboardButton("📊 Анализ", callback_data="menu:analysis")],
        [InlineKeyboardButton("💰 Баланс", callback_data="menu:set_balance")],
        [InlineKeyboardButton("💳 Долги", callback_data="menu:set_debts")],
        [InlineKeyboardButton(f"Переключить на {account_label}", callback_data=f"switch:{switch_to}")],
    ])


def kb_choose_type() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➖ Затраты", callback_data="type:expense")],
        [InlineKeyboardButton("➕ Доход", callback_data="type:income")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:menu")],
    ])


def kb_expense_categories(categories: Dict[str, List[str]]) -> InlineKeyboardMarkup:
    """Динамическая клавиатура категорий расходов"""
    cats = list(categories.keys())
    rows = []
    row = []
    for i, c in enumerate(cats):
        row.append(InlineKeyboardButton(c, callback_data=f"expcat:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:choose_type")])
    return InlineKeyboardMarkup(rows)


def kb_expense_subcategories(subcats: List[str]) -> InlineKeyboardMarkup:
    """Динамическая клавиатура подкатегорий расходов"""
    rows = []
    row = []
    for i, s in enumerate(subcats):
        row.append(InlineKeyboardButton(s, callback_data=f"expsub:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:exp_cat")])
    return InlineKeyboardMarkup(rows)


def kb_income_categories(categories: List[str]) -> InlineKeyboardMarkup:
    """Динамическая клавиатура категорий доходов"""
    rows = []
    row = []
    for i, c in enumerate(categories):
        row.append(InlineKeyboardButton(c, callback_data=f"inccat:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:choose_type")])
    return InlineKeyboardMarkup(rows)


def kb_skip_comment() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пропустить", callback_data="comment:skip")],
    ])


def kb_analysis_kind() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➖ Затраты", callback_data="akind:expense")],
        [InlineKeyboardButton("➕ Доходы", callback_data="akind:income")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:menu")],
    ])


def kb_analysis_period() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Сегодня", callback_data="aperiod:today")],
        [InlineKeyboardButton("В этом месяце", callback_data="aperiod:month")],
        [InlineKeyboardButton("В этом году", callback_data="aperiod:year")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:analysis_kind")],
    ])


# =========================
# Amount parsing
# =========================
def parse_amount(text: str) -> Optional[float]:
    if not text:
        return None
    s0 = text.strip().lower()

    mult = 1.0
    s = re.sub(r"\s+", "", s0)
    if s.endswith("к") or s.endswith("k"):
        mult = 1000.0
        s = s[:-1]

    has_comma = "," in s
    has_dot = "." in s

    if has_comma and has_dot:
        last_comma = s.rfind(",")
        last_dot = s.rfind(".")
        dec_pos = max(last_comma, last_dot)
        int_part = re.sub(r"[.,]", "", s[:dec_pos])
        frac_part = re.sub(r"[.,]", "", s[dec_pos + 1:])
        s = f"{int_part}.{frac_part}"
    elif has_comma and not has_dot:
        s = s.replace(",", ".")
    else:
        pass

    s = re.sub(r"[^0-9.\-]", "", s)
    try:
        val = float(s) * mult
        if val < 0:
            return None
        return round(val, 2)
    except Exception:
        return None


# =========================
# GAS API
# =========================
async def gas_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(payload)
    payload["user_id"] = USER_TG_ID

    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(SCRIPT_URL, json=payload) as resp:
            txt = await resp.text()
            try:
                data = await resp.json()
            except Exception:
                logger.error("GAS non-json response: %s", txt)
                raise RuntimeError("GAS вернул не-JSON ответ")
            if not data.get("ok"):
                raise RuntimeError(data.get("error") or "GAS error")
            return data["data"]


async def month_screen_text(account_type: str = "personal") -> str:
    """Получить текст главного экрана"""
    s = await gas_request({"cmd": "summary_month", "account_type": account_type})
    
    account_label = "💼 Бизнес" if account_type == "business" else "👤 Личное"
    month = s.get("month_label", "Текущий месяц")
    exp = s.get("expenses", 0)
    inc = s.get("incomes", 0)
    bal_month = s.get("balance_month", 0)
    bal_start = s.get("balance_start", 0)
    bal_current = s.get("balance_current", 0)
    debts = s.get("debts", 0)
    
    return (
        f"<b>{account_label}</b>\n"
        f"<b>{month}</b>\n\n"
        f"💰 Начальный баланс: <b>{bal_start:,.2f}</b> ₽\n"
        f"➖ Расходы: <b>{exp:,.2f}</b> ₽\n"
        f"➕ Доходы: <b>{inc:,.2f}</b> ₽\n"
        f"🟰 За месяц: <b>{bal_month:,.2f}</b> ₽\n"
        f"💵 Текущий баланс: <b>{bal_current:,.2f}</b> ₽\n"
        f"💳 Долги: <b>{debts:,.2f}</b> ₽"
    ).replace(",", " ")


async def get_categories(account_type: str = "personal") -> Dict[str, Any]:
    """Получить категории для выбранного типа счета"""
    return await gas_request({"cmd": "get_categories", "account_type": account_type})


# =========================
# Handlers
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text(DENY_TEXT)
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["account_type"] = "personal"  # По умолчанию личный счет
    
    txt = await month_screen_text("personal")
    await update.message.reply_text(txt, reply_markup=kb_main("personal"), parse_mode=ParseMode.HTML)
    
    return ST_MENU


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(DENY_TEXT)
        return ConversationHandler.END

    q = update.callback_query
    await q.answer()
    
    account_type = context.user_data.get("account_type", "personal")

    # Переключение типа счета
    if q.data.startswith("switch:"):
        new_type = q.data.split(":")[1]
        context.user_data["account_type"] = new_type
        txt = await month_screen_text(new_type)
        await q.edit_message_text(txt, reply_markup=kb_main(new_type), parse_mode=ParseMode.HTML)
        return ST_MENU

    if q.data == "menu:add":
        await q.edit_message_text("Окей 🙂 Что вносим?", reply_markup=kb_choose_type())
        context.user_data["working_message_id"] = q.message.message_id
        return ST_ADD_CHOOSE_TYPE

    if q.data == "menu:analysis":
        await q.edit_message_text("Что посмотрим?", reply_markup=kb_analysis_kind())
        context.user_data["working_message_id"] = q.message.message_id
        return ST_ANALYSIS_KIND

    if q.data == "menu:set_balance":
        account_label = "бизнеса" if account_type == "business" else "личный"
        await q.edit_message_text(
            f"Какой у тебя сейчас баланс ({account_label})? 💰\n\n"
            f"Напиши сумму (например: 50000 или 50к)",
            parse_mode=ParseMode.HTML
        )
        context.user_data["working_message_id"] = q.message.message_id
        return ST_SET_BALANCE

    if q.data == "menu:set_debts":
        account_label = "бизнеса" if account_type == "business" else "личные"
        await q.edit_message_text(
            f"Сколько у тебя долгов ({account_label})? 💳\n\n"
            f"Напиши сумму (например: 10000 или 10к)",
            parse_mode=ParseMode.HTML
        )
        context.user_data["working_message_id"] = q.message.message_id
        return ST_SET_DEBTS

    return ST_MENU


async def back_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    account_type = context.user_data.get("account_type", "personal")

    if q.data == "back:menu":
        await delete_working_message(context, update.effective_chat.id)
        txt = await month_screen_text(account_type)
        await update.effective_chat.send_message(txt, reply_markup=kb_main(account_type), parse_mode=ParseMode.HTML)
        return ST_MENU

    if q.data == "back:choose_type":
        await q.edit_message_text("Окей 🙂 Что вносим?", reply_markup=kb_choose_type())
        return ST_ADD_CHOOSE_TYPE

    if q.data == "back:exp_cat":
        categories = await get_categories(account_type)
        await q.edit_message_text(random.choice(PH_EXP_CAT), reply_markup=kb_expense_categories(categories["expenses"]))
        return ST_EXP_CATEGORY

    if q.data == "back:analysis_kind":
        await q.edit_message_text("Что посмотрим?", reply_markup=kb_analysis_kind())
        return ST_ANALYSIS_KIND

    return ST_MENU


async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    context.user_data.pop("tx", None)
    context.user_data["tx"] = {}
    
    account_type = context.user_data.get("account_type", "personal")
    categories = await get_categories(account_type)

    if q.data == "type:expense":
        context.user_data["categories"] = categories
        await q.edit_message_text(random.choice(PH_EXP_CAT), reply_markup=kb_expense_categories(categories["expenses"]))
        return ST_EXP_CATEGORY

    if q.data == "type:income":
        context.user_data["categories"] = categories
        await q.edit_message_text(random.choice(PH_INC_CAT), reply_markup=kb_income_categories(categories["incomes"]))
        return ST_INC_CATEGORY

    return ST_ADD_CHOOSE_TYPE


async def expense_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    categories = context.user_data.get("categories", {}).get("expenses", {})
    cats = list(categories.keys())
    idx = int(q.data.split(":")[1])
    cat = cats[idx]

    tx = context.user_data.get("tx", {})
    tx["type"] = "расход"
    tx["category"] = cat
    context.user_data["tx"] = tx
    context.user_data["current_subcats"] = categories[cat]

    msg = random.choice(PH_EXP_SUB).format(cat=cat)
    await q.edit_message_text(msg, reply_markup=kb_expense_subcategories(categories[cat]), parse_mode=ParseMode.MARKDOWN)
    return ST_EXP_SUBCATEGORY


async def expense_subcategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    tx = context.user_data.get("tx", {})
    subs = context.user_data.get("current_subcats", [])
    idx = int(q.data.split(":")[1])
    sub = subs[idx] if 0 <= idx < len(subs) else ""

    tx["subcategory"] = sub
    context.user_data["tx"] = tx

    prompt = random.choice(PH_AMOUNT_EXP) + "\n\nПримеры: <code>2500</code>, <code>2 500</code>, <code>2.500</code>, <code>2500,50</code>, <code>2к</code>"
    await q.edit_message_text(prompt, parse_mode=ParseMode.HTML)
    return ST_AMOUNT


async def income_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    categories = context.user_data.get("categories", {}).get("incomes", [])
    idx = int(q.data.split(":")[1])
    cat = categories[idx]

    tx = context.user_data.get("tx", {})
    tx["type"] = "доход"
    tx["category"] = cat
    tx["subcategory"] = ""
    context.user_data["tx"] = tx

    prompt = random.choice(PH_AMOUNT_INC) + "\n\nПримеры: <code>2500</code>, <code>2 500</code>, <code>2.500</code>, <code>2500,50</code>, <code>2к</code>"
    await q.edit_message_text(prompt, parse_mode=ParseMode.HTML)
    return ST_AMOUNT


async def amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text(DENY_TEXT)
        return ConversationHandler.END

    amt = parse_amount(update.message.text)
    
    try:
        await update.message.delete()
    except Exception:
        pass
    
    if amt is None:
        await delete_working_message(context, update.effective_chat.id)
        msg = await update.effective_chat.send_message(
            "Не понял сумму 🙈\nНапиши, пожалуйста, например: 2500 / 2 500 / 2500,50 / 2к"
        )
        context.user_data["working_message_id"] = msg.message_id
        return ST_AMOUNT

    tx = context.user_data.get("tx", {})
    tx["amount"] = amt
    context.user_data["tx"] = tx

    work_msg_id = context.user_data.get("working_message_id")
    if work_msg_id:
        try:
            text = random.choice(PH_COMMENT_EXP) if tx.get("type") == "расход" else random.choice(PH_COMMENT_INC)
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=work_msg_id,
                text=text,
                reply_markup=kb_skip_comment()
            )
        except Exception:
            pass
    
    return ST_COMMENT


async def comment_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    tx = context.user_data.get("tx", {})
    tx["comment"] = ""
    context.user_data["tx"] = tx

    await save_and_finish_(update, context)
    return ST_MENU


async def comment_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text(DENY_TEXT)
        return ConversationHandler.END

    try:
        await update.message.delete()
    except Exception:
        pass

    tx = context.user_data.get("tx", {})
    tx["comment"] = (update.message.text or "").strip()
    context.user_data["tx"] = tx

    await save_and_finish_(update, context)
    return ST_MENU


async def save_and_finish_(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранить транзакцию и показать финальное подтверждение + главный экран"""
    
    await delete_working_message(context, update.effective_chat.id)
    
    tx = context.user_data.get("tx", {})
    account_type = context.user_data.get("account_type", "personal")
    
    payload = {
        "cmd": "add",
        "type": tx.get("type"),
        "category": tx.get("category"),
        "subcategory": tx.get("subcategory", ""),
        "amount": tx.get("amount"),
        "comment": tx.get("comment", ""),
        "account_type": account_type
    }

    await gas_request(payload)

    if tx.get("type") == "расход":
        # Проверяем - если это транспорт, даем спецфразу
        category = tx.get("category", "").lower()
        is_car = "транспорт" in category or "авто" in category or "машин" in category or "логистика" in category
        
        if is_car:
            header = random.choice(PH_SAVED_EXP_CAR)
        else:
            header = random.choice(PH_SAVED_EXP)
        
        if tx.get("subcategory"):
            detail = f"{tx.get('category')} → {tx.get('subcategory')} — {tx.get('amount'):,.2f} ₽".replace(",", " ")
        else:
            detail = f"{tx.get('category')} — {tx.get('amount'):,.2f} ₽".replace(",", " ")
    else:
        header = random.choice(PH_SAVED_INC)
        detail = f"{tx.get('category')} — {tx.get('amount'):,.2f} ₽".replace(",", " ")

    comment = tx.get("comment", "").strip()
    if comment:
        detail += f"\nКоммент: {comment}"

    await update.effective_chat.send_message(f"{header}\n{detail}")

    txt_month = await month_screen_text(account_type)
    await update.effective_chat.send_message(
        txt_month,
        reply_markup=kb_main(account_type),
        parse_mode=ParseMode.HTML
    )


async def analysis_kind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "akind:expense":
        context.user_data["analysis_kind"] = "расход"
        await q.edit_message_text("Окей 🙂 За какой период?", reply_markup=kb_analysis_period())
        return ST_ANALYSIS_PERIOD

    if q.data == "akind:income":
        context.user_data["analysis_kind"] = "доход"
        await q.edit_message_text("Окей 🙂 За какой период?", reply_markup=kb_analysis_period())
        return ST_ANALYSIS_PERIOD

    return ST_ANALYSIS_KIND


async def analysis_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    period = q.data.split(":")[1]
    kind = context.user_data.get("analysis_kind", "расход")
    account_type = context.user_data.get("account_type", "personal")

    res = await gas_request({"cmd": "analysis", "kind": kind, "period": period, "account_type": account_type})

    label_map = {"today": "Сегодня", "month": "В этом месяце", "year": "В этом году"}
    kind_label = "Затраты" if kind == "расход" else "Доходы"

    total = res.get("total", 0)
    text = f"<b>{kind_label}</b> — <b>{label_map.get(period, period)}</b>\nСумма: <b>{total:,.2f}</b> ₽"
    text = text.replace(",", " ")

    await delete_working_message(context, update.effective_chat.id)
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML)
    
    txt = await month_screen_text(account_type)
    await update.effective_chat.send_message(txt, reply_markup=kb_main(account_type), parse_mode=ParseMode.HTML)
    
    return ST_MENU


async def set_balance_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text(DENY_TEXT)
        return ConversationHandler.END

    try:
        await update.message.delete()
    except Exception:
        pass

    amt = parse_amount(update.message.text)
    if amt is None or amt < 0:
        await delete_working_message(context, update.effective_chat.id)
        msg = await update.effective_chat.send_message(
            "Не понял сумму 🙈\nНапиши, пожалуйста, например: 50000 / 50 000 / 50к"
        )
        context.user_data["working_message_id"] = msg.message_id
        return ST_SET_BALANCE

    account_type = context.user_data.get("account_type", "personal")
    await gas_request({"cmd": "set_balance", "amount": amt, "account_type": account_type})

    await delete_working_message(context, update.effective_chat.id)

    account_label = "бизнеса" if account_type == "business" else "личный"
    await update.effective_chat.send_message(
        f"Отлично! ✅ Баланс ({account_label}) установлен: <b>{amt:,.2f}</b> ₽".replace(",", " "),
        parse_mode=ParseMode.HTML
    )
    
    txt = await month_screen_text(account_type)
    await update.effective_chat.send_message(txt, reply_markup=kb_main(account_type), parse_mode=ParseMode.HTML)
    
    return ST_MENU


async def set_debts_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text(DENY_TEXT)
        return ConversationHandler.END

    try:
        await update.message.delete()
    except Exception:
        pass

    amt = parse_amount(update.message.text)
    if amt is None or amt < 0:
        await delete_working_message(context, update.effective_chat.id)
        msg = await update.effective_chat.send_message(
            "Не понял сумму 🙈\nНапиши, пожалуйста, например: 10000 / 10 000 / 10к"
        )
        context.user_data["working_message_id"] = msg.message_id
        return ST_SET_DEBTS

    account_type = context.user_data.get("account_type", "personal")
    await gas_request({"cmd": "set_debts", "amount": amt, "account_type": account_type})

    await delete_working_message(context, update.effective_chat.id)

    account_label = "бизнеса" if account_type == "business" else "личные"
    await update.effective_chat.send_message(
        f"Отлично! ✅ Долги ({account_label}) установлены: <b>{amt:,.2f}</b> ₽".replace(",", " "),
        parse_mode=ParseMode.HTML
    )
    
    txt = await month_screen_text(account_type)
    await update.effective_chat.send_message(txt, reply_markup=kb_main(account_type), parse_mode=ParseMode.HTML)
    
    return ST_MENU


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text(DENY_TEXT)
        return
    await update.message.reply_text(
        "Кнопки внизу 🙂\n"
        "• Внести транзакцию\n"
        "• Анализ\n"
        "• Установить баланс\n"
        "• Установить долги\n"
        "• Переключить личное/бизнес"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Ой, что-то пошло не так 🙈 Попробуем ещё раз?")
    except Exception:
        pass


def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ST_MENU: [
                CallbackQueryHandler(on_menu, pattern=r"^(menu:|switch:)"),
            ],
            ST_ADD_CHOOSE_TYPE: [
                CallbackQueryHandler(choose_type, pattern=r"^type:"),
                CallbackQueryHandler(back_router, pattern=r"^back:"),
            ],
            ST_EXP_CATEGORY: [
                CallbackQueryHandler(expense_category, pattern=r"^expcat:\d+$"),
                CallbackQueryHandler(back_router, pattern=r"^back:"),
            ],
            ST_EXP_SUBCATEGORY: [
                CallbackQueryHandler(expense_subcategory, pattern=r"^expsub:\d+$"),
                CallbackQueryHandler(back_router, pattern=r"^back:"),
            ],
            ST_INC_CATEGORY: [
                CallbackQueryHandler(income_category, pattern=r"^inccat:\d+$"),
                CallbackQueryHandler(back_router, pattern=r"^back:"),
            ],
            ST_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, amount_received),
            ],
            ST_COMMENT: [
                CallbackQueryHandler(comment_skip, pattern=r"^comment:skip$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, comment_received),
            ],
            ST_ANALYSIS_KIND: [
                CallbackQueryHandler(analysis_kind, pattern=r"^akind:"),
                CallbackQueryHandler(back_router, pattern=r"^back:"),
            ],
            ST_ANALYSIS_PERIOD: [
                CallbackQueryHandler(analysis_period, pattern=r"^aperiod:"),
                CallbackQueryHandler(back_router, pattern=r"^back:"),
            ],
            ST_SET_BALANCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_balance_received),
            ],
            ST_SET_DEBTS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_debts_received),
            ],
        },
        fallbacks=[CommandHandler("help", cmd_help)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_error_handler(error_handler)
    return app


def run():
    app = build_app()

    if WEBHOOK_URL:
        url_path = WEBHOOK_PATH or _default_webhook_path()
        full_webhook = f"{WEBHOOK_URL.rstrip('/')}/{url_path}"

        logger.info("Starting webhook on 0.0.0.0:%s", PORT)

        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=url_path,
            webhook_url=full_webhook,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Starting polling")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run()
```
