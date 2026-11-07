#!/usr/bin/env python3
"""
Maddy’s Captions
================
Kisi bhi video se, kisi bhi language me (Hinglish samet), audio ke saath
synced captions banao aur .srt file lo. Ek hi file, ek hi app.

Chalane ka tareeka (Windows / Mac / Linux):
    python maddys_captions.py
  Pehli baar ye apne aap zaroori packages install kar lega.
  Phir browser khud khulega. Phone/tablet se: wahi Wi-Fi par terminal me
  dikhaya gaya address kholo.

Bina browser ke (command line):
    python maddys_captions.py video.mp4 -l hinglish,en,ta
    python maddys_captions.py video.mp4 -l hi --burn

Settings (environment variables, optional):
    APP_PASSWORD  password lagao (cloud par zaroor)
    PORT, HOST    port / host badlo
"""
import importlib.util
import subprocess
import sys

FROZEN = getattr(sys, "frozen", False)  # True jab .exe / .app ban chuka ho
if FROZEN:
    import multiprocessing
    import os as _os
    multiprocessing.freeze_support()
    if sys.stdout is None or sys.stderr is None:  # windowed app: console nahi hota
        _log = open(_os.path.join(_os.path.expanduser("~"), "MaddysCaptions.log"),
                    "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or _log
        sys.stderr = sys.stderr or _log

# ---- Pehli baar: zaroori packages khud install (sirf script mode me) -------
_NEED = [] if FROZEN else [pkg for mod, pkg in [("flask", "flask"),
                              ("faster_whisper", "faster-whisper"),
                              ("deep_translator", "deep-translator"),
                              ("imageio_ffmpeg", "imageio-ffmpeg")]
         if importlib.util.find_spec(mod) is None]
if _NEED:
    print("Pehli baar setup: install ho raha hai ->", ", ".join(_NEED))
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *_NEED])
    except subprocess.CalledProcessError:
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "--break-system-packages", *_NEED])

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import tempfile
import textwrap
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file

APP_NAME = "Maddy’s Captions"


def _find_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    import imageio_ffmpeg  # bundled ffmpeg, kuch alag install nahi karna
    return imageio_ffmpeg.get_ffmpeg_exe()


FFMPEG = _find_ffmpeg()

# =============================== ENGINE =====================================
END_PUNCT = tuple(".?!।。！？")
CJK = {"zh-CN", "zh-TW", "zh", "ja", "ko"}


def extract_audio(video: str, wav: str) -> None:
    """16kHz mono WAV - Whisper ke liye ideal, aur timing exact rehti hai."""
    cmd = [FFMPEG, "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", wav]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE)
    except FileNotFoundError:
        sys.exit("ffmpeg nahi mila.")
    except subprocess.CalledProcessError as e:
        sys.exit("Audio extract fail:\n" + e.stderr.decode(errors="ignore")[-500:])


def transcribe(wav: str, model_size: str, language):
    from faster_whisper import WhisperModel
    if model_size not in _MODELS:  # ek baar load, baar baar reuse (server ke liye tez)
        _MODELS[model_size] = WhisperModel(model_size, device="auto", compute_type="auto")
    model = _MODELS[model_size]
    segments, info = model.transcribe(
        wav,
        language=language,              # None = auto-detect
        word_timestamps=True,           # har word ka exact start/end
        vad_filter=True,                # silence/music par fake captions nahi
        vad_parameters=dict(min_silence_duration_ms=300),
        condition_on_previous_text=False,  # timing drift / repetition rokta hai
        beam_size=5,
    )
    words = []
    for seg in segments:
        for w in seg.words or []:
            t = w.word.strip()
            if t:
                words.append((float(w.start), float(w.end), t))
    return words, info.language


