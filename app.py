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

import io
import re
import time
import zipfile
import unicodedata
from typing import List, Tuple, Optional, Dict

import requests
import streamlit as st
from instaloader import Instaloader, Post

st.set_page_config(page_title="IG Media Downloader (HQ, Batch)", page_icon="📸", layout="centered")

st.title("📸 Instagram – Téléchargeur de médias (qualité max, multi-liens)")
st.caption("Colle **un ou plusieurs liens** de publications Instagram **publiques** (un par ligne ou séparés par des espaces/virgules). L’app récupère **photos et vidéos**.")

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

@st.cache_data(show_spinner=False)
def fetch_post_bundle(shortcode: str) -> Dict[str, object]:
    """
    Retourne un dict:
    {
        "shortcode": str,
        "username": str,
        "caption": str,
        "media": List[{"kind": "photo"|"video", "url": str}]
    }
    """
    L = Instaloader(download_comments=False, save_metadata=False, post_metadata_txt_pattern="")
    post = Post.from_shortcode(L.context, shortcode)
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

    return {"shortcode": shortcode, "username": username, "caption": caption, "media": media}

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
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
            "Accept": "*/*",
            "Accept-Language": "fr,en;q=0.9"
        })
        for bidx, b in enumerate(bundles, start=1):
            caption = b["caption"] or ""
            shortcode = b["shortcode"]
            folder_caption = sanitize_filename(caption, 40)
            folder = f"{shortcode}_{folder_caption or 'post'}"
            base = sanitize_filename(caption)  # pour les noms de fichiers
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
                    if kind == "video":
                        ext = _ext_from_content_type(ctype, ".mp4")
                    else:
                        ext = _ext_from_content_type(ctype, ".jpg")
                    filename = f"{folder}/{base}_{midx:02d}{ext}"
                    zf.writestr(filename, content)
                else:
                    zf.writestr(f"{folder}/ERREUR_{midx:02d}.txt", f"Impossible de telecharger {url}\n{err}")
    buf.seek(0)
    return buf.read()

def parse_urls(text: str) -> List[str]:
    # Sépare par nouvelles lignes, espaces, virgules, points-virgules
    raw = re.split(r"[,\s;]+", text.strip())
    urls = [u for u in raw if u]
    # Remove duplicates en conservant l'ordre
    seen = set()
    dedup = []
    for u in urls:
        if u not in seen:
            dedup.append(u)
            seen.add(u)
    return dedup

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
        for i, u in enumerate(urls, 1):
            try:
                shortcode = extract_shortcode(u)
                bundle = fetch_post_bundle(shortcode)
                if not bundle["media"]:
                    errors.append((u, "Aucun média trouvé (post privé ou non accessible)."))
                else:
                    bundles.append(bundle)
                    st.write(f"✅ {u} → {len(bundle['media'])} média(s).")
            except Exception as e:
                errors.append((u, str(e)))
            prog.progress(i / len(urls))
            time.sleep(0.1)

        if errors:
            with st.expander("⚠️ Liens en erreur"):
                for (u, e) in errors:
                    st.write(f"- {u} → {e}")

        if not bundles:
            st.error("Aucun média téléchargeable trouvé.")
        else:
            st.success(f"Prêt: {len(bundles)} post(s) valides, {sum(len(b['media']) for b in bundles)} média(s) au total.")
            # Optionnel: petit aperçu du premier média de chaque post
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
            # Nom du zip basé sur le premier shortcode + compteur
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
""")
