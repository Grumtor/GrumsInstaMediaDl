# app.py
# --- Streamlit: Instagram Post Media Downloader (HQ, Batch) ---
# - Télécharge les médias d'une ou plusieurs publications Instagram **publiques** (photos + vidéos).
# - Qualité maximale pour photos (display_resources) et vidéos (video_versions / video_url).
# - Noms de fichiers basés sur la légende (bio) + index; chaque post est placé dans un **sous-dossier** {shortcode}_{legende-raccourcie}.
# - Fournit un seul ZIP avec tout.
#
# ⚠️ Respectez le droit d'auteur et les CGU d'Instagram. N'utilisez ceci que pour du contenu dont vous avez les droits.
#
# Remarques:
# - Pas de connexion: ne fonctionne pas avec les posts privés / restreints.
# - Les stories ne sont pas prises en charge.
# Start the app : python -m streamlit run app.py

import io
import re
import time
import zipfile
import unicodedata
from typing import List, Optional, Dict

import os, html, random
import requests
import streamlit as st
from instaloader import Instaloader, Post
from instaloader.exceptions import ConnectionException, BadResponseException
from urllib.parse import urlparse

import hashlib


st.set_page_config(page_title="IG Media Downloader (HQ, Batch)", page_icon="📸", layout="centered")

st.title("Download Instagram Media - By Grumtor")
st.caption("Colle **un ou plusieurs liens** de publications Instagram **publiques** (un par ligne ou séparés par des espaces/virgules). L’app récupère **photos et vidéos**.")

# =========================
# Helpers AUTH (sessionid)
# =========================
def _extract_sessionid_from_cookie_string(cookie_str: str) -> Optional[str]:
    """Permet de coller un 'cookie string' complet (copié du navigateur) et d'en extraire la valeur de sessionid."""
    if not cookie_str:
        return None
    m = re.search(r"(?:^|;\s*)sessionid=([^;]+)", cookie_str)
    return m.group(1).strip() if m else None

def _get_current_sessionid() -> Optional[str]:
    """Ordre de priorité :
    1) valeur saisie par l'utilisateur (session_state)
    2) st.secrets (si dispo)
    3) variable d'environnement IG_SESSIONID
    Cette version ne plante pas si aucun secrets.toml n'est présent.
    """
    # 1) Saisie UI (mémoire de session)
    try:
        sid_user = st.session_state.get("IG_SESSIONID_USER")
        if sid_user:
            return sid_user
    except Exception:
        pass

    # 2) Secrets Streamlit (peut ne pas exister)
    try:
        secrets_obj = st.secrets  # l'accès lui-même peut lever si aucun secrets.toml
        InstaDict = secrets_obj.get("instagram", {})
        if isinstance(InstaDict, dict):
            sid = InstaDict.get("sessionid")
            if sid:
                return sid
        sid = secrets_obj.get("IG_SESSIONID")
        if sid:
            return sid
    except Exception:
        pass

    # 3) Variable d'environnement
    return os.getenv("IG_SESSIONID", None)

