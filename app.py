# app.py
from flask import Flask, g, jsonify, request, render_template, send_file
import sqlite3, os, csv, io
from datetime import datetime

DB_PATH = 'bookings.db'
BUILDING_NAME = "Leora Villa"

app = Flask(__name__)

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        db.row_factory = sqlite3.Row
    return db

def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv

def init_db():
    if os.path.exists(DB_PATH):
        return
    with app.app_context():
        db = get_db()
        db.executescript("""
        CREATE TABLE apartments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          code TEXT NOT NULL UNIQUE,
          name TEXT
        );
        CREATE TABLE bookings (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          apartment_id INTEGER NOT NULL,
          guest_name TEXT NOT NULL,
          guest_phone TEXT,
          guest_email TEXT,
          checkin DATE NOT NULL,
          checkout DATE NOT NULL,
          booking_type TEXT NOT NULL DEFAULT '2BHK',
          price REAL DEFAULT 0,
          notes TEXT,
          is_lock INTEGER DEFAULT 0,
          parent_id INTEGER,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (apartment_id) REFERENCES apartments(id),
          FOREIGN KEY (parent_id) REFERENCES bookings(id)
        );
        """)
        aparts = [
            ('Platinum','Platinum Room'),
            ('Gold','Gold Room'),
            ('Diamond','Diamond Room'),
            ('Silver','Silver Room'),
            ('Titanium','Titanium Room')
        ]
        db.executemany("INSERT INTO apartments(code,name) VALUES(?,?)", aparts)
        db.commit()

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def booking_conflict(apartment_id, checkin, checkout, exclude_id=None):
    if exclude_id:
        sql = "SELECT COUNT(*) as c FROM bookings WHERE id != ? AND apartment_id = ? AND NOT (checkout <= ? OR checkin >= ?)"
        params = (exclude_id, apartment_id, checkin, checkout)
    else:
        sql = "SELECT COUNT(*) as c FROM bookings WHERE apartment_id = ? AND NOT (checkout <= ? OR checkin >= ?)"
        params = (apartment_id, checkin, checkout)
    cur = get_db().execute(sql, params)
    r = cur.fetchone()
    cur.close()
    return r['c'] > 0

@app.route('/')
def index():
    return render_template('index.html', building_name=BUILDING_NAME)

@app.route('/api/apartments')
def api_apartments():
    rows = query("SELECT * FROM apartments ORDER BY id")
    return jsonify([dict(r) for r in rows])

@app.route('/api/bookings')
def api_bookings():
    apartment_id = request.args.get('apartment_id', type=int)
    if apartment_id:
        rows = query("SELECT * FROM bookings WHERE apartment_id = ? ORDER BY checkin", (apartment_id,))
    else:
        rows = query("SELECT * FROM bookings ORDER BY checkin")
    res = []
    for r in rows:
        rec = dict(r)
        res.append({
            'id': rec['id'],
            'apartment_id': rec['apartment_id'],
            'guest_name': rec['guest_name'],
            'guest_phone': rec['guest_phone'],
            'guest_email': rec['guest_email'],
            'checkin': rec['checkin'],
            'checkout': rec['checkout'],
            'booking_type': rec['booking_type'],
            'price': rec['price'],
            'notes': rec['notes'],
            'is_lock': rec['is_lock'],
            'parent_id': rec['parent_id']
        })
    return jsonify(res)

@app.route('/api/availability', methods=['GET'])
def api_availability():
    checkin = request.args.get('checkin')
    checkout = request.args.get('checkout')
    if not checkin or not checkout:
        return jsonify({'error': 'checkin and checkout required (YYYY-MM-DD)'}), 400
    if checkout <= checkin:
        return jsonify({'error': 'checkout must be after checkin'}), 400

    db = get_db()
    aps = query("SELECT * FROM apartments ORDER BY id")
    results = []
    for a in aps:
        cur = db.execute("""
            SELECT id, guest_name, guest_phone, guest_email, checkin, checkout, booking_type, price, notes, is_lock, parent_id
            FROM bookings
            WHERE apartment_id = ? AND NOT (checkout <= ? OR checkin >= ?)
            ORDER BY checkin
        """, (a['id'], checkin, checkout))
        conflicts = [dict(r) for r in cur.fetchall()]
        cur.close()
        results.append({
            'id': a['id'],
            'code': a['code'],
            'name': a['name'],
            'available': len(conflicts) == 0,
            'conflicts': conflicts
        })
    return jsonify(results)

