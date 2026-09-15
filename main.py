import os
import json
import sqlite3
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)
DB_PATH = "/tmp/radicalium_cloud.db" if os.path.exists("/tmp") else "radicalium_cloud.db"

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
    conn.commit()
    conn.close()

init_db()

@app.route('/', methods=['GET'])
@app.route('/api/gps', methods=['GET'])
def receive_gps():
    """Ricevitore Traccar Client per smartphone e tracker in tutto il mondo"""
    device_id = request.args.get('id')
    lat = request.args.get('lat')
    lon = request.args.get('lon')

    if device_id and lat and lon:
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO tracciamento_gps (device_id, lat, lon) VALUES (?, ?, ?)",
                      (str(device_id).strip(), float(lat), float(lon)))
            conn.commit()
            conn.close()
            return "OK", 200
        except Exception as e:
            return f"Error: {e}", 500
    return "RADICALIUM GOOGLE CLOUD ENGINE ACTIVE", 200

@app.route('/api/posizioni', methods=['GET'])
def get_posizioni():
    """API per la Dashboard PC: Restituisce l'ultima posizione di tutti i dispositivi"""
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
            data[r[0]] = {"lat": r[1], "lon": r[2], "last_update": r[3]}
        return jsonify(data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)