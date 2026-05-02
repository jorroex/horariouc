import os
import asyncio
import nest_asyncio
nest_asyncio.apply()

import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler, filters
import pytz

# Configuración de zona horaria - PERÚ
ZONA_HORARIA = pytz.timezone('America/Lima')

# MODIFICACIÓN PARA RENDER: Leer token y puerto desde el servidor
TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 8443))
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL")

# Estados para la conversación
REGISTRO_ASIGNATURA, REGISTRO_DIA, REGISTRO_HORA, REGISTRO_SALON = range(4)

# Memoria de la aplicación
db_usuarios = {}
recordatorios_activos = {}

# Días de la semana
DIAS = {'Lunes': 0, 'Martes': 1, 'Miércoles': 2, 'Jueves': 3, 'Viernes': 4, 'Sábado': 5, 'Domingo': 6}
DIAS_LISTA = list(DIAS.keys())

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ==================== TECLADOS PRE-COMPILADOS ====================

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

# ==================== FUNCIONES BASE ====================

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

# ==================== SISTEMA ANTI-CONGELAMIENTO (HISTORIAL) ====================

async def _borrar_msg(bot, chat_id, msg_id):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception:
        pass

def registrar_msg_usuario(update: Update):
    if not update.message: return
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    u_data = get_user(user_id)
    u_data['historial'].append(update.message.message_id)
    while len(u_data['historial']) > 4:
        v_id = u_data['historial'].pop(0)
        asyncio.create_task(_borrar_msg(update.get_bot(), chat_id, v_id))

async def enviar_msg(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str, reply_markup=KB_MAIN):
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)
    u_data = get_user(user_id)
    msg = await context.bot.send_message(chat_id=chat_id, text=texto, parse_mode='Markdown', reply_markup=reply_markup)
    u_data['historial'].append(msg.message_id)
    while len(u_data['historial']) > 4:
        v_id = u_data['historial'].pop(0)
        asyncio.create_task(_borrar_msg(context.bot, chat_id, v_id))
    return msg

# ==================== RECORDATORIOS ====================

async def enviar_recordatorio(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    u_data = get_user(data['user_id'])
    if u_data.get('recordatorios', True):
        texto = f"🔔 *¡RECORDATORIO!*\n\n📚 {data['materia']}\n🕐 Inicia en 10 min ({data['hora']})\n📍 Salón: {data['salon']}"
        try:
            msg = await context.bot.send_message(chat_id=u_data['chat_id'], text=texto, parse_mode='Markdown')
            u_data['historial'].append(msg.message_id)
            while len(u_data['historial']) > 4:
                asyncio.create_task(_borrar_msg(context.bot, u_data['chat_id'], u_data['historial'].pop(0)))
        except: pass

async def reprogramar_alarmas(job_queue, user_id):
    if not job_queue: return
    u_data = get_user(user_id)
    for job in recordatorios_activos.get(user_id, []): job.schedule_removal()
    recordatorios_activos[user_id] = []
    if not u_data['recordatorios']: return
    ahora = get_ahora()
    for c in u_data['materias']:
        try:
            h, m = map(int, c['hora'].split(':'))
            dias_hasta = (DIAS[c['dia']] - ahora.weekday()) % 7
            fecha = (ahora + timedelta(days=dias_hasta)).replace(hour=h, minute=m, second=0, microsecond=0)
            alerta = fecha - timedelta(minutes=10)
            if alerta < ahora: alerta += timedelta(days=7)
            segundos = (alerta - ahora).total_seconds()
            if segundos > 0:
                job = job_queue.run_once(enviar_recordatorio, when=segundos, data={'user_id': user_id, **c})
                recordatorios_activos[user_id].append(job)
        except Exception as e:
            logging.error(f"Error en alarma: {e}")

# ==================== COMANDOS Y ENRUTADORES (TU LÓGICA) ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    registrar_msg_usuario(update)
    user_id = str(update.effective_user.id)
    get_user(user_id)['chat_id'] = update.effective_chat.id
    await reprogramar_alarmas(context.job_queue, user_id)
    ahora = get_ahora()
    txt = f"🎓 *¡Bienvenido!*\n\n🇵🇪 Hora: {ahora.strftime('%H:%M')}\n📅 Fecha: {ahora.strftime('%d/%m/%Y')}\n\nSelecciona una opción:"
    await enviar_msg(update, context, txt)

async def cmd_horario_completo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u_data = get_user(str(update.effective_user.id))
    if not u_data['materias']:
        return await enviar_msg(update, context, "📭 *No tienes clases registradas*", KB_HORARIO)
    bloques = ["🗓️ *HORARIO SEMANAL COMPLETO*\n"]
    for dia in DIAS_LISTA:
        clases = [c for c in u_data['materias'] if c['dia'] == dia]
        if clases:
            clases.sort(key=lambda x: x['hora'])
            bloques.append(f"*{dia.upper()}*")
            bloques.extend([f"• {c['hora']} - *{c['materia']}* ({c['salon']})" for c in clases])
            bloques.append("") 
    await enviar_msg(update, context, "\n".join(bloques), KB_HORARIO)

async def cmd_horario_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u_data = get_user(str(update.effective_user.id))
    hoy_es = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo'][get_ahora().weekday()]
    clases = sorted([c for c in u_data['materias'] if c['dia'] == hoy_es], key=lambda x: x['hora'])
    if not clases:
        txt = f"☀️ *HOY ({hoy_es})*\n✅ ¡Día libre, sin clases!"
    else:
        bloques = [f"📖 *HORARIO DE HOY ({hoy_es})*\n"]
        bloques.extend([f"• {c['hora']} - *{c['materia']}*\n📍 Salón: {c['salon']}\n" for c in clases])
        txt = "\n".join(bloques)
    await enviar_msg(update, context, txt, KB_HORARIO)

async def cmd_proxima_clase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = obtener_proxima_clase(get_user(str(update.effective_user.id)))
    txt = f"⏰ *PRÓXIMA CLASE*\n\n📚 {c['materia']}\n📅 {c['dia']} {c['hora']}\n📍 Salón: {c['salon']}" if c else "✅ No tienes más clases."
    await enviar_msg(update, context, txt)

# --- FLUJO REGISTRO ---
async def reg_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    registrar_msg_usuario(update)
    context.user_data.clear()
    await enviar_msg(update, context, "📝 *Registro (1/4)*\n\nNombre de la asignatura:", KB_CANCELAR)
    return REGISTRO_ASIGNATURA

async def reg_asignatura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    registrar_msg_usuario(update)
    if update.message.text in ["❌ Cancelar", "cancelar"]:
        await enviar_msg(update, context, "❌ Cancelado.")
        return ConversationHandler.END
    context.user_data['materia'] = update.message.text
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(d, callback_data=f"dia_{d}")] for d in DIAS.keys()])
    await enviar_msg(update, context, f"Asignatura: {update.message.text}\n📅 *Selecciona el día:*", kb)
    return REGISTRO_DIA

