import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================
# VARIÁVEIS DE AMBIENTE
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
MERCADO_PAGO_ACCESS_TOKEN = os.getenv("MERCADO_PAGO_ACCESS_TOKEN", "")
START_IMAGE_URL = os.getenv("START_IMAGE_URL", "")
BASE_URL = os.getenv("BASE_URL", "")
PAYMENT_NAME = os.getenv("PAYMENT_NAME", "Telegram Filipe Dias")
CHANNEL_INVITE_LINK = os.getenv("CHANNEL_INVITE_LINK", "")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
TELEGRAM_CHANNEL_USERNAME = os.getenv("TELEGRAM_CHANNEL_USERNAME", "")

# =========================
# DADOS DOS PLANOS
# =========================
PLANS = {
    "mensal": {
        "title": "R$ 70,00 / Mensal 🔥",
        "amount": Decimal("70.00"),
        "description": "Acesso VIP por 30 dias.",
        "days": 30,
    },
    "exclusivos": {
        "title": "R$ 80,00 / Exclusivos",
        "amount": Decimal("80.00"),
        "description": "Acesso ao conteúdo exclusivo.",
        "days": 30,
    },
    "trimestral": {
        "title": "R$ 260,00 / Trimestral 🔥",
        "amount": Decimal("260.00"),
        "description": "Acesso VIP por 90 dias.",
        "days": 90,
    },
    "anual": {
        "title": "R$ 450,00 / Anual",
        "amount": Decimal("450.00"),
        "description": "Acesso VIP por 365 dias.",
        "days": 365,
    },
    "vitalicio": {
        "title": "R$ 745,00 / Vitalício 💋🔥",
        "amount": Decimal("745.00"),
        "description": "Acesso vitalício ao canal VIP.",
        "days": None,
    },
}

WELCOME_TEXT = (
    "🔥 <b>Bem-vindo ao conteúdo exclusivo</b>\n\n"
    "Conteúdo VIP liberado apenas para membros.\n\n"
    "Receba conteúdos exclusivos, atualizações privadas e acesso antecipado.\n\n"
    "<b>Escolha um dos planos abaixo:</b>"
)

# =========================
# MEMÓRIA TEMPORÁRIA
# =========================
payments_store: dict[str, dict[str, Any]] = {}
user_last_pending: dict[int, str] = {}

# =========================
# TELEGRAM LAZY INIT
# =========================
telegram_app: Application | None = None
telegram_ready = False
telegram_started = False
telegram_lock = asyncio.Lock()


# =========================
# TECLADOS
# =========================
def build_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("R$ 70,00 / Mensal 🔥", callback_data="plan_mensal")],
            [InlineKeyboardButton("R$ 80,00 / Exclusivos", callback_data="plan_exclusivos")],
            [InlineKeyboardButton("R$ 260,00 / Trimestral 🔥", callback_data="plan_trimestral")],
            [InlineKeyboardButton("R$ 450,00 / Anual", callback_data="plan_anual")],
            [InlineKeyboardButton("R$ 745,00 / Vitalício 💋🔥", callback_data="plan_vitalicio")],
        ]
    )


def build_plan_keyboard(plan_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Quero este plano", callback_data=f"buy_{plan_key}")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="back_to_home")],
        ]
    )


def build_payment_keyboard(payment_id: str, ticket_url: str = "") -> InlineKeyboardMarkup:
    rows = []
    if ticket_url:
        rows.append([InlineKeyboardButton("💳 Abrir QR Code Pix", url=ticket_url)])
    rows.append([InlineKeyboardButton("🔄 Verificar pagamento", callback_data=f"check_{payment_id}")])
    rows.append([InlineKeyboardButton("⬅️ Voltar aos planos", callback_data="back_to_home")])
    return InlineKeyboardMarkup(rows)


def build_access_keyboard(invite_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔓 Entrar no canal VIP", url=invite_link)]]
    )


# =========================
# UTILITÁRIOS
# =========================
def brl(value: Decimal) -> str:
    return f"{value:.2f}".replace(".", ",")


def require_env(var_name: str, value: str) -> None:
    if not value:
        raise RuntimeError(f"Variável ausente: {var_name}")


def get_mp_headers() -> dict[str, str]:
    require_env("MERCADO_PAGO_ACCESS_TOKEN", MERCADO_PAGO_ACCESS_TOKEN)
    return {
        "Authorization": f"Bearer {MERCADO_PAGO_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


async def get_telegram_app() -> Application:
    global telegram_app

    if telegram_app is None:
        require_env("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)

        app = Application.builder().token(TELEGRAM_BOT_TOKEN).updater(None).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CallbackQueryHandler(button_handler))
        telegram_app = app

    return telegram_app


async def ensure_telegram_ready() -> Application:
    global telegram_ready, telegram_started

    if telegram_ready and telegram_app is not None:
        return telegram_app

    async with telegram_lock:
        if telegram_ready and telegram_app is not None:
            return telegram_app

        app = await get_telegram_app()

        logger.info("Inicializando Telegram...")
        await app.initialize()

        logger.info("Iniciando Telegram...")
        await app.start()

        me = await app.bot.get_me()
        logger.info("Bot autenticado com sucesso: @%s", me.username)

        telegram_ready = True
        telegram_started = True
        return app


async def send_home(bot, chat_id: int) -> None:
    if START_IMAGE_URL:
        await bot.send_photo(
            chat_id=chat_id,
            photo=START_IMAGE_URL,
            caption=WELCOME_TEXT,
            parse_mode="HTML",
            reply_markup=build_main_keyboard(),
        )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=WELCOME_TEXT,
            parse_mode="HTML",
            reply_markup=build_main_keyboard(),
        )


