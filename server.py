import json
import os
import re
import uuid
import threading
import subprocess
import traceback
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
CLIP_SECONDS = 40
ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi"
}

WHISPER_MODEL_NAME = os.environ.get(
    "WHISPER_MODEL", "tiny"
)

JOBS = {}
LOCK = threading.Lock()

IMPORTANT_WORDS = {
    "increíble", "importante", "atención",
    "nunca", "siempre", "verdad", "problema",
    "solución", "dinero", "secreto", "resultado",
    "recuerda", "aprendí", "sorpresa", "mejor",
    "peor", "historia", "finalmente", "error",
    "consejo", "descubrí", "imagina", "realmente",
    "exactamente", "éxito", "porqué", "porque"
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


# ==========================================
# ANÁLISIS DEL CONTENIDO CON IA
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
        transcript.append({
            "start": float(segment.start),
            "end": float(segment.end),
            "text": segment.text.strip()
        })

    return transcript


def score_window(start, end, transcript):
    relevant = []

    for segment in transcript:
        midpoint = (
            segment["start"] + segment["end"]
        ) / 2

        if start <= midpoint < end:
            relevant.append(segment)

    if not relevant:
        return 0.1

    text = " ".join(
        item["text"] for item in relevant
    ).lower()

    words = re.findall(r"\b\w+\b", text)

    if not words:
        return 0.1

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
        1
    )

    keyword_count = sum(
        1 for word in words
        if word in IMPORTANT_WORDS
    )

    keyword_score = min(
        keyword_count / 8,
        1
    )

    length_score = min(
        len(words) / 100,
        1
    )

    sentence_score = min(
        text.count(".") / 4,
        1
    )

    return (
        speech_density * 0.35
        + keyword_score * 0.30
        + length_score * 0.25
        + sentence_score * 0.10
    )


def select_best_moments(duration, transcript):
    if duration < CLIP_SECONDS:
        raise RuntimeError(
            "El video debe durar al menos 40 segundos."
        )

    last_start = duration - CLIP_SECONDS

    starts = list(range(
        0,
        int(last_start) + 1,
        5
    ))

    if not starts or starts[-1] != last_start:
        starts.append(last_start)

    candidates = []

    for start in starts:
        candidates.append({
            "start": float(start),
            "end": float(start + CLIP_SECONDS),
            "score": score_window(
                start,
                start + CLIP_SECONDS,
                transcript
            )
        })

    desired_count = int(duration // CLIP_SECONDS)

    candidates.sort(
        key=lambda item: item["score"],
        reverse=True
    )

    selected = []

    for candidate in candidates:
        overlaps = any(
            not (
                candidate["end"] <= chosen["start"]
                or candidate["start"] >= chosen["end"]
            )
            for chosen in selected
        )

        if not overlaps:
            selected.append(candidate)

        if len(selected) >= desired_count:
            break

    selected.sort(
        key=lambda item: item["start"]
    )

    return selected


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
# PROCESAMIENTO DEL VIDEO
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

            clips.append({
                "name": filename,
                "url": f"/clips/{job_id}/{filename}",
                "start": round(moment["start"], 2),
                "duration": CLIP_SECONDS,
                "score": round(moment["score"], 3)
            })

            progress = 50 + int(
                index / total * 45
            )

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
            "Access-Control-Allow-Origin", "*"
        )
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header(
            "Access-Control-Allow-Origin", "*"
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
                "Content-Length", str(len(content))
            )
            self.end_headers()
            self.wfile.write(content)
            return

        if path == "/api/health":
            self.send_json({
                "status": "online",
                "app": "MAILON CLIP AI",
                "clip_seconds": CLIP_SECONDS
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

            if (
                not re.fullmatch(r"[a-f0-9]{32}", job_id)
                or not re.fullmatch(r"clip_\d{3}\.mp4", filename)
            ):
                self.send_error(404)
                return

            clip_path = CLIPS_DIR / job_id / filename

            if not clip_path.is_file():
                self.send_error(404)
                return

            self.send_response(200)
            self.send_header(
                "Content-Type", "video/mp4"
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

            with clip_path.open("rb") as video:
                while True:
                    chunk = video.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

            return

        self.send_error(404, "Ruta no encontrada")

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/create-clips":
            self.send_json({
                "error": (
                    "La importación mediante enlaces todavía "
                    "no está habilitada. Sube un video desde "
                    "tu dispositivo."
                )
            }, 501)
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
                "Content-Type", ""
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
