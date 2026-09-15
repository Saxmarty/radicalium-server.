import os
import json
import sqlite3
import math
import struct
import socket
import threading
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# ==========================================
# CONFIGURAZIONE AMBIENTE & DATABASE
# ==========================================
DB_PATH = "/tmp/hardware_gps.db" if os.path.exists("/tmp") else "hardware_gps.db"
COORDINATE_CASA = {"lat": 40.8518, "lon": 14.2681}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracciamento_hardware (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            lat REAL,
            lon REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

DISPOSITIVI_DB = {}

def registra_posizione(dev_id, lat, lon, origine="Hardware"):
    """Registra la coordinata ricevuta nel database e in memoria"""
    dev_id_str = str(dev_id).strip()
    DISPOSITIVI_DB[dev_id_str] = {
        "id": dev_id_str,
        "lat": lat,
        "lon": lon,
        "origine": origine
    }
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO tracciamento_hardware (device_id, lat, lon) VALUES (?, ?, ?)", (dev_id_str, lat, lon))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Errore salvataggio DB: {e}")

# ==========================================
# PARSER TELTONIKA (TCP SOCKET - CODEC 8)
# ==========================================
def gestisci_connessione_teltonika(client_socket, addr):
    try:
        # 1. Handshake IMEI
        data = client_socket.recv(1024)
        if not data or len(data) < 2:
            client_socket.close()
            return
        
        imei_len = struct.unpack('>H', data[:2])[0]
        imei = data[2:2+imei_len].decode('utf-8')
        client_socket.send(b'\x01')  # Risposta accettazione IMEI

        # 2. Ricezione pacchetti AVL
        while True:
            packet = client_socket.recv(1024)
            if not packet or len(packet) < 12:
                break

            num_records = packet[9] if len(packet) > 9 else 1
            client_socket.send(struct.pack('>I', num_records))  # Risposta ACK

            # Parsing coordinate (Codec 8)
            try:
                if len(packet) >= 30:
                    lon_raw = struct.unpack('>i', packet[17:21])[0]
                    lat_raw = struct.unpack('>i', packet[21:25])[0]
                    
                    lat = lat_raw / 10000000.0
                    lon = lon_raw / 10000000.0

                    if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat != 0 and lon != 0):
                        registra_posizione(imei, lat, lon, "Teltonika TCP Socket")
            except Exception as e:
                print(f"Errore decodifica AVL {imei}: {e}")

    except Exception as e:
        print(f"Errore connessione socket {addr}: {e}")
    finally:
        client_socket.close()

def avvia_server_socket():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(('0.0.0.0', 5027))
        server.listen(10)
        while True:
            client, addr = server.accept()
            threading.Thread(target=gestisci_connessione_teltonika, args=(client, addr), daemon=True).start()
    except Exception as e:
        print(f"Server Socket porta 5027 non disponibile: {e}")

threading.Thread(target=avvia_server_socket, daemon=True).start()

# ==========================================
# ROTTE WEB & INGESTION HTTP
# ==========================================

@app.route('/api/gps', methods=['GET', 'POST'])
def receive_gps_http():
    """Ingestion flessibile via HTTP per tracker configurati in modalità web"""
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
            registra_posizione(device_id, float(lat), float(lon), "HTTP Web Ingestion")
            return "OK", 200
        except Exception as e:
            return f"Error: {e}", 500

    return "TELTONIKA RECEIVER ACTIVE", 200

@app.route('/api/posizioni', methods=['GET'])
def get_posizioni():
    """Restituisce le ultime posizioni note dei tracker hardware"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT device_id, lat, lon, MAX(timestamp) FROM tracciamento_hardware GROUP BY device_id")
        rows = c.fetchall()
        conn.close()
        return jsonify({r[0]: {"lat": r[1], "lon": r[2], "last_update": r[3]} for r in rows}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==========================================
# DASHBOARD WEB DEDICATA ALL'HARDWARE
# ==========================================
HTML_DASHBOARD = """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="utf-8" />
    <title>RADICALIUM - HARDWARE TRACKER COMMAND CENTER</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body { margin: 0; padding: 0; background-color: #080a0f; color: #00ffcc; font-family: 'Courier New', monospace; }
        #map { height: 100vh; width: 100vw; }
        .hud-panel {
            position: absolute; top: 15px; right: 15px; z-index: 1000;
            background: rgba(10, 15, 25, 0.95); padding: 15px 20px; border-radius: 8px;
            border: 1px solid #00ffcc; box-shadow: 0 0 15px rgba(0, 255, 204, 0.3); min-width: 250px;
        }
        .hud-title { font-size: 13px; font-weight: bold; color: #fff; margin-bottom: 8px; border-bottom: 1px solid #00ffcc; padding-bottom: 4px; }
        .tracker-item { font-size: 11px; margin-top: 6px; padding: 4px; background: rgba(0, 255, 204, 0.1); border-radius: 4px; }
    </style>
</head>
<body>
    <div class="hud-panel">
        <div class="hud-title">📡 DISPOSITIVI HARDWARE CONNECTED</div>
        <div id="tracker-list">In attesa di segnale GPS...</div>
    </div>
    <div id="map"></div>

    <script>
        var map = L.map('map').setView([40.8518, 14.2681], 10);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19, attribution: '© OpenStreetMap | Radicalium Hardware Engine'
        }).addTo(map);

        L.circle([40.8518, 14.2681], {
            color: 'red', fillColor: '#f03', fillOpacity: 0.1, radius: 5000
        }).addTo(map);

        var markers = {};

        function syncTracker() {
            fetch('/api/posizioni')
                .then(r => r.json())
                .then(data => {
                    var listContainer = document.getElementById("tracker-list");
                    listContainer.innerHTML = "";
                    var count = 0;

                    for (var id in data) {
                        count++;
                        var dev = data[id];
                        
                        if (markers[id]) {
                            markers[id].setLatLng([dev.lat, dev.lon]);
                        } else {
                            markers[id] = L.marker([dev.lat, dev.lon]).addTo(map)
                                .bindPopup('<b>📟 HARDWARE TRACKER</b><br>IMEI/ID: ' + id + '<br>Ultimo segnale: ' + dev.last_update);
                            map.flyTo([dev.lat, dev.lon], 14);
                        }

                        listContainer.innerHTML += `
                            <div class="tracker-item">
                                <b>IMEI/ID:</b> ${id}<br>
                                <b>COORDS:</b> ${dev.lat.toFixed(5)}, ${dev.lon.toFixed(5)}<br>
                                <b>UPDATE:</b> ${dev.last_update}
                            </div>
                        `;
                    }

                    if (count === 0) {
                        listContainer.innerHTML = "<span style='color:#888;'>Nessun tracker connesso.</span>";
                    }
                });
        }

        setInterval(syncTracker, 3000);
        syncTracker();
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_DASHBOARD)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