def create_pix_payment(user_id: int, username: str | None, plan_key: str) -> dict[str, Any]:
    require_env("BASE_URL", BASE_URL)

    plan = PLANS[plan_key]
    payment_id = str(uuid4())
    external_reference = f"vip-{plan_key}-{user_id}-{payment_id}"

    payload = {
        "transaction_amount": float(plan["amount"]),
        "description": f"{PAYMENT_NAME} - {plan['title']}",
        "payment_method_id": "pix",
        "external_reference": external_reference,
        "notification_url": f"{BASE_URL}/webhook/mercadopago",
        "payer": {
            "email": f"user{user_id}@telegram.local",
            "first_name": username or "Cliente",
        },
    }

    response = requests.post(
        "https://api.mercadopago.com/v1/payments",
        headers=get_mp_headers(),
        json=payload,
        timeout=30,
    )

    if response.status_code not in (200, 201):
        logger.error("Erro Mercado Pago: %s", response.text)
        raise HTTPException(status_code=500, detail="Não foi possível criar o Pix.")

    data = response.json()
    tx_data = data.get("point_of_interaction", {}).get("transaction_data", {})

    payments_store[payment_id] = {
        "internal_payment_id": payment_id,
        "mp_payment_id": data.get("id"),
        "user_id": user_id,
        "username": username,
        "plan_key": plan_key,
        "plan_title": plan["title"],
        "amount": str(plan["amount"]),
        "status": data.get("status", "pending"),
        "external_reference": external_reference,
        "qr_code": tx_data.get("qr_code", ""),
        "qr_code_base64": tx_data.get("qr_code_base64", ""),
        "ticket_url": tx_data.get("ticket_url", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "approved_at": None,
        "access_released": False,
        "invite_link": "",
    }
    user_last_pending[user_id] = payment_id
    return payments_store[payment_id]


def get_mp_payment(mp_payment_id: str) -> dict[str, Any]:
    response = requests.get(
        f"https://api.mercadopago.com/v1/payments/{mp_payment_id}",
        headers=get_mp_headers(),
        timeout=30,
    )
    if response.status_code != 200:
        logger.error("Falha ao consultar pagamento MP: %s", response.text)
        raise HTTPException(status_code=500, detail="Falha ao consultar o pagamento.")
    return response.json()


async def create_dynamic_invite_link(user_id: int) -> str:
    if CHANNEL_INVITE_LINK and not TELEGRAM_CHANNEL_ID and not TELEGRAM_CHANNEL_USERNAME:
        return CHANNEL_INVITE_LINK

    chat_ref = TELEGRAM_CHANNEL_ID or TELEGRAM_CHANNEL_USERNAME
    if not chat_ref:
        if CHANNEL_INVITE_LINK:
            return CHANNEL_INVITE_LINK
        raise RuntimeError("Defina CHANNEL_INVITE_LINK ou TELEGRAM_CHANNEL_ID.")

    app = await ensure_telegram_ready()

    try:
        expire_date = datetime.now(timezone.utc) + timedelta(hours=24)
        invite = await app.bot.create_chat_invite_link(
            chat_id=chat_ref,
            expire_date=expire_date,
            member_limit=1,
            name=f"vip-user-{user_id}",
        )
        return invite.invite_link
    except Exception as exc:
        logger.warning("Falha ao criar link dinâmico. Usando link fixo. Erro: %s", exc)
        if CHANNEL_INVITE_LINK:
            return CHANNEL_INVITE_LINK
        raise


async def release_access(internal_payment_id: str) -> None:
    payment = payments_store.get(internal_payment_id)
    if not payment or payment.get("access_released"):
        return

    app = await ensure_telegram_ready()
    invite_link = await create_dynamic_invite_link(payment["user_id"])

    payment["access_released"] = True
    payment["invite_link"] = invite_link
    payment["approved_at"] = datetime.now(timezone.utc).isoformat()

    text = (
        f"✅ <b>Pagamento aprovado!</b>\n\n"
        f"<b>Plano:</b> {payment['plan_title']}\n"
        f"<b>Status:</b> Acesso liberado\n\n"
        "Toque no botão abaixo para entrar no canal VIP."
    )

    await app.bot.send_message(
        chat_id=payment["user_id"],
        text=text,
        parse_mode="HTML",
        reply_markup=build_access_keyboard(invite_link),
    )


# =========================
# HANDLERS TELEGRAM
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    await send_home(context.bot, chat.id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "/start - abrir vitrine de planos\n"
        "/help - mostrar ajuda"
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return

    await query.answer()
    data = query.data or ""
    user = query.from_user

    if data == "back_to_home":
        await send_home(context.bot, query.message.chat.id)
        return

    if data.startswith("plan_"):
        plan_key = data.replace("plan_", "")
        plan = PLANS.get(plan_key)
        if not plan:
            await query.message.reply_text("Plano não encontrado.")
            return

        text = (
            f"<b>{plan['title']}</b>\n\n"
            f"{plan['description']}\n\n"
            f"<b>Valor:</b> R$ {brl(plan['amount'])}\n\n"
            "Ao continuar, o bot vai gerar um Pix automático para pagamento imediato."
        )
        await query.message.reply_text(
            text=text,
            parse_mode="HTML",
            reply_markup=build_plan_keyboard(plan_key),
        )
        return

    if data.startswith("buy_"):
        plan_key = data.replace("buy_", "")
        if plan_key not in PLANS:
            await query.message.reply_text("Plano não encontrado.")
            return

        payment = create_pix_payment(
            user_id=user.id,
            username=user.username or user.first_name,
            plan_key=plan_key,
        )

        text = (
            f"<b>{payment['plan_title']}</b>\n\n"
            f"<b>Valor:</b> R$ {brl(Decimal(payment['amount']))}\n"
            f"<b>Status:</b> Aguardando pagamento\n\n"
            f"<b>Copia e cola Pix:</b>\n<code>{payment['qr_code']}</code>\n\n"
            "Após pagar, o sistema libera seu acesso automaticamente.\n"
            "Você também pode tocar em verificar pagamento abaixo."
        )

        await query.message.reply_text(
            text=text,
            parse_mode="HTML",
            reply_markup=build_payment_keyboard(
                payment["internal_payment_id"],
                payment.get("ticket_url", ""),
            ),
        )
        return

    if data.startswith("check_"):
        internal_payment_id = data.replace("check_", "")
        payment = payments_store.get(internal_payment_id)
        if not payment:
            await query.message.reply_text("Pagamento não encontrado.")
            return

        mp_data = get_mp_payment(str(payment["mp_payment_id"]))
        status = mp_data.get("status", "pending")
        payment["status"] = status

        if status == "approved":
            await release_access(internal_payment_id)
            await query.message.reply_text(
                "✅ Pagamento confirmado. Seu acesso foi liberado no chat."
            )
            return

        await query.message.reply_text(
            f"⏳ Pagamento ainda não aprovado. Status atual: <b>{status}</b>",
            parse_mode="HTML",
        )
        return

    await query.message.reply_text("Opção não reconhecida.")


# =========================
# FASTAPI
# =========================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("FastAPI iniciada.")
    try:
        yield
    finally:
        global telegram_started, telegram_ready

        if telegram_app is not None:
            try:
                if telegram_started:
                    await telegram_app.stop()
                    logger.info("Telegram stop OK")
            except Exception as exc:
                logger.warning("Falha no stop do Telegram: %s", exc)

            try:
                if telegram_ready:
                    await telegram_app.shutdown()
                    logger.info("Telegram shutdown OK")
            except Exception as exc:
                logger.warning("Falha no shutdown do Telegram: %s", exc)


app = FastAPI(title="Telegram VIP Bot", lifespan=lifespan)


@app.get("/")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "telegram_ready": telegram_ready,
        "base_url_ok": bool(BASE_URL),
        "image_url_ok": bool(START_IMAGE_URL),
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> JSONResponse:
    try:
        app_telegram = await ensure_telegram_ready()
        data = await request.json()
        update = Update.de_json(data, app_telegram.bot)
        await app_telegram.process_update(update)
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.exception("Erro no webhook do Telegram: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/webhook/mercadopago")
async def mercadopago_webhook(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        logger.info("Webhook Mercado Pago recebido: %s", payload)

        event_type = payload.get("type") or payload.get("action")
        data = payload.get("data", {})
        mp_payment_id = data.get("id")

        if not mp_payment_id:
            return JSONResponse({"received": True, "ignored": True})

        mp_data = get_mp_payment(str(mp_payment_id))
        external_reference = mp_data.get("external_reference", "")
        status = mp_data.get("status", "pending")

        found_id = None
        for internal_payment_id, payment in payments_store.items():
            if str(payment.get("mp_payment_id")) == str(mp_payment_id):
                payment["status"] = status
                found_id = internal_payment_id
                break
            if payment.get("external_reference") == external_reference:
                payment["status"] = status
                payment["mp_payment_id"] = mp_payment_id
                found_id = internal_payment_id
                break

        if found_id and status == "approved":
            await release_access(found_id)

        return JSONResponse(
            {
                "received": True,
                "event_type": event_type,
                "status": status,
            }
        )
    except Exception as exc:
        logger.exception("Erro no webhook do Mercado Pago: %s", exc)
        return JSONResponse({"received": False, "error": str(exc)}, status_code=500)


@app.get("/debug/payment/{internal_payment_id}")
async def debug_payment(internal_payment_id: str) -> dict[str, Any]:
    payment = payments_store.get(internal_payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Pagamento não encontrado.")
    return payment
