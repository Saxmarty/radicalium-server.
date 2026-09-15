import os
import json
import sqlite3
import math
import requests
import struct
import socket
import threading
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

def registra_posizione_dispositivo(dev_id, lat, lon, stato="Live Cloud"):
    """Salva la posizione inviata da telefoni o da localizzatori Teltonika"""
    DISPOSITIVI_DB[dev_id] = {
        "nome": f"Teltonika ({dev_id})" if str(dev_id).isdigit() else dev_id,
        "proprietario": "Hardware Tracker" if str(dev_id).isdigit() else "Tracker Mobile",
        "colore": "#22c55e" if str(dev_id).isdigit() else "#f97316",
        "tipo": "TELTONIKA" if str(dev_id).isdigit() else "GPS",
        "attivo": True, "lat": lat, "lon": lon, "stato": stato
    }
    salva_dispositivi(DISPOSITIVI_DB)
    
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO storico_gps (device_id, lat, lon) VALUES (?, ?, ?)", (str(dev_id), lat, lon))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Errore salvataggio DB:", e)

# ==========================================
# PARSER TELTONIKA (CODEC 8 / TCP SOCKET)
# ==========================================
def gestisci_connessione_teltonika(client_socket, addr):
    try:
        data = client_socket.recv(1024)
        if not data or len(data) < 2:
            client_socket.close()
            return
        
        imei_len = struct.unpack('>H', data[:2])[0]
        imei = data[2:2+imei_len].decode('utf-8')
        client_socket.send(b'\x01')

        while True:
            packet = client_socket.recv(1024)
            if not packet or len(packet) < 12:
                break

            num_records = packet[9] if len(packet) > 9 else 1
            client_socket.send(struct.pack('>I', num_records))

            try:
                if len(packet) >= 30:
                    lon_raw = struct.unpack('>i', packet[17:21])[0]
                    lat_raw = struct.unpack('>i', packet[21:25])[0]
                    
                    lat = lat_raw / 10000000.0
                    lon = lon_raw / 10000000.0

                    if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat != 0 and lon != 0):
                        registra_posizione_dispositivo(imei, lat, lon, "Teltonika GPS Live")
            except Exception as e:
                print(f"Errore parsing Teltonika {imei}:", e)

    except Exception as e:
        print(f"Errore socket Teltonika:", e)
    finally:
        client_socket.close()

def avvia_server_socket_teltonika():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(('0.0.0.0', 5027))
        server.listen(10)
        while True:
            client, addr = server.accept()
            threading.Thread(target=gestisci_connessione_teltonika, args=(client, addr), daemon=True).start()
    except Exception as e:
        print("Errore Socket Teltonika 5027:", e)

threading.Thread(target=avvia_server_socket_teltonika, daemon=True).start()

# ==========================================
# ROTTE API FLASK PER LA MAPPA
# ==========================================

@app.route('/api/dispositivi', methods=['GET'])
def get_dispositivi_api():
    return jsonify(DISPOSITIVI_DB)

@app.route('/api/radar_timestamp', methods=['GET'])
def get_latest_radar_timestamp_api():
    try:
        res = requests.get("https://api.rainviewer.com/public/weather-maps.json", timeout=5)
        if res.status_code == 200:
            data = res.json()
            radar_past = data.get("radar", {}).get("past", [])
            if radar_past:
                return jsonify({"status": "ok", "time": radar_past[-1].get("time"), "host": data.get("host")})
    except Exception as e:
        print("Errore radar:", e)
    return jsonify({"status": "error"})

@app.route('/api/mcdonalds', methods=['GET'])
def fetch_mcdonalds_api():
    lamin = request.args.get("lamin")
    lomin = request.args.get("lomin")
    lamax = request.args.get("lamax")
    lomax = request.args.get("lomax")

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) RadicaliumMap/1.0'}
    url_nom = f"https://nominatim.openstreetmap.org/search?q=McDonald's&format=json&addressdetails=1&bounded=1&viewbox={lomin},{lamax},{lomax},{lamin}&limit=50"
    
    try:
        res = requests.get(url_nom, headers=headers, timeout=6)
        if res.status_code == 200:
            data = res.json()
            locali = []
            for item in data:
                addr = item.get("address", {})
                locali.append({
                    "id": item.get("place_id"),
                    "nome": "McDonald's",
                    "lat": float(item.get("lat")),
                    "lon": float(item.get("lon")),
                    "citta": addr.get("city") or addr.get("town") or addr.get("suburb") or "Città",
                    "regione": addr.get("state") or "Regione",
                    "stato": addr.get("country") or "Italia",
                    "via": addr.get("road") or "Indirizzo in Mappa",
                    "orario": "07:00 - 01:00",
                    "order_link": "https://www.mcdonalds.it/trova-il-ristorante"
                })
            return jsonify({"status": "OK", "count": len(locali), "data": locali})
    except Exception as e:
        print("Errore McD:", e)
    return jsonify({"status": "ERROR", "count": 0, "data": []})