def _build_browsery_session() -> requests.Session:
    """Session HTTP avec UA navigateur + cookie sessionid si dispo."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"),
        "Accept": "*/*",
        "Accept-Language": "fr,en;q=0.9",
        "Referer": "https://www.instagram.com/",
    })
    sid = _get_current_sessionid()
    if sid:
        s.cookies.set("sessionid", sid, domain=".instagram.com")
    return s

def _get_instaloader_with_auth(use_auth: bool) -> Instaloader:
    """Instaloader avec les mêmes headers/cookies (si use_auth=True)."""
    L = Instaloader(
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        quiet=True,
        max_connection_attempts=3,
    )
    if use_auth:
        s = L.context._session
        s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"),
            "Accept": "*/*",
            "Accept-Language": "fr,en;q=0.9",
            "Referer": "https://www.instagram.com/",
        })
        sid = _get_current_sessionid()
        if sid:
            s.cookies.set("sessionid", sid, domain=".instagram.com")
    return L

def _scrape_og_from_reel(shortcode: str) -> Optional[Dict[str, str]]:
    """Fallback minimal: lit og:video / og:image de la page du Reel si Instaloader ne renvoie rien."""
    try:
        sess = _build_browsery_session()
        url = f"https://www.instagram.com/reel/{shortcode}/"
        r = sess.get(url, timeout=20)
        r.raise_for_status()
        html_text = r.text
        # (fix regex: \s au lieu de \\s)
        m = re.search(r'property="og:video"\s+content="([^"]+)"', html_text)
        if m:
            return {"kind": "video", "url": html.unescape(m.group(1))}
        m = re.search(r'property="og:image"\s+content="([^"]+)"', html_text)
        if m:
            return {"kind": "photo", "url": html.unescape(m.group(1))}
    except Exception:
        pass
    return None

# =========================
# Cache scope par session
# =========================
def _cache_scope() -> str:
    sid = _get_current_sessionid()
    return hashlib.sha256(sid.encode()).hexdigest()[:10] if sid else "anon"

# =========================
# URL / filename helpers
# =========================
def extract_shortcode(url: str) -> str:
    if not url:
        raise ValueError("URL vide.")
    clean = url.split("?")[0].split("#")[0]
    u = urlparse(clean)
    parts = [p for p in u.path.split("/") if p]

    for tag in ("p", "reel", "tv"):
        if tag in parts:
            i = parts.index(tag)
            if i + 1 < len(parts):
                code = parts[i + 1]
                if re.fullmatch(r"[A-Za-z0-9_\-]+", code):
                    return code

    m = re.search(r"(?:^|/)(?:p|reel|tv)/([A-Za-z0-9_\-]+)(?:/|$)", u.path)
    if m:
        return m.group(1)

    raise ValueError("URL invalide. Exemple: https://www.instagram.com/p/XXXXXXXXX/ ou https://www.instagram.com/<user>/p/XXXXXXXXX/")

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

# =========================
# Backoff / retries anti-429
# =========================
def _post_from_shortcode_with_backoff(L: Instaloader, shortcode: str, max_attempts: int = 5) -> Post:
    """
    Essaie de récupérer le Post avec exponentiel backoff si IG renvoie
    'Please wait a few minutes', Unauthorized, 429, etc.
    """
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return Post.from_shortcode(L.context, shortcode)
        except (ConnectionException, BadResponseException, Exception) as e:
            last_exc = e
            msg = str(e)
            transient = any(s in msg for s in [
                "Please wait a few minutes", "Too many requests", "429",
                "Unauthorized", "checkpoint", "try again later"
            ])
            if transient and attempt < max_attempts - 1:
                sleep_s = min(60.0, (1.6 ** attempt) + random.uniform(0.2, 0.8))
                time.sleep(sleep_s)
                continue
            break
    raise last_exc if last_exc else RuntimeError("Échec de récupération du post.")

# =========================
# Fetch bundle (avec auth)
# =========================
@st.cache_data(show_spinner=False)
def fetch_post_bundle(shortcode: str, use_auth: bool, scope: str, max_attempts: int) -> dict:
    """
    Retourne un dict:
    {
        "shortcode": str,
        "username": str,
        "caption": str,
        "media": List[{"kind": "photo"|"video", "url": str}]
    }
    """
    # 'scope' n'est pas utilisé dans le corps : il sert à isoler le cache par utilisateur
    L = _get_instaloader_with_auth(use_auth)
    post = _post_from_shortcode_with_backoff(L, shortcode, max_attempts=max_attempts)
    caption = post.caption or ""
    username = getattr(post, "owner_username", "") or ""
    media: List[Dict[str, str]] = []

    # Carrousel
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

    # Single
    if not media:
        if post.is_video:
            v = _post_best_single_video_url(post)
            if v:
                media.append({"kind": "video", "url": v})
        else:
            p = _post_best_single_photo_url(post)
            if p:
                media.append({"kind": "photo", "url": p})

    # Fallback OG si toujours rien (utile pour certains Reels)
    if not media:
        og = _scrape_og_from_reel(shortcode)
        if og:
            media.append(og)

    return {"shortcode": shortcode, "username": username, "caption": caption, "media": media}

# =========================
# Download zip
# =========================
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

def download_all_as_zip(bundles: List[Dict[str, object]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        session = _build_browsery_session()
        for bidx, b in enumerate(bundles, start=1):
            caption = b["caption"] or ""
            shortcode = b["shortcode"]
            folder_caption = sanitize_filename(caption, 40)
            folder = f"{shortcode}_{folder_caption or 'post'}"
            base = sanitize_filename(caption)
            media = b["media"]
            for midx, item in enumerate(media, start=1):
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
                    ext = _ext_from_content_type(ctype, ".mp4" if kind == "video" else ".jpg")
                    filename = f"{folder}/{base}_{midx:02d}{ext}"
                    zf.writestr(filename, content)
                else:
                    zf.writestr(f"{folder}/ERREUR_{midx:02d}.txt", f"Impossible de telecharger {url}\n{err}")
    buf.seek(0)
    return buf.read()

def parse_urls(text: str) -> List[str]:
    raw = re.split(r"[,\s;]+", text.strip())
    urls = [u for u in raw if u]
    seen = set()
    dedup = []
    for u in urls:
        if u not in seen:
            dedup.append(u)
            seen.add(u)
    return dedup

# =========================
# Sidebar: Connexion + Mode Safe
# =========================
with st.sidebar:
    st.header("🔐 Connexion Instagram (optionnel)")
    st.caption("Colle ton cookie **sessionid** (compte dédié recommandé). "
               "Pour persistance, utilise plutôt `.streamlit/secrets.toml` ou les *Secrets* de Streamlit Cloud.")

    mode = st.radio(
        "Méthode d'entrée",
        options=["Entrer uniquement la valeur sessionid", "Coller le cookie complet (ligne entière)"],
        index=0,
        horizontal=False
    )

    if mode == "Entrer uniquement la valeur sessionid":
        sid_input = st.text_input("sessionid", type="password", placeholder="ex: 123456789%3Aabcdefghijklmnop%3A1%3A...")
        if st.button("Enregistrer le cookie dans cette session"):
            if sid_input.strip():
                st.session_state["IG_SESSIONID_USER"] = sid_input.strip()
                st.success("Cookie enregistré en mémoire (tactique).")
            else:
                st.warning("Aucune valeur saisie.")
    else:
        cookie_str = st.text_area("Colle ici la ligne de cookies complète copiée du navigateur", height=80, placeholder="csrftoken=...; sessionid=...; mid=...;")
        if st.button("Extraire & enregistrer"):
            sid = _extract_sessionid_from_cookie_string(cookie_str or "")
            if sid:
                st.session_state["IG_SESSIONID_USER"] = sid
                st.success("Cookie `sessionid` extrait et enregistré en mémoire.")
            else:
                st.error("Impossible de trouver `sessionid` dans la chaîne de cookies.")

    # Bouton de suppression du cookie (BYO-cookie propre)
    if st.button("🔓 Supprimer le cookie de cette session"):
        st.session_state.pop("IG_SESSIONID_USER", None)
        st.success("Cookie retiré de la mémoire de session.")

    current_sid = _get_current_sessionid()
    if current_sid:
        st.success("✅ Authentifié (cookie actif).")
    else:
        st.info("🚫 Non authentifié (mode invité). Certains Reels/vidéos peuvent échouer.")

    st.subheader("⚙️ Mode Safe (anti rate-limit)")
    SAFE_DELAY_S = st.number_input("Délai min entre posts (secondes)", min_value=0.0, max_value=5.0, value=1.0, step=0.1)
    MAX_ATTEMPTS = st.number_input("Tentatives max / post", min_value=1, max_value=10, value=5, step=1)

# =========================
# Formulaire principal
# =========================
with st.form("batch_form"):
    url_text = st.text_area("Colle tes liens de publications Instagram (un par ligne, ou séparés par espaces/virgules)", height=160, placeholder="https://www.instagram.com/p/AAA/\nhttps://www.instagram.com/reel/BBB/\nhttps://www.instagram.com/p/CCC/")
    submit = st.form_submit_button("Télécharger en lot (photos + vidéos)")

if submit:
    urls = parse_urls(url_text or "")
    if not urls:
        st.error("Ajoute au moins un lien.")
    else:
        st.info(f"{len(urls)} lien(s) détecté(s). Analyse en cours…")
        bundles = []
        errors = []
        prog = st.progress(0)
        scope = _cache_scope()  # <— isole le cache par utilisateur
        for i, u in enumerate(urls, 1):
            try:
                # Throttle global pour éviter les 401/429
                if i > 1 and SAFE_DELAY_S > 0:
                    time.sleep(SAFE_DELAY_S + random.uniform(0, SAFE_DELAY_S * 0.25))

                shortcode = extract_shortcode(u)
                use_auth = bool(_get_current_sessionid())
                bundle = fetch_post_bundle(shortcode, use_auth=use_auth, scope=scope, max_attempts=int(MAX_ATTEMPTS))
                if not bundle["media"]:
                    errors.append((u, "Aucun média trouvé (post privé ou non accessible)."))
                else:
                    bundles.append(bundle)
                    st.write(f"✅ {u} → {len(bundle['media'])} média(s).")
            except Exception as e:
                msg = str(e)
                if ("Please wait a few minutes" in msg) or ("Unauthorized" in msg) or ("429" in msg):
                    msg = ("Limite Instagram atteinte (rate-limit). "
                           "Active le Mode Safe, augmente le délai entre posts, "
                           "et vérifie ton cookie `sessionid`.")
                errors.append((u, msg))
            prog.progress(i / len(urls))
            # petit yield UI
            time.sleep(0.05)

        if errors:
            with st.expander("⚠️ Liens en erreur"):
                for (u, e) in errors:
                    st.write(f"- {u} → {e}")

        if not bundles:
            st.error("Aucun média téléchargeable trouvé.")
        else:
            st.success(f"Prêt: {len(bundles)} post(s) valides, {sum(len(b['media']) for b in bundles)} média(s) au total.")
            with st.expander("Aperçu rapide (premier média de chaque post)"):
                for b in bundles:
                    if b["media"]:
                        first = b["media"][0]
                        if first["kind"] == "photo":
                            st.image(first["url"], caption=f"{b['shortcode']} – photo 1", use_container_width=True)
                        else:
                            try:
                                st.video(first["url"])
                            except Exception:
                                st.write(f"{b['shortcode']} – vidéo 1: {first['url']}")

            zip_bytes = download_all_as_zip(bundles)
            zip_name = "instagram_medias_batch.zip"
            st.download_button(
                "📦 Télécharger le ZIP (qualité max)",
                data=zip_bytes,
                file_name=zip_name,
                mime="application/zip",
                type="primary"
            )

st.divider()
with st.expander("ℹ️ Conseils et limites"):
    st.markdown("""
