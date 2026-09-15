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
DB_PATH = "/tmp/radicalium_cloud.db" if os.path.exists("/tmp") else "radicalium_cloud.db"

COORDINATE_CASA = {"lat": 40.8518, "lon": 14.2681}
RAGGIO_SICUREZZA_KM = 5.0

# OpenSky Network API Credentials
CLIENT_ID = "admin@sgmanagemets.net-api-client"
CLIENT_SECRET = "1HlykeQYkPTK1wmzcaorGM7K70nbbdPV"
TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"

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
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

def determina_categoria(icao24, callsign, type_code=""):
    icao24 = str(icao24 or "").lower().strip()
    cs = str(callsign or "").upper().strip()
    tc = str(type_code or "").upper().strip()
    return "AEREO"

# ==========================================
# INTERFACCIA GRAFICA CLOUD (DASHBOARD HTML/LEAFLET)
# ==========================================
HTML_DASHBOARD = """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="utf-8" />
    <title>RADICALIUM TACTICAL COMMAND CENTER - GLOBAL CLOUD</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body { margin: 0; padding: 0; background-color: #080a0f; color: #00ffcc; font-family: 'Courier New', monospace; }
        #map { height: 100vh; width: 100vw; }
        .hud-panel {
            position: absolute; top: 15px; right: 15px; z-index: 1000;
            background: rgba(10, 15, 25, 0.92); padding: 15px 20px; border-radius: 8px;
            border: 1px solid #00ffcc; box-shadow: 0 0 15px rgba(0, 255, 204, 0.3);
        }
        .hud-title { font-size: 14px; font-weight: bold; color: #fff; margin-bottom: 8px; border-bottom: 1px solid #00ffcc; padding-bottom: 4px; }
        .legend-item { margin-top: 5px; font-size: 12px; }
        .badge-dev { color: #00ffcc; font-weight: bold; }
        .badge-air { color: #ffcc00; font-weight: bold; }
        .badge-sea { color: #0099ff; font-weight: bold; }
    </style>
</head>
<body>
    <div class="hud-panel">
        <div class="hud-title">RADICALIUM GLOBAL RADAR</div>
        <div class="legend-item">📱 UNITA TERRESTRI: <span id="cnt-dev" class="badge-dev">0</span></div>
        <div class="legend-item">✈️ TRAFFICO AEREO: <span id="cnt-air" class="badge-air">0</span></div>
        <div class="legend-item">🚢 TRAFFICO MARITTIMO: <span id="cnt-sea" class="badge-sea">0</span></div>
    </div>
    <div id="map"></div>

    <script>
        var map = L.map('map').setView([40.8518, 14.2681], 9);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: '© OpenStreetMap | Radicalium Tactical Cloud Engine'
        }).addTo(map);

        // Cerchio Area Tattica
        L.circle([40.8518, 14.2681], {
            color: 'red', fillColor: '#f03', fillOpacity: 0.1, radius: 5000
        }).addTo(map).bindPopup("RAGGIO SICUREZZA 5KM");

        var markersGPS = {};
        var markersAerei = {};

        var iconAereo = L.divIcon({html: '✈️', className: 'plane-icon', iconSize: [20, 20]});

        function syncSystem() {
            // 1. Fetch Dispositivi GPS / Traccar
            fetch('/api/posizioni')
                .then(r => r.json())
                .then(data => {
                    var c = 0;
                    for (var id in data) {
                        c++;
                        var item = data[id];
                        if (markersGPS[id]) {
                            markersGPS[id].setLatLng([item.lat, item.lon]);
                        } else {
                            markersGPS[id] = L.marker([item.lat, item.lon]).addTo(map)
                                .bindPopup('<b>📱 UNITA GPS: ' + id + '</b><br>Update: ' + item.last_update);
                        }
                    }
                    document.getElementById('cnt-dev').innerText = c;
                });

            // 2. Fetch Aerei OpenSky Network
            fetch('/api/aerei')
                .then(r => r.json())
                .then(data => {
                    var c = 0;
                    if (data.states) {
                        data.states.forEach(plane => {
                            var callsign = plane[1] ? plane[1].trim() : plane[0];
                            var lon = plane[5];
                            var lat = plane[6];
                            var alt = plane[7] || 0;

                            if (lat && lon) {
                                c++;
                                if (markersAerei[callsign]) {
                                    markersAerei[callsign].setLatLng([lat, lon]);
                                } else {
                                    markersAerei[callsign] = L.marker([lat, lon], {icon: iconAereo}).addTo(map)
                                        .bindPopup('<b>✈️ VOLO: ' + callsign + '</b><br>Quota: ' + alt + 'm');
                                }
                            }
                        });
                    }
                    document.getElementById('cnt-air').innerText = c;
                }).catch(e => console.log("Aerei non disponibili"));
        }

        setInterval(syncSystem, 4000);
        syncSystem();
    </script>
</body>
</html>
"""

# ==========================================
# ROTTE API FLASK
# ==========================================

@app.route('/')
def index():
    """Restituisce la Dashboard Tattica online"""
    return render_template_string(HTML_DASHBOARD)

@app.route('/api/gps', methods=['GET', 'POST'])
def receive_gps():
    """Ingestion posizioni da smartphone (Traccar Client)"""
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
    return "RECEIVER ACTIVE", 200

@app.route('/api/posizioni', methods=['GET'])
def get_posizioni():
    """Restituisce le ultime posizioni dei cellulari alla mappa"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT device_id, lat, lon, MAX(timestamp) FROM tracciamento_gps GROUP BY device_id")
        rows = c.fetchall()
        conn.close()
        return jsonify({r[0]: {"lat": r[1], "lon": r[2], "last_update": r[3]} for r in rows}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/aerei', methods=['GET'])
def get_aerei():
    """Interroga OpenSky Network in tempo reale attorno all'area di interesse"""
    try:
        lamin = COORDINATE_CASA["lat"] - 1.5
        lamax = COORDINATE_CASA["lat"] + 1.5
        lomin = COORDINATE_CASA["lon"] - 1.5
        lomax = COORDINATE_CASA["lon"] + 1.5
        
        url = f"https://opensky-network.org/api/states/all?lamin={lamin}&lamax={lamax}&lomin={lomin}&lomax={lomax}"
        res = requests.get(url, timeout=4)
        if res.status_code == 200:
            return jsonify(res.json()), 200
        return jsonify({"states": []}), 200
    except Exception as e:
        return jsonify({"states": [], "error": str(e)}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