async def reg_dia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try: await q.message.delete()
    except: pass
    context.user_data['dia'] = q.data.split('_')[1]
    await enviar_msg(update, context, f"✅ Día: {context.user_data['dia']}\n\n*Paso 3/4:* Hora (HH:MM):", KB_CANCELAR)
    return REGISTRO_HORA

async def reg_hora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    registrar_msg_usuario(update)
    if update.message.text in ["❌ Cancelar", "cancelar"]:
        await enviar_msg(update, context, "❌ Cancelado.")
        return ConversationHandler.END
    try:
        datetime.strptime(update.message.text, "%H:%M")
        context.user_data['hora'] = update.message.text
        await enviar_msg(update, context, "*Paso 4/4:* Salón/Aula:", KB_CANCELAR)
        return REGISTRO_SALON
    except:
        await enviar_msg(update, context, "❌ Formato inválido. Reintenta:", KB_CANCELAR)
        return REGISTRO_HORA

async def reg_salon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    registrar_msg_usuario(update)
    if update.message.text in ["❌ Cancelar", "cancelar"]:
        await enviar_msg(update, context, "❌ Cancelado.")
        return ConversationHandler.END
    u_data = get_user(str(update.effective_user.id))
    clase = {'materia': context.user_data.get('materia'), 'dia': context.user_data.get('dia'), 'hora': context.user_data.get('hora'), 'salon': update.message.text}
    u_data['materias'].append(clase)
    await reprogramar_alarmas(context.job_queue, str(update.effective_user.id))
    await enviar_msg(update, context, f"✅ *¡Guardada!*\n{clase['materia']} | {clase['dia']} {clase['hora']}")
    return ConversationHandler.END

async def enrutador_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    registrar_msg_usuario(update)
    t = update.message.text
    uid = str(update.effective_user.id)
    if t == "📚 Ver Horario": await enviar_msg(update, context, "👀 ¿Qué deseas ver?", KB_HORARIO)
    elif t == "📖 Horario de Hoy": await cmd_horario_hoy(update, context)
    elif t == "🗓️ Horario Semanal": await cmd_horario_completo(update, context)
    elif t == "⏰ Próxima Clase": await cmd_proxima_clase(update, context)
    elif t == "🗑 Eliminar Clase":
        materias = get_user(uid)['materias']
        if not materias: await enviar_msg(update, context, "📭 No hay clases.")
        else:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"{c['materia']} ({c['dia']})", callback_data=f"del_{i}")] for i, c in enumerate(materias)] + [[InlineKeyboardButton("❌ Cancelar", callback_data="del_cancel")]])
            await enviar_msg(update, context, "🗑 Selecciona para eliminar:", kb)
    elif t == "🔔 Recordatorios":
        kb = ReplyKeyboardMarkup([["✅ Activar Recordatorios"], ["❌ Desactivar Recordatorios"], ["⬅️ Volver al Menú"]], resize_keyboard=True)
        await enviar_msg(update, context, f"🔔 Estado: {'ACTIVOS' if get_user(uid)['recordatorios'] else 'INACTIVOS'}", kb)
    elif t == "🕐 Hora Perú": await enviar_msg(update, context, f"🇵🇪 *Hora:* {get_ahora().strftime('%H:%M:%S')}")
    elif t in ["✅ Activar Recordatorios", "❌ Desactivar Recordatorios"]:
        get_user(uid)['recordatorios'] = "Activar" in t
        await reprogramar_alarmas(context.job_queue, uid)
        await enviar_msg(update, context, "✅ Guardado.")
    elif t in ["❓ Ayuda", "⬅️ Volver al Menú"]: await enviar_msg(update, context, "Menú principal:", KB_MAIN)

async def enrutador_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try: await q.message.delete()
    except: pass
    if q.data != "del_cancel":
        idx = int(q.data.split('_')[1])
        get_user(str(q.from_user.id))['materias'].pop(idx)
        await reprogramar_alarmas(context.job_queue, str(q.from_user.id))
        await enviar_msg(update, context, "✅ Eliminada.")

# ==================== INICIO (AJUSTADO PARA WEBHOOK) ====================
def main():
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
    app.add_handler(CallbackQueryHandler(enrutador_eliminar, pattern="^del_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, enrutador_texto))

    if RENDER_URL:
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=TOKEN, webhook_url=f"{RENDER_URL}/{TOKEN}")
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
