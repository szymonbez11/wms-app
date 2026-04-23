import os
import re
import json
import shutil
import pdfplumber

from flask import Flask, render_template, request, redirect, session, send_from_directory
from datetime import datetime

from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4 
from reportlab.lib.styles import getSampleStyleSheet

# =========================
# CONFIG
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

WEJSCIE = os.path.join(BASE_DIR, "wejscie")
WYJSCIE = os.path.join(BASE_DIR, "wyjscie")
DB_FILE = os.path.join(BASE_DIR, "db.json")
WORKERS_FILE = os.path.join(BASE_DIR, "workers.json")

ADMIN_TOKEN = "MEGA_SECRET_123"

app = Flask(__name__)
app.secret_key = "secret"

orders = {}
workers = {}

# =========================
# INIT
# =========================
def ensure():
    os.makedirs(WEJSCIE, exist_ok=True)
    os.makedirs(WYJSCIE, exist_ok=True)

# =========================
# DB
# =========================
def save_db():
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=2)

def load_db():
    global orders
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            orders = json.load(f)

def save_workers():
    with open(WORKERS_FILE, "w", encoding="utf-8") as f:
        json.dump(workers, f, ensure_ascii=False, indent=2)

def load_workers():
    global workers
    if os.path.exists(WORKERS_FILE):
        with open(WORKERS_FILE, "r", encoding="utf-8") as f:
            workers = json.load(f)

# =========================
# NORMALIZE
# =========================
def normalize(t):
    if not t:
        return ""
    t = str(t).lower()
    repl = {"ą":"a","ć":"c","ę":"e","ł":"l","ń":"n","ó":"o","ś":"s","ż":"z","ź":"z"}
    for k,v in repl.items():
        t = t.replace(k,v)
    return t

# =========================
# KONTRAHENT
# =========================
def extract_contractor(pdf_path):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if not text:
                    continue

                for line in text.split("\n"):
                    if "kontrahent" in normalize(line):
                        parts = re.split(r"kontrahent[:\s]*", line, flags=re.IGNORECASE)
                        if len(parts) > 1:
                            name = parts[1].strip()
                            name = re.sub(r'[\\/*?:"<>|]', "", name)
                            return name[:40]

        return "kontrahent"
    except:
        return "blad"

# =========================
# PARSER PDF
# =========================
def extract_items(pdf_path):
    items = []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()

                for table in tables:
                    header = None
                    col_t = None
                    col_i = None

                    for i, row in enumerate(table):
                        clean = [normalize(x) for x in row if x]

                        if any("towar" in c for c in clean) and any("ilo" in c for c in clean):
                            header = i

                            for idx, c in enumerate(clean):
                                if "towar" in c:
                                    col_t = idx
                                if "ilo" in c:
                                    col_i = idx
                            break

                    if header is None:
                        continue

                    for row in table[header+1:]:
                        try:
                            towar = row[col_t]
                            ilosc = float(re.search(r"[\d.,]+", str(row[col_i])).group().replace(",", "."))
                            items.append({
                                "id": len(items)+1,
                                "towar": towar,
                                "ilosc": ilosc,
                                "zebrane": 0
                            })
                        except:
                            continue

        return items if items else [{"id":1,"towar":"BRAK DANYCH","ilosc":1,"zebrane":0}]
    except:
        return [{"id":1,"towar":"BŁĄD PDF","ilosc":1,"zebrane":0}]

# =========================
# PDF
# =========================
def generate_pdf(path, order, contractor, worker):
    doc = SimpleDocTemplate(path, pagesize=A4)
    styles = getSampleStyleSheet()

    elements = []

    elements.append(Paragraph(f"Kontrahent: {contractor}", styles["Normal"]))
    elements.append(Paragraph(f"Pracownik: {worker}", styles["Normal"]))
    elements.append(Paragraph(f"Data: {datetime.now()}", styles["Normal"]))

    data = [["Towar", "Zamówione", "Zebrane"]]

    for i in order["items"]:
        data.append([i["towar"], i["ilosc"], i["zebrane"]])

    table = Table(data)
    table.setStyle(TableStyle([("GRID",(0,0),(-1,-1),1,colors.black)]))

    elements.append(table)
    doc.build(elements)

# =========================
# SYNC
# =========================
def sync():
    files = [f for f in os.listdir(WEJSCIE) if f.lower().endswith(".pdf")]

    for f in files:
        path = os.path.join(WEJSCIE, f)

        if f not in orders:
            contractor = extract_contractor(path)

            orders[f] = {
                "file": f,
                "display_name": contractor,
                "contractor": contractor,
                "items": extract_items(path),
                "status": "free",
                "worker": None
            }

    for f in list(orders.keys()):
        if f not in files:
            del orders[f]

    save_db()
# =========================
# ROUTES
# =========================
@app.route("/")
def index():
    if not session.get("user"):
        return redirect("/login")

    sync()
    return render_template("index.html", orders=orders, user=session["user"])