def build_cues(words, max_chars=42, max_lines=2, max_dur=6.0, pause=0.6):
    """Words ko readable caption cues me todta hai (timing words se aati hai)."""
    cues, cur = [], []

    def flush():
        if cur:
            cues.append([cur[0][0], cur[-1][1], " ".join(w[2] for w in cur)])
            cur.clear()

    for w in words:
        if cur:
            length = len(" ".join(x[2] for x in cur)) + 1 + len(w[2])
            too_long = length > max_chars * max_lines
            too_slow = (w[1] - cur[0][0]) > max_dur
            paused = (w[0] - cur[-1][1]) > pause
            sentence_end = cur[-1][2].endswith(END_PUNCT) and length > max_chars * 0.5
            if too_long or too_slow or paused or sentence_end:
                flush()
        cur.append(w)
    flush()
    return cues


def polish_timing(cues, min_dur=0.8, max_extend=0.4, gap=0.04):
    """Overlap hatao, bahut chhote cues ko thoda rukne do."""
    for i, c in enumerate(cues):
        nxt = cues[i + 1][0] if i + 1 < len(cues) else None
        if c[1] - c[0] < min_dur:
            want = c[0] + min_dur
            limit = c[1] + max_extend
            c[1] = min(want, limit)
        if nxt is not None and c[1] > nxt - gap:
            c[1] = max(c[0] + 0.1, nxt - gap)
    return cues


def translate_cues(cues, src: str, tgt: str):
    from deep_translator import GoogleTranslator
    tr = GoogleTranslator(source=src or "auto", target=tgt)
    texts = [c[2] for c in cues]
    out = []
    for i in range(0, len(texts), 25):
        batch = texts[i:i + 25]
        try:
            res = tr.translate_batch(batch)
        except Exception:
            res = []
            for t in batch:
                try:
                    res.append(tr.translate(t))
                except Exception:
                    res.append(t)  # fail ho to original text rakho
        out.extend(r if r else b for r, b in zip(res, batch))
    return [[c[0], c[1], t] for c, t in zip(cues, out)]


_MODELS = {}

# ---------------------------------------------------------------------------
# Hinglish: Devanagari Hindi -> Roman (offline, rule-based, timing untouched)
# ---------------------------------------------------------------------------
_C = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh",
      "ज": "j", "झ": "jh", "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh",
      "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n", "प": "p",
      "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l",
      "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h", "ळ": "l",
      "\u0958": "k", "\u0959": "kh", "\u095a": "g", "\u095b": "z", "\u095c": "r",
      "\u095d": "rh", "\u095e": "f", "\u095f": "y", "\ue000": "gy"}
_NUKTA = {"क": "k", "ख": "kh", "ग": "g", "ज": "z", "ड": "r", "ढ": "rh", "फ": "f", "य": "y"}
_IV = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri",
       "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o"}
_M = {"ा": "\x00", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri", "े": "e",
      "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e"}
_DIG = str.maketrans("०१२३४५६७८९", "0123456789")
_COMMON = {  # aam shabdon ki natural Hinglish spelling
    "में": "mein", "है": "hai", "हैं": "hain", "का": "ka", "की": "ki", "के": "ke",
    "को": "ko", "से": "se", "और": "aur", "नहीं": "nahi", "यह": "yeh", "वह": "woh",
    "तो": "to", "भी": "bhi", "कि": "ki", "मैं": "main", "हम": "hum", "आप": "aap",
    "तुम": "tum", "क्या": "kya", "कैसे": "kaise", "क्यों": "kyun", "कुछ": "kuch",
    "ये": "ye", "वो": "wo", "था": "tha", "थी": "thi", "थे": "the", "हूँ": "hoon",
    "हो": "ho", "रहा": "raha", "रही": "rahi", "रहे": "rahe", "लिए": "liye",
    "बहुत": "bahut", "अब": "ab", "जब": "jab", "तब": "tab", "एक": "ek",
    "मुझे": "mujhe", "हमें": "humein", "इस": "is", "उस": "us", "पर": "par",
    "ही": "hi", "सब": "sab", "अगर": "agar", "लेकिन": "lekin", "मेरा": "mera",
    "मेरी": "meri", "तुम्हारा": "tumhara", "आपका": "aapka", "आपकी": "aapki",
    "कर": "kar", "करो": "karo", "यहाँ": "yahan", "वहाँ": "wahan", "कहाँ": "kahan",
}


