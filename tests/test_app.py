import io
import urllib.error
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import app as app_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test_ecoweb.db"
    monkeypatch.setattr(app_module, "DB_PATH", db_file)
    monkeypatch.setattr(app_module, "TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setattr(app_module, "TELEGRAM_CHAT_ID", "7572757457")
    monkeypatch.setattr(app_module, "SENSOR_API_KEY", "")
    monkeypatch.setattr(app_module, "UMBRAL_COOLDOWN_SEGUNDOS", 900)

    app_module.crear_base_datos()
    app_module.app.config["TESTING"] = True
    app_module.app.config["LOGIN_DISABLED"] = False

    with app_module.app.test_client() as test_client:
        yield test_client


def _crear_y_loguear_usuario(
    client,
    *,
    google_id="gid-1",
    nombre="Tester",
    email="tester@ejemplo.com",
    rol="trabajador",
    activo=1,
):
    ahora = datetime.now(timezone.utc).isoformat()
    with app_module.conectar_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO usuarios (google_id, nombre, email, rol, activo, fecha_creacion)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (google_id, nombre, email, rol, activo, ahora),
        )
        user_id = cursor.lastrowid

    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True

    return user_id


def test_rutas_protegidas_requieren_login():
    with app_module.app.test_client() as anon_client:
        res_api = anon_client.get("/api/ultima-limpieza")
        assert res_api.status_code == 401

        res_home = anon_client.get("/")
        assert res_home.status_code in (302, 401)


def test_permisos_de_roles(client):
    _crear_y_loguear_usuario(
        client,
        google_id="gid-user",
        nombre="Comun",
        email="comun@ejemplo.com",
        rol="usuario",
    )

    assert client.get("/api/limpiezas").status_code == 403
    assert client.post("/api/limpiezas", json={"accion": "vaciado"}).status_code == 403
    assert client.get("/api/usuarios").status_code == 403
    assert client.get("/api/ultima-limpieza").status_code == 200


@patch("app.urllib.request.urlopen")
def test_registrar_y_listar_limpiezas_envia_telegram(mock_urlopen, client):
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"ok":true}'
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    _crear_y_loguear_usuario(client, rol="trabajador", nombre="Ana")

    # Acción inválida
    bad = client.post("/api/limpiezas", json={"accion": "invalida"})
    assert bad.status_code == 400

    # Registro válido
    res = client.post(
        "/api/limpiezas",
        json={"accion": "vaciado", "observaciones": "Todo limpio"},
    )
    assert res.status_code == 201
    data = res.get_json()
    assert data["accion"] == "vaciado"
    assert data["responsable"] == "Ana"

    # Historial y última limpieza
    lista = client.get("/api/limpiezas")
    assert lista.status_code == 200
    assert len(lista.get_json()) == 1

    ultima = client.get("/api/ultima-limpieza")
    assert ultima.status_code == 200
    assert ultima.get_json()["fecha"] is not None

    with app_module.conectar_db() as conn:
        notif = conn.execute("SELECT estado, mensaje FROM notificaciones").fetchone()
        assert notif["estado"] == "enviado"
        assert "EcoDrop" in notif["mensaje"]


@patch("app.urllib.request.urlopen")
def test_alerta_llenado_cooldown_y_estado_cesto(mock_urlopen, client, monkeypatch):
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"ok":true}'
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    # Primera alerta pasa
    r1 = client.post(
        "/api/alerta-llenado",
        json={"distancia_cm": 5.8, "porcentaje": 88},
    )
    assert r1.status_code == 200
    assert r1.get_json()["enviado"] is True

    # Segunda alerta inmediata queda frenada por cooldown
    r2 = client.post(
        "/api/alerta-llenado",
        json={"distancia_cm": 5.5, "porcentaje": 90},
    )
    assert r2.status_code == 200
    assert r2.get_json()["enviado"] is False
    assert r2.get_json()["motivo"] == "cooldown_activo"

    # Con clave de sensor configurada
    monkeypatch.setattr(app_module, "SENSOR_API_KEY", "secreto-123")
    sin_clave = client.post("/api/alerta-llenado", json={"distancia_cm": 6.0})
    assert sin_clave.status_code == 401

    con_clave = client.post(
        "/api/alerta-llenado",
        json={"distancia_cm": 6.0},
        headers={"X-Sensor-Key": "secreto-123"},
    )
    assert con_clave.status_code == 200

    # Estado visual del cesto
    _crear_y_loguear_usuario(client, rol="usuario")
    estado = client.get("/api/estado-cesto")
    assert estado.status_code == 200
    assert estado.get_json()["estado_visual"] == "filling"


def test_gestion_usuarios_y_baja_logica(client):
    admin_id = _crear_y_loguear_usuario(
        client,
        google_id="gid-admin",
        nombre="Admin",
        email="admin@ejemplo.com",
        rol="administrador",
    )

    ahora = datetime.now(timezone.utc).isoformat()
    with app_module.conectar_db() as conn:
        target_id = conn.execute(
            """
            INSERT INTO usuarios (google_id, nombre, email, rol, activo, fecha_creacion)
            VALUES ('gid-target', 'Carlos', 'carlos@ejemplo.com', 'usuario', 1, ?)
            """,
            (ahora,),
        ).lastrowid

    # No puede modificarse a sí mismo
    self_patch = client.patch(f"/api/usuarios/{admin_id}", json={"rol": "trabajador"})
    assert self_patch.status_code == 403

    # Ascender a trabajador y luego dar baja lógica
    up = client.patch(f"/api/usuarios/{target_id}", json={"rol": "trabajador"})
    assert up.status_code == 200

    baja = client.patch(f"/api/usuarios/{target_id}", json={"activo": 0})
    assert baja.status_code == 200

    with app_module.conectar_db() as conn:
        fila = conn.execute(
            "SELECT rol, activo, fecha_baja FROM usuarios WHERE id = ?",
            (target_id,),
        ).fetchone()
        assert fila["rol"] == "trabajador"
        assert fila["activo"] == 0
        assert fila["fecha_baja"] is not None