@app.route("/logout")
def logout():
    user = session.get("user")

    if user:
        for o in orders.values():
            if o.get("worker") == user and o.get("status") == "progress":
                o["status"] = "free"
                o["worker"] = None

        if user in workers:
            workers[user]["status"] = "offline"
            workers[user]["order"] = "-"

        save_db()
        save_workers()

    session.clear()
    return redirect("/login")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        user = request.form.get("user")
        session["user"] = user

        workers[user] = {
            "status": "online",
            "completed": workers.get(user, {}).get("completed", 0),
            "order": "-"
        }

        save_workers()
        return redirect("/")

    return render_template("login.html")

# 🔥 UPLOAD
@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("file")

    if not file:
        return "brak pliku", 400

    path = os.path.join(WEJSCIE, file.filename)
    file.save(path)

    contractor = extract_contractor(path)

    orders[file.filename] = {
        "file": file.filename,
        "display_name": contractor,
        "contractor": contractor,
        "items": extract_items(path),
        "status": "free",
        "worker": None
    }

    save_db()
    sync()
    return "ok"

# 🔥 ORDER
@app.route("/order/<file>")
def order(file):
    if not session.get("user"):
        return redirect("/login")

    sync()

    o = orders.get(file)
    if not o:
        return "Brak zamówienia", 404

    user = session["user"]

    if o["status"] == "free":
        o["status"] = "progress"
        o["worker"] = user

        workers.setdefault(user, {"completed": 0})
        workers[user]["status"] = "working"
        workers[user]["order"] = o.get("display_name")

        save_workers()

    elif o["status"] == "progress" and o["worker"] != user:
        return "Zajęte", 403

    save_db()

    return render_template("order.html", order=o, items=o["items"], name=file, user=user)

# 🔥 UPDATE
@app.route("/update/<file>/<int:item_id>", methods=["POST"])
def update(file, item_id):
    qty = float(request.form.get("qty", 0))

    if file not in orders:
        return "brak", 404

    for i in orders[file]["items"]:
        if int(i["id"]) == int(item_id):
            i["zebrane"] = qty
            break

    save_db()
    return "ok"

# 🔥 FINISH
@app.route("/finish/<file>", methods=["POST"])
def finish(file):
    o = orders[file]
    src = os.path.join(WEJSCIE, file)

    contractor = o.get("contractor")
    worker = session.get("user")

    name = re.sub(r'[\\/*?:"<>|]', "", f"{contractor}_{worker}")

    pdf_path = os.path.join(WYJSCIE, name + ".pdf")

    generate_pdf(pdf_path, o, contractor, worker)
    shutil.copy(src, os.path.join(WYJSCIE, name + "_oryginal.pdf"))

    o["status"] = "done"
    o["worker"] = worker

    if worker:
        workers.setdefault(worker, {"completed": 0})
        workers[worker]["status"] = "online"
        workers[worker]["order"] = "-"
        workers[worker]["completed"] += 1

        save_workers()

    save_db()
    return redirect("/")

# 🔥 DELETE
@app.route("/api/delete/<path:file>", methods=["POST"])
def delete(file):
    if request.headers.get("X-ADMIN-TOKEN") != ADMIN_TOKEN:
        return "unauthorized", 403

    orders.pop(file, None)

    try:
        os.remove(os.path.join(WEJSCIE, file))
    except:
        pass

    save_db()
    return "ok"

# 🔥🔥🔥 DOWNLOAD (TO BYŁ BRAK)
@app.route("/download/<path:filename>")
def download(filename):
    return send_from_directory(WYJSCIE, filename, as_attachment=True)

# =========================
# API
# =========================
@app.route("/api/admin")
def api_admin():
    sync()

    result = {}

    for f, o in orders.items():
        items = o.get("items", [])
        total = sum(i["ilosc"] for i in items) or 1
        done = sum(i["zebrane"] for i in items)

        result[f] = {
            "name": o.get("display_name", f),
            "status": o.get("status"),
            "worker": o.get("worker"),
            "progress": int((done/total)*100),
            "items": items,

            "done_file": f"{o.get('contractor')}_{o.get('worker')}.pdf"
            if o.get("status") == "done" and o.get("worker")
            else None
        }

    return result

@app.route("/api/workers")
def api_workers():
    return workers

@app.route("/api/done")
def api_done():
    return [f for f,o in orders.items() if o.get("status")=="done"]

# 🔐 ADMIN LOGIN (DODANE NA DOLE)
ADMIN_USER = "iwona"
ADMIN_PASS = "1234"

@app.route("/admin/login", methods=["POST"])
def admin_login():
    data = request.json

    user = data.get("user")
    password = data.get("password")

    if user == ADMIN_USER and password == ADMIN_PASS:
        return {"token": ADMIN_TOKEN}

    return {"error": "bad login"}, 403

# =========================
# RUN
# =========================
if __name__ == "__main__":
    ensure()
    load_db()
    load_workers()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
