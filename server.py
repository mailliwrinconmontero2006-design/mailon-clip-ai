import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


class MailonClipHandler(BaseHTTPRequestHandler):

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            try:
                with open("index.html", "rb") as file:
                    body = file.read()

                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            except FileNotFoundError:
                self.send_json({
                    "error": "No se encontró index.html"
                }, 500)

        else:
            self.send_json({"error": "Ruta no encontrada"}, 404)

    def do_POST(self):
        if urlparse(self.path).path != "/api/create-clips":
            self.send_json({"error": "Ruta no encontrada"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            video_url = data.get("url", "").strip()

            if "youtube.com/" not in video_url and "youtu.be/" not in video_url:
                self.send_json({"error": "Enlace de YouTube no válido"}, 400)
                return

            self.send_json({
                "status": "received",
                "message": "Enlace recibido correctamente. Motor de clips pendiente de conexión.",
                "duration_seconds": 40
            }, 202)

        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "Solicitud no válida"}, 400)


port = int(os.environ.get("PORT", 10000))
server = HTTPServer(("0.0.0.0", port), MailonClipHandler)

print(f"MAILON CLIP AI funcionando en el puerto {port}")
server.serve_forever()