@app.route('/api/bookings', methods=['POST'])
def api_create_booking():
    data = request.json
    required = ['apartment_id','guest_name','checkin','checkout']
    for k in required:
        if k not in data:
            return jsonify({'error': f'{k} required'}), 400
    checkin = data['checkin']
    checkout = data['checkout']
    if checkout <= checkin:
        return jsonify({'error': 'checkout must be after checkin'}), 400

    apt_id = int(data['apartment_id'])
    booking_type = data.get('booking_type', '2BHK').upper()
    price = float(data.get('price', 0))

    if booking_conflict(apt_id, checkin, checkout):
        return jsonify({'error': 'apartment not available for selected dates'}), 400

    db = get_db()
    cur = db.execute("""INSERT INTO bookings
      (apartment_id, guest_name, guest_phone, guest_email, checkin, checkout, booking_type, price, notes, is_lock, parent_id)
      VALUES(?,?,?,?,?,?,?,?,?,0,NULL)""",
      (apt_id, data['guest_name'], data.get('guest_phone'), data.get('guest_email'),
       checkin, checkout, booking_type, price, data.get('notes'))
    )
    db.commit()
    main_id = cur.lastrowid

    block_apartment_id = data.get('block_apartment_id')
    if booking_type == '1BHK' and block_apartment_id:
        block_id = int(block_apartment_id)
        if booking_conflict(block_id, checkin, checkout):
            db.execute("DELETE FROM bookings WHERE id = ?", (main_id,))
            db.commit()
            return jsonify({'error': 'cannot block requested apartment; it is not available'}), 400
        lock_note = f"LOCKED — key given to {data['guest_name']} (parent {main_id})"
        db.execute("""INSERT INTO bookings
            (apartment_id, guest_name, guest_phone, guest_email, checkin, checkout, booking_type, price, notes, is_lock, parent_id)
            VALUES(?,?,?,?,?,?,?,?,?,1,?)""",
            (block_id, 'LOCKED', None, None, checkin, checkout, 'LOCK', 0.0, lock_note, main_id)
        )
        db.commit()

    return jsonify({'id': main_id}), 201

@app.route('/api/bookings/<int:bid>', methods=['PUT'])
def api_update_booking(bid):
    data = request.json
    existing = query("SELECT * FROM bookings WHERE id = ?", (bid,), one=True)
    if not existing:
        return jsonify({'error':'not found'}), 404
    apartment_id = int(data.get('apartment_id', existing['apartment_id']))
    checkin = data.get('checkin', existing['checkin'])
    checkout = data.get('checkout', existing['checkout'])
    if checkout <= checkin:
        return jsonify({'error': 'checkout must be after checkin'}), 400
    if booking_conflict(apartment_id, checkin, checkout, exclude_id=bid):
        return jsonify({'error': 'apartment not available for selected dates'}), 400
    db = get_db()
    db.execute("""UPDATE bookings SET apartment_id=?, guest_name=?, guest_phone=?, guest_email=?, checkin=?, checkout=?, booking_type=?, price=?, notes=?
                  WHERE id=?""",
               (apartment_id, data.get('guest_name', existing['guest_name']),
                data.get('guest_phone', existing['guest_phone']),
                data.get('guest_email', existing['guest_email']),
                checkin, checkout, data.get('booking_type', existing['booking_type']), float(data.get('price', existing['price'] or 0)), data.get('notes', existing['notes']), bid))
    db.commit()
    return jsonify({'ok': True})

@app.route('/api/bookings/<int:bid>', methods=['DELETE'])
def api_delete_booking(bid):
    db = get_db()
    db.execute("DELETE FROM bookings WHERE parent_id = ?", (bid,))
    db.execute("DELETE FROM bookings WHERE id = ?", (bid,))
    db.commit()
    return jsonify({'ok': True})

@app.route('/api/export')
def api_export():
    rows = query("SELECT b.*, a.code as apartment_code FROM bookings b JOIN apartments a ON a.id = b.apartment_id ORDER BY checkin")
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['id','apartment_code','guest_name','guest_phone','guest_email','checkin','checkout','booking_type','price','notes','is_lock','parent_id','created_at'])
    for r in rows:
        cw.writerow([r['id'], r['apartment_code'], r['guest_name'], r['guest_phone'], r['guest_email'], r['checkin'], r['checkout'], r['booking_type'], r['price'], r['notes'], r['is_lock'], r['parent_id'], r['created_at']])
    output = io.BytesIO()
    output.write(si.getvalue().encode('utf-8'))
    output.seek(0)
    return send_file(output, mimetype='text/csv', as_attachment=True, download_name='bookings.csv')

if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5000)