def _roman_word(w: str) -> str:
    if w in _COMMON:
        return _COMMON[w]
    w = w.replace("\u200c", "").replace("\u200d", "").replace("ज्ञ", "\ue000")
    ak = []  # [consonant, vowel, inherent_schwa?, suffix, base_char]
    for ch in w:
        if ch in _C:
            ak.append([_C[ch], "a", True, "", ch])
        elif ch == "\u093c" and ak:
            ak[-1][0] = _NUKTA.get(ak[-1][4], ak[-1][0])
        elif ch == "\u094d" and ak:          # halant
            ak[-1][1], ak[-1][2] = "", False
        elif ch in _M and ak:                # matra
            ak[-1][1], ak[-1][2] = _M[ch], False
        elif ch in _IV:
            ak.append(["", _IV[ch], False, "", ch])
        elif ch in "ंँ" and ak:
            ak[-1][3], ak[-1][2] = "n", False
        elif ch == "ः" and ak:
            ak[-1][3], ak[-1][2] = "h", False
        else:
            ak.append([ch, "", False, "", ch])

    n = len(ak)
    gone = [False] * n                       # schwa deletion (kamal, samajhna, apna)
    for i in range(n - 1, -1, -1):
        if n == 1 or not ak[i][2]:
            continue
        if i == n - 1:
            gone[i] = True
        elif i > 0 and not gone[i + 1] and ak[i + 1][1] != "" and ak[i - 1][1] != "":
            gone[i] = True

    out = []
    for i, (c, v, _inh, post, _b) in enumerate(ak):
        if gone[i]:
            v = ""
        elif v == "\x00":
            v = "aa" if i == 0 else "a"
        out.append(c + v + post)
    return "".join(out).replace("chchh", "cch")


def romanize_hindi(text: str) -> str:
    text = text.replace("।", ".").replace("॥", ".").translate(_DIG)
    return re.sub(r"[\u0900-\u097F\u200c\u200d]+", lambda m: _roman_word(m.group()), text)


def cues_for_lang(base, source: str, lang: str):
    """Ek language ke cues. 'hinglish' = Hindi ko Roman me (timing wahi)."""
    if lang == source:
        return base
    if lang == "hinglish":
        hi = base if source == "hi" else translate_cues(base, source, "hi")
        return [[a, b, romanize_hindi(t)] for a, b, t in hi]
    return translate_cues(base, source, lang)


def fmt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def write_srt(cues, path: Path, lang: str):
    width = 20 if lang in CJK else 42
    lines = []
    for i, (a, b, text) in enumerate(cues, 1):
        wrapped = textwrap.fill(text, width=width, break_long_words=False)
        lines.append(f"{i}\n{fmt_time(a)} --> {fmt_time(b)}\n{wrapped}\n")
    path.write_text("\n".join(lines), encoding="utf-8")


