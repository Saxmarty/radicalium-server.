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

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS storico_gps (
            id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT,
            lat REAL, lon REAL, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

DISPOSITIVI_DB = {}

def registra_posizione_dispositivo(dev_id, lat, lon, stato="Live Cloud"):
    """Salva la posizione inviata da telefoni o da localizzatori Teltonika"""
    DISPOSITIVI_DB[dev_id] = {
        "nome": f"Teltonika ({dev_id})" if dev_id.isdigit() else dev_id,
        "proprietario": "Hardware Tracker" if dev_id.isdigit() else "Tracker Mobile",
        "colore": "#22c55e" if dev_id.isdigit() else "#f97316",
        "tipo": "TELTONIKA" if dev_id.isdigit() else "GPS",
        "attivo": True, "lat": lat, "lon": lon, "stato": stato
    }
    
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
    """Gestisce l'handshake e la decodifica coordinate dei tracker Teltonika"""
    try:
        # 1. Ricezione IMEI dal dispositivo
        data = client_socket.recv(1024)
        if not data or len(data) < 2:
            client_socket.close()
            return
        
        imei_len = struct.unpack('>H', data[:2])[0]
        imei = data[2:2+imei_len].decode('utf-8')
        
        # Risposta di conferma IMEI (0x01 = Accettato)
        client_socket.send(b'\x01')

        # 2. Ricezione pacchetto dati GPS (Codec 8)
        while True:
            packet = client_socket.recv(1024)
            if not packet or len(packet) < 12:
                break

            # Rispondi con il numero di Record ricevuti per confermare la ricezione
            num_records = packet[9] if len(packet) > 9 else 1
            client_socket.send(struct.pack('>I', num_records))

            # Estrazione Coordinate da Codec 8 (Longitudine e Latitudine)
            try:
                # Cerca i valori int32 all'interno del buffer del pacchetto
                if len(packet) >= 30:
                    lon_raw = struct.unpack('>i', packet[17:21])[0]
                    lat_raw = struct.unpack('>i', packet[21:25])[0]
                    
                    lat = lat_raw / 10000000.0
                    lon = lon_raw / 10000000.0

                    if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat != 0 and lon != 0):
                        registra_posizione_dispositivo(imei, lat, lon, "Teltonika GPS Live")
            except Exception as e:
                print(f"Errore parsing pacchetto Teltonika {imei}:", e)

    except Exception as e:
        print(f"Errore socket Teltonika da {addr}:", e)
    finally:
        client_socket.close()

def avvia_server_socket_teltonika():
    """Avvia il server socket in ascolto sulla porta 5027 per i dispositivi Teltonika"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(('0.0.0.0', 5027))
        server.listen(10)
        while True:
            client, addr = server.accept()
            t = threading.Thread(target=gestisci_connessione_teltonika, args=(client, addr), daemon=True)
            t.start()
    except Exception as e:
        print("Errore avvio Socket Teltonika (porta 5027):", e)

# Avvio del listener Teltonika in background
threading.Thread(target=avvia_server_socket_teltonika, daemon=True).start()

# ==========================================
# ROTTE WEB FLASK & TRACCAR
# ==========================================

@app.route('/api/gps', methods=['GET', 'POST'])
def receive_gps_traccar():
    """Riceve le coordinate inviate da Traccar Client (Smartphone)"""
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

@app.route('/')
def index():
    return "RADICALIUM CLOUD ENGINE - TELTONIKA & TRACCAR MULTI-TRACKER ACTIVE"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
