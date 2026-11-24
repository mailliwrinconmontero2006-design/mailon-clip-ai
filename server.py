import json
import os
import re
import uuid
import threading
import subprocess
import traceback
import socket
import ipaddress
import urllib.request
import urllib.error

from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote
from email.parser import BytesParser
from email.policy import default

import imageio_ffmpeg

try:
    from faster_whisper import WhisperModel
except Exception:
    WhisperModel = None


# ==========================================
# CONFIGURACIÓN DE MAILON CLIP AI
# ==========================================

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
CLIPS_DIR = BASE_DIR / "clips"

UPLOAD_DIR.mkdir(exist_ok=True)
CLIPS_DIR.mkdir(exist_ok=True)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

MAX_UPLOAD = 500 * 1024 * 1024
MAX_URL_BODY = 64 * 1024
CLIP_SECONDS = 40

ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi"
}

ALLOWED_VIDEO_TYPES = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/x-msvideo": ".avi",
    "application/octet-stream": None
}

WHISPER_MODEL_NAME = os.environ.get("WHISPER_MODEL", "tiny")

JOBS = {}
LOCK = threading.Lock()


# ==========================================
# PALABRAS PARA EL ANÁLISIS INTELIGENTE
# ==========================================

HOOK_WORDS = {
    "increíble", "atención", "secreto", "descubrí",
    "imagina", "sorpresa", "nadie", "jamás",
    "nunca", "siempre", "importante", "resultado",
    "verdad", "realmente", "exactamente", "finalmente",
    "error", "éxito", "dinero", "problema", "solución",
    "ojo", "escucha", "fíjate", "brutal", "increíblemente"
}

STORY_WORDS = {
    "historia", "entonces", "después", "recuerdo",
    "pasó", "sucedió", "aprendí", "experiencia",
    "momento", "principio", "finalmente", "resulta",
    "ocurrió", "viví", "conocí"
}

ADVICE_WORDS = {
    "consejo", "recomiendo", "debes", "deberías",
    "aprende", "recuerda", "importante", "solución",
    "evita", "necesitas", "puedes", "haz",
    "intenta", "procura", "nunca", "mejor"
}

QUESTION_WORDS = {
    "qué", "cómo", "cuándo", "dónde",
    "por", "quién", "cuál", "cuánto"
}

TRANSITION_WORDS = {
    "pero", "porque", "aunque", "entonces",
    "además", "sin embargo", "por eso", "así que"
}


# ==========================================
# FUNCIONES GENERALES
# ==========================================

def update_job(job_id, **values):
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(values)


def run_ffmpeg(arguments):
    result = subprocess.run(
        [FFMPEG] + arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr[-2500:] or "Error de FFmpeg"
        )

    return result


def get_video_duration(video_path):
    result = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-i", str(video_path),
            "-f", "null",
            "-"
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    match = re.search(
        r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
        result.stderr
    )

    if not match:
        raise RuntimeError(
            "No se pudo determinar la duración del video."
        )

    hours = int(match.group(1))
    minutes = int(match.group(2))
    seconds = float(match.group(3))

    return hours * 3600 + minutes * 60 + seconds


def start_video_job(input_path):
    job_id = uuid.uuid4().hex

    with LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "progress": 0,
            "message": "Video recibido correctamente.",
            "clips": []
        }

    thread = threading.Thread(
        target=process_video,
        args=(job_id, input_path),
        daemon=True
    )
    thread.start()

    return job_id


# ==========================================
# VALIDACIÓN Y DESCARGA DE ENLACES DIRECTOS
# ==========================================

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_public_video_url(url):
    parsed = urlparse(url)

    if parsed.scheme.lower() != "https":
        raise ValueError(
            "Los enlaces directos deben comenzar con https://"
        )

    if not parsed.hostname:
        raise ValueError("El enlace no contiene un dominio válido.")

    if parsed.username or parsed.password:
        raise ValueError("No se permiten credenciales en el enlace.")

    if parsed.port not in (None, 443):
        raise ValueError("El enlace utiliza un puerto no permitido.")

    hostname = parsed.hostname.rstrip(".").lower()

    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Ese dominio no está permitido.")

    try:
        addresses = socket.getaddrinfo(
            hostname,
            443,
            type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        raise ValueError("No se pudo encontrar el servidor del enlace.")

    if not addresses:
        raise ValueError("No se pudo verificar el servidor.")

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])

        if not ip.is_global:
            raise ValueError(
                "El enlace apunta a una dirección no pública."
            )

    return parsed


