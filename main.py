import os
import json
import sqlite3
import math
import requests
import threading
import time
import random
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# ==========================================
# CONFIGURAZIONE CLOUD & DATABASE
# ==========================================
DB_PATH = "/tmp/eventi_tattici.db" if os.path.exists("/tmp") else "eventi_tattici.db"
FILE_DISPOSITIVI = "/tmp/dispositivi.json" if os.path.exists("/tmp") else "dispositivi.json"

COORDINATE_CASA = {"lat": 40.8518, "lon": 14.2681}
RAGGIO_SICUREZZA_KM = 5.0

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
CLIENT_ID = "admin@sgmanagemets.net-api-client"
CLIENT_SECRET = "lHLykeQYkPTk1wmzcaorGM7K70nbbdPV"

def determina_categoria(icao24, callsign, type_code=""):
    icao24 = str(icao24 or "").lower().strip()
    cs = str(callsign or "").upper().strip()
    tc = str(type_code or "").upper().strip()
    
    tipi_elicotteri = ("EC35", "EC45", "EC55", "A109", "A139", "A169", "AW1", "B06", "B412", "H60", "H47", "R44", "R66", "AS35", "AS50", "BELL")
    if (tc.startswith(tipi_elicotteri) or 
        any(cs.startswith(p) for p in ("HELI", "HLE", "EMS", "PEGASO", "SAMU", "REGA", "POLICE", "POLIZIA", "CARAB", "CORPO"))):
        return "ELICOTTERO"

    if (icao24.startswith(('33f', '300', '301', '33e', 'ae', 'af', '43c', '3f')) or 
        any(cs.startswith(p) for p in ("IAM", "RCH", "GAF", "AME", "ASY", "RRR", "FORZA"))):
        return "MILITARE"

    if any(cs.startswith(p) for p in ("DHL", "FDX", "UPS", "CLX", "GTI", "BOX", "BCS")):
        return "CARGO"

    if (cs.startswith("N") and len(cs) <= 6 and cs[1:].isalnum()) or cs.startswith(("M-", "T7-", "VP-C", "3B-")):
        return "PRIVATO"

    return "LINEA"

class TokenManager:
    def __init__(self):
        self.token = None
        self.expires_at = None
        self.session = requests.Session()

    def get_token(self):
        if self.token and self.expires_at and datetime.now() < self.expires_at:
            return self.token
        try:
            r = self.session.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                },
                timeout=5
            )
            r.raise_for_status()
            data = r.json()
            self.token = data["access_token"]
            expires_in = data.get("expires_in", 1800)
            self.expires_at = datetime.now() + timedelta(seconds=expires_in - 30)
            return self.token
        except Exception as e:
            print("Errore OAuth2 OpenSky:", e)
            return None

    def headers(self):
        token = self.get_token()
        h = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

