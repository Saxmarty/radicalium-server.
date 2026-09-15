import os
import json
import sqlite3
import math
import requests
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# ==========================================
# CONFIGURAZIONE AMBIENTE CLOUD & DATABASE
# ==========================================
# Su Render usiamo la cartella temporanea per SQLite o la radice del progetto
DB_PATH = "/tmp/radicalium_cloud.db" if os.path.exists("/tmp") else "radicalium_cloud.db"

COORDINATE_CASA = {"lat": 40.8518, "lon": 14.2681}
RAGGIO_SICUREZZA_KM = 5.0

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
CLIENT_ID = "admin@sgmanagemets.net-api-client"
CLIENT_SECRET = "1HlykeQYkPTK1wmzcaorGM7K70nbbdPV"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tracciamento_gps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            lat REAL,
            lon REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
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

# ==========================================
# ALGORITMI TATTICI & MATH ENGINE
# ==========================================
def calcola_distanza_haversine(lat1, lon1, lat2, lon2):
    R = 6371.0  # Raggio della Terra in KM
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def determina_categoria(icao24, callsign, type_code=""):
    icao24 = str(icao24 or "").lower().strip()
    cs = str(callsign or "").upper().strip()
    tc = str(type_code or "").upper().strip()
    return "AEREO"

# ==========================================
# INTERFACCIA GRAFICA DASHBOARD (HTML / LEAFLET)
# ==========================================
HTML_DASHBOARD = """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="utf-8" />
    <title>RADICALIUM TACTICAL DASHBOARD - GLOBAL CLOUD</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body { margin: 0; padding: 0; background-color: #0a0a0a; color: #00ffcc; font-family: 'Courier New', monospace; }
        #map { height: 100vh; width: 100vw; }
        .hud-panel {
            position: absolute; top: 15px; right: 15px; z-index: 1000;
            background: rgba(10, 15, 20, 0.9); padding: 15px 25px; border-radius: 8px;
            border: 1px solid #00ffcc; box-shadow: 0 0 15px rgba(0, 255, 204, 0.3);
        }
        .hud-title { font-weight: bold; font-size: 14px; margin-bottom: 5px; color: #ffffff; }
        .status-badge { color: #00ffcc; font-weight: bold; }
    </style>
</head>
<body>
    <div class="hud-panel">
        <div class="hud-title">RADICALIUM ENGINE CLOUD</div>
        <div>STATO: <span class="status-badge">ONLINE (RENDER)</span></div>
        <div id="device-count">DISPOSITIVI ATTIVI: 0</div>
    </div>
    <div id="map"></div>

    <script>
        var map = L.map('map').setView([40.8518, 14.2681], 11);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: '© OpenStreetMap | Radicalium Tactical Engine'
        }).addTo(map);

        // Marker posizione di controllo (CASA)
        L.circle([40.8518, 14.2681], {
            color: 'red',
            fillColor: '#f03',
            fillOpacity: 0.15,
            radius: 5000
        }).addTo(map).bindPopup("RAGGIO SICUREZZA 5KM");

        var markers = {};

        function fetchCloudPositions() {
            fetch('/api/posizioni')
                .then(res => res.json())
                .then(data => {
                    var activeCount = 0;
                    for (var dev_id in data) {
                        activeCount++;
                        var item = data[dev_id];
                        var lat = item.lat;
                        var lon = item.lon;

                        if (markers[dev_id]) {
                            markers[dev_id].setLatLng([lat, lon]);
                        } else {
                            markers[dev_id] = L.marker([lat, lon]).addTo(map)
                                .bindPopup('<b>ID UNITA: ' + dev_id + '</b><br>Ultimo segnale: ' + item.last_update);
                        }
                    }
                    document.getElementById('device-count').innerText = "DISPOSITIVI ATTIVI: " + activeCount;
                })
                .catch(err => console.error("Errore sync mappa:", err));
        }

        setInterval(fetchCloudPositions, 3000);
        fetchCloudPositions();
    </script>
</body>
</html>
"""

# ==========================================
# ROTTE API FLASK (INGESTION & DASHBOARD)
# ==========================================

@app.route('/', methods=['GET'])
def index():
    """Mostra la Dashboard Tattica direttamente nel Browser"""
    return render_template_string(HTML_DASHBOARD)

@app.route('/api/gps', methods=['GET', 'POST'])
def receive_gps():
    """Riceve le posizioni inviate dagli smartphone (Traccar Client)"""
    if request.method == 'GET':
        device_id = request.args.get('id')
        lat = request.args.get('lat')
        lon = request.args.get('lon')
    else:
        data = request.get_json(silent=True) or {}
        device_id = data.get('id')
        lat = data.get('lat')
        lon = data.get('lon')

    if device_id and lat and lon:
        try:
            device_id_clean = str(device_id).strip()
            lat_val = float(lat)
            lon_val = float(lon)

            # Verifica evento tattico Haversine (distanza da casa)
            distanza = calcola_distanza_haversine(
                COORDINATE_CASA["lat"], COORDINATE_CASA["lon"], lat_val, lon_val
            )

            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO tracciamento_gps (device_id, lat, lon) VALUES (?, ?, ?)",
                      (device_id_clean, lat_val, lon_val))
            
            if distanza > RAGGIO_SICUREZZA_KM:
                c.execute("INSERT INTO allarmi (device_id, lat, lon, distanza) VALUES (?, ?, ?, ?)",
                          (device_id_clean, lat_val, lon_val, distanza))
            
            conn.commit()
            conn.close()
            return "OK", 200
        except Exception as e:
            return f"Error: {e}", 500

    return "RADICALIUM CLOUD RECEIVER ACTIVE", 200

@app.route('/api/posizioni', methods=['GET'])
def get_posizioni():
    """Fornisce le ultime posizioni note di ogni dispositivo alla Mappa"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            SELECT device_id, lat, lon, MAX(timestamp) 
            FROM tracciamento_gps 
            GROUP BY device_id
        """)
        rows = c.fetchall()
        conn.close()
        
        data = {}
        for r in rows:
            data[r[0]] = {
                "lat": r[1],
                "lon": r[2],
                "last_update": r[3]
            }
        return jsonify(data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
