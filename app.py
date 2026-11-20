import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)

BASE_DIR = Path(__file__).resolve().parent

# Carga de variables de entorno: prioriza flask.env y usa .env como respaldo
env_file = BASE_DIR / "flask.env"
if env_file.exists():
    load_dotenv(dotenv_path=env_file, override=True)
else:
    load_dotenv(dotenv_path=BASE_DIR / ".env", override=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "clave-temporal-cambiar")

DB_PATH = BASE_DIR / "ecoweb.db"
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()

# Configuración Telegram Bot API y alertas del sensor
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SENSOR_API_KEY = os.getenv("SENSOR_API_KEY", "").strip()
UMBRAL_COOLDOWN_SEGUNDOS = 900  # 15 minutos entre alertas de llenado

# -------------------------
# AUTENTICACIÓN (OPENID / OAUTH2)
# -------------------------

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

oauth = OAuth(app)

google = oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url=(
        "https://accounts.google.com/.well-known/openid-configuration"
    ),
    client_kwargs={"scope": "openid email profile"},
)


# -------------------------
# BASE DE DATOS
# -------------------------

def conectar_db():
    conexion = sqlite3.connect(DB_PATH)
    conexion.row_factory = sqlite3.Row
    return conexion


def crear_base_datos():
    with conectar_db() as conexion:

        conexion.execute("""
            CREATE TABLE IF NOT EXISTS limpiezas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                responsable TEXT NOT NULL DEFAULT 'Sin especificar',
                fecha TEXT NOT NULL,
                accion TEXT NOT NULL DEFAULT 'vaciado',
                observaciones TEXT DEFAULT ''
            )
        """)

        conexion.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                google_id TEXT UNIQUE NOT NULL,
                nombre TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                rol TEXT NOT NULL DEFAULT 'usuario',
                activo INTEGER NOT NULL DEFAULT 1,
                fecha_creacion TEXT NOT NULL,
                fecha_baja TEXT
            )
        """)

        conexion.execute("""
            CREATE TABLE IF NOT EXISTS notificaciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canal TEXT NOT NULL DEFAULT 'telegram',
                destino TEXT NOT NULL,
                mensaje TEXT NOT NULL,
                estado TEXT NOT NULL,
                detalle TEXT DEFAULT '',
                fecha_envio TEXT NOT NULL
            )
        """)


# -------------------------
# NOTIFICACIONES (TELEGRAM BOT API)
# -------------------------

def enviar_notificacion_telegram(mensaje: str, chat_id: str = "") -> dict:
    destino_chat = (chat_id or TELEGRAM_CHAT_ID).strip()
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not destino_chat or not TELEGRAM_BOT_TOKEN:
        estado = "sin_configurar"
        detalle = "Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en flask.env."
    else:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": destino_chat,
            "text": mensaje,
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                cuerpo = resp.read().decode("utf-8", errors="replace")
                estado = "enviado"
                detalle = cuerpo[:300]
        except urllib.error.HTTPError as err:
            cuerpo_err = err.read().decode("utf-8", errors="replace")
            estado = "error"
            detalle = f"HTTP {err.code}: {cuerpo_err[:280]}"
        except Exception as exc:
            estado = "error"
            detalle = str(exc)[:300]

    with conectar_db() as conexion:
        conexion.execute("""
            INSERT INTO notificaciones
            (canal, destino, mensaje, estado, detalle, fecha_envio)
            VALUES ('telegram', ?, ?, ?, ?, ?)
        """, (destino_chat or "—", mensaje, estado, detalle, ahora))

    return {"estado": estado, "detalle": detalle}


def formatear_mensaje_limpieza(accion: str, responsable: str, fecha: str, observaciones: str = "") -> str:
    acciones_legibles = {
        "vaciado": "Vaciado del recolector 🗑️",
        "filtro": "Cambio de filtro 🧼",
        "ambos": "Vaciado y cambio de filtro ✨🗑️",
    }
    detalle_accion = acciones_legibles.get(accion, f"{accion} 🔧")
    lineas = [
        "🌊 *EcoDrop · Limpieza registrada* ✅",
        f"🛠️ Acción: {detalle_accion}",
        f"👤 Responsable: {responsable}",
        f"🕒 Fecha: {fecha}",
    ]
    if observaciones:
        lineas.append(f"📝 Obs.: {observaciones}")
    return "\n".join(lineas)


def formatear_alerta_llenado(porcentaje=None, distancia_cm=None) -> str:
    lineas = [
        "🚨🌊 *ALERTA ECODROP: EL CESTO ESTÁ POR LLENARSE* ⚠️🗑️",
        "¡REVISÁ EL RECOLECTOR CUANTO ANTES! 👀",
    ]
    if porcentaje is not None:
        lineas.append(f"📊 NIVEL ESTIMADO: {porcentaje}%")
    if distancia_cm is not None:
        lineas.append(f"📏 DISTANCIA AL SENSOR: {distancia_cm} CM")
    return "\n".join(lineas)


# -------------------------
# MODELO DE USUARIO
# -------------------------

class Usuario(UserMixin):

    def __init__(self, fila):
        self.id = str(fila["id"])
        self.google_id = fila["google_id"]
        self.nombre = fila["nombre"]
        self.email = fila["email"]
        self.rol = fila["rol"]
        self.activo = bool(fila["activo"])


@login_manager.user_loader
def cargar_usuario(user_id):
    with conectar_db() as conexion:
        fila = conexion.execute(
            "SELECT * FROM usuarios WHERE id = ? AND activo = 1",
            (user_id,),
        ).fetchone()

    return Usuario(fila) if fila else None


# -------------------------
# CONTROL DE PERMISOS
# -------------------------

def requiere_rol(*roles):
    def decorador(funcion):
        @wraps(funcion)
        @login_required
        def protegida(*args, **kwargs):
            if current_user.rol not in roles:
                return jsonify({
                    "error": "No tenés permisos para realizar esta acción."
                }), 403

            return funcion(*args, **kwargs)

        return protegida
    return decorador


@login_manager.unauthorized_handler
def no_autorizado():
    if request.path.startswith("/api/"):
        return jsonify({"error": "Necesitás iniciar sesión."}), 401

    return redirect(url_for("login"))


# -------------------------
# INICIO Y CIERRE DE SESIÓN
# -------------------------

@app.route("/login")
def login():
    return render_template("login.html")


@app.route("/login/google")
def login_google():
    return google.authorize_redirect(
        url_for("callback_google", _external=True)
    )


@app.route("/auth/callback")
def callback_google():

    token = google.authorize_access_token()

    informacion = token.get("userinfo")

    if not informacion:
        informacion = google.get("userinfo").json()

    email = informacion.get("email", "").lower().strip()
    google_id = informacion.get("sub")
    nombre = informacion.get("name", "Usuario")

    if not email or not google_id or not informacion.get("email_verified"):
        flash("No se pudo verificar tu cuenta de Google.")
        return redirect(url_for("login"))

    ahora = datetime.now(timezone.utc).isoformat()
    es_admin_configurado = bool(ADMIN_EMAIL and email == ADMIN_EMAIL)

    with conectar_db() as conexion:

        usuario = conexion.execute(
            "SELECT * FROM usuarios WHERE google_id = ?",
            (google_id,),
        ).fetchone()

        if usuario and not usuario["activo"]:
            flash("Tu cuenta está dada de baja. Contactá al administrador.")
            return redirect(url_for("login"))

        if not usuario:
            rol = "administrador" if es_admin_configurado else "usuario"

            conexion.execute("""
                INSERT INTO usuarios
                (google_id, nombre, email, rol, activo, fecha_creacion)
                VALUES (?, ?, ?, ?, 1, ?)
            """, (google_id, nombre, email, rol, ahora))

            usuario = conexion.execute(
                "SELECT * FROM usuarios WHERE google_id = ?",
                (google_id,),
            ).fetchone()

        else:
            nuevo_rol = (
                "administrador"
                if es_admin_configurado and usuario["rol"] != "administrador"
                else usuario["rol"]
            )

            conexion.execute("""
                UPDATE usuarios
                SET nombre = ?, email = ?, rol = ?
                WHERE google_id = ?
            """, (nombre, email, nuevo_rol, google_id))

            usuario = conexion.execute(
                "SELECT * FROM usuarios WHERE google_id = ?",
                (google_id,),
            ).fetchone()

    login_user(Usuario(usuario))
    return redirect(url_for("inicio"))


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# -------------------------
# PANEL PRINCIPAL Y ADMIN
# -------------------------

@app.route("/")
@login_required
def inicio():
    return render_template("index.html")


@app.route("/admin")
@requiere_rol("administrador")
def panel_admin():
    return render_template("admin.html")


# -------------------------
# LIMPIEZAS Y ALERTAS DE LLENADO
# -------------------------

@app.route("/api/ultima-limpieza", methods=["GET"])
@login_required
def obtener_ultima_limpieza():
    with conectar_db() as conexion:
        fila = conexion.execute("""
            SELECT fecha
            FROM limpiezas
            ORDER BY id DESC
            LIMIT 1
        """).fetchone()

    return jsonify({
        "fecha": fila["fecha"] if fila else None
    })


@app.route("/api/limpiezas", methods=["GET"])
@requiere_rol("trabajador", "administrador")
def obtener_limpiezas():

    with conectar_db() as conexion:
        filas = conexion.execute("""
            SELECT id, responsable, fecha, accion, observaciones
            FROM limpiezas
            ORDER BY id DESC
        """).fetchall()

    return jsonify([dict(fila) for fila in filas])


@app.route("/api/limpiezas", methods=["POST"])
@requiere_rol("trabajador", "administrador")
def registrar_limpieza():

    datos = request.get_json(silent=True) or {}

    responsable = current_user.nombre
    accion = datos.get("accion", "").strip()
    observaciones = datos.get("observaciones", "").strip()
    fecha = datos.get("fecha")

    if accion not in {"vaciado", "filtro", "ambos"}:
        return jsonify({"error": "Seleccioná una tarea válida."}), 400

    if len(observaciones) > 500:
        return jsonify({"error": "Las observaciones son demasiado largas."}), 400

    if not fecha:
        fecha = datetime.now(timezone.utc).isoformat(timespec="seconds")
    else:
        try:
            datetime.fromisoformat(fecha)
        except ValueError:
            return jsonify({"error": "La fecha no es válida."}), 400

    with conectar_db() as conexion:
        cursor = conexion.execute("""
            INSERT INTO limpiezas (responsable, fecha, accion, observaciones)
            VALUES (?, ?, ?, ?)
        """, (responsable, fecha, accion, observaciones))

        limpieza_id = cursor.lastrowid

    enviar_notificacion_telegram(
        formatear_mensaje_limpieza(accion, responsable, fecha, observaciones)
    )

    return jsonify({
        "id": limpieza_id,
        "responsable": responsable,
        "fecha": fecha,
        "accion": accion,
        "observaciones": observaciones,
    }), 201


@app.route("/api/alerta-llenado", methods=["POST"])
def alerta_llenado():
    if SENSOR_API_KEY:
        clave_recibida = request.headers.get("X-Sensor-Key", "").strip()
        if clave_recibida != SENSOR_API_KEY:
            return jsonify({"error": "Clave de sensor no válida."}), 401

    datos = request.get_json(silent=True) or {}
    porcentaje = datos.get("porcentaje")
    distancia_cm = datos.get("distancia_cm")

    ahora_dt = datetime.now(timezone.utc)
    ahora_iso = ahora_dt.isoformat(timespec="seconds")

    with conectar_db() as conexion:
        ultima = conexion.execute("""
            SELECT fecha_envio
            FROM notificaciones
            WHERE canal = 'telegram'
              AND mensaje LIKE '%ALERTA ECODROP: EL CESTO ESTÁ POR LLENARSE%'
              AND estado = 'enviado'
            ORDER BY id DESC
            LIMIT 1
        """).fetchone()

        if ultima:
            try:
                ultima_dt = datetime.fromisoformat(ultima["fecha_envio"])
                if (ahora_dt - ultima_dt).total_seconds() < UMBRAL_COOLDOWN_SEGUNDOS:
                    return jsonify({
                        "enviado": False,
                        "motivo": "cooldown_activo"
                    }), 200
            except ValueError:
                pass

    mensaje_alerta = formatear_alerta_llenado(
        porcentaje=porcentaje,
        distancia_cm=distancia_cm,
    )
    resultado = enviar_notificacion_telegram(mensaje_alerta)
    return jsonify({
        "enviado": resultado["estado"] == "enviado",
        "resultado": resultado,
        "fecha": ahora_iso,
    }), 200


@app.route("/api/estado-cesto", methods=["GET"])
@login_required
def obtener_estado_cesto():
    with conectar_db() as conexion:
        ultima_limpieza = conexion.execute("""
            SELECT fecha
            FROM limpiezas
            ORDER BY id DESC
            LIMIT 1
        """).fetchone()

        ultima_alerta = conexion.execute("""
            SELECT mensaje, fecha_envio
            FROM notificaciones
            WHERE canal = 'telegram'
              AND mensaje LIKE '%ALERTA ECODROP: EL CESTO ESTÁ POR LLENARSE%'
              AND estado = 'enviado'
            ORDER BY id DESC
            LIMIT 1
        """).fetchone()

    estado_visual = "available"
    ultima_alerta_fecha = None
    ultimo_mensaje = None

    if ultima_alerta:
        ultima_alerta_fecha = ultima_alerta["fecha_envio"]
        ultimo_mensaje = ultima_alerta["mensaje"]
        limpieza_posterior = False

        if ultima_limpieza and ultima_limpieza["fecha"]:
            try:
                dt_alerta = datetime.fromisoformat(ultima_alerta_fecha)
                dt_limpieza = datetime.fromisoformat(ultima_limpieza["fecha"])
                if dt_alerta.tzinfo is None:
                    dt_alerta = dt_alerta.replace(tzinfo=timezone.utc)
                if dt_limpieza.tzinfo is None:
                    dt_limpieza = dt_limpieza.replace(tzinfo=timezone.utc)
                limpieza_posterior = dt_limpieza >= dt_alerta
            except ValueError:
                limpieza_posterior = False

        if not limpieza_posterior:
            estado_visual = "filling"

    return jsonify({
        "estado_visual": estado_visual,
        "ultima_alerta": ultima_alerta_fecha,
        "ultimo_mensaje": ultimo_mensaje,
    })


# -------------------------
# GESTIÓN DE USUARIOS
# -------------------------

@app.route("/api/usuarios", methods=["GET"])
@requiere_rol("administrador")
def listar_usuarios():

    with conectar_db() as conexion:
        filas = conexion.execute("""
            SELECT id, nombre, email, rol, activo,
                   fecha_creacion, fecha_baja
            FROM usuarios
            ORDER BY id
        """).fetchall()

    return jsonify([dict(fila) for fila in filas])


@app.route("/api/usuarios/<int:usuario_id>", methods=["PATCH"])
@requiere_rol("administrador")
def modificar_usuario(usuario_id):

    datos = request.get_json(silent=True) or {}

    with conectar_db() as conexion:

        usuario = conexion.execute(
            "SELECT * FROM usuarios WHERE id = ?",
            (usuario_id,),
        ).fetchone()

        if not usuario:
            return jsonify({"error": "Usuario inexistente."}), 404

        if str(usuario["id"]) == str(current_user.id):
            return jsonify({
                "error": "No podés modificar tu propia cuenta desde el panel."
            }), 403

        if usuario["rol"] == "administrador":
            return jsonify({
                "error": "No se puede modificar al administrador principal desde esta función."
            }), 403

        rol = datos.get("rol", usuario["rol"])
        activo = datos.get("activo", usuario["activo"])

        if rol not in {"usuario", "trabajador"}:
            return jsonify({"error": "Rol no válido."}), 400

        if activo not in (0, 1, False, True):
            return jsonify({"error": "Estado no válido."}), 400

        activo = int(activo)

        fecha_baja = (
            datetime.now(timezone.utc).isoformat()
            if activo == 0
            else None
        )

        conexion.execute("""
            UPDATE usuarios
            SET rol = ?, activo = ?, fecha_baja = ?
            WHERE id = ?
        """, (rol, activo, fecha_baja, usuario_id))

    return jsonify({"mensaje": "Usuario actualizado correctamente."})


# -------------------------
# EJECUCIÓN
# -------------------------

crear_base_datos()

if __name__ == "__main__":
    app.run(debug=True)