@app.route('/api/navi_live', methods=['GET'])
def fetch_navi_live_api():
    latC = request.args.get("lat", 41.9)
    lonC = request.args.get("lon", 12.5)
    try:
        url = f"https://api.adsb.lol/v2/point/{latC}/{lonC}/350"
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        navi = []
        if res.status_code == 200:
            ac = res.json().get("ac", []) or []
            for p in ac:
                if p.get("lat") is not None and p.get("lon") is not None:
                    alt = p.get("alt_baro", 0)
                    if alt == "ground" or (isinstance(alt, (int, float)) and alt <= 80):
                        navi.append({
                            "mmsi": str(p.get("hex", "AIS-SHIP")).upper(),
                            "nome": str(p.get("flight", "") or p.get("r", "")).strip() or "NAVE IN TRANSITO",
                            "tipo": "NAVE COMMERCIAL / FERRY",
                            "lat": float(p["lat"]),
                            "lon": float(p["lon"]),
                            "velocita": round(float(p.get("gs", 0) or 0))
                        })
            return jsonify(navi)
    except Exception as e:
        print("Errore navi:", e)
    return jsonify([])

@app.route('/api/trasporti_campania', methods=['GET'])
def fetch_trasporti_campania_api():
    trasporti = [
        {"id": "ANM_L1_GARIBALDI", "nome": "ANM Metro L1 - Garibaldi", "rete": "ANM METRO", "modello": "AnsaldoBreda CAF", "tratta": "Piscinola ➔ Garibaldi", "lat": 40.8525, "lon": 14.2721, "stato": "REGOLARE"},
        {"id": "EAV_CIRCUM_SORRENTO", "nome": "EAV Circumvesuviana Express", "rete": "EAV FERROVIE", "modello": "ETR 211", "tratta": "Napoli P. Nolana ➔ Sorrento", "lat": 40.8522, "lon": 14.2718, "stato": "ATTIVO"},
        {"id": "ANM_BUS_ALIBUS", "nome": "ANM Alibus Aeroporto", "rete": "ANM BUS", "modello": "Iveco Bus Hybrid", "tratta": "Capodichino ➔ Molo Beverello", "lat": 40.8845, "lon": 14.2862, "stato": "IN TRANSITO"}
    ]
    return jsonify(trasporti)

