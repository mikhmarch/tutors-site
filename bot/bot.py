"""Telegram-бот Алины Пензевой и команды репетиторов.

Что умеет:
  • открывается с сайта по ссылке ?start=<метка> и сразу показывает
    нужного преподавателя (метки: alina, maria, roman, arseniy, nina, masha, help);
  • листает карточки преподавателей с фото и фильтром по предмету;
  • собирает заявку: предмет → класс → цель → контакт → подтверждение;
  • отправляет заявку администратору (Алине) и сохраняет её в data/applications.csv;
  • пересылает администратору любые другие сообщения пользователей,
    а ответ администратора (через «Ответить» на сообщение бота) доставляет обратно.

Запуск: python bot.py   (настройки в файле .env, см. .env.example)
"""

import asyncio
import csv
import html
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from tutors import GRADES, SUBJECTS, THANKS, TUTORS, WELCOME

BASE_DIR = Path(__file__).resolve().parent
PHOTOS_DIR = BASE_DIR / "photos"
DATA_DIR = BASE_DIR / "data"
CSV_PATH = DATA_DIR / "applications.csv"

load_dotenv(BASE_DIR / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0").strip() or 0)

log = logging.getLogger("tutor_bot")
router = Router()

TUTOR_IDS = list(TUTORS)
# file_id загруженных фото, чтобы не отправлять файлы повторно
photo_cache: dict[str, str] = {}
# id сообщения у администратора -> id пользователя (для ответов)
reply_map: dict[int, int] = {}
USER_TAG = re.compile(r"#id(\d+)")


class Booking(StatesGroup):
    subject = State()
    grade = State()
    goal = State()
    contact = State()
    confirm = State()


# ---------------------------------------------------------------- helpers

def e(text: str) -> str:
    return html.escape(text or "")


def subjects_line(tutor: dict) -> str:
    return " · ".join(SUBJECTS[s] for s in tutor["subjects"])


def tutor_caption(tid: str) -> str:
    t = TUTORS[tid]
    lines = [f"<b>{e(t['name'])}</b>"]
    if t.get("role"):
        lines.append(f"<i>{e(t['role'])}</i>")
    lines.append(subjects_line(t))
    if t.get("about"):
        lines += ["", e(t["about"])]
    return "\n".join(lines)


def filtered(flt: str) -> list[str]:
    if flt == "all":
        return TUTOR_IDS
    return [tid for tid in TUTOR_IDS if flt in TUTORS[tid]["subjects"]]


def photo_input(tid: str):
    return photo_cache.get(tid) or FSInputFile(PHOTOS_DIR / TUTORS[tid]["photo"])


def remember_photo(tid: str, msg: Message | bool | None) -> None:
    if isinstance(msg, Message) and msg.photo:
        photo_cache[tid] = msg.photo[-1].file_id


def user_title(user) -> str:
    name = e(user.full_name)
    link = f'<a href="tg://user?id={user.id}">{name}</a>'
    if user.username:
        link += f" (@{e(user.username)})"
    return link


# ---------------------------------------------------------------- keyboards

def kb_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👩‍🏫 Преподаватели", callback_data="tutors")
    b.button(text="📝 Оставить заявку", callback_data="book:any")
    b.adjust(1)
    return b.as_markup()


def kb_subject_filter() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Все преподаватели", callback_data="list:all:0")
    for code, title in SUBJECTS.items():
        b.button(text=title, callback_data=f"list:{code}:0")
    b.button(text="⬅️ Назад", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


def kb_carousel(flt: str, idx: int) -> InlineKeyboardMarkup:
    ids = filtered(flt)
    tid = ids[idx]
    n = len(ids)
    rows = []
    if n > 1:
        rows.append([
            InlineKeyboardButton(text="◀️", callback_data=f"list:{flt}:{(idx - 1) % n}"),
            InlineKeyboardButton(text=f"{idx + 1} / {n}", callback_data="noop"),
            InlineKeyboardButton(text="▶️", callback_data=f"list:{flt}:{(idx + 1) % n}"),
        ])
    if TUTORS[tid].get("bio"):
        rows.append([InlineKeyboardButton(text="📖 Подробнее", callback_data=f"bio:{tid}")])
    rows.append([InlineKeyboardButton(
        text=f"📝 Записаться к {TUTORS[tid]['name_to']}", callback_data=f"book:{tid}")])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu_new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_bio(tid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📝 Записаться к {TUTORS[tid]['name_to']}", callback_data=f"book:{tid}")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
    ])


def kb_choices(prefix: str, options: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for value, title in options:
        b.button(text=title, callback_data=f"{prefix}:{value}")
    b.button(text="✖️ Отмена", callback_data="cancel")
    b.adjust(1)
    return b.as_markup()


def kb_contact() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
            [KeyboardButton(text="Пишите мне в Telegram")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить заявку", callback_data="send")],
        [InlineKeyboardButton(text="✏️ Заполнить заново", callback_data="restart"),
         InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel")],
    ])


# ---------------------------------------------------------------- /start, меню

async def show_tutor(message: Message, tid: str) -> None:
    flt = "all"
    sent = await message.answer_photo(
        photo_input(tid), caption=tutor_caption(tid),
        reply_markup=kb_carousel(flt, TUTOR_IDS.index(tid)))
    remember_photo(tid, sent)


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext) -> None:
    payload = (command.args or "").strip().lower()
    await state.clear()
    await state.update_data(source=payload or "telegram")

    if payload in TUTORS:
        await message.answer(WELCOME, reply_markup=ReplyKeyboardRemove())
        await show_tutor(message, payload)
        return
    if payload == "help":
        await message.answer(WELCOME, reply_markup=ReplyKeyboardRemove())
        await start_booking(message, state, "any")
        return
    await message.answer(WELCOME, reply_markup=kb_menu())


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(
        f"ID этого чата: <code>{message.chat.id}</code>\n"
        "Впишите его в ADMIN_CHAT_ID в файле .env, чтобы получать заявки сюда.")


@router.message(Command("cancel"))
@router.message(Command("menu"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    await state.update_data(source=data.get("source", "telegram"))
    await message.answer("Главное меню:", reply_markup=ReplyKeyboardRemove())
    await message.answer(WELCOME, reply_markup=kb_menu())


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        await cb.message.edit_text(WELCOME, reply_markup=kb_menu())
    except TelegramBadRequest:
        await cb.message.answer(WELCOME, reply_markup=kb_menu())


@router.callback_query(F.data == "menu_new")
async def cb_menu_new(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(WELCOME, reply_markup=kb_menu())


@router.callback_query(F.data == "tutors")
async def cb_tutors(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.edit_text("Какой предмет интересует?", reply_markup=kb_subject_filter())


@router.callback_query(F.data.startswith("list:"))
async def cb_list(cb: CallbackQuery) -> None:
    _, flt, idx_s = cb.data.split(":")
    ids = filtered(flt)
    if not ids:
        await cb.answer("Пока нет преподавателей по этому предмету", show_alert=True)
        return
    idx = int(idx_s) % len(ids)
    tid = ids[idx]
    await cb.answer()

    if cb.message.photo:  # листаем карусель — меняем фото в том же сообщении
        try:
            res = await cb.message.edit_media(
                InputMediaPhoto(media=photo_input(tid), caption=tutor_caption(tid)),
                reply_markup=kb_carousel(flt, idx))
            remember_photo(tid, res)
        except TelegramBadRequest as err:
            if "not modified" not in str(err):
                raise
        return

    # первое открытие из текстового меню
    sent = await cb.message.answer_photo(
        photo_input(tid), caption=tutor_caption(tid), reply_markup=kb_carousel(flt, idx))
    remember_photo(tid, sent)
    try:
        await cb.message.delete()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("bio:"))
async def cb_bio(cb: CallbackQuery) -> None:
    tid = cb.data.split(":", 1)[1]
    if tid not in TUTORS:
        await cb.answer()
        return
    await cb.answer()
    t = TUTORS[tid]
    text = f"<b>{e(t['name'])}</b> · {e(subjects_line(t))}\n\n{t['bio']}"  # bio уже в HTML
    await cb.message.answer(text, reply_markup=kb_bio(tid))


# ---------------------------------------------------------------- заявка

async def start_booking(message: Message, state: FSMContext, tid: str) -> None:
    data = await state.get_data()
    await state.set_data({"source": data.get("source", "telegram"), "tutor": tid})

    subjects = TUTORS[tid]["subjects"] if tid in TUTORS else list(SUBJECTS)
    who = f" к {TUTORS[tid]['name_to']}" if tid in TUTORS else ""

    if len(subjects) == 1:
        await state.update_data(subject=subjects[0])
        await ask_grade(message, state, intro=f"Записываемся{who} · {SUBJECTS[subjects[0]]}\n\n")
        return

    await state.set_state(Booking.subject)
    head = f"Записываемся{who}." if who else "Оставьте заявку, и мы поможем подобрать преподавателя."
    await message.answer(
        f"{head}\n\n<b>Шаг 1 из 4.</b> Какой предмет?",
        reply_markup=kb_choices("subj", [(s, SUBJECTS[s]) for s in subjects]))


async def ask_grade(message: Message, state: FSMContext, intro: str = "", edit: bool = False) -> None:
    await state.set_state(Booking.grade)
    text = f"{intro}<b>Шаг 2 из 4.</b> Кто будет заниматься?"
    markup = kb_choices("grade", [(str(i), g) for i, g in enumerate(GRADES)])
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("book:"))
async def cb_book(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await start_booking(cb.message, state, cb.data.split(":", 1)[1])


@router.callback_query(Booking.subject, F.data.startswith("subj:"))
async def cb_subject(cb: CallbackQuery, state: FSMContext) -> None:
    code = cb.data.split(":", 1)[1]
    if code not in SUBJECTS:
        await cb.answer()
        return
    await cb.answer()
    await state.update_data(subject=code)
    await ask_grade(cb.message, state, intro=f"Предмет: {SUBJECTS[code]}\n\n", edit=True)


@router.callback_query(Booking.grade, F.data.startswith("grade:"))
async def cb_grade(cb: CallbackQuery, state: FSMContext) -> None:
    i = int(cb.data.split(":", 1)[1])
    await cb.answer()
    await state.update_data(grade=GRADES[i])
    await state.set_state(Booking.goal)
    await cb.message.edit_text(
        f"Класс: {GRADES[i]}\n\n<b>Шаг 3 из 4.</b> Напишите в паре предложений, "
        "какая цель: например, подготовка к ОГЭ/ЕГЭ, подтянуть оценки, "
        "разобраться в конкретной теме.")


@router.message(Booking.goal, F.text)
async def msg_goal(message: Message, state: FSMContext) -> None:
    await state.update_data(goal=message.text.strip()[:1000])
    await state.set_state(Booking.contact)
    await message.answer(
        "<b>Шаг 4 из 4.</b> Как с вами связаться?\n\n"
        "Можно поделиться номером телефона кнопкой ниже "
        "или просто договориться в Telegram.",
        reply_markup=kb_contact())


@router.message(Booking.contact, F.contact)
async def msg_contact_phone(message: Message, state: FSMContext) -> None:
    await state.update_data(phone=message.contact.phone_number)
    await show_summary(message, state)


@router.message(Booking.contact, F.text)
async def msg_contact_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    phone = "" if text == "Пишите мне в Telegram" else text[:100]
    await state.update_data(phone=phone)
    await show_summary(message, state)


def summary_text(data: dict) -> str:
    tid = data.get("tutor", "any")
    tutor = TUTORS[tid]["name"] if tid in TUTORS else "Поможем подобрать"
    return (
        f"👩‍🏫 Преподаватель: {e(tutor)}\n"
        f"📚 Предмет: {e(SUBJECTS.get(data.get('subject', ''), '—'))}\n"
        f"🎓 Класс: {e(data.get('grade', '—'))}\n"
        f"🎯 Цель: {e(data.get('goal', '—'))}\n"
        f"📱 Контакт: {e(data.get('phone') or 'Telegram')}"
    )


async def show_summary(message: Message, state: FSMContext) -> None:
    await state.set_state(Booking.confirm)
    data = await state.get_data()
    await message.answer("Почти готово!", reply_markup=ReplyKeyboardRemove())
    await message.answer(
        "Проверьте заявку:\n\n" + summary_text(data), reply_markup=kb_confirm())


@router.callback_query(Booking.confirm, F.data == "send")
async def cb_send(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    user = cb.from_user
    await cb.answer()

    save_application(data, user)

    if ADMIN_CHAT_ID:
        admin_text = (
            "🆕 <b>Новая заявка</b>\n\n"
            f"👤 {user_title(user)}\n"
            f"{summary_text(data)}\n"
            f"🔗 Откуда: {e(data.get('source', 'telegram'))}\n\n"
            f"<i>Ответьте на это сообщение, чтобы написать ученику через бота.</i> #id{user.id}"
        )
        try:
            sent = await bot.send_message(ADMIN_CHAT_ID, admin_text)
            reply_map[sent.message_id] = user.id
        except Exception:  # noqa: BLE001 — заявка уже сохранена в CSV
            log.exception("Не удалось отправить заявку администратору")
    else:
        log.warning("ADMIN_CHAT_ID не задан: заявка сохранена только в %s", CSV_PATH)

    source = data.get("source", "telegram")
    await state.clear()
    await state.update_data(source=source)
    await cb.message.edit_text("Заявка:\n\n" + summary_text(data))
    await cb.message.answer(THANKS, reply_markup=kb_menu())


@router.callback_query(F.data == "restart")
async def cb_restart(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    data = await state.get_data()
    await cb.message.delete()
    await start_booking(cb.message, state, data.get("tutor", "any"))


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer("Заявка отменена")
    data = await state.get_data()
    await state.clear()
    await state.update_data(source=data.get("source", "telegram"))
    await cb.message.edit_text(WELCOME, reply_markup=kb_menu())


@router.callback_query()
async def cb_stale(cb: CallbackQuery) -> None:
    """Кнопка из старого сообщения (например, после перезапуска бота)."""
    await cb.answer("Эта кнопка устарела. Отправьте /start, чтобы начать заново.", show_alert=True)


@router.message(StateFilter(Booking.subject, Booking.grade, Booking.confirm))
async def msg_use_buttons(message: Message) -> None:
    await message.answer("Выберите вариант кнопкой выше 👆 или отправьте /cancel, чтобы выйти.")


@router.message(StateFilter(Booking.goal, Booking.contact))
async def msg_need_text(message: Message) -> None:
    await message.answer("Напишите ответ текстом, пожалуйста 🙂")


def save_application(data: dict, user) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    new_file = not CSV_PATH.exists()
    tid = data.get("tutor", "any")
    with CSV_PATH.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        if new_file:
            w.writerow(["Дата", "Имя", "Username", "Telegram ID", "Преподаватель",
                        "Предмет", "Класс", "Цель", "Телефон", "Откуда"])
        w.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            user.full_name,
            f"@{user.username}" if user.username else "",
            user.id,
            TUTORS[tid]["name"] if tid in TUTORS else "Подобрать",
            SUBJECTS.get(data.get("subject", ""), ""),
            data.get("grade", ""),
            data.get("goal", ""),
            data.get("phone", ""),
            data.get("source", ""),
        ])


# ---------------------------------------------------------------- переписка через бота

@router.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message)
async def admin_reply(message: Message, bot: Bot) -> None:
    """Администратор отвечает на сообщение бота — ответ уходит пользователю."""
    target = message.reply_to_message
    user_id = reply_map.get(target.message_id)
    if not user_id:
        m = USER_TAG.search(target.text or target.caption or "")
        user_id = int(m.group(1)) if m else None
    if not user_id:
        await message.reply("Не понял, кому ответить. Отвечайте на сообщение с пометкой #id.")
        return
    try:
        await bot.copy_message(user_id, message.chat.id, message.message_id)
        await message.reply("✅ Отправлено")
    except Exception as err:  # noqa: BLE001
        await message.reply(f"Не удалось отправить: {e(str(err))}")


@router.message(F.chat.type == "private")
async def user_message(message: Message, bot: Bot) -> None:
    """Любое сообщение вне заявки пересылаем администратору."""
    if message.chat.id == ADMIN_CHAT_ID:
        await message.answer("Это чат администратора. Ответьте на сообщение ученика, "
                             "чтобы написать ему, или отправьте /start.")
        return
    if not ADMIN_CHAT_ID:
        await message.answer(WELCOME, reply_markup=kb_menu())
        return
    user = message.from_user
    head = await bot.send_message(
        ADMIN_CHAT_ID,
        f"💬 Сообщение от {user_title(user)}\n"
        f"<i>Ответьте на это сообщение, чтобы написать через бота.</i> #id{user.id}")
    reply_map[head.message_id] = user.id
    copy = await bot.copy_message(ADMIN_CHAT_ID, message.chat.id, message.message_id)
    reply_map[copy.message_id] = user.id
    await message.answer("Передали сообщение Алине, скоро ответим 🙌", reply_markup=kb_menu())


# ---------------------------------------------------------------- запуск

def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен.")
    if not ADMIN_CHAT_ID:
        log.warning("ADMIN_CHAT_ID не задан. Напишите боту /id и впишите число в .env.")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="cancel", description="Отменить заявку"),
    ])
    dp = build_dispatcher()
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