def download_direct_video(url, destination):
    parsed = validate_public_video_url(url)

    path_extension = Path(
        unquote(parsed.path)
    ).suffix.lower()

    if path_extension and path_extension not in ALLOWED_EXTENSIONS:
        raise ValueError(
            "El enlace debe apuntar a un video compatible."
        )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MAILON-CLIP-AI/1.0",
            "Accept": "video/*,application/octet-stream"
        },
        method="GET"
    )

    opener = urllib.request.build_opener(NoRedirectHandler())

    try:
        response = opener.open(request, timeout=30)

    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            raise ValueError(
                "El servidor redirigió el enlace. "
                "Necesitas la URL directa del archivo."
            )

        raise ValueError(
            f"No se pudo abrir el enlace (HTTP {error.code})."
        )

    except Exception as error:
        raise ValueError(
            f"No se pudo descargar el video: {error}"
        )

    with response:
        content_type = (
            response.headers.get("Content-Type", "")
            .split(";")[0]
            .strip()
            .lower()
        )

        if content_type not in ALLOWED_VIDEO_TYPES:
            raise ValueError(
                "El enlace no parece ser un archivo de video compatible."
            )

        content_length = response.headers.get("Content-Length")

        if content_length:
            try:
                if int(content_length) > MAX_UPLOAD:
                    raise ValueError(
                        "El video supera el límite de 500 MB."
                    )
            except ValueError as error:
                if "supera" in str(error):
                    raise

                raise ValueError(
                    "El servidor indicó un tamaño inválido."
                )

        extension = path_extension

        if not extension:
            extension = ALLOWED_VIDEO_TYPES.get(content_type)

        if not extension:
            raise ValueError(
                "No se pudo identificar el formato del video."
            )

        if extension not in ALLOWED_EXTENSIONS:
            raise ValueError("Formato de video no permitido.")

        total = 0

        try:
            with destination.open("wb") as output:

                while True:
                    chunk = response.read(1024 * 1024)

                    if not chunk:
                        break

                    total += len(chunk)

                    if total > MAX_UPLOAD:
                        raise ValueError(
                            "El video supera el límite de 500 MB."
                        )

                    output.write(chunk)

        except Exception:
            destination.unlink(missing_ok=True)
            raise

    if total == 0:
        destination.unlink(missing_ok=True)
        raise ValueError("El enlace no entregó ningún archivo.")

    return extension


# ==========================================
# TRANSCRIPCIÓN AUTOMÁTICA CON WHISPER
# ==========================================

def transcribe_video(video_path):

    if WhisperModel is None:
        return []

    model = WhisperModel(
        WHISPER_MODEL_NAME,
        device="cpu",
        compute_type="int8"
    )

    segments, info = model.transcribe(
        str(video_path),
        beam_size=1,
        vad_filter=True
    )

    transcript = []

    for segment in segments:
        text = segment.text.strip()

        if not text:
            continue

        transcript.append({
            "start": float(segment.start),
            "end": float(segment.end),
            "text": text
        })

    return transcript


# ==========================================
# ANÁLISIS INTELIGENTE DEL CONTENIDO
# ==========================================

def normalize_words(text):
    return re.findall(
        r"\b[\wáéíóúüñ]+\b",
        text.lower()
    )