token_manager = TokenManager()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS terremoti (
            id TEXT PRIMARY KEY, magnitudo REAL, luogo TEXT,
            lat REAL, lon REAL, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS eventi_custom (
            id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, categoria TEXT,
            lat REAL, lon REAL, colore TEXT DEFAULT '#a855f7', timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS storico_gps (
            id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT,
            lat REAL, lon REAL, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_sessione (
            id INTEGER PRIMARY KEY CHECK (id = 1), ultimo_logout DATETIME
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO app_sessione (id, ultimo_logout) VALUES (1, CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()

init_db()

def salva_timestamp_logout():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE app_sessione SET ultimo_logout = CURRENT_TIMESTAMP WHERE id = 1")
        conn.commit()
        conn.close()
    except Exception as e:
        print("Errore logout:", e)

def get_timestamp_logout():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT ultimo_logout FROM app_sessione WHERE id = 1")
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception as e:
        print("Errore lettura logout:", e)
    return "1970-01-01 00:00:00"

DISPOSITIVI_DEFAULT = {
    "PC_PRINCIPALE": {
        "nome": "PC Lavoro / Personale", "proprietario": "Tu", 
        "colore": "#00f2fe", "tipo": "IP", "attivo": True,
        "lat": 40.8518, "lon": 14.2681, "stato": "In casa"
    },
    "IPHONE_MARTINA": {
        "nome": "iPhone di Martina", "proprietario": "Figlia", 
        "colore": "#e11d48", "tipo": "GPS", "attivo": True,
        "lat": 40.8518, "lon": 14.2681, "stato": "In casa"
    }
}

def carica_dispositivi():
    if os.path.exists(FILE_DISPOSITIVI):
        try:
            with open(FILE_DISPOSITIVI, "r", encoding="utf-8") as f:
                dati = json.load(f)
                if dati:
                    if "IPHONE_MARTINA" not in dati:
                        dati["IPHONE_MARTINA"] = DISPOSITIVI_DEFAULT["IPHONE_MARTINA"]
                        salva_dispositivi(dati)
                    return dati
        except:
            pass
    salva_dispositivi(DISPOSITIVI_DEFAULT)
    return DISPOSITIVI_DEFAULT.copy()

def salva_dispositivi(db):
    try:
        with open(FILE_DISPOSITIVI, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=4)
    except Exception as e:
        print(f"Errore JSON: {e}")

DISPOSITIVI_DB = carica_dispositivi()

def calcola_distanza_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

# ==========================================
# INTERFACCIA WEB (HTML UI ADATTATA FLASK)
# ==========================================
# Sostituiamo il mockup di pywebview con le rotte nativi HTTP Fetch
HTML_UI_CLOUD = """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <title>RADICALIUM - Tactical Multi-Hazard Control Center</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        body, html { width: 100%; height: 100%; overflow: hidden; background: #06101e; color: #fff; }
        #app-container { display: flex; width: 100vw; height: 100vh; flex-direction: column; position: relative; }

        #topbar { height: 48px; background: #0f172a; border-bottom: 1px solid #1e293b; display: flex; align-items: center; justify-content: space-between; padding: 0 20px; z-index: 1100; }
        .brand { font-size: 15px; font-weight: bold; color: #00f2fe; letter-spacing: 1.5px; }
        .nav-buttons { display: flex; gap: 8px; }
        .btn-nav { background: #1e293b; color: white; border: 1px solid #334155; padding: 6px 12px; border-radius: 6px; font-size: 11px; font-weight: bold; cursor: pointer; transition: 0.2s; }
        .btn-nav:hover { background: #0284c7; border-color: #0284c7; }
        .btn-scan { background: #8b5cf6; border-color: #7c3aed; }
        .btn-scan:hover { background: #6d28d9; }
        .btn-custom-evt { background: #a855f7; border-color: #9333ea; }
        .btn-custom-evt:hover { background: #7e22ce; }

        #main-body { display: flex; flex: 1; height: calc(100vh - 48px); position: relative; }
        #map { flex: 1; height: 100%; background: #06101e; }
        #sidebar { width: 380px; background: #0f172a; border-left: 1px solid #1e293b; display: flex; flex-direction: column; padding: 14px; z-index: 1000; box-shadow: -5px 0 15px rgba(0,0,0,0.5); gap: 12px; overflow-y: auto; }
        
        .status-badge { color: #00ffcc; font-weight: bold; margin-right: 15px; font-size: 12px; }
    </style>
</head>
<body>
    <div id="app-container">
        <div id="topbar">
            <div class="brand">RADICALIUM // TACTICAL CONTROL CENTER CLOUD</div>
            <div style="display:flex; align-items:center;">
                <span class="status-badge">● ONLINE (RENDER)</span>
                <div class="nav-buttons">
                    <button class="btn-nav" style="background:#0284c7;" onclick="volaACasa()">🏠 CASA</button>
                    <button class="btn-nav btn-scan" onclick="forzaRilevamentoEventiConAnimazione()">🔄 RILEVA EVENTI</button>
                </div>
            </div>
        </div>
        <div id="main-body">
            <div id="map"></div>
            <div id="sidebar">
                <div style="color:#00f2fe; font-size:14px; font-weight:bold; padding-bottom:10px; border-bottom:1px solid #1e293b;">⚡ RADAR TATTICO ATTIVO</div>
                <div id="deviceCount" style="font-size:12px; margin-top:10px;">Monitoraggio costante in corso...</div>
            </div>
        </div>
    </div>
    <script>
        var map = L.map('map').setView([40.8518, 14.2681], 9);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19, attribution: '© OpenStreetMap | Radicalium Engine'
        }).addTo(map);

        L.circle([40.8518, 14.2681], { color: 'red', fillColor: '#f03', fillOpacity: 0.1, radius: 5000 }).addTo(map);

        var markers = {};

        function volaACasa() { map.flyTo([40.8518, 14.2681], 12); }
        function forzaRilevamentoEventiConAnimazione() { updateGPS(); }

        function updateGPS() {
            fetch('/api/posizioni')
                .then(r => r.json())
                .then(data => {
                    var c = 0;
                    for (var id in data) {
                        c++;
                        var item = data[id];
                        if (markers[id]) {
                            markers[id].setLatLng([item.lat, item.lon]);
                        } else {
                            markers[id] = L.marker([item.lat, item.lon]).addTo(map)
                                .bindPopup('<b>📱 UNITA: ' + id + '</b><br>Ultimo segnale: ' + item.last_update);
                        }
                    }
                    document.getElementById('deviceCount').innerText = "Dispositivi tracciati online: " + c;
                });
        }

        setInterval(updateGPS, 3000);
        updateGPS();
    </script>
</body>
</html>
"""

# ==========================================
# ROTTE API FLASK (SERVER ENGINE)
# ==========================================

@app.route('/')
def index():
    """Mostra la Dashboard Tattica direttamente dal Browser"""
    return render_template_string(HTML_UI_CLOUD)

@app.route('/api/gps', methods=['GET', 'POST'])
def receive_gps():
    """Riceve le coordinate inviate da Traccar Client dagli smartphone"""
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
            f_lat = float(lat)
            f_lon = float(lon)
            dev_id = str(device_id).strip()

            DISPOSITIVI_DB[dev_id] = {
                "nome": dev_id, "proprietario": "Tracker Mobile",
                "colore": "#f97316", "tipo": "GPS", "attivo": True,
                "lat": f_lat, "lon": f_lon, "stato": "Live Cloud"
            }
            salva_dispositivi(DISPOSITIVI_DB)

            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO storico_gps (device_id, lat, lon) VALUES (?, ?, ?)", (dev_id, f_lat, f_lon))
            conn.commit()
            conn.close()
            return "OK", 200
        except Exception as e:
            return f"Error: {e}", 500

    return "RECEIVER ACTIVE", 200

@app.route('/api/posizioni', methods=['GET'])
def get_posizioni():
    """Fornisce le ultime posizioni lette alla dashboard"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            SELECT device_id, lat, lon, MAX(timestamp) 
            FROM storico_gps 
            GROUP BY device_id
        """)
        rows = c.fetchall()
        conn.close()
        
        data = {}
        for r in rows:
            data[r[0]] = {"lat": r[1], "lon": r[2], "last_update": r[3]}
        return jsonify(data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
