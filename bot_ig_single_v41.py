# bot_ig_single_v41.py
# Telegram bot (fichier unique) – Instagram médias (posts/reels/tv) HQ
# Améliorations anti-403:
#  • Prewarm cookies: requête HTML sur la page du post avant GraphQL (p/reel/tv)
#  • Exponential backoff (jusqu'à 3 tentatives)
#  • Throttle configurable entre URLs: THROTTLE_SECONDS (par défaut 0.6s)
#  • Login optionnel via session Instaloader ou user/pass (voir v4)
#  • /status pour diagnostic
# Compatible aiogram 3.3+ et 3.7+

import io
import os
import re
import time
import zipfile
import unicodedata
from typing import List, Optional, Dict

import asyncio
import requests

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode, ChatAction
from aiogram.types import Message
from aiogram.filters import Command
try:
    from aiogram.client.default import DefaultBotProperties
    _DEFAULT_KW = {"default": DefaultBotProperties(parse_mode=ParseMode.HTML)}
except Exception:
    _DEFAULT_KW = {"parse_mode": ParseMode.HTML}

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from functools import lru_cache
from instaloader import Instaloader, Post, TwoFactorAuthRequiredException, BadCredentialsException

BOT_TOKEN_DEFAULT = "7791366265:AAHCwUpDDV4u8xsIQ_AKI2HlOnuk3-VDDt4"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip() or BOT_TOKEN_DEFAULT

IG_USERNAME = os.getenv("IG_USERNAME", "").strip()
IG_PASSWORD = os.getenv("IG_PASSWORD", "").strip()
IG_SESSION_FILE = os.getenv("IG_SESSION_FILE", "").strip()
IG_USER_AGENT = os.getenv("IG_USER_AGENT", "").strip() or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"

THROTTLE_SECONDS = float(os.getenv("THROTTLE_SECONDS", "0.6"))
ZIP_LIMIT_BYTES = 45 * 1024 * 1024
UA = IG_USER_AGENT

def extract_shortcode(url: str) -> str:
    if not url:
        raise ValueError("URL vide.")
    url = url.split("?")[0].split("#")[0]
    m = re.search(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_\-]+)/?", url)
    if not m:
        raise ValueError("URL invalide. Exemple: https://www.instagram.com/p/XXXXXXXXX/")
    return m.group(1)

def sanitize_filename(text: str, max_len: int = 90) -> str:
    if not text:
        text = "sans_legende"
    text = " ".join(text.split())
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[\\/:*?\"<>|#%&{}$!'@`+=~]", "", text)
    text = re.sub(r"[^\w\s\-\.\(\)\[\]]", "", text)
    text = text.strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip()
    return text or "sans_legende"

def _best_from_display_resources(node_dict: dict) -> Optional[str]:
    resources = None
    if isinstance(node_dict, dict):
        resources = node_dict.get("display_resources") or node_dict.get("thumbnail_resources")
    if not resources or not isinstance(resources, list):
        return None
    try:
        best = max(resources, key=lambda r: (r.get("config_width", 0), r.get("config_height", 0)))
        return best.get("src")
    except Exception:
        return None

def _best_from_video_versions(node_dict: dict) -> Optional[str]:
    if not isinstance(node_dict, dict):
        return None
    versions = node_dict.get("video_versions") or node_dict.get("video_resources") or None
    if not versions or not isinstance(versions, list):
        url = node_dict.get("video_url")
        return url
    try:
        def keyfun(v):
            return (v.get("width", 0), v.get("height", 0), v.get("bitrate", 0))
        best = max(versions, key=keyfun)
        return best.get("url") or best.get("src")
    except Exception:
        return node_dict.get("video_url")

def _node_to_best_photo_url(node) -> Optional[str]:
    node_dict = getattr(node, "_node", None)
    best = None
    if isinstance(node_dict, dict):
        best = _best_from_display_resources(node_dict)
        if best:
            return best
    return getattr(node, "display_url", None) or getattr(node, "url", None)