def score_window(start, end, transcript):

    relevant = []

    for segment in transcript:
        midpoint = (
            segment["start"] + segment["end"]
        ) / 2

        if start <= midpoint < end:
            relevant.append(segment)

    if not relevant:
        return 0.0

    relevant.sort(key=lambda item: item["start"])

    text = " ".join(
        item["text"] for item in relevant
    ).lower()

    words = normalize_words(text)

    if not words:
        return 0.0

    speech_duration = sum(
        max(
            0,
            min(end, item["end"])
            - max(start, item["start"])
        )
        for item in relevant
    )

    speech_density = min(
        speech_duration / CLIP_SECONDS,
        1.0
    )

    word_count_score = min(len(words) / 100, 1.0)

    unique_ratio = len(set(words)) / max(len(words), 1)

    diversity_score = min(unique_ratio / 0.65, 1.0)

    hook_score = min(
        sum(1 for word in words if word in HOOK_WORDS) / 5,
        1.0
    )

    story_score = min(
        sum(1 for word in words if word in STORY_WORDS) / 4,
        1.0
    )

    advice_score = min(
        sum(1 for word in words if word in ADVICE_WORDS) / 4,
        1.0
    )

    question_score = min(
        sum(1 for word in words if word in QUESTION_WORDS) / 3,
        1.0
    )

    punctuation_score = min(
        (text.count("?") + text.count("!")) / 4,
        1.0
    )

    sentence_score = min(
        (text.count(".") + text.count("!") + text.count("?")) / 6,
        1.0
    )

    transition_score = min(
        sum(
            1 for phrase in TRANSITION_WORDS
            if phrase in text
        ) / 3,
        1.0
    )

    # Favorece segmentos con conversación suficiente,
    # contenido interesante y variedad de palabras.
    score = (
        speech_density * 0.18
        + word_count_score * 0.12
        + hook_score * 0.20
        + story_score * 0.14
        + advice_score * 0.10
        + question_score * 0.08
        + punctuation_score * 0.06
        + sentence_score * 0.04
        + diversity_score * 0.04
        + transition_score * 0.04
    )

    return round(score, 4)


def intervals_overlap(first, second):
    return not (
        first["end"] <= second["start"]
        or first["start"] >= second["end"]
    )


