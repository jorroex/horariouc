import os
import asyncio
import logging
from datetime import datetime, timedelta
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler, filters

# CONFIGURACIÓN
TOKEN = os.getenv("TELEGRAM_TOKEN")
ZONA_HORARIA = pytz.timezone('America/Lima')
PORT = int(os.environ.get("PORT", 8443))  # Render asigna este puerto automáticamente
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL") # URL de tu servicio en Render

# Estados
REGISTRO_ASIGNATURA, REGISTRO_DIA, REGISTRO_HORA, REGISTRO_SALON = range(4)

# Memoria temporal
db_usuarios = {}
recordatorios_activos = {}
DIAS = {'Lunes': 0, 'Martes': 1, 'Miércoles': 2, 'Jueves': 3, 'Viernes': 4, 'Sábado': 5, 'Domingo': 6}

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- TECLADOS ---
KB_MAIN = ReplyKeyboardMarkup([
    [KeyboardButton("📚 Ver Horario"), KeyboardButton("⏰ Próxima Clase")],
    [KeyboardButton("➕ Registrar Clase"), KeyboardButton("🗑 Eliminar Clase")],
    [KeyboardButton("🔔 Recordatorios"), KeyboardButton("🕐 Hora Perú")],
    [KeyboardButton("❓ Ayuda")]
], resize_keyboard=True)

KB_HORARIO = ReplyKeyboardMarkup([
    [KeyboardButton("📖 Horario de Hoy"), KeyboardButton("🗓️ Horario Semanal")],
    [KeyboardButton("⬅️ Volver al Menú")]
], resize_keyboard=True)

KB_CANCELAR = ReplyKeyboardMarkup([[KeyboardButton("❌ Cancelar")]], resize_keyboard=True)

# --- FUNCIONES AUXILIARES ---
def get_ahora(): return datetime.now(ZONA_HORARIA)

def get_user(user_id):
    if user_id not in db_usuarios:
        db_usuarios[user_id] = {'materias': [], 'recordatorios': True, 'chat_id': None, 'historial': []}
    return db_usuarios[user_id]

async def _borrar_msg(bot, chat_id, msg_id):
    try: await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except: pass

async def enviar_msg(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str, reply_markup=KB_MAIN):
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)
    u_data = get_user(user_id)
    msg = await context.bot.send_message(chat_id=chat_id, text=texto, parse_mode='Markdown', reply_markup=reply_markup)
    u_data['historial'].append(msg.message_id)
    while len(u_data['historial']) > 4:
        asyncio.create_task(_borrar_msg(context.bot, chat_id, u_data['historial'].pop(0)))
    return msg

# --- COMANDOS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    get_user(user_id)['chat_id'] = update.effective_chat.id
    await enviar_msg(update, context, "🎓 *¡Bot de Horario Activo!* Usa el menú inferior.")

async def reg_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await enviar_msg(update, context, "📝 *Nombre de la asignatura:*", KB_CANCELAR)
    return REGISTRO_ASIGNATURA

async def reg_asignatura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Cancelar": return ConversationHandler.END
    context.user_data['materia'] = update.message.text
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(d, callback_data=f"dia_{d}")] for d in DIAS.keys()])
    await enviar_msg(update, context, "📅 *Selecciona el día:*", kb)
    return REGISTRO_DIA

async def reg_dia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data['dia'] = q.data.split('_')[1]
    await q.message.delete()
    await enviar_msg(update, context, f"✅ Día: {context.user_data['dia']}\n\n*Hora (HH:MM):*", KB_CANCELAR)
    return REGISTRO_HORA

async def reg_hora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hora = update.message.text
    try:
        datetime.strptime(hora, "%H:%M")
        context.user_data['hora'] = hora
        await enviar_msg(update, context, "*Salón/Aula:*", KB_CANCELAR)
        return REGISTRO_SALON
    except:
        await enviar_msg(update, context, "❌ Formato inválido. Usa HH:MM (Ej: 14:30):")
        return REGISTRO_HORA

async def reg_salon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    u_data = get_user(user_id)
    clase = {
        'materia': context.user_data['materia'], 
        'dia': context.user_data['dia'], 
        'hora': context.user_data['hora'], 
        'salon': update.message.text
    }
    u_data['materias'].append(clase)
    await enviar_msg(update, context, "✅ *¡Clase Guardada!*")
    return ConversationHandler.END

async def enrutador_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text
    uid = str(update.effective_user.id)
    if t == "📚 Ver Horario": await enviar_msg(update, context, "¿Qué deseas ver?", KB_HORARIO)
    elif t == "📖 Horario de Hoy":
        hoy = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo'][get_ahora().weekday()]
        clases = sorted([c for c in get_user(uid)['materias'] if c['dia'] == hoy], key=lambda x: x['hora'])
        txt = f"📖 *HORARIO DE HOY ({hoy})*\n" + ("\n".join([f"• {c['hora']} - {c['materia']} ({c['salon']})" for c in clases]) if clases else "Día libre")
        await enviar_msg(update, context, txt)
    elif t == "⬅️ Volver al Menú": await enviar_msg(update, context, "Menú principal:", KB_MAIN)
    elif t == "🕐 Hora Perú": await enviar_msg(update, context, f"🇵🇪 Hora: {get_ahora().strftime('%H:%M')}")

# --- MAIN CON WEBHOOK ---
def main():
    if not TOKEN: return
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Registrar Clase$"), reg_inicio)],
        states={
            REGISTRO_ASIGNATURA: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_asignatura)],
            REGISTRO_DIA: [CallbackQueryHandler(reg_dia, pattern="^dia_")],
            REGISTRO_HORA: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_hora)],
            REGISTRO_SALON: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_salon)]
        },
        fallbacks=[CommandHandler("cancel", start)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, enrutador_texto))

    # Lógica de Webhook para Render
    if RENDER_URL:
        logging.info(f"Iniciando Webhook en {RENDER_URL}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{RENDER_URL}/{TOKEN}"
        )
    else:
        logging.info("Iniciando Polling (Local)...")
        app.run_polling()

if __name__ == "__main__":
    main()
