import asyncio
import hashlib
import logging
import os
import sys
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
# Queste librerie scrivono gli indirizzi delle richieste (che contengono il token):
# le silenziamo per non registrarlo mai nei log.
for noisy in ("httpx", "httpcore", "tornado.access"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger("videobolle")

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # limite della Bot API per scaricare file
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # limite della Bot API per inviare file
MAX_SECONDS = 60  # durata massima di una video nota
SIZE = 384  # lato del quadrato in pixel (piu' basso = piu' veloce)
FFMPEG_TIMEOUT = 900  # secondi massimi per una conversione


def _read_id(name):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw  # per esempio @nomecanale


OWNER_ID = _read_id("OWNER_ID")
if not isinstance(OWNER_ID, int):
    OWNER_ID = None
CHANNEL_ID = _read_id("CHANNEL_ID")


class ConversionError(Exception):
    pass


async def convert_to_video_note(src: Path, dst: Path) -> None:
    video_filter = (
        f"crop='min(iw,ih)':'min(iw,ih)',scale={SIZE}:{SIZE},setsar=1,fps=30"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-t", str(MAX_SECONDS),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", video_filter,
        "-c:v", "libx264", "-profile:v", "baseline",
        "-preset", "ultrafast", "-crf", "28",
        "-pix_fmt", "yuv420p", "-threads", "1",
        "-c:a", "aac", "-b:a", "64k", "-ac", "1",
        "-movflags", "+faststart",
        str(dst),
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(), timeout=FFMPEG_TIMEOUT
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        logger.error("FFmpeg errore: %s", stderr.decode(errors="ignore")[-500:])
        raise ConversionError("ffmpeg non e' riuscito a convertire il file")


def get_video_media(message):
    if message is None:
        return None
    if message.video:
        return message.video
    doc = message.document
    if doc and (doc.mime_type or "").startswith("video/"):
        return doc
    return None


async def process_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media, to_channel: bool) -> None:
    message = update.effective_message
    chat_id = update.effective_chat.id

    if to_channel:
        if OWNER_ID is None or CHANNEL_ID is None:
            await message.reply_text("La funzione canale non e' ancora configurata.")
            return
        user = update.effective_user
        if user is None or user.id != OWNER_ID:
            await message.reply_text(
                "Solo il proprietario del bot puo' pubblicare nel canale."
            )
            return

    if media.file_size and media.file_size > MAX_DOWNLOAD_BYTES:
        await message.reply_text(
            "Il video e' troppo grande: il limite e' 20 MB. "
            "Prova con un video piu' corto o di qualita' inferiore."
        )
        return

    original_duration = getattr(media, "duration", None)
    status = await message.reply_text(
        "Sto convertendo il video, puo' richiedere qualche minuto..."
    )
    target = CHANNEL_ID if to_channel else chat_id

    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input"
            dst = Path(tmp) / "note.mp4"

            telegram_file = await context.bot.get_file(media.file_id)
            await telegram_file.download_to_drive(custom_path=src)

            await convert_to_video_note(src, dst)

            if not dst.exists() or dst.stat().st_size == 0:
                raise ConversionError("file convertito vuoto")
            if dst.stat().st_size > MAX_UPLOAD_BYTES:
                raise ConversionError("file convertito troppo grande")

            await context.bot.send_chat_action(chat_id, ChatAction.RECORD_VIDEO_NOTE)

            duration = None
            if original_duration:
                duration = min(int(original_duration), MAX_SECONDS)

            with dst.open("rb") as video_file:
                await context.bot.send_video_note(
                    chat_id=target,
                    video_note=video_file,
                    length=SIZE,
                    duration=duration,
                    read_timeout=180,
                    write_timeout=180,
                )

        if to_channel:
            await message.reply_text("Fatto: video bolla pubblicata nel canale.")
        if original_duration and original_duration > MAX_SECONDS:
            await message.reply_text(
                "Nota: il video durava piu' di 60 secondi, ho tenuto solo il primo minuto."
            )
    except asyncio.TimeoutError:
        await message.reply_text(
            "La conversione ha impiegato troppo tempo. Prova con un video piu' corto."
        )
    except ConversionError:
        await message.reply_text(
            "Non sono riuscito a convertire questo video. Il formato potrebbe non essere valido."
        )
    except TelegramError as error:
        logger.error("Errore Telegram: %s", type(error).__name__)
        if "too big" in str(error).lower():
            await message.reply_text(
                "Il video e' troppo grande: il limite e' 20 MB."
            )
        elif to_channel:
            await message.reply_text(
                "Non riesco a scrivere nel canale. Controlla che il bot sia "
                "amministratore del canale e che CHANNEL_ID sia corretto."
            )
        else:
            await message.reply_text(
                "Si e' verificato un errore con Telegram. Riprova tra poco."
            )
    except Exception:
        logger.exception("Errore durante l'elaborazione del video")
        await message.reply_text(
            "Si e' verificato un errore. Riprova, oppure prova con un altro video."
        )
    finally:
        try:
            await status.delete()
        except Exception:
            pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Ciao! Mandami un video e te lo restituisco come video bolla rotonda.\n"
        "Per pubblicarlo nel canale rispondi al video con /canale "
        "(oppure invialo con la didascalia /canale).\n"
        "Il video viene ritagliato quadrato e tagliato a 60 secondi se e' piu' lungo. "
        "Dimensione massima del file: 20 MB."
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is not None:
        await update.effective_message.reply_text(f"Il tuo ID utente: {user.id}")


async def report_channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None)
    if chat is not None and getattr(chat, "type", "") == "channel":
        await message.reply_text(f"ID del canale: {chat.id}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Mandami un video dalla galleria e lo trasformo in una video bolla."
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    media = get_video_media(message)
    if media is None:
        if message.document:
            await message.reply_text(
                "Questo file non sembra un video. Mandami un video."
            )
        return
    caption = (message.caption or "").strip().lower()
    to_channel = caption.startswith("/canale")
    await process_media(update, context, media, to_channel)


async def channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    media = get_video_media(message.reply_to_message)
    if media is None:
        await message.reply_text(
            "Rispondi a un video con /canale, oppure invia un video con la didascalia /canale."
        )
        return
    await process_media(update, context, media, True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Errore non gestito: %s", type(context.error).__name__)


def main() -> None:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        print("ERRORE: manca la variabile d'ambiente BOT_TOKEN.")
        sys.exit(1)

    base_url = (
        os.environ.get("WEBHOOK_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
    ).rstrip("/")
    if not base_url:
        print("ERRORE: manca l'indirizzo pubblico del servizio (RENDER_EXTERNAL_URL).")
        sys.exit(1)

    port = int(os.environ.get("PORT", "10000"))

    # Percorso e codice segreto del webhook derivati dal token,
    # cosi' il token non compare mai negli indirizzi.
    digest = hashlib.sha256(token.encode()).hexdigest()
    url_path = digest[:40]
    secret_token = digest[40:]

    application = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("id", show_id))
    application.add_handler(CommandHandler("canale", channel_command))
    application.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)
    )
    application.add_handler(
        MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, report_channel_id),
        group=1,
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.FORWARDED, handle_text
        )
    )
    application.add_error_handler(error_handler)

    logger.info("Avvio del bot in modalita' webhook sulla porta %s", port)
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=url_path,
        webhook_url=f"{base_url}/{url_path}",
        secret_token=secret_token,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    main()
