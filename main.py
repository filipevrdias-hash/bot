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

# ============================================================
# BOT TELEGRAM + PIX AUTOMÁTICO (MERCADO PAGO) + CANAL VIP
# ============================================================
# Dependências:
#   pip install fastapi uvicorn python-telegram-bot requests python-dotenv
#
# Start command no Railway:
#   uvicorn main:app --host 0.0.0.0 --port $PORT
#
# Variáveis no Railway:
#   TELEGRAM_BOT_TOKEN=...
#   MERCADO_PAGO_ACCESS_TOKEN=...
#   START_IMAGE_URL=https://sua-imagem-publica.jpg
#   BASE_URL=https://seu-app.up.railway.app
#   PAYMENT_NAME=Telegram Filipe Dias
#   CHANNEL_INVITE_LINK=https://t.me/+...
#   TELEGRAM_CHANNEL_ID=-100...
#   TELEGRAM_CHANNEL_USERNAME=@seucanal   (opcional)
#
# Observações:
# - O bot precisa ser ADMIN do canal VIP para criar links dinâmicos.
# - Se não conseguir criar links dinâmicos, ele usa CHANNEL_INVITE_LINK.
# - Para produção, prefira guardar tudo em variáveis do Railway.
# ============================================================

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
# BANCO SIMPLES EM MEMÓRIA
# =========================
# Em produção, o ideal é trocar por PostgreSQL, Redis, Supabase ou SQLite.
payments_store: dict[str, dict[str, Any]] = {}
user_last_pending: dict[int, str] = {}


# =========================
# TELEGRAM APPLICATION
# =========================
telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).updater(None).build()


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
def validate_settings() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not MERCADO_PAGO_ACCESS_TOKEN:
        missing.append("MERCADO_PAGO_ACCESS_TOKEN")
    if not BASE_URL:
        missing.append("BASE_URL")
    if not START_IMAGE_URL:
        missing.append("START_IMAGE_URL")
    if not CHANNEL_INVITE_LINK and not TELEGRAM_CHANNEL_ID and not TELEGRAM_CHANNEL_USERNAME:
        missing.append("CHANNEL_INVITE_LINK ou TELEGRAM_CHANNEL_ID")
    if missing:
        raise RuntimeError(f"Configurações ausentes: {', '.join(missing)}")


def brl(value: Decimal) -> str:
    return f"{value:.2f}".replace(".", ",")


def get_mp_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {MERCADO_PAGO_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def create_pix_payment(user_id: int, username: str | None, plan_key: str) -> dict[str, Any]:
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
        return CHANNEL_INVITE_LINK

    try:
        expire_date = datetime.now(timezone.utc) + timedelta(hours=24)
        invite = await telegram_app.bot.create_chat_invite_link(
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
    if not payment:
        return
    if payment.get("access_released"):
        return

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
    await telegram_app.bot.send_message(
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

    await context.bot.send_photo(
        chat_id=chat.id,
        photo=START_IMAGE_URL,
        caption=WELCOME_TEXT,
        parse_mode="HTML",
        reply_markup=build_main_keyboard(),
    )


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
        await query.message.reply_photo(
            photo=START_IMAGE_URL,
            caption=WELCOME_TEXT,
            parse_mode="HTML",
            reply_markup=build_main_keyboard(),
        )
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
            reply_markup=build_payment_keyboard(payment["internal_payment_id"], payment.get("ticket_url", "")),
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
            await query.message.reply_text("✅ Pagamento confirmado. Seu acesso foi liberado no chat.")
            return

        await query.message.reply_text(
            f"⏳ Pagamento ainda não aprovado. Status atual: <b>{status}</b>",
            parse_mode="HTML",
        )
        return

    await query.message.reply_text("Opção não reconhecida.")


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("help", help_command))
telegram_app.add_handler(CallbackQueryHandler(button_handler))


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_settings()
    await telegram_app.initialize()
    await telegram_app.start()
    logger.info("Aplicação iniciada com sucesso.")
    try:
        yield
    finally:
        await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("Aplicação finalizada.")


app = FastAPI(title="Telegram VIP Bot", lifespan=lifespan)


# =========================
# ROTAS FASTAPI
# =========================
@app.get("/")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> JSONResponse:
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.update_queue.put(update)
    return JSONResponse({"ok": True})


@app.post("/webhook/mercadopago")
async def mercadopago_webhook(request: Request) -> JSONResponse:
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


@app.get("/debug/payment/{internal_payment_id}")
async def debug_payment(internal_payment_id: str) -> dict[str, Any]:
    payment = payments_store.get(internal_payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Pagamento não encontrado.")
    return payment
