"""
Studio Lavori Bot — Telegram bot per gestire i lavori da telefono.
Usa Google Sheets come database condiviso.

Comandi:
  /aggiungi  — aggiunge un lavoro (flow guidato)
  /oggi      — lavori di oggi
  /mese      — resoconto mese corrente
  /totale    — totale anno corrente
  /cerca     — cerca per nome cliente
  /esporta   — manda backup JSON (importabile nell'app web)
  /annulla   — annulla operazione in corso
"""
import os
import io
import json
import logging
import asyncio
import threading
import urllib.request
from datetime import date, datetime, timedelta

from flask import Flask, request as flask_request
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, CallbackQueryHandler, filters, ContextTypes
)
import sheets

# ── Configurazione ───────────────────────────────────────────────────────────

BOT_TOKEN   = os.environ["BOT_TOKEN"]
CHAT_ID     = int(os.environ.get("CHAT_ID", "511720056"))
PORT        = int(os.environ.get("PORT", 8080))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Flask health-check (per tenere Render sveglio) ───────────────────────────

flask_app = Flask(__name__)

@flask_app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

@flask_app.route("/")
def health():
    return "Studio Lavori Bot attivo ✅", 200

@flask_app.route("/invia-backup", methods=["POST", "OPTIONS"])
def invia_backup():
    if flask_request.method == "OPTIONS":
        from flask import Response
        return Response(status=204)
    try:
        data = flask_request.get_json(force=True, silent=True) or {}
        if isinstance(data, list):
            lavori = data
        else:
            lavori = data.get("lavori") or data.get("registro") or []
        if not lavori:
            return {"error": "formato non riconosciuto o backup vuoto"}, 400
        _pending_backup[CHAT_ID] = lavori
        n = len(lavori)
        kb = json.dumps({"inline_keyboard": [[
            {"text": "✅ Sincronizza", "callback_data": "backup_ok"},
            {"text": "❌ Annulla",    "callback_data": "backup_cancel"}
        ]]})
        payload = json.dumps({
            "chat_id": CHAT_ID,
            "text": (
                f"📦 *Backup ricevuto dal PC*\n\n"
                f"Contiene *{n} lavori*.\n\n"
                f"⚠️ Questa operazione *sostituisce tutti i dati* del bot con quelli dell\'app web.\n\n"
                f"Cosa vuoi fare?"
            ),
            "parse_mode": "Markdown",
            "reply_markup": kb
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=10)
        return {"ok": True, "lavori": n}
    except Exception as e:
        logger.error("invia_backup error: %s", e)
        return {"error": str(e)}, 500


@flask_app.route("/sync-lavoro", methods=["POST", "OPTIONS"])
def sync_lavoro():
    """Sync real-time: aggiunge, modifica o elimina un singolo lavoro su Sheets."""
    if flask_request.method == "OPTIONS":
        from flask import Response
        return Response(status=204)
    try:
        data = flask_request.get_json(force=True, silent=True) or {}
        op = data.get("op", "add")
        if op == "delete":
            record_id = str(data.get("id", ""))
            if not record_id:
                return {"error": "id mancante"}, 400
            sheets.delete_lavoro(record_id)
            return {"ok": True, "op": "delete"}
        else:
            lavoro = data.get("lavoro")
            if not lavoro:
                return {"error": "lavoro mancante"}, 400
            sheets.upsert_lavoro(lavoro)
            return {"ok": True, "op": op}
    except Exception as e:
        logger.error("sync_lavoro error: %s", e)
        return {"error": str(e)}, 500

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ── Guard: solo il tuo chat_id può usare il bot ──────────────────────────────

def solo_pino(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != CHAT_ID:
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper

# ── Utility date ─────────────────────────────────────────────────────────────

def parse_data(testo: str) -> str | None:
    """
    Converte input umano in YYYY-MM-DD.
    Accetta: oggi, ieri, DD/MM/YYYY, DD/MM (anno corrente), YYYY-MM-DD.
    Ritorna None se non riconosce il formato.
    """
    t = testo.strip().lower()
    if t in ("oggi", "o", ""):
        return date.today().isoformat()
    if t in ("ieri", "i"):
        return (date.today() - timedelta(days=1)).isoformat()
    # DD/MM/YYYY o DD/MM
    for fmt in ("%d/%m/%Y", "%d/%m"):
        try:
            d = datetime.strptime(testo.strip(), fmt)
            if d.year == 1900:
                d = d.replace(year=date.today().year)
            return d.date().isoformat()
        except ValueError:
            continue
    # YYYY-MM-DD
    try:
        datetime.strptime(testo.strip(), "%Y-%m-%d")
        return testo.strip()
    except ValueError:
        pass
    return None

def fmt_data(data_iso: str) -> str:
    """YYYY-MM-DD → 'GG/MM/YYYY'"""
    try:
        return datetime.strptime(data_iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return data_iso

def fmt_prezzo(p) -> str:
    try:
        return f"{float(p):.2f} €"
    except Exception:
        return str(p)

def lavoro_str(l: dict) -> str:
    riga = f"📅 {fmt_data(l['data'])}  •  {l['descrizione']}  •  {fmt_prezzo(l['prezzo'])}"
    if l.get("nota"):
        riga += f"\n    📝 {l['nota']}"
    if l.get("tempo"):
        riga += f"  •  ⏱ {l['tempo']}"
    return riga

# ── ConversationHandler: /aggiungi ───────────────────────────────────────────

DATA_STEP, NOME_STEP, PREZZO_STEP, NOTA_STEP = range(4)
CERCA_STEP = 10

@solo_pino
async def aggiungi_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "📅 *Che data?*\n"
        "Scrivi _oggi_, _ieri_, oppure GG/MM o GG/MM/AAAA",
        parse_mode="Markdown"
    )
    return DATA_STEP

async def aggiungi_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = parse_data(update.message.text)
    if data is None:
        await update.message.reply_text(
            "❌ Data non riconosciuta. Prova con _oggi_, _ieri_, o _12/06_",
            parse_mode="Markdown"
        )
        return DATA_STEP
    context.user_data["data"] = data
    await update.message.reply_text("👤 *Nome cliente?*", parse_mode="Markdown")
    return NOME_STEP

async def aggiungi_nome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["descrizione"] = update.message.text.strip()
    await update.message.reply_text("💶 *Prezzo?* (es. 50 o 35.50)", parse_mode="Markdown")
    return PREZZO_STEP

async def aggiungi_prezzo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    testo = update.message.text.replace(",", ".").strip()
    try:
        prezzo = float(testo)
    except ValueError:
        await update.message.reply_text("❌ Prezzo non valido. Scrivi un numero, es. _45_ o _35.50_", parse_mode="Markdown")
        return PREZZO_STEP
    context.user_data["prezzo"] = prezzo
    kb = [[InlineKeyboardButton("⏭ Salta nota", callback_data="skip_nota")]]
    await update.message.reply_text(
        "📝 *Nota?* (opzionale)\nScrivi la nota oppure premi il pulsante.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return NOTA_STEP

async def aggiungi_nota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["nota"] = update.message.text.strip()
    return await _salva_lavoro(update, context)

async def skip_nota_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["nota"] = ""
    return await _salva_lavoro(update, context, is_callback=True)

async def _salva_lavoro(update, context, is_callback=False):
    d  = context.user_data
    try:
        l = sheets.add_lavoro(
            data_str=d["data"],
            descrizione=d["descrizione"],
            prezzo=d["prezzo"],
            nota=d.get("nota", ""),
        )
        msg = (
            f"✅ *Lavoro aggiunto!*\n\n"
            f"{lavoro_str(l)}"
        )
    except Exception as e:
        msg = f"❌ Errore nel salvataggio: {e}"

    if is_callback:
        await update.callback_query.edit_message_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")
    return ConversationHandler.END

@solo_pino
async def annulla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Operazione annullata.")
    return ConversationHandler.END

# ── /oggi ────────────────────────────────────────────────────────────────────

@solo_pino
async def cmd_oggi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Recupero lavori di oggi…")
    try:
        lavori = sheets.get_lavori_oggi()
        if not lavori:
            await update.message.reply_text("📭 Nessun lavoro registrato oggi.")
            return
        totale = sum(float(l["prezzo"]) for l in lavori)
        righe = [lavoro_str(l) for l in lavori]
        testo = (
            f"📋 *Oggi — {date.today().strftime('%d/%m/%Y')}*\n\n"
            + "\n".join(righe)
            + f"\n\n💰 *Totale oggi: {totale:.2f} €*"
        )
        await update.message.reply_text(testo, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Errore: {e}")

# ── /mese ────────────────────────────────────────────────────────────────────

@solo_pino
async def cmd_mese(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Recupero resoconto mese…")
    try:
        oggi = date.today()
        lavori = sheets.get_lavori_mese(oggi.year, oggi.month)
        nomi_mesi = ["","Gennaio","Febbraio","Marzo","Aprile","Maggio","Giugno",
                     "Luglio","Agosto","Settembre","Ottobre","Novembre","Dicembre"]
        mese_nome = nomi_mesi[oggi.month]
        if not lavori:
            await update.message.reply_text(f"📭 Nessun lavoro in {mese_nome} {oggi.year}.")
            return
        totale = sum(float(l["prezzo"]) for l in lavori)
        # Raggruppa per giorno
        giorni: dict = {}
        for l in lavori:
            giorni.setdefault(l["data"], []).append(l)

        righe = []
        for d_iso in sorted(giorni.keys()):
            righe.append(f"*{fmt_data(d_iso)}*")
            for l in giorni[d_iso]:
                righe.append(f"  • {l['descrizione']}  {fmt_prezzo(l['prezzo'])}"
                              + (f"  📝{l['nota']}" if l.get("nota") else ""))

        testo = (
            f"📋 *{mese_nome} {oggi.year}*\n\n"
            + "\n".join(righe)
            + f"\n\n💰 *Totale mese: {totale:.2f} €*"
        )
        await update.message.reply_text(testo, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Errore: {e}")

# ── /totale ──────────────────────────────────────────────────────────────────

@solo_pino
async def cmd_totale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Calcolo totale anno…")
    try:
        anno = date.today().year
        totale = sheets.get_totale_anno(anno)
        await update.message.reply_text(
            f"💰 *Totale {anno}: {totale:.2f} €*",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Errore: {e}")

# ── /cerca ───────────────────────────────────────────────────────────────────

async def _esegui_cerca(update: Update, query: str):
    await update.message.reply_text(f"⏳ Cerco «{query}»…")
    try:
        lavori = sheets.cerca_lavori(query)
        if not lavori:
            await update.message.reply_text(f"📭 Nessun risultato per «{query}».")
            return ConversationHandler.END
        totale = sum(float(l["prezzo"]) for l in lavori)
        righe = [lavoro_str(l) for l in lavori[-20:]]
        testo = (
            f"🔍 *Risultati per «{query}»*\n\n"
            + "\n".join(righe)
            + f"\n\n💰 *Totale trovati: {totale:.2f} €*  ({len(lavori)} lavori)"
        )
        await update.message.reply_text(testo, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Errore: {e}")
    return ConversationHandler.END

@solo_pino
async def cmd_cerca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        return await _esegui_cerca(update, " ".join(context.args).strip())
    await update.message.reply_text("🔍 Chi vuoi cercare?")
    return CERCA_STEP

async def cerca_testo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _esegui_cerca(update, update.message.text.strip())

# ── /esporta ─────────────────────────────────────────────────────────────────

@solo_pino
async def cmd_esporta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Preparo il backup da importare nell'app web…")
    try:
        lavori = sheets.export_to_json()
        # Formato compatibile con il backup dell'app web
        backup = {
            "version": 1,
            "exported_at": datetime.now().isoformat(),
            "source": "bot",
            "registro": lavori,
        }
        data_str = datetime.now().strftime("%Y%m%d_%H%M")
        json_bytes = json.dumps(backup, ensure_ascii=False, indent=2).encode("utf-8")
        file_obj = io.BytesIO(json_bytes)
        file_obj.name = f"backup_registro_{data_str}.json"
        await update.message.reply_document(
            document=InputFile(file_obj, filename=file_obj.name),
            caption=(
                f"📦 Backup di {len(lavori)} lavori\n"
                "Importalo nell'app web: _💾 Backup & Ripristino → scegli questo file_"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Errore: {e}")

# ── Ricezione backup JSON dall'app web ───────────────────────────────────────

_pending_backup: dict = {}  # chat_id → parsed lavori list

@solo_pino
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.endswith(".json"):
        await update.message.reply_text("⚠️ Manda solo file .json (backup dell'app web).")
        return

    await update.message.reply_text("⏳ Leggo il file…")
    try:
        file = await context.bot.get_file(doc.file_id)
        data_bytes = await file.download_as_bytearray()
        backup = json.loads(data_bytes.decode("utf-8"))

        # Supporta sia {registro:[...]} che lista diretta
        if isinstance(backup, list):
            lavori = backup
        elif isinstance(backup, dict) and "registro" in backup:
            lavori = backup["registro"]
        elif isinstance(backup, dict) and "lavori" in backup:
            lavori = backup["lavori"]
        else:
            await update.message.reply_text("❌ Formato backup non riconosciuto.")
            return

        _pending_backup[update.effective_chat.id] = lavori
        n = len(lavori)
        kb = [
            [
                InlineKeyboardButton("✅ Sincronizza", callback_data="backup_ok"),
                InlineKeyboardButton("❌ Annulla",    callback_data="backup_cancel"),
            ]
        ]
        await update.message.reply_text(
            f"📦 *Backup ricevuto dal PC*\n\n"
            f"Contiene *{n} lavori*.\n\n"
            f"⚠️ Questa operazione *sostituisce tutti i dati* del bot con quelli dell'app web.\n\n"
            f"Cosa vuoi fare?",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Errore nella lettura del file: {e}")

async def backup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    if query.data == "backup_cancel":
        _pending_backup.pop(chat_id, None)
        await query.edit_message_text("❌ Sincronizzazione annullata.")
        return

    if query.data == "backup_ok":
        lavori = _pending_backup.pop(chat_id, None)
        if not lavori:
            await query.edit_message_text("❌ Backup non trovato. Rimanda il file.")
            return
        await query.edit_message_text("⏳ Importo i dati…")
        try:
            count = sheets.import_from_json(lavori)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ *Sincronizzazione completata!*\n{count} lavori importati.",
                parse_mode="Markdown"
            )
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Errore: {e}")

# ── /start e /help ────────────────────────────────────────────────────────────

@solo_pino
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠 *Studio Lavori Bot*\n\n"
        "Comandi disponibili:\n"
        "/aggiungi — Aggiungi un lavoro\n"
        "/oggi — Lavori di oggi\n"
        "/mese — Resoconto mese corrente\n"
        "/totale — Totale anno in corso\n"
        "/cerca <nome> — Cerca per cliente\n"
        "/esporta — Backup JSON per l'app web\n"
        "/annulla — Annulla operazione in corso\n\n"
        "💡 Puoi anche mandare direttamente il file JSON di backup dall'app web per sincronizzare.",
        parse_mode="Markdown"
    )

# ── Setup Application ─────────────────────────────────────────────────────────

def build_app() -> Application:
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("aggiungi", aggiungi_start)],
        states={
            DATA_STEP:   [MessageHandler(filters.TEXT & ~filters.COMMAND, aggiungi_data)],
            NOME_STEP:   [MessageHandler(filters.TEXT & ~filters.COMMAND, aggiungi_nome)],
            PREZZO_STEP: [MessageHandler(filters.TEXT & ~filters.COMMAND, aggiungi_prezzo)],
            NOTA_STEP:   [
                MessageHandler(filters.TEXT & ~filters.COMMAND, aggiungi_nota),
                CallbackQueryHandler(skip_nota_callback, pattern="^skip_nota$"),
            ],
        },
        fallbacks=[CommandHandler("annulla", annulla)],
        allow_reentry=True,
    )

    cerca_handler = ConversationHandler(
        entry_points=[CommandHandler("cerca", cmd_cerca)],
        states={
            CERCA_STEP: [MessageHandler(filters.TEXT & ~filters.COMMAND, cerca_testo)],
        },
        fallbacks=[CommandHandler("annulla", annulla)],
        allow_reentry=True,
    )

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_start))
    app.add_handler(conv_handler)
    app.add_handler(cerca_handler)
    app.add_handler(CommandHandler("oggi",   cmd_oggi))
    app.add_handler(CommandHandler("mese",   cmd_mese))
    app.add_handler(CommandHandler("totale", cmd_totale))
    app.add_handler(CommandHandler("esporta",cmd_esporta))
    app.add_handler(CommandHandler("annulla", annulla))
    app.add_handler(CallbackQueryHandler(backup_callback, pattern="^backup_"))
    return app


async def run_bot():
    app = build_app()
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Bot avviato in polling.")
        await asyncio.Event().wait()  # blocca finché non viene fermato


if __name__ == "__main__":
    # Flask in thread separato (health check per Render)
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    logger.info(f"Flask health-check su porta {PORT}")
    # Bot nel thread principale
    asyncio.run(run_bot())