- Fonctionne **sans connexion** uniquement pour les **publications publiques**. 
- Les **stories** ne sont pas supportées.
- Qualité **maximale** pour photos et vidéos quand disponible.
- Chaque post a son **sous-dossier** `{shortcode}_{legende-raccourcie}` pour éviter les collisions de noms.
- Utilisez ce téléchargeur uniquement pour du contenu dont vous avez les **droits**.
- Pour débloquer plus de Reels/vidéos, ajoutez un **cookie `sessionid`** (compte dédié recommandé) dans la **barre latérale**.
""")

st.divider()
with st.expander("ℹ️ Où récupérer mon SessionId"):
    st.markdown("""
**Safari (macOS)**  
- Safari → Réglages… → Avancées → coche “Afficher le menu Développement”.  
- Connecte-toi sur instagram.com (pas m.instagram.com).  
- Développement → Afficher l’inspecteur Web (⌥⌘I).  
- Onglet Stockage → Cookies → https://www.instagram.com.  
- Trouve la ligne `sessionid` → copie la colonne **Value**.

**Chrome / Brave / Edge (Chromium)**  
- Connecte-toi sur instagram.com.  
- Ouvre DevTools (⌥⌘I) → onglet **Application**.  
- Storage → Cookies → https://www.instagram.com.  
- Clique sur `sessionid` → copie **Value**.

**Firefox**  
- Connecte-toi sur instagram.com.  
- Outils → Outils du navigateur → Outils de développement (⌥⌘I).  
- Onglet Stockage → Cookies → https://www.instagram.com.  
- Sélectionne `sessionid` → copie **Value**.

**Astuces**  
- Si tu ne vois pas `sessionid`, rafraîchis après connexion, ou ouvre un post.  
- En navigation privée, le cookie peut disparaître à la fermeture.  
- Changer le mot de passe / se déconnecter invalide le cookie.  
- Tu peux aussi coller la **ligne complète de cookies** : l’app extrait automatiquement `sessionid`.  
- Ensuite, colle la valeur dans la **barre latérale → “🔐 Connexion Instagram”** et clique **“Enregistrer”**.  
Si tout est ok, tu verras **✅ Authentifié (cookie actif)**.
""")