def _node_to_best_video_url(node) -> Optional[str]:
    node_dict = getattr(node, "_node", None)
    url = getattr(node, "video_url", None)
    if url:
        return url
    if isinstance(node_dict, dict):
        best = _best_from_video_versions(node_dict)
        if best:
            return best
    return None

def _post_best_single_photo_url(post: Post) -> Optional[str]:
    node_dict = getattr(post, "_node", None)
    if isinstance(node_dict, dict):
        best = _best_from_display_resources(node_dict)
        if best:
            return best
    if not post.is_video:
        return post.url
    return None

def _post_best_single_video_url(post: Post) -> Optional[str]:
    if getattr(post, "video_url", None):
        return post.video_url
    node_dict = getattr(post, "_node", None)
    if isinstance(node_dict, dict):
        best = _best_from_video_versions(node_dict) or node_dict.get("video_url")
        if best:
            return best
    return None

@lru_cache(maxsize=1)
def get_loader() -> Instaloader:
    L = Instaloader(download_comments=False, save_metadata=False, post_metadata_txt_pattern="")
    try:
        L.context._session.headers.update({"User-Agent": IG_USER_AGENT, "Accept-Language": "fr,en;q=0.9"})
    except Exception:
        pass

    if IG_USERNAME and IG_SESSION_FILE and os.path.exists(IG_SESSION_FILE):
        try:
            L.load_session_from_file(IG_USERNAME, IG_SESSION_FILE)
        except Exception:
            try:
                L.load_session_from_file(IG_USERNAME)
            except Exception:
                pass
    elif IG_USERNAME and IG_PASSWORD:
        try:
            L.login(IG_USERNAME, IG_PASSWORD)
        except Exception:
            pass
    return L

def _prewarm_cookies(L: Instaloader, shortcode: str):
    sess = L.context._session
    headers = {"User-Agent": IG_USER_AGENT, "Referer": "https://www.instagram.com/", "Accept-Language": "fr,en;q=0.9"}
    for path in ("p", "reel", "tv"):
        try:
            resp = sess.get(f"https://www.instagram.com/{path}/{shortcode}/", headers=headers, timeout=15)
            # On n'a pas besoin du contenu; l'objectif est d'obtenir les cookies initiaux (csrftoken, mid, etc.)
            if resp.status_code in (200, 302, 301, 403, 404):
                break
        except Exception:
            time.sleep(0.2)

def fetch_post_bundle(shortcode: str) -> Dict[str, object]:
    L = get_loader()
    last_exc = None
    # Jusqu'à 3 tentatives avec prewarm + backoff
    for attempt in range(3):
        try:
            _prewarm_cookies(L, shortcode)
            post = Post.from_shortcode(L.context, shortcode)
            caption = post.caption or ""
            username = getattr(post, "owner_username", "") or ""
            media: List[Dict[str, str]] = []

            try:
                nodes = list(post.get_sidecar_nodes())
                if nodes:
                    for node in nodes:
                        if getattr(node, "is_video", False):
                            vurl = _node_to_best_video_url(node)
                            if vurl:
                                media.append({"kind": "video", "url": vurl})
                        else:
                            purl = _node_to_best_photo_url(node)
                            if purl:
                                media.append({"kind": "photo", "url": purl})
            except Exception:
                pass

            if not media:
                if post.is_video:
                    v = _post_best_single_video_url(post)
                    if v:
                        media.append({"kind": "video", "url": v})
                else:
                    p = _post_best_single_photo_url(post)
                    if p:
                        media.append({"kind": "photo", "url": p})

            return {"shortcode": shortcode, "username": username, "caption": caption, "media": media}
        except Exception as e:
            last_exc = e
            time.sleep(1.0 * (attempt + 1))  # backoff 1s, 2s, 3s
    raise last_exc if last_exc else RuntimeError("Echec inconnu")

