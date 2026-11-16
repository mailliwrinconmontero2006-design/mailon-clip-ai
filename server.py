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
        if urlparse(self.path).path == "/":
            self.send_json({
                "app": "MAILON CLIP AI",
                "status": "online",
                "message": "Servidor funcionando correctamente"
            })
        else:
            self.send_json({"error": "Ruta no encontrada"}, 404)

    def do_POST(self):
        if urlparse(self.path).path != "/api/create-clips":
            self.send_json({"error": "Ruta no encontrada"}, 404)
            return

        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        video_url = data.get("url", "")

        if "youtube.com/" not in video_url and "youtu.be/" not in video_url:
            self.send_json({"error": "Enlace de YouTube no válido"}, 400)
            return

        self.send_json({
            "status": "received",
            "message": "Enlace recibido. Falta conectar el motor de creación de clips.",
            "duration_seconds": 40
        }, 202)


port = int(os.environ.get("PORT", 10000))
server = HTTPServer(("0.0.0.0", port), MailonClipHandler)

print(f"MAILON CLIP AI funcionando en el puerto {port}")
server.serve_forever()
