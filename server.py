import json
import os
import re
import uuid
import threading
import subprocess
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

import imageio_ffmpeg


BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
CLIPS_DIR = BASE_DIR / "clips"

UPLOAD_DIR.mkdir(exist_ok=True)
CLIPS_DIR.mkdir(exist_ok=True)

JOBS = {}
LOCK = threading.Lock()
MAX_UPLOAD = 500 * 1024 * 1024
CLIP_SECONDS = 40

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}


def process_video(job_id, input_path):
    output_dir = CLIPS_DIR / job_id
    output_dir.mkdir(exist_ok=True)

    with LOCK:
        JOBS[job_id]["status"] = "processing"
        JOBS[job_id]["message"] = "Procesando video..."

    try:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        output_pattern = str(output_dir / "clip_%03d.mp4")

        command = [
            ffmpeg,
            "-y",
            "-i", str(input_path),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-force_key_frames", "expr:gte(t,n_forced*40)",
            "-f", "segment",
            "-segment_time", str(CLIP_SECONDS),
            "-reset_timestamps", "1",
            output_pattern
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=7200
        )

        if result.returncode != 0:
            raise RuntimeError("No se pudo procesar el video.")

        files = sorted(output_dir.glob("clip_*.mp4"))

        if not files:
            raise RuntimeError("No se pudieron generar clips.")

        clips = [
            {
                "name": file.name,
                "url": f"/clips/{job_id}/{file.name}"
            }
            for file in files
        ]

        with LOCK:
            JOBS[job_id]["status"] = "completed"
            JOBS[job_id]["message"] = "Clips generados correctamente."
            JOBS[job_id]["clips"] = clips

    except Exception as error:
        with LOCK:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["message"] = str(error)[:300]

    finally:
        try:
            input_path.unlink(missing_ok=True)
        except OSError:
            pass


class MailonClipHandler(BaseHTTPRequestHandler):

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Filename")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_json({"status": "ok"})

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            try:
                body = (BASE_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_json({"error": "No se encontró index.html"}, 404)
            return

        if path == "/api/health":
            self.send_json({
                "status": "online",
                "application": "MAILON CLIP AI",
                "clip_duration": CLIP_SECONDS
            })
            return

        if path.startswith("/api/jobs/"):
            job_id = path.split("/")[-1]

            with LOCK:
                job = JOBS.get(job_id)

                if job is None:
                    self.send_json({"error": "Trabajo no encontrado"}, 404)
                else:
                    self.send_json(job)
            return

        if path.startswith("/clips/"):
            parts = path.strip("/").split("/")

            if len(parts) != 3:
                self.send_json({"error": "Ruta no válida"}, 404)
                return

            job_id, filename = parts[1], unquote(parts[2])

            if not re.fullmatch(r"[a-f0-9]{32}", job_id):
                self.send_json({"error": "Ruta no válida"}, 404)
                return

            if not re.fullmatch(r"clip_[0-9]{3}\.mp4", filename):
                self.send_json({"error": "Archivo no válido"}, 404)
                return

            file_path = CLIPS_DIR / job_id / filename

            if not file_path.is_file():
                self.send_json({"error": "Clip no encontrado"}, 404)
                return

            body = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_json({"error": "Ruta no encontrada"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

     if path == "/api/create-clips":
            try:
                length = int(self.headers.get("Content-Length", 0))

                if length <= 0 or length > 10000:
                    self.send_json({
                        "error": "Solicitud no válida."
                    }, 400)
                    return

                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8"))

                video_url = data.get("url", "").strip()
                parsed = urlparse(video_url)

                allowed_hosts = {
                    "youtube.com",
                    "www.youtube.com",
                    "m.youtube.com",
                    "youtu.be"
                }

                if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
                    self.send_json({
                        "error": "Introduce un enlace válido de YouTube."
                    }, 400)
                    return

                self.send_json({
                    "status": "received",
                    "message": "Enlace recibido correctamente. Para procesar el vídeo necesitamos acceso autorizado al archivo audiovisual.",
                    "url": video_url
                }, 202)

            except Exception:
                self.send_json({
                    "error": "No se pudo validar el enlace."
                }, 400)
            return

        if path != "/api/upload":
            self.send_json({"error": "Ruta no encontrada"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))

            if length <= 0 or length > MAX_UPLOAD:
                self.send_json({
                    "error": "Archivo vacío o superior al límite de 500 MB."
                }, 413)
                return

            filename = unquote(
                self.headers.get("X-Filename", "video.mp4")
            )
            extension = Path(filename).suffix.lower()

            if extension not in ALLOWED_EXTENSIONS:
                self.send_json({
                    "error": "Formato no permitido."
                }, 400)
                return

            job_id = uuid.uuid4().hex
            input_path = UPLOAD_DIR / f"{job_id}{extension}"

            remaining = length

            with open(input_path, "wb") as output:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("Subida incompleta.")
                    output.write(chunk)
                    remaining -= len(chunk)

            with LOCK:
                JOBS[job_id] = {
                    "id": job_id,
                    "status": "queued",
                    "message": "Video recibido. Esperando procesamiento.",
                    "clips": []
                }

            worker = threading.Thread(
                target=process_video,
                args=(job_id, input_path),
                daemon=True
            )
            worker.start()

            self.send_json({
                "status": "queued",
                "job_id": job_id,
                "message": "Video recibido correctamente.",
                "status_url": f"/api/jobs/{job_id}"
            }, 202)

        except Exception as error:
            self.send_json({
                "error": str(error)[:300]
            }, 400)


port = int(os.environ.get("PORT", 10000))
server = ThreadingHTTPServer(("0.0.0.0", port), MailonClipHandler)

print(f"MAILON CLIP AI funcionando en el puerto {port}")
server.serve_forever()