def _ext_from_content_type(ct: str, fallback: str) -> str:
    ct = (ct or "").lower()
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "gif" in ct:
        return ".gif"
    if "mp4" in ct:
        return ".mp4"
    if "quicktime" in ct or "mov" in ct:
        return ".mov"
    if "webm" in ct:
        return ".webm"
    return fallback

def download_bundle_zip(bundle: Dict[str, object], session: Optional[requests.Session] = None) -> bytes:
    if session is None:
        session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "fr,en;q=0.9"
    })
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        caption = bundle.get("caption") or ""
        shortcode = bundle.get("shortcode") or "post"
        folder_caption = sanitize_filename(caption, 40)
        folder = f"{shortcode}_{folder_caption or 'post'}"
        base = sanitize_filename(caption)
        for idx, item in enumerate(bundle.get("media", []), start=1):
            url = item["url"]
            kind = item["kind"]
            err = None
            content = None
            ctype = None
            for attempt in range(3):
                try:
                    r = session.get(url, timeout=30)
                    r.raise_for_status()
                    content = r.content
                    ctype = r.headers.get("Content-Type", "")
                    break
                except Exception as e:
                    err = e
                    time.sleep(0.7 * (attempt + 1))
            if content is not None:
                if kind == "video":
                    ext = _ext_from_content_type(ctype, ".mp4")
                else:
                    ext = _ext_from_content_type(ctype, ".jpg")
                filename = f"{folder}/{base}_{idx:02d}{ext}"
                zf.writestr(filename, content)
            else:
                zf.writestr(f"{folder}/ERREUR_{idx:02d}.txt", f"Impossible de telecharger {url}\n{err}")
    buf.seek(0)
    return buf.read()

def parse_urls(text: str) -> List[str]:
    raw = re.split(r"[,\s;]+", (text or "").strip())
    urls = [u for u in raw if u]
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            out.append(u)
            seen.add(u)
    return out

HELP_TEXT = (
    "👋 <b>Instagram Media Downloader</b>\n\n"
    "Envoie-moi un ou plusieurs liens de publications Instagram <b>publiques</b> (posts / reels / tv).\n"
    "• Un lien par ligne, ou séparés par espaces/virgules\n"
    "• Je télécharge <b>photos + vidéos</b> en qualité max et je te renvoie un ZIP par post\n"
    "• Si un ZIP dépasse la limite, j'envoie les fichiers individuellement\n\n"
    "⚙️ <b>Stabilité vidéo</b>: configure un <code>login</code> via session Instaloader (voir /status)\n"
    "<i>Utilise ce bot uniquement pour du contenu dont tu as les droits.</i>"
)