def burn_in(video: str, srt: Path, out: Path):
    # srt ke folder me chala ke path escaping ki dikkat bachate hain
    cmd = [FFMPEG, "-y", "-i", str(Path(video).resolve()),
           "-vf", f"subtitles={srt.name}:force_style='Fontsize=20,Outline=2'",
           "-c:a", "copy", str(out.resolve())]
    subprocess.run(cmd, check=True, cwd=srt.parent,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cli_main(argv=None):
    ap = argparse.ArgumentParser(description="Video -> synced multi-language SRT")
    ap.add_argument("video")
    ap.add_argument("-l", "--langs", default="",
                    help="target languages, comma separated (en,hi,ta,es...). "
                         "Khali = original language")
    ap.add_argument("--src", default=None, help="source language (default: auto-detect)")
    ap.add_argument("-m", "--model", default="medium",
                    help="tiny/base/small/medium/large-v3 (bada = zyada accurate)")
    ap.add_argument("-o", "--outdir", default=None)
    ap.add_argument("--burn", action="store_true",
                    help="pehli target language ke captions video me burn karo")
    a = ap.parse_args(argv)

    video = Path(a.video)
    if not video.exists():
        sys.exit(f"File nahi mili: {video}")
    outdir = Path(a.outdir) if a.outdir else video.parent
    outdir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        wav = str(Path(tmp) / "audio.wav")
        print("1/4 Audio nikal raha hoon...")
        extract_audio(str(video), wav)
        print(f"2/4 Transcribe ({a.model}) - word timestamps ke saath...")
        words, detected = transcribe(wav, a.model, a.src)

    src = a.src or detected
    print(f"    Detected language: {detected}, words: {len(words)}")
    if not words:
        sys.exit("Koi speech nahi mili.")

    print("3/4 Cues bana raha hoon...")
    base = polish_timing(build_cues(words))

    targets = [x.strip() for x in a.langs.split(",") if x.strip()] or [src]
    first_srt = None
    print("4/4 SRT files:")
    for lang in targets:
        cues = cues_for_lang(base, src, lang)
        cues = polish_timing([list(c) for c in cues])
        path = outdir / f"{video.stem}.{lang}.srt"
        write_srt(cues, path, lang)
        first_srt = first_srt or path
        print("   ", path)

    if a.burn and first_srt:
        out = outdir / f"{video.stem}.captioned.mp4"
        print("Burning captions ->", out)
        burn_in(str(video), first_srt, out)
    print("Done.")




# ================================ WEB APP ===================================
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 ** 3  # 4 GB
WORK = Path(tempfile.gettempdir()) / "maddys_captions_jobs"
WORK.mkdir(exist_ok=True)
JOBS = {}
LOCK = threading.Semaphore(1)  # ek baar me ek hi video process (server overload na ho)
PASSWORD = os.environ.get("APP_PASSWORD")  # set karo to password lag jayega

LANGS = {
    "hinglish": "Hinglish (Roman Hindi)", "en": "English", "hi": "Hindi", "bn": "Bengali", "ta": "Tamil",
    "te": "Telugu", "mr": "Marathi", "gu": "Gujarati", "kn": "Kannada",
    "ml": "Malayalam", "pa": "Punjabi", "ur": "Urdu", "ne": "Nepali",
    "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
    "pt": "Portuguese", "ru": "Russian", "ja": "Japanese", "ko": "Korean",
    "zh-CN": "Chinese", "ar": "Arabic", "tr": "Turkish", "id": "Indonesian",
    "th": "Thai", "vi": "Vietnamese", "nl": "Dutch", "pl": "Polish",
    "uk": "Ukrainian", "fa": "Persian", "sv": "Swedish", "el": "Greek",
}


def run_job(*args):
    JOBS[args[0]].update(stage="Queue me (pichla video khatam hone ka wait)...")
    with LOCK:
        _run_job(*args)


def _run_job(jid, vpath, targets, model, src, burn):
    job = JOBS[jid]
    try:
        d = vpath.parent
        job.update(stage="Audio nikal raha hoon...", pct=5)
        wav = str(d / "audio.wav")
        extract_audio(str(vpath), wav)

        job.update(stage="Speech samajh raha hoon (video lamba ho to time lagega)...", pct=15)
        words, detected = transcribe(wav, model, src)
        if not words:
            raise RuntimeError("Video me koi speech nahi mili.")
        source = src or detected
        base = polish_timing(build_cues(words))

        tg = targets or [source]
        for i, lang in enumerate(tg):
            job.update(stage=f"Captions bana raha hoon: {lang}", pct=int(50 + 40 * i / len(tg)))
            cues = cues_for_lang(base, source, lang)
            cues = polish_timing([list(c) for c in cues])
            write_srt(cues, d / f"captions.{lang}.srt", lang)

        burned = False
        if burn:
            job.update(stage="Captions video me chipka raha hoon...", pct=92)
            burn_in(str(vpath), d / f"captions.{tg[0]}.srt", d / "captioned.mp4")
            burned = True

        job.update(stage="Ho gaya!", pct=100, done=True, langs=tg,
                   detected=detected, burned=burned)
    except Exception as e:  # noqa: BLE001
        job.update(error=str(e), done=True)


@app.before_request
def gate():
    if not PASSWORD:
        return None
    a = request.authorization
    if not a or not secrets.compare_digest(a.password or "", PASSWORD):
        return Response("Password chahiye", 401,
                        {"WWW-Authenticate": 'Basic realm="Maddys Captions"'})
    return None


def purge_old(hours=6):
    for d in WORK.iterdir():
        if d.is_dir() and time.time() - d.stat().st_mtime > hours * 3600:
            JOBS.pop(d.name, None)
            shutil.rmtree(d, ignore_errors=True)


PING = {"t": None}


@app.get("/api/ping")
def ping():
    PING["t"] = time.time()
    return jsonify(ok=True)


def _watchdog():
    """Desktop app: browser tab band ho jaye (aur koi kaam na chal raha ho) to app band."""
    while True:
        time.sleep(10)
        busy = any(not j.get("done") for j in JOBS.values())
        if PING["t"] and not busy and time.time() - PING["t"] > 150:
            os._exit(0)


def pick_port(start: int) -> int:
    """Free port dhundho (Mac par 5000 AirPlay se bhara hota hai)."""
    for p in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


@app.get("/manifest.json")
def manifest():
    return jsonify(name="Maddy’s Captions", short_name="Maddy’s", start_url="/",
                   display="standalone", background_color="#161614",
                   theme_color="#2f5bea",
                   icons=[{"src": "/icon.svg", "sizes": "any",
                           "type": "image/svg+xml", "purpose": "any"}])


@app.get("/icon.svg")
def icon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
           '<rect width="512" height="512" rx="110" fill="#2f5bea"/>'
           '<rect x="76" y="170" width="360" height="172" rx="26" fill="none" '
           'stroke="#fff" stroke-width="22"/>'
           '<rect x="122" y="228" width="120" height="20" rx="10" fill="#fff"/>'
           '<rect x="266" y="228" width="124" height="20" rx="10" fill="#fff"/>'
           '<rect x="122" y="268" width="170" height="20" rx="10" fill="#fff"/></svg>')
    return Response(svg, mimetype="image/svg+xml")