def select_best_moments(duration, transcript):

    if duration < CLIP_SECONDS:
        raise RuntimeError(
            "El video debe durar al menos 40 segundos."
        )

    last_start = duration - CLIP_SECONDS
    desired_count = max(1, int(duration // CLIP_SECONDS))

    if not transcript:
        if desired_count == 1:
            fallback_starts = [last_start / 2]
        else:
            fallback_starts = [
                last_start * index / (desired_count - 1)
                for index in range(desired_count)
            ]

        return [
            {
                "start": round(start, 3),
                "end": round(start + CLIP_SECONDS, 3),
                "score": 0.0
            }
            for start in fallback_starts
        ]

    step = 5

    starts = [
        float(value)
        for value in range(
            0,
            int(last_start) + 1,
            step
        )
    ]

    if not starts or starts[-1] < last_start:
        starts.append(float(last_start))

    candidates = []

    for start in starts:
        end = start + CLIP_SECONDS

        candidates.append({
            "start": start,
            "end": end,
            "score": score_window(start, end, transcript)
        })

    # Se ordenan primero los momentos con mayor puntuación.
    candidates.sort(
        key=lambda item: item["score"],
        reverse=True
    )

    selected = []

    # Primera selección: mejores puntuaciones sin solapamiento.
    for candidate in candidates:

        if all(
            not intervals_overlap(candidate, chosen)
            for chosen in selected
        ):
            selected.append(candidate)

        if len(selected) >= desired_count:
            break

    # Si faltan clips, se buscan ventanas alternativas.
    if len(selected) < desired_count:

        for candidate in sorted(
            candidates,
            key=lambda item: item["start"]
        ):

            if candidate in selected:
                continue

            if all(
                not intervals_overlap(candidate, chosen)
                for chosen in selected
            ):
                selected.append(candidate)

            if len(selected) >= desired_count:
                break

    selected.sort(key=lambda item: item["start"])

    return selected


# ==========================================
# GENERADOR DE SUBTÍTULOS AUTOMÁTICOS
# ==========================================

def format_srt_time(seconds):

    milliseconds = int(
        round(max(0, seconds) * 1000)
    )

    hours = milliseconds // 3600000
    minutes = (milliseconds % 3600000) // 60000
    seconds_part = (milliseconds % 60000) // 1000
    milliseconds_part = milliseconds % 1000

    return (
        f"{hours:02d}:{minutes:02d}:"
        f"{seconds_part:02d},{milliseconds_part:03d}"
    )


def create_subtitle_file(
    output_path,
    transcript,
    clip_start
):

    clip_end = clip_start + CLIP_SECONDS
    subtitle_entries = []

    for segment in transcript:

        start = max(segment["start"], clip_start)
        end = min(segment["end"], clip_end)

        if end <= start:
            continue

        text = segment["text"].strip()

        if not text:
            continue

        local_start = start - clip_start
        local_end = end - clip_start

        subtitle_entries.append(
            f"{len(subtitle_entries) + 1}\n"
            f"{format_srt_time(local_start)} --> "
            f"{format_srt_time(local_end)}\n"
            f"{text}\n"
        )

    output_path.write_text(
        "\n".join(subtitle_entries),
        encoding="utf-8"
    )


# ==========================================
# CREACIÓN DE CLIPS DE 40 SEGUNDOS
# ==========================================

def create_clip(input_path, output_path, start):

    run_ffmpeg([
        "-y",
        "-ss", str(start),
        "-i", str(input_path),
        "-t", str(CLIP_SECONDS),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_path)
    ])


# ==========================================
# PROCESAMIENTO COMPLETO DEL VIDEO
# ==========================================

def process_video(job_id, input_path):

    job_folder = CLIPS_DIR / job_id
    job_folder.mkdir(exist_ok=True)

    try:

        update_job(
            job_id,
            status="processing",
            progress=5,
            message="Preparando el video..."
        )

        duration = get_video_duration(input_path)

        if duration < CLIP_SECONDS:
            raise RuntimeError(
                "El video debe durar al menos 40 segundos."
            )

        update_job(
            job_id,
            progress=15,
            message="Analizando el contenido del video..."
        )

        transcript = []

        try:
            transcript = transcribe_video(input_path)

        except Exception as error:
            print("Error de transcripción:", error)

        if transcript:
            message = "Buscando los mejores momentos..."
        else:
            message = "Aplicando selección alternativa..."

        update_job(
            job_id,
            progress=45,
            message=message
        )

        moments = select_best_moments(
            duration,
            transcript
        )

        if not moments:
            raise RuntimeError(
                "No se encontraron momentos para crear clips."
            )

        clips = []
        total = len(moments)

        for index, moment in enumerate(moments, start=1):

            filename = f"clip_{index:03d}.mp4"
            output_path = job_folder / filename

            create_clip(
                input_path,
                output_path,
                moment["start"]
            )

            subtitle_filename = f"clip_{index:03d}.srt"
            subtitle_path = job_folder / subtitle_filename

            create_subtitle_file(
                subtitle_path,
                transcript,
                moment["start"]
            )

            clips.append({
                "name": filename,
                "url": f"/clips/{job_id}/{filename}",
                "subtitles": subtitle_filename,
                "subtitles_url": f"/clips/{job_id}/{subtitle_filename}",
                "start": round(moment["start"], 2),
                "duration": CLIP_SECONDS,
                "score": round(moment["score"], 3)
            })

            progress = 50 + int(index / total * 45)

            update_job(
                job_id,
                progress=progress,
                message=f"Creando clip {index} de {total}..."
            )

        update_job(
            job_id,
            status="completed",
            progress=100,
            message="¡Tus clips están listos!",
            clips=clips,
            total_clips=len(clips)
        )

    except Exception as error:

        traceback.print_exc()

        update_job(
            job_id,
            status="failed",
            progress=0,
            error=str(error),
            message="No se pudo procesar el video."
        )

    finally:

        try:
            Path(input_path).unlink(missing_ok=True)

        except Exception:
            pass


# ==========================================
# SERVIDOR WEB
# ==========================================

class MailonClipHandler(BaseHTTPRequestHandler):

    def send_json(self, data, status=200):

        body = json.dumps(data).encode("utf-8")

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):

        self.send_response(204)

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type"
        )

        self.end_headers()

    def do_GET(self):

        path = unquote(urlparse(self.path).path)

        if path == "/":

            index_path = BASE_DIR / "index.html"

            if not index_path.exists():
                self.send_error(404, "No se encontró index.html")
                return

            content = index_path.read_bytes()

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(content))
            )

            self.end_headers()
            self.wfile.write(content)
            return

        if path == "/api/health":

            self.send_json({
                "status": "online",
                "app": "MAILON CLIP AI",
                "clip_seconds": CLIP_SECONDS,
                "url_import": "direct_video_links",
                "youtube": "recognized_but_not_downloaded"
            })

            return

        if path.startswith("/api/jobs/"):

            job_id = path.split("/")[-1]

            with LOCK:
                job = JOBS.get(job_id)

                if job:
                    job = dict(job)

            if not job:
                self.send_json(
                    {"error": "Trabajo no encontrado"},
                    404
                )
                return

            self.send_json(job)
            return

        if path.startswith("/clips/"):

            parts = path.strip("/").split("/")

            if len(parts) != 3:
                self.send_error(404)
                return

            job_id, filename = parts[1], parts[2]

            if not re.fullmatch(r"[a-f0-9]{32}", job_id):
                self.send_error(404)
                return

            is_video = re.fullmatch(
                r"clip_\d{3}\.mp4",
                filename
            )

            is_subtitle = re.fullmatch(
                r"clip_\d{3}\.srt",
                filename
            )

            if not is_video and not is_subtitle:
                self.send_error(404)
                return

            clip_path = CLIPS_DIR / job_id / filename

            if not clip_path.is_file():
                self.send_error(404)
                return

            if is_video:
                content_type = "video/mp4"
            else:
                content_type = "application/x-subrip; charset=utf-8"

            self.send_response(200)

            self.send_header(
                "Content-Type",
                content_type
            )

            self.send_header(
                "Content-Length",
                str(clip_path.stat().st_size)
            )

            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{filename}"'
            )

            self.end_headers()

            with clip_path.open("rb") as file:

                while True:
                    chunk = file.read(1024 * 1024)

                    if not chunk:
                        break

                    self.wfile.write(chunk)

            return

        self.send_error(404, "Ruta no encontrada")

    def do_POST(self):

        path = urlparse(self.path).path

        if path == "/api/create-clips":
            self.handle_url_import()
            return

        if path != "/api/upload":
            self.send_error(404)
            return

        try:

            content_length = int(
                self.headers.get("Content-Length", "0")
            )

            if content_length <= 0:
                self.send_json(
                    {"error": "No se recibió ningún archivo."},
                    400
                )
                return

            if content_length > MAX_UPLOAD + 2 * 1024 * 1024:
                self.send_json(
                    {"error": "El archivo supera el límite permitido."},
                    413
                )
                return

            content_type = self.headers.get(
                "Content-Type",
                ""
            )

            if "multipart/form-data" not in content_type:
                self.send_json(
                    {"error": "Formato de envío no válido."},
                    400
                )
                return

            body = self.rfile.read(content_length)

            if len(body) != content_length:
                self.send_json(
                    {"error": "La carga del archivo quedó incompleta."},
                    400
                )
                return

            message = (
                f"Content-Type: {content_type}\r\n"
                "MIME-Version: 1.0\r\n\r\n"
            ).encode() + body

            parsed = BytesParser(
                policy=default
            ).parsebytes(message)

            uploaded_file = None
            original_name = None

            for part in parsed.iter_parts():

                filename = part.get_filename()

                if filename:

                    uploaded_file = part.get_payload(
                        decode=True
                    )

                    original_name = filename
                    break

            if not uploaded_file or not original_name:
                self.send_json(
                    {"error": "No se encontró el archivo de video."},
                    400
                )
                return

            extension = Path(
                original_name
            ).suffix.lower()

            if extension not in ALLOWED_EXTENSIONS:
                self.send_json(
                    {"error": "Formato de video no permitido."},
                    400
                )
                return

            if len(uploaded_file) > MAX_UPLOAD:
                self.send_json(
                    {"error": "El archivo supera los 500 MB."},
                    413
                )
                return

            job_id = uuid.uuid4().hex

            input_path = UPLOAD_DIR / (
                job_id + extension
            )

            input_path.write_bytes(uploaded_file)

            with LOCK:
                JOBS[job_id] = {
                    "id": job_id,
                    "status": "queued",
                    "progress": 0,
                    "message": "Video recibido correctamente.",
                    "clips": []
                }

            thread = threading.Thread(
                target=process_video,
                args=(job_id, input_path),
                daemon=True
            )

            thread.start()

            self.send_json({
                "success": True,
                "job_id": job_id,
                "status_url": f"/api/jobs/{job_id}",
                "message": "Video recibido correctamente."
            }, 202)

        except Exception as error:

            traceback.print_exc()

            self.send_json({
                "error": "Error al recibir el video.",
                "details": str(error)
            }, 500)

    def handle_url_import(self):

        try:

            content_length = int(
                self.headers.get("Content-Length", "0")
            )

            if content_length <= 0 or content_length > MAX_URL_BODY:
                self.send_json(
                    {"error": "Envía un enlace válido."},
                    400
                )
                return

            raw_body = self.rfile.read(content_length)

            data = json.loads(
                raw_body.decode("utf-8")
            )

            url = str(data.get("url", "")).strip()

            if not url:
                self.send_json(
                    {"error": "Pega el enlace del video."},
                    400
                )
                return

            parsed = urlparse(url)
            hostname = (parsed.hostname or "").lower()

            youtube_hosts = {
                "youtube.com",
                "www.youtube.com",
                "m.youtube.com",
                "youtu.be",
                "www.youtube-nocookie.com"
            }

            if (
                hostname in youtube_hosts
                or hostname.endswith(".youtube.com")
            ):

                self.send_json({
                    "error": (
                        "Reconocimos tu enlace de YouTube. "
                        "Esta versión no descarga videos directamente "
                        "desde YouTube. Si tienes los derechos o autorización, "
                        "exporta tu propio archivo y súbelo desde tu dispositivo."
                    ),
                    "code": "YOUTUBE_AUTHORIZATION_REQUIRED"
                }, 422)

                return

            job_id = uuid.uuid4().hex

            temporary_path = UPLOAD_DIR / (
                job_id + ".download"
            )

            try:

                extension = download_direct_video(
                    url,
                    temporary_path
                )

                final_path = UPLOAD_DIR / (
                    job_id + extension
                )

                temporary_path.replace(final_path)

                with LOCK:
                    JOBS[job_id] = {
                        "id": job_id,
                        "status": "queued",
                        "progress": 0,
                        "message": "Video descargado. Preparando el análisis...",
                        "clips": []
                    }

                thread = threading.Thread(
                    target=process_video,
                    args=(job_id, final_path),
                    daemon=True
                )

                thread.start()

                self.send_json({
                    "success": True,
                    "job_id": job_id,
                    "status_url": f"/api/jobs/{job_id}",
                    "message": "Enlace recibido. El video se está procesando."
                }, 202)

            except Exception:
                temporary_path.unlink(missing_ok=True)
                raise

        except json.JSONDecodeError:

            self.send_json(
                {"error": "El formato de la solicitud no es válido."},
                400
            )

        except ValueError as error:

            self.send_json(
                {"error": str(error)},
                400
            )

        except Exception as error:

            traceback.print_exc()

            self.send_json({
                "error": "No se pudo procesar el enlace.",
                "details": str(error)
            }, 500)


# ==========================================
# INICIAR MAILON CLIP AI
# ==========================================

port = int(os.environ.get("PORT", 10000))

server = ThreadingHTTPServer(
    ("0.0.0.0", port),
    MailonClipHandler
)

print(
    f"MAILON CLIP AI funcionando en el puerto {port}"
)

server.serve_forever()