async def fetch_and_send_for_url(bot, chat_id: int, url: str, reply_to: Optional[int] = None):
    from aiogram.types.input_file import BufferedInputFile

    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        shortcode = extract_shortcode(url)
    except Exception as e:
        await bot.send_message(chat_id, f"❌ Lien invalide:\n<code>{url}</code>\n{e}", reply_to_message_id=reply_to)
        return

    status_msg = await bot.send_message(chat_id, f"🔎 Analyse du post: <code>{shortcode}</code> …", reply_to_message_id=reply_to)
    try:
        bundle = fetch_post_bundle(shortcode)
    except Exception as e:
        await bot.edit_message_text(
            f"❌ Impossible de récupérer le post <code>{shortcode}</code>:\n{e}",
            chat_id=chat_id, message_id=status_msg.message_id
        )
        return

    media_count = len(bundle.get("media", []))
    if not media_count:
        await bot.edit_message_text(
            f"⚠️ Aucun média trouvé pour <code>{shortcode}</code> (post privé / non accessible).",
            chat_id=chat_id, message_id=status_msg.message_id
        )
        return

    await bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)

    zip_bytes = download_bundle_zip(bundle)
    caption = bundle.get("caption") or ""
    folder_caption = sanitize_filename(caption, 40)
    zip_name = f"{bundle.get('shortcode')}_{folder_caption or 'post'}.zip"

    if len(zip_bytes) <= ZIP_LIMIT_BYTES:
        await bot.edit_message_text(
            f"📦 Envoi du ZIP pour <code>{shortcode}</code> ({media_count} média)…",
            chat_id=chat_id, message_id=status_msg.message_id
        )
        file = BufferedInputFile(zip_bytes, filename=zip_name)
        await bot.send_document(chat_id, file, caption=f"{bundle.get('shortcode')} • {media_count} média(s)", reply_to_message_id=reply_to)
    else:
        await bot.edit_message_text(
            f"📦 ZIP trop volumineux pour <code>{shortcode}</code> → envoi des fichiers individuellement…",
            chat_id=chat_id, message_id=status_msg.message_id
        )
        session = requests.Session()
        session.headers.update({
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "fr,en;q=0.9"
        })
        base = sanitize_filename(caption)
        sent = 0
        for idx, item in enumerate(bundle.get("media", []), start=1):
            try:
                r = session.get(item["url"], timeout=30)
                r.raise_for_status()
                ctype = r.headers.get("Content-Type", "").lower()
                if "mp4" in ctype or item["kind"] == "video":
                    ext = ".mp4"
                elif "png" in ctype:
                    ext = ".png"
                elif "webp" in ctype:
                    ext = ".webp"
                elif "gif" in ctype:
                    ext = ".gif"
                else:
                    ext = ".jpg"
                fname = f"{bundle.get('shortcode')}_{folder_caption}/{base}_{idx:02d}{ext}"
                await bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
                await bot.send_document(chat_id, BufferedInputFile(r.content, filename=fname), reply_to_message_id=reply_to)
                sent += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                await bot.send_message(chat_id, f"❌ Erreur pour un média de <code>{shortcode}</code>:\n{e}", reply_to_message_id=reply_to)
                await asyncio.sleep(0.2)
        await bot.edit_message_text(
            f"✅ Terminé pour <code>{shortcode}</code> • {sent}/{media_count} fichier(s) envoyés.",
            chat_id=chat_id, message_id=status_msg.message_id
        )

async def main():
    if not BOT_TOKEN or BOT_TOKEN == "REPLACE_ME":
        raise SystemExit("❌ BOT_TOKEN manquant. Définissez la variable d'environnement BOT_TOKEN ou modifiez BOT_TOKEN_DEFAULT dans le fichier.")
    bot = Bot(BOT_TOKEN, **_DEFAULT_KW)
    dp = Dispatcher()

    @dp.message(Command("start", "help"))
    async def cmd_start(message: Message):
        await message.reply(HELP_TEXT)

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        L = get_loader()
        has_session = bool(L.context._session.cookies.get("sessionid"))
        msg = [
            "<b>Statut bot</b>",
            f"• Connexion IG: {'<b>OK</b>' if has_session else 'anonyme'}",
            f"• IG_USERNAME: {IG_USERNAME or '-'}",
            f"• Session file: {'OK' if (IG_SESSION_FILE and os.path.exists(IG_SESSION_FILE)) else '-'}",
            f"• UA: {UA[:60]}{'...' if len(UA)>60 else ''}",
            f"• THROTTLE_SECONDS: {THROTTLE_SECONDS}",
        ]
        await message.reply("\n".join(msg))

    @dp.message(F.text)
    async def handle_text(message: Message):
        urls = parse_urls(message.text)
        if not urls:
            await message.reply("Ajoute au moins un lien de publication Instagram.")
            return
        await message.reply(f"🧾 {len(urls)} lien(s) détecté(s). Je m'en occupe…")
        for u in urls:
            await fetch_and_send_for_url(bot, message.chat.id, u, reply_to=message.message_id)
            # Throttle configurable entre URL pour éviter rate-limit
            await asyncio.sleep(THROTTLE_SECONDS)

    import logging
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
