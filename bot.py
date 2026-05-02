import os
import asyncio
import logging
from datetime import datetime, timedelta
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler, filters

# CONFIGURACIÓN DE SEGURIDAD
# Render leerá el Token desde las variables de entorno
TOKEN = os.getenv("TELEGRAM_TOKEN")
ZONA_HORARIA = pytz.timezone('America/Lima')

# Estados para la conversación
REGISTRO_ASIGNATURA, REGISTRO_DIA, REGISTRO_HORA, REGISTRO_SALON = range(4)

# Memoria de la aplicación (Temporal, se borra si el bot se reinicia)
db_usuarios = {}
recordatorios_activos = {}

# Días de la semana
DIAS = {'Lunes': 0, 'Martes': 1, 'Miércoles': 2, 'Jueves': 3, 'Viernes': 4, 'Sábado': 5, 'Domingo': 6}
DIAS_LISTA = list(DIAS.keys())

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ==================== TECLADOS ====================
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

KB_CANCELAR = ReplyKeyboardMarkup([[KeyboardButton("❌ Cancelar")]], resize_keyboard=True, one_time_keyboard=True)

# ==================== FUNCIONES AUXILIARES ====================
def get_ahora(): return datetime.now(ZONA_HORARIA)

def get_user(user_id):
    if user_id not in db_usuarios:
        db_usuarios[user_id] = {'materias': [], 'recordatorios': True, 'chat_id': None, 'historial': []}
    return db_usuarios[user_id]

def obtener_proxima_clase(u_data):
    if not u_data['materias']: return None
    ahora = get_ahora()
    proximas = []
    for c in u_data['materias']:
        h, m = map(int, c['hora'].split(':'))
        dias_hasta = (DIAS[c['dia']] - ahora.weekday()) % 7
        fecha_clase = ahora + timedelta(days=dias_hasta)
        fecha_clase = fecha_clase.replace(hour=h, minute=m, second=0, microsecond=0)
        if dias_hasta == 0 and fecha_clase <= ahora:
            fecha_clase += timedelta(days=7)
        proximas.append((fecha_clase, c))
    proximas.sort(key=lambda x: x[0])
    return proximas[0][1]

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

# ==================== LÓGICA DE ALARMAS ====================
async def enviar_recordatorio(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    u_data = get_user(data['user_id'])
    if u_data.get('recordatorios', True):
        texto = f"🔔 *¡RECORDATORIO!*\n\n📚 {data['materia']}\n🕐 Inicia en 10 min ({data['hora']})\n📍 Salón: {data['salon']}"
        try: await context.bot.send_message(chat_id=u_data['chat_id'], text=texto, parse_mode='Markdown')
        except: pass

async def reprogramar_alarmas(job_queue, user_id):
    if not job_queue: return
    u_data = get_user(user_id)
    for job in recordatorios_activos.get(user_id, []): job.schedule_removal()
    recordatorios_activos[user_id] = []
    if not u_data['recordatorios']: return
    ahora = get_ahora()
    for c in u_data['materias']:
        h, m = map(int, c['hora'].split(':'))
        dias_hasta = (DIAS[c['dia']] - ahora.weekday()) % 7
        fecha = (ahora + timedelta(days=dias_hasta)).replace(hour=h, minute=m, second=0, microsecond=0)
        alerta = fecha - timedelta(minutes=10)
        if alerta < ahora: alerta += timedelta(days=7)
        segundos = (alerta - ahora).total_seconds()
        if segundos > 0:
            job = job_queue.run_once(enviar_recordatorio, when=segundos, data={'user_id': user_id, **c})
            recordatorios_activos[user_id].append(job)

# ==================== COMANDOS Y FLUJOS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    get_user(user_id)['chat_id'] = update.effective_chat.id
    await reprogramar_alarmas(context.job_queue, user_id)
    await enviar_msg(update, context, "🎓 *¡Bienvenido!* Selecciona una opción:")

async def reg_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await enviar_msg(update, context, "📝 *Registro (1/4)*\nNombre de la asignatura:", KB_CANCELAR)
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
    await enviar_msg(update, context, f"✅ Día: {context.user_data['dia']}\n\n*Paso 3/4:* Hora (HH:MM):", KB_CANCELAR)
    return REGISTRO_HORA

async def reg_hora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hora = update.message.text
    try:
        datetime.strptime(hora, "%H:%M")
        context.user_data['hora'] = hora
        await enviar_msg(update, context, "*Paso 4/4:* Salón/Aula:", KB_CANCELAR)
        return REGISTRO_SALON
    except:
        await enviar_msg(update, context, "❌ Formato HH:MM inválido. Reintenta:")
        return REGISTRO_HORA

async def reg_salon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    u_data = get_user(user_id)
    clase = {'materia': context.user_data['materia'], 'dia': context.user_data['dia'], 'hora': context.user_data['hora'], 'salon': update.message.text}
    u_data['materias'].append(clase)
    await reprogramar_alarmas(context.job_queue, user_id)
    await enviar_msg(update, context, "✅ *¡Clase Guardada!*")
    return ConversationHandler.END

async def enrutador_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text
    uid = str(update.effective_user.id)
    if t == "📚 Ver Horario": await enviar_msg(update, context, "¿Qué deseas ver?", KB_HORARIO)
    elif t == "📖 Horario de Hoy":
        hoy = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo'][get_ahora().weekday()]
        clases = sorted([c for c in get_user(uid)['materias'] if c['dia'] == hoy], key=lambda x: x['hora'])
        txt = f"📖 *HORARIO DE HOY ({hoy})*\n" + ("\n".join([f"• {c['hora']} - {c['materia']}" for c in clases]) if clases else "Día libre")
        await enviar_msg(update, context, txt)
    elif t == "⏰ Próxima Clase":
        c = obtener_proxima_clase(get_user(uid))
        await enviar_msg(update, context, f"⏰ *PRÓXIMA:* {c['materia']} ({c['hora']})" if c else "Sin clases")
    elif t == "⬅️ Volver al Menú": await enviar_msg(update, context, "Menú principal:", KB_MAIN)
    # Agrega aquí más opciones si deseas...

def main():
    if not TOKEN:
        print("ERROR: No se encontró el TOKEN en las variables de entorno.")
        return
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
    app.run_polling()

if __name__ == "__main__":
    main()