@app.get("/sw.js")
def sw():
    return Response("self.addEventListener('fetch',()=>{});",
                    mimetype="application/javascript")


@app.post("/api/start")
def start():
    purge_old()
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify(error="Video file chuno."), 400
    jid = uuid.uuid4().hex[:12]
    d = WORK / jid
    d.mkdir()
    vpath = d / ("input" + Path(f.filename).suffix.lower())
    f.save(vpath)
    targets = [x for x in request.form.getlist("langs") if x in LANGS]
    model = request.form.get("model", "small")
    src = request.form.get("src") or None
    burn = request.form.get("burn") == "1"
    JOBS[jid] = dict(stage="Queue me...", pct=1, done=False,
                     stem=Path(f.filename).stem)
    threading.Thread(target=run_job, args=(jid, vpath, targets, model, src, burn),
                     daemon=True).start()
    return jsonify(id=jid)


@app.get("/api/status/<jid>")
def status(jid):
    if jid not in JOBS:
        abort(404)
    return jsonify(JOBS[jid])


def job_dir(jid):
    if jid not in JOBS:
        abort(404)
    return WORK / jid


@app.get("/video/<jid>")
def video(jid):
    d = job_dir(jid)
    v = next(d.glob("input.*"), None)
    if not v:
        abort(404)
    return send_file(v, conditional=True)


@app.get("/captioned/<jid>")
def captioned(jid):
    p = job_dir(jid) / "captioned.mp4"
    if not p.exists():
        abort(404)
    return send_file(p, as_attachment=True,
                     download_name=f"{JOBS[jid]['stem']}.captioned.mp4")


@app.get("/srt/<jid>/<lang>")
def srt(jid, lang):
    p = job_dir(jid) / f"captions.{lang}.srt"
    if not p.exists():
        abort(404)
    return send_file(p, as_attachment=True,
                     download_name=f"{JOBS[jid]['stem']}.{lang}.srt")