@app.route('/api/voli_hybrid', methods=['GET'])
def fetch_voli_hybrid_api():
    lamin = request.args.get("lamin")
    lomin = request.args.get("lomin")
    lamax = request.args.get("lamax")
    lomax = request.args.get("lomax")
    latC = float(request.args.get("lat", 41.9))
    lonC = float(request.args.get("lon", 12.5))
    zoom = int(request.args.get("zoom", 6))

    voli = []
    try:
        url_os = f"https://opensky-network.org/api/states/all?lamin={lamin}&lomin={lomin}&lamax={lamax}&lomax={lomax}"
        res_os = token_manager.session.get(url_os, headers=token_manager.headers(), timeout=5)

        if res_os.status_code == 200:
            states = res_os.json().get("states", []) or []
            for p in states:
                if p[6] is not None and p[5] is not None:
                    icao24 = str(p[0] or "").upper()
                    cs = str(p[1] or "").strip() or icao24
                    cat_aereo = determina_categoria(icao24, cs)
                    voli.append({
                        "icao24": icao24, "callsign": cs, "lat": float(p[6]), "lon": float(p[5]),
                        "heading": round(float(p[10] or 0)), "alt_ft": round(float(p[7] or 0) * 3.28084),
                        "speed_kn": round(float(p[9] or 0) * 1.94384), "categoria": cat_aereo, "type_code": "OpenSky Asset"
                    })
            return jsonify({"status": "200 OK (OpenSky)", "count": len(voli), "data": voli})
    except Exception as e:
        print("OpenSky fallback:", e)

    radius = 500 if zoom <= 4 else (350 if zoom <= 6 else 200)
    url_fb = f"https://api.adsb.lol/v2/point/{latC}/{lonC}/{radius}"
    try:
        res_fb = requests.get(url_fb, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        if res_fb.status_code == 200:
            ac = res_fb.json().get("ac", []) or []
            for p in ac:
                if p.get("lat") is not None and p.get("lon") is not None:
                    cs = str(p.get("flight", "") or p.get("r", "")).strip() or str(p.get("hex", "N/A")).upper()
                    icao24 = str(p.get("hex", "N/A")).upper()
                    type_code = str(p.get("t", "") or "")
                    voli.append({
                        "icao24": icao24, "callsign": cs, "lat": float(p["lat"]), "lon": float(p["lon"]),
                        "heading": round(float(p.get("track", 0) or 0)), "alt_ft": round(float(p.get("alt_baro", 0) if isinstance(p.get("alt_baro"), (int, float)) else 0)),
                        "speed_kn": round(float(p.get("gs", 0) or 0)), "categoria": determina_categoria(icao24, cs, type_code), "type_code": type_code if type_code else "ND"
                    })
            return jsonify({"status": "Fallback Open", "count": len(voli), "data": voli})
    except Exception:
        pass

    return jsonify({"status": "Zero", "count": 0, "data": []})

@app.route('/api/gps', methods=['GET', 'POST'])
def receive_gps_traccar():
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
            registra_posizione_dispositivo(str(device_id).strip(), float(lat), float(lon), "Traccar Mobile")
            return "OK", 200
        except Exception as e:
            return f"Error: {e}", 500

    return "RECEIVER ACTIVE", 200

@app.route('/api/posizioni', methods=['GET'])
def get_posizioni():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT device_id, lat, lon, MAX(timestamp) FROM storico_gps GROUP BY device_id")
        rows = c.fetchall()
        conn.close()
        return jsonify({r[0]: {"lat": r[1], "lon": r[2], "last_update": r[3]} for r in rows}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==========================================
# INTERFACCIA GRAFICA COMPLETA MAPPA (HTML/LEAFLET)
# ==========================================
HTML_DASHBOARD_COMPLETA = """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <title>RADICALIUM - Tactical Control Center</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Segoe UI', sans-serif; }
        body, html { width: 100%; height: 100%; overflow: hidden; background: #06101e; color: #fff; }
        #app-container { display: flex; width: 100vw; height: 100vh; flex-direction: column; }
        #topbar { height: 48px; background: #0f172a; border-bottom: 1px solid #1e293b; display: flex; align-items: center; justify-content: space-between; padding: 0 20px; z-index: 1100; }
        .brand { font-size: 15px; font-weight: bold; color: #00f2fe; letter-spacing: 1.5px; }
        .btn-nav { background: #1e293b; color: white; border: 1px solid #334155; padding: 6px 12px; border-radius: 6px; font-size: 11px; font-weight: bold; cursor: pointer; }
        .btn-nav:hover { background: #0284c7; }
        #main-body { display: flex; flex: 1; height: calc(100vh - 48px); position: relative; }
        #map { flex: 1; height: 100%; background: #06101e; }
        #sidebar { width: 380px; background: #0f172a; border-left: 1px solid #1e293b; display: flex; flex-direction: column; padding: 14px; z-index: 1000; overflow-y: auto; gap: 12px; }
        .group-container { background: #1e293b; border-radius: 6px; overflow: hidden; border: 1px solid #334155; }
        .group-header { padding: 8px 10px; background: #0f172a; font-weight: bold; font-size: 12px; color: #00f2fe; }
        .event-card { background: #0f172a; border-radius: 6px; margin: 6px; border-left: 4px solid #00f2fe; padding: 8px; font-size: 11px; }
    </style>
</head>
<body>
    <div id="app-container">
        <div id="topbar">
            <div class="brand">RADICALIUM // COMMAND CENTER CLOUD</div>
            <div>
                <button class="btn-nav" style="background:#0284c7;" onclick="map.flyTo([40.8518, 14.2681], 12);">🏠 CASA</button>
                <button class="btn-nav" style="background:#8b5cf6;" onclick="syncAll();">🔄 RILEVA EVENTI</button>
            </div>
        </div>
        <div id="main-body">
            <div id="map"></div>
            <div id="sidebar">
                <div class="group-container"><div class="group-header">📍 UNITA GPS / TELTONIKA / PHONE</div><div id="deviceList"></div></div>
                <div class="group-container"><div class="group-header">✈️ VOLI & AEREI LIVE</div><div id="listaVoliContainer"></div></div>
                <div class="group-container"><div class="group-header">🍔 MCDONALD'S REGISTRATI</div><div id="listaMcDContainer"></div></div>
                <div class="group-container"><div class="group-header">🚢 TRACCIAMENTO NAVALE LIVE</div><div id="listaNaviContainer"></div></div>
                <div class="group-container"><div class="group-header">🚆 TRASPORTI TPL CAMPANIA</div><div id="listaTransitContainer"></div></div>
            </div>
        </div>
    </div>
    <script>
        var map = L.map('map').setView([41.9, 12.5], 6);
        const mapStreet = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19 }).addTo(map);
        const mapSatellitare = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', { maxZoom: 18 });
        const mapDark = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', { maxZoom: 16 });

        var layerPlanes = L.layerGroup().addTo(map);
        var layerShips = L.layerGroup().addTo(map);
        var layerMcD = L.layerGroup().addTo(map);
        var layerTransit = L.layerGroup().addTo(map);

        L.control.layers({ "Stradale": mapStreet, "Satellitare": mapSatellitare, "Scura": mapDark }, 
                         { "✈️ Voli": layerPlanes, "🚢 Navi": layerShips, "🍔 McD": layerMcD, "🚆 Trasporti": layerTransit }, 
                         { position: 'topleft' }).addTo(map);

        L.circle([40.8518, 14.2681], { color: 'red', fillColor: '#ef4444', fillOpacity: 0.1, radius: 5000 }).addTo(map);

        function syncAll() {
            // 1. GPS Devices
            fetch('/api/posizioni').then(r => r.json()).then(data => {
                const c = document.getElementById("deviceList"); c.innerHTML = "";
                for (var id in data) {
                    var dev = data[id];
                    L.marker([dev.lat, dev.lon]).addTo(map).bindPopup("📍 <b>" + id + "</b>");
                    c.innerHTML += `<div class="event-card">📍 <b>${id}</b><br>Lat: ${dev.lat}, Lon: ${dev.lon}</div>`;
                }
            });
            // 2. Voli
            const b = map.getBounds();
            fetch(`/api/voli_hybrid?lamin=${b.getSouth()}&lomin=${b.getWest()}&lamax=${b.getNorth()}&lomax=${b.getEast()}&lat=${map.getCenter().lat}&lon=${map.getCenter().lng}&zoom=${map.getZoom()}`)
                .then(r => r.json()).then(res => {
                    layerPlanes.clearLayers();
                    const c = document.getElementById("listaVoliContainer"); c.innerHTML = "";
                    (res.data || []).forEach(p => {
                        L.marker([p.lat, p.lon]).addTo(layerPlanes).bindPopup(`✈️ <b>${p.callsign}</b><br>Alt: ${p.alt_ft}ft`);
                        c.innerHTML += `<div class="event-card">✈️ <b>${p.callsign}</b> (${p.categoria})<br>Quota: ${p.alt_ft} ft</div>`;
                    });
                });
            // 3. Navi
            fetch(`/api/navi_live?lat=${map.getCenter().lat}&lon=${map.getCenter().lng}`).then(r => r.json()).then(data => {
                layerShips.clearLayers();
                const c = document.getElementById("listaNaviContainer"); c.innerHTML = "";
                data.forEach(s => {
                    L.marker([s.lat, s.lon]).addTo(layerShips).bindPopup(`🚢 <b>${s.nome}</b>`);
                    c.innerHTML += `<div class="event-card">🚢 <b>${s.nome}</b> (${s.velocita} kn)</div>`;
                });
            });
            // 4. McDonald's
            fetch(`/api/mcdonalds?lamin=${b.getSouth()}&lomin=${b.getWest()}&lamax=${b.getNorth()}&lomax=${b.getEast()}`).then(r => r.json()).then(res => {
                layerMcD.clearLayers();
                const c = document.getElementById("listaMcDContainer"); c.innerHTML = "";
                (res.data || []).forEach(m => {
                    L.marker([m.lat, m.lon]).addTo(layerMcD).bindPopup(`🍔 <b>McDonald's ${m.citta}</b>`);
                    c.innerHTML += `<div class="event-card">🍔 <b>McDonald's ${m.citta}</b><br>${m.via}</div>`;
                });
            });
            // 5. Trasporti
            fetch('/api/trasporti_campania').then(r => r.json()).then(data => {
                layerTransit.clearLayers();
                const c = document.getElementById("listaTransitContainer"); c.innerHTML = "";
                data.forEach(t => {
                    L.marker([t.lat, t.lon]).addTo(layerTransit).bindPopup(`🚆 <b>${t.nome}</b>`);
                    c.innerHTML += `<div class="event-card">🚆 <b>${t.nome}</b><br>${t.tratta}</div>`;
                });
            });
        }

        syncAll();
        setInterval(syncAll, 12000);
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_DASHBOARD_COMPLETA)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