def test_notificacion_telegram_sin_configurar(client, monkeypatch):
    monkeypatch.setattr(app_module, "TELEGRAM_BOT_TOKEN", "")
    res = app_module.enviar_notificacion_telegram("Mensaje prueba")
    assert res["estado"] == "sin_configurar"


def test_vistas_login_logout_e_inicio(client):
    res_login = client.get("/login")
    assert res_login.status_code == 200

    user_id = _crear_y_loguear_usuario(
        client,
        google_id="gid-vista",
        nombre="Sofi",
        email="sofi@ejemplo.com",
        rol="usuario",
    )
    assert user_id > 0

    res_home = client.get("/")
    assert res_home.status_code == 200

    # Un usuario común no entra a /admin
    res_admin_denegado = client.get("/admin")
    assert res_admin_denegado.status_code == 403

    res_logout = client.get("/logout", follow_redirects=False)
    assert res_logout.status_code == 302


def test_vista_admin_con_rol_administrador(client):
    _crear_y_loguear_usuario(
        client,
        google_id="gid-super",
        nombre="Admin",
        email="admin@ejemplo.com",
        rol="administrador",
    )

    res_admin = client.get("/admin")
    assert res_admin.status_code == 200


@patch("app.google")
def test_callback_google_crea_y_loguea_usuario(mock_google, client, monkeypatch):
    monkeypatch.setattr(app_module, "ADMIN_EMAIL", "admin@ejemplo.com")
    mock_google.authorize_access_token.return_value = {
        "userinfo": {
            "sub": "gid-nuevo-123",
            "email": "admin@ejemplo.com",
            "email_verified": True,
            "name": "Admin Google",
        }
    }

    res = client.get("/auth/callback", follow_redirects=False)
    assert res.status_code == 302

    with app_module.conectar_db() as conn:
        usuario = conn.execute(
            "SELECT rol, activo FROM usuarios WHERE google_id = 'gid-nuevo-123'"
        ).fetchone()
        assert usuario is not None
        assert usuario["rol"] == "administrador"
        assert usuario["activo"] == 1


@patch("app.google")
def test_callback_google_rechaza_usuario_inactivo(mock_google, client):
    _crear_y_loguear_usuario(
        client,
        google_id="gid-inactivo",
        nombre="Baja",
        email="baja@ejemplo.com",
        rol="usuario",
        activo=0,
    )
    with client.session_transaction() as sess:
        sess.clear()

    mock_google.authorize_access_token.return_value = {
        "userinfo": {
            "sub": "gid-inactivo",
            "email": "baja@ejemplo.com",
            "email_verified": True,
            "name": "Baja",
        }
    }

    res = client.get("/auth/callback", follow_redirects=False)
    assert res.status_code == 302


@patch("app.urllib.request.urlopen")
def test_telegram_maneja_error_http_y_excepcion(mock_urlopen, client):
    error_http = urllib.error.HTTPError(
        url="https://api.telegram.org/bot123/sendMessage",
        code=400,
        msg="Bad Request",
        hdrs=None,
        fp=io.BytesIO(b'{"ok":false,"description":"Bad Request: chat not found"}'),
    )
    mock_urlopen.side_effect = error_http

    res_http = app_module.enviar_notificacion_telegram("Prueba error HTTP")
    assert res_http["estado"] == "error"
    assert "HTTP 400" in res_http["detalle"]

    mock_urlopen.side_effect = RuntimeError("Sin red")
    res_exc = app_module.enviar_notificacion_telegram("Prueba excepción")
    assert res_exc["estado"] == "error"
    assert "Sin red" in res_exc["detalle"]


def test_modificar_usuario_inexistente_y_datos_invalidos(client):
    _crear_y_loguear_usuario(
        client,
        google_id="gid-adm2",
        nombre="Admin2",
        email="admin2@ejemplo.com",
        rol="administrador",
    )

    inexistente = client.patch("/api/usuarios/99999", json={"rol": "trabajador"})
    assert inexistente.status_code == 404

    ahora = "2026-11-19T22:00:00+00:00"
    with app_module.conectar_db() as conn:
        uid = conn.execute(
            """
            INSERT INTO usuarios (google_id, nombre, email, rol, activo, fecha_creacion)
            VALUES ('gid-u2', 'User2', 'u2@ejemplo.com', 'usuario', 1, ?)
            """,
            (ahora,),
        ).lastrowid

    rol_malo = client.patch(f"/api/usuarios/{uid}", json={"rol": "superroot"})
    assert rol_malo.status_code == 400

    estado_malo = client.patch(f"/api/usuarios/{uid}", json={"activo": "no-valido"})
    assert estado_malo.status_code == 400