@app.get("/vtt/<jid>/<lang>")
def vtt(jid, lang):
    p = job_dir(jid) / f"captions.{lang}.srt"
    if not p.exists():
        abort(404)
    text = re.sub(r"(\d\d:\d\d:\d\d),(\d{3})", r"\1.\2", p.read_text(encoding="utf-8"))
    return Response("WEBVTT\n\n" + text, mimetype="text/vtt")


@app.post("/api/cleanup/<jid>")
def cleanup(jid):
    JOBS.pop(jid, None)
    shutil.rmtree(WORK / jid, ignore_errors=True)
    return jsonify(ok=True)


PAGE = """<!doctype html>
<html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Maddy’s Captions</title>
<link rel="manifest" href="/manifest.json" crossorigin="use-credentials">
<link rel="icon" href="/icon.svg"><link rel="apple-touch-icon" href="/icon.svg">
<meta name="theme-color" content="#2f5bea">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<style>
:root{--bg:#f6f6f4;--card:#fff;--tx:#1c1c1a;--mut:#6b6b66;--ac:#2f5bea;--bd:#e2e2dc}
@media(prefers-color-scheme:dark){:root{--bg:#161614;--card:#21211f;--tx:#eeeee9;--mut:#9a9a93;--ac:#6f8fff;--bd:#33332f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:16px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:26px;margin:0 0 4px}
.sub{color:var(--mut);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:18px;margin-bottom:16px}
label.l{display:block;font-weight:600;margin:0 0 8px}
input[type=file]{width:100%;padding:14px;border:2px dashed var(--bd);border-radius:10px;background:transparent;color:var(--tx)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:6px}
.chip{display:flex;gap:6px;align-items:center;padding:6px 8px;border:1px solid var(--bd);border-radius:8px;font-size:14px;cursor:pointer}
.row{display:flex;gap:12px;flex-wrap:wrap}
.row>div{flex:1;min-width:180px}
select{width:100%;padding:9px;border-radius:8px;border:1px solid var(--bd);background:var(--card);color:var(--tx)}
button{background:var(--ac);color:#fff;border:0;border-radius:10px;padding:12px 20px;font-size:16px;font-weight:600;cursor:pointer;width:100%}
button:disabled{opacity:.5;cursor:default}
.bar{height:10px;background:var(--bd);border-radius:6px;overflow:hidden;margin:10px 0}
.bar i{display:block;height:100%;width:0;background:var(--ac);transition:width .4s}
video{width:100%;border-radius:10px;background:#000}
.dl{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.dl a{padding:8px 12px;border:1px solid var(--bd);border-radius:8px;color:var(--ac);text-decoration:none;font-size:14px;font-weight:600}
.err{color:#d33;font-weight:600}
small{color:var(--mut)}
</style></head><body><main>
<h1>Maddy’s Captions</h1>
<p class="sub">Video do, languages chuno, synced captions (SRT) pao.</p>

<div class="card" id="form">
  <label class="l">1. Video</label>
  <input type="file" id="video" accept="video/*,audio/*">
  <br><br>
  <label class="l">2. Captions kis kis language me chahiye?</label>
  <div class="grid" id="langs"></div>
  <small>Kuch na chuno to video ki original language me captions banenge.</small>
  <br><br>
  <div class="row">
    <div><label class="l">Accuracy</label>
      <select id="model">
        <option value="base">Fast (base)</option>
        <option value="small" selected>Balanced (small)</option>
        <option value="medium">Accurate (medium)</option>
        <option value="large-v3">Best (large-v3, slow)</option>
      </select></div>
    <div><label class="l">Video ki language</label>
      <select id="src"><option value="">Auto-detect</option></select></div>
  </div>
  <br>
  <label class="chip" style="width:fit-content"><input type="checkbox" id="burn">
    Captions video me hamesha ke liye chipka do (pehli language)</label>
  <br>
  <button id="go">Captions banao</button>
</div>

<div class="card" id="prog" hidden>
  <b id="stage">...</b>
  <div class="bar"><i id="fill"></i></div>
  <small id="note"></small>
</div>

<div class="card" id="res" hidden>
  <video id="player" controls></video>
  <div class="row" style="margin-top:12px"><div>
    <label class="l">Preview language</label><select id="pick"></select></div></div>
  <div class="dl" id="dl"></div>
</div>

<script>
const L = __LANGS__;
setInterval(() => fetch('/api/ping').catch(() => {}), 5000);
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(()=>{});
const $ = id => document.getElementById(id);
for (const [c,n] of Object.entries(L)) {
  $('langs').insertAdjacentHTML('beforeend',
    `<label class="chip"><input type="checkbox" value="${c}"> ${n}</label>`);
  $('src').insertAdjacentHTML('beforeend', `<option value="${c}">${n}</option>`);
}
$('go').onclick = async () => {
  const f = $('video').files[0];
  if (!f) { alert('Pehle video chuno'); return; }
  const fd = new FormData();
  fd.append('video', f);
  document.querySelectorAll('#langs input:checked').forEach(i => fd.append('langs', i.value));
  fd.append('model', $('model').value);
  fd.append('src', $('src').value);
  fd.append('burn', $('burn').checked ? '1' : '0');
  $('go').disabled = true;
  $('form').hidden = true; $('prog').hidden = false;
  $('stage').textContent = 'Video upload ho raha hai...';
  let r;
  try { r = await (await fetch('/api/start', {method:'POST', body: fd})).json(); }
  catch (e) { return fail('Upload fail ho gaya.'); }
  if (r.error) return fail(r.error);
  poll(r.id);
};
function fail(m) {
  $('stage').innerHTML = `<span class="err">${m}</span>`;
  $('note').innerHTML = '<a href="/">Dobara try karo</a>';
}
async function poll(id) {
  const s = await (await fetch('/api/status/' + id)).json();
  if (s.error) return fail(s.error);
  $('stage').textContent = s.stage;
  $('fill').style.width = s.pct + '%';
  if (!s.done) return setTimeout(() => poll(id), 1500);
  show(id, s);
}
function show(id, s) {
  $('prog').hidden = true; $('res').hidden = false;
  const p = $('player'); p.src = '/video/' + id;
  s.langs.forEach((c, i) => {
    const t = document.createElement('track');
    t.kind = 'subtitles'; t.label = L[c] || c; t.srclang = c === 'hinglish' ? 'hi-Latn' : c;
    t.src = `/vtt/${id}/${c}`; if (i === 0) t.default = true;
    p.appendChild(t);
    $('pick').insertAdjacentHTML('beforeend', `<option value="${i}">${L[c] || c}</option>`);
    $('dl').insertAdjacentHTML('beforeend',
      `<a href="/srt/${id}/${c}">${L[c] || c} .srt</a>`);
  });
  if (s.burned) $('dl').insertAdjacentHTML('beforeend',
    `<a href="/captioned/${id}">Captioned video (.mp4)</a>`);
  const show = i => { [...p.textTracks].forEach((t, k) => t.mode = k == i ? 'showing' : 'disabled'); };
  setTimeout(() => show(0), 300);
  $('pick').onchange = e => show(e.target.value);
}
</script></main></body></html>"""


@app.get("/")
def index():
    import json
    return PAGE.replace("__LANGS__", json.dumps(LANGS))


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "APNE-PC-KA-IP"


def run_server():
    host = os.environ.get("HOST", "127.0.0.1" if FROZEN else "0.0.0.0")
    port = pick_port(int(os.environ.get("PORT", "5000")))
    if FROZEN or os.environ.get("AUTO_EXIT") == "1":
        threading.Thread(target=_watchdog, daemon=True).start()
    print(f"\n  {APP_NAME}")
    print(f"  Computer par : http://localhost:{port}")
    print(f"  Phone/tablet : http://{lan_ip()}:{port}  (same Wi-Fi)")
    if not PASSWORD:
        print("  Tip: bina password ke koi bhi use kar sakta hai. APP_PASSWORD set karo.")
    print("  Band karne ke liye Ctrl+C dabao.\n")
    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--server"):
        cli_main()
    else:
        run_server()
