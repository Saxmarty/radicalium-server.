import os
import json
import sqlite3
import math
import requests
import threading
import time
import random
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import webview

# --- CONFIGURAZIONE CLOUD & PARAMETRI ---
REMOTE_GPS_URL = "https://radicalium-server.onrender.com/api/posizioni"
COORDINATE_CASA = {"lat": 40.8518, "lon": 14.2681}
RAGGIO_SICUREZZA_KM = 5.0
FILE_DISPOSITIVI = "dispositivi.json"
DB_PATH = "eventi_tattici.db"
REMOTE_GPS_URL = "https://radicalium-server.onrender.com/api/posizioni"

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
CLIENT_ID = "admin@sgmanagemets.net-api-client"
CLIENT_SECRET = "1HlykeQYkPTK1wmzcaorGM7K70nbbdPV"

def determina_categoria(icao24, callsign, type_code=""):
    icao24 = str(icao24 or "").lower().strip()
    cs = str(callsign or "").upper().strip()
    tc = str(type_code or "").upper().strip()
    return "AEREO"

def calcola_distanza_haversine(lat1, lon1, lat2, lon2):
    R = 6371.0 # Raggio della Terra in KM
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS allarmi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            lat REAL,
            lon REAL,
            distanza REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# --- SERVER LOCALE DASHBOARD HTML ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Radicalium Tactical Map</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body { margin: 0; padding: 0; background-color: #121212; color: #fff; font-family: sans-serif; }
        #map { height: 100vh; width: 100vw; }
        .status-panel {
            position: absolute; top: 10px; right: 10px; z-index: 1000;
            background: rgba(0, 0, 0, 0.85); padding: 12px 20px; border-radius: 8px;
            border: 1px solid #00ffcc; box-shadow: 0 0 10px rgba(0, 255, 204, 0.3);
        }
        .status-online { color: #00ffcc; font-weight: bold; }
    </style>
</head>
<body>
    <div class="status-panel">
        <div>RADICALIUM CLOUD ENGINE: <span class="status-online">ONLINE</span></div>
        <div id="device-count">Dispositivi attivi: 0</div>
    </div>
    <div id="map"></div>

    <script>
        var map = L.map('map').setView([40.8518, 14.2681], 12);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: '© OpenStreetMap'
        }).addTo(map);

        var markers = {};

        function updateMap() {
            fetch('/api/posizioni')
                .then(res => res.json())
                .then(data => {
                    var count = 0;
                    for (var dev_id in data) {
                        count++;
                        var item = data[dev_id];
                        var lat = item.lat;
                        var lon = item.lon;

                        if (markers[dev_id]) {
                            markers[dev_id].setLatLng([lat, lon]);
                        } else {
                            markers[dev_id] = L.marker([lat, lon]).addTo(map)
                                .bindPopup('<b>ID: ' + dev_id + '</b><br>Ultimo segnale: ' + item.last_update);
                        }
                    }
                    document.getElementById('device-count').innerText = "Dispositivi attivi: " + count;
                })
                .catch(err => console.error("Errore update mappa:", err));
        }

        setInterval(updateMap, 3000);
        updateMap();
    </script>
</body>
</html>
"""

class LocalRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode('utf-8'))
        elif self.path == '/api/posizioni':
            try:
                response = requests.get(REMOTE_GPS_URL, timeout=5)
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(response.content)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

def run_local_server():
    server = HTTPServer(('127.0.0.1', 9999), LocalRequestHandler)
    server.serve_forever()

if __name__ == '__main__':
    # Avvia il server locale di bridging in background
    server_thread = threading.Thread(target=run_local_server, daemon=True)
    server_thread.start()

    # Avvia la finestra GUI Webview locale
    webview.create_window('RADICALIUM CONTROL CENTER', 'http://127.0.0.1:9999', width=1280, height=800)
    webview.start()
