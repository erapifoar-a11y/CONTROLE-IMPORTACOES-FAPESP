from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import secrets
import shutil
import sqlite3
import sys
import threading
import tempfile
import webbrowser
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from pypdf import PdfReader, PdfWriter


APP_NAME = "Controle de Importações FAPESP"
APP_VERSION = "0.2.5"
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
WEB_DIR = ROOT / "web"
if os.environ.get("CONTROLE_IMPORTACOES_DATA"):
    DATA_DIR = Path(os.environ["CONTROLE_IMPORTACOES_DATA"])
elif getattr(sys, "frozen", False) and sys.platform == "win32":
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ControleImportacoesFAPESP"
else:
    DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DATA_DIR / "controle_importacoes.db"
DOCS_DIR = DATA_DIR / "documentos"
BACKUP_DIR = DATA_DIR / "backups"


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def normalize_decimal(value: object) -> str:
    raw = str(value or "0").strip().replace(" ", "")
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return format(Decimal(raw), "f")
    except InvalidOperation as exc:
        raise ValueError("Valor inválido") from exc


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS researchers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                email TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processes (
                id INTEGER PRIMARY KEY,
                number TEXT NOT NULL COLLATE NOCASE UNIQUE,
                researcher_id INTEGER NOT NULL REFERENCES researchers(id),
                notes TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY,
                process_id INTEGER NOT NULL REFERENCES processes(id),
                exporter TEXT NOT NULL,
                manufacturer TEXT NOT NULL DEFAULT '',
                representative TEXT NOT NULL DEFAULT '',
                proforma_number TEXT NOT NULL DEFAULT '',
                currency TEXT NOT NULL,
                value_amount TEXT NOT NULL,
                opened_on TEXT NOT NULL,
                nature TEXT NOT NULL DEFAULT 'goods',
                price_mode TEXT NOT NULL DEFAULT 'three_quotes',
                budget_line TEXT NOT NULL DEFAULT '',
                shipment TEXT NOT NULL DEFAULT '',
                justification TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'preparation',
                next_action TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS proposals (
                id INTEGER PRIMARY KEY,
                import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('winner','coverage','principal_extra')),
                supplier TEXT NOT NULL,
                number TEXT NOT NULL DEFAULT '',
                currency TEXT NOT NULL,
                value_amount TEXT NOT NULL DEFAULT '0',
                document_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY,
                import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
                category TEXT NOT NULL,
                label TEXT NOT NULL,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'current',
                signed INTEGER NOT NULL DEFAULT 0,
                sent INTEGER NOT NULL DEFAULT 0,
                merged_from TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_import_process ON imports(process_id);
            CREATE INDEX IF NOT EXISTS idx_doc_import ON documents(import_id);
            CREATE INDEX IF NOT EXISTS idx_event_import ON events(import_id);
            """
        )
        columns = {r[1] for r in con.execute("PRAGMA table_info(imports)")}
        if "needs_review" not in columns:
            con.execute("ALTER TABLE imports ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0")
        if "source_folder" not in columns:
            con.execute("ALTER TABLE imports ADD COLUMN source_folder TEXT NOT NULL DEFAULT ''")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def password_hash(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return salt, digest.hex()


TOKENS: set[str] = set()


class Handler(SimpleHTTPRequestHandler):
    server_version = "ControleImportacoes/0.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        return

    def json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def send_json(self, data: object, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def token(self) -> str:
        auth = self.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else ""

    def authorized(self) -> bool:
        with connect() as con:
            setup = con.execute("SELECT value FROM settings WHERE key='password_hash'").fetchone()
        return setup is None or self.token() in TOKENS

    def require_auth(self) -> bool:
        if self.authorized():
            return True
        self.send_json({"error": "Não autorizado"}, HTTPStatus.UNAUTHORIZED)
        return False

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/session":
            with connect() as con:
                configured = con.execute("SELECT 1 FROM settings WHERE key='password_hash'").fetchone() is not None
            return self.send_json({"configured": configured, "authenticated": self.authorized(), "version": APP_VERSION})
        if path.startswith("/api/") and not self.require_auth():
            return
        if path == "/api/dashboard":
            return self.send_json(self.dashboard())
        if path == "/api/researchers":
            with connect() as con:
                rows = con.execute("SELECT * FROM researchers WHERE active=1 ORDER BY name").fetchall()
            return self.send_json([dict(r) for r in rows])
        if path == "/api/suggestions":
            return self.send_json(self.suggestions())
        if path.startswith("/api/imports/"):
            try:
                import_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                return self.send_json({"error": "Identificador inválido"}, 400)
            return self.send_json(self.import_detail(import_id))
        if path.startswith("/api/documents/") and path.endswith("/download"):
            parts = path.strip("/").split("/")
            return self.download_document(int(parts[2]))
        if path == "/api/backup":
            return self.send_json({"backups": sorted([p.name for p in BACKUP_DIR.glob("*.zip")], reverse=True)})
        return super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/setup":
            return self.setup_password()
        if path == "/api/login":
            return self.login()
        if path.startswith("/api/") and not self.require_auth():
            return
        try:
            if path == "/api/imports":
                return self.create_import()
            if path.endswith("/documents") and path.startswith("/api/imports/"):
                import_id = int(path.split("/")[3])
                return self.add_document(import_id)
            if path.endswith("/merge-coverage") and path.startswith("/api/imports/"):
                import_id = int(path.split("/")[3])
                return self.merge_coverage(import_id)
            if path.endswith("/status") and path.startswith("/api/imports/"):
                import_id = int(path.split("/")[3])
                return self.update_status(import_id)
            if path == "/api/historical-import":
                return self.import_history()
            if path == "/api/backup":
                return self.create_backup()
            if path == "/api/restore":
                return self.restore_backup()
        except (ValueError, KeyError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            return self.send_json({"error": f"Falha interna: {exc}"}, 500)
        return self.send_json({"error": "Rota não encontrada"}, 404)

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/") and not self.require_auth():
            return
        try:
            if path.startswith("/api/imports/") and path.count("/") == 3:
                return self.update_import(int(path.rsplit("/", 1)[1]))
            if path.startswith("/api/documents/") and path.count("/") == 3:
                return self.update_document(int(path.rsplit("/", 1)[1]))
        except (ValueError, KeyError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            return self.send_json({"error": f"Falha interna: {exc}"}, 500)
        return self.send_json({"error": "Rota não encontrada"}, 404)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/") and not self.require_auth():
            return
        try:
            if path.startswith("/api/imports/") and path.count("/") == 3:
                return self.delete_import(int(path.rsplit("/", 1)[1]))
            if path.startswith("/api/documents/") and path.count("/") == 3:
                return self.delete_document(int(path.rsplit("/", 1)[1]))
        except (ValueError, KeyError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            return self.send_json({"error": f"Falha interna: {exc}"}, 500)
        return self.send_json({"error": "Rota não encontrada"}, 404)

    def setup_password(self) -> None:
        body = self.json_body()
        password = str(body.get("password", ""))
        if len(password) < 6:
            return self.send_json({"error": "A senha deve possuir pelo menos 6 caracteres"}, 400)
        with connect() as con:
            if con.execute("SELECT 1 FROM settings WHERE key='password_hash'").fetchone():
                return self.send_json({"error": "Senha já configurada"}, 409)
            salt, digest = password_hash(password)
            con.executemany("INSERT INTO settings(key,value) VALUES(?,?)", [("password_salt", salt), ("password_hash", digest)])
        token = secrets.token_urlsafe(32)
        TOKENS.add(token)
        self.send_json({"token": token})

    def login(self) -> None:
        password = str(self.json_body().get("password", ""))
        with connect() as con:
            salt_row = con.execute("SELECT value FROM settings WHERE key='password_salt'").fetchone()
            hash_row = con.execute("SELECT value FROM settings WHERE key='password_hash'").fetchone()
        if not hash_row:
            return self.send_json({"error": "Senha ainda não configurada"}, 400)
        _, candidate = password_hash(password, salt_row[0])
        if not secrets.compare_digest(candidate, hash_row[0]):
            return self.send_json({"error": "Senha incorreta"}, 401)
        token = secrets.token_urlsafe(32)
        TOKENS.add(token)
        self.send_json({"token": token})

    def dashboard(self) -> dict:
        authorized = {"authorized", "execution", "completed"}
        with connect() as con:
            processes = con.execute(
                """SELECT p.id,p.number,r.name researcher_name
                   FROM processes p JOIN researchers r ON r.id=p.researcher_id
                   WHERE p.active=1 ORDER BY p.number DESC"""
            ).fetchall()
            result = []
            for proc in processes:
                imports = [dict(r) for r in con.execute("SELECT * FROM imports WHERE process_id=? ORDER BY opened_on DESC,id DESC", (proc["id"],)).fetchall()]
                totals: dict[str, Decimal] = {}
                pending: dict[str, Decimal] = {}
                for item in imports:
                    target = totals if item["status"] in authorized else pending
                    target[item["currency"]] = target.get(item["currency"], Decimal("0")) + Decimal(item["value_amount"])
                result.append({**dict(proc), "imports": imports, "totals": {k: str(v) for k, v in totals.items()}, "pending_totals": {k: str(v) for k, v in pending.items()}})
        return {"processes": result, "version": APP_VERSION}

    def suggestions(self) -> dict:
        with connect() as con:
            researchers = [r[0] for r in con.execute("SELECT name FROM researchers WHERE active=1 ORDER BY name")]
            processes = [r[0] for r in con.execute("SELECT number FROM processes WHERE active=1 ORDER BY number")]
            def distinct(field: str) -> list[str]:
                return [r[0] for r in con.execute(f"SELECT DISTINCT {field} FROM imports WHERE TRIM({field})<>'' ORDER BY {field} COLLATE NOCASE")]
            return {"researchers": researchers, "processes": processes, "exporters": distinct("exporter"), "manufacturers": distinct("manufacturer"), "representatives": distinct("representative")}

    def import_detail(self, import_id: int) -> dict:
        with connect() as con:
            row = con.execute(
                """SELECT i.*,p.number process_number,r.name researcher_name
                   FROM imports i JOIN processes p ON p.id=i.process_id
                   JOIN researchers r ON r.id=p.researcher_id WHERE i.id=?""", (import_id,)
            ).fetchone()
            if not row:
                return {"error": "Importação não encontrada"}
            docs = [dict(r) for r in con.execute("SELECT * FROM documents WHERE import_id=? ORDER BY category,version DESC", (import_id,)).fetchall()]
            proposals = [dict(r) for r in con.execute("SELECT * FROM proposals WHERE import_id=? ORDER BY id", (import_id,)).fetchall()]
            events = [dict(r) for r in con.execute("SELECT * FROM events WHERE import_id=? ORDER BY id DESC", (import_id,)).fetchall()]
        return {"import": dict(row), "documents": docs, "proposals": proposals, "events": events}

    def create_import(self) -> None:
        b = self.json_body()
        researcher = str(b["researcher_name"]).strip()
        process_number = str(b["process_number"]).strip()
        exporter = str(b["exporter"]).strip()
        currency = str(b["currency"]).strip().upper()
        if not researcher or not process_number or not exporter or not currency:
            raise ValueError("Pesquisador, processo, exportador e moeda são obrigatórios")
        value = normalize_decimal(b.get("value_amount"))
        stamp = now_iso()
        with connect() as con:
            con.execute("INSERT OR IGNORE INTO researchers(name,created_at) VALUES(?,?)", (researcher, stamp))
            researcher_id = con.execute("SELECT id FROM researchers WHERE name=? COLLATE NOCASE", (researcher,)).fetchone()[0]
            existing = con.execute("SELECT researcher_id FROM processes WHERE number=? COLLATE NOCASE", (process_number,)).fetchone()
            if existing and existing[0] != researcher_id:
                raise ValueError("Este processo já está vinculado a outro pesquisador")
            con.execute("INSERT OR IGNORE INTO processes(number,researcher_id,created_at) VALUES(?,?,?)", (process_number, researcher_id, stamp))
            process_id = con.execute("SELECT id FROM processes WHERE number=? COLLATE NOCASE", (process_number,)).fetchone()[0]
            cur = con.execute(
                """INSERT INTO imports(process_id,exporter,manufacturer,representative,proforma_number,currency,value_amount,opened_on,nature,price_mode,budget_line,shipment,justification,status,next_action,notes,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'preparation',?,?,?,?)""",
                (process_id, exporter, b.get("manufacturer", ""), b.get("representative", ""), b.get("proforma_number", ""), currency, value, b.get("opened_on") or date.today().isoformat(), b.get("nature", "goods"), b.get("price_mode", "three_quotes"), b.get("budget_line", ""), b.get("shipment", ""), b.get("justification", ""), b.get("next_action", ""), b.get("notes", ""), stamp, stamp),
            )
            import_id = cur.lastrowid
            con.execute("INSERT INTO events(import_id,event_type,description,created_at) VALUES(?,?,?,?)", (import_id, "created", "Importação criada", stamp))
        self.send_json({"id": import_id}, 201)

    def update_import(self, import_id: int) -> None:
        b = self.json_body()
        researcher = str(b["researcher_name"]).strip()
        process_number = str(b["process_number"]).strip()
        exporter = str(b["exporter"]).strip()
        currency = str(b["currency"]).strip().upper()
        if not researcher or not process_number or not exporter or not currency:
            raise ValueError("Pesquisador, processo, exportador e moeda são obrigatórios")
        value = normalize_decimal(b.get("value_amount"))
        stamp = now_iso()
        with connect() as con:
            if not con.execute("SELECT 1 FROM imports WHERE id=?", (import_id,)).fetchone():
                raise ValueError("Importação não encontrada")
            con.execute("INSERT OR IGNORE INTO researchers(name,created_at) VALUES(?,?)", (researcher, stamp))
            researcher_id = con.execute("SELECT id FROM researchers WHERE name=? COLLATE NOCASE", (researcher,)).fetchone()[0]
            existing = con.execute("SELECT researcher_id FROM processes WHERE number=? COLLATE NOCASE", (process_number,)).fetchone()
            if existing and existing[0] != researcher_id:
                raise ValueError("Este processo já está vinculado a outro pesquisador")
            con.execute("INSERT OR IGNORE INTO processes(number,researcher_id,created_at) VALUES(?,?,?)", (process_number, researcher_id, stamp))
            process_id = con.execute("SELECT id FROM processes WHERE number=? COLLATE NOCASE", (process_number,)).fetchone()[0]
            con.execute("""UPDATE imports SET process_id=?,exporter=?,manufacturer=?,representative=?,proforma_number=?,currency=?,value_amount=?,opened_on=?,nature=?,price_mode=?,next_action=?,notes=?,needs_review=0,updated_at=? WHERE id=?""",
                        (process_id, exporter, b.get("manufacturer", ""), b.get("representative", ""), b.get("proforma_number", ""), currency, value, b.get("opened_on") or date.today().isoformat(), b.get("nature", "goods"), b.get("price_mode", "three_quotes"), b.get("next_action", ""), b.get("notes", ""), stamp, import_id))
            con.execute("INSERT INTO events(import_id,event_type,description,created_at) VALUES(?,?,?,?)", (import_id, "edited", "Dados da importação editados", stamp))
        self.send_json({"ok": True})

    def delete_import(self, import_id: int) -> None:
        folder = DOCS_DIR / f"importacao_{import_id:06d}"
        with connect() as con:
            if not con.execute("SELECT 1 FROM imports WHERE id=?", (import_id,)).fetchone():
                raise ValueError("Importação não encontrada")
            con.execute("DELETE FROM imports WHERE id=?", (import_id,))
        if folder.exists():
            shutil.rmtree(folder)
        self.send_json({"ok": True})

    def add_document(self, import_id: int) -> None:
        b = self.json_body()
        original = Path(str(b["name"])).name
        raw = base64.b64decode(str(b["content_base64"]).split(",")[-1])
        category = str(b.get("category", "other"))
        folder = DOCS_DIR / f"importacao_{import_id:06d}"
        folder.mkdir(parents=True, exist_ok=True)
        with connect() as con:
            last = con.execute("SELECT COALESCE(MAX(version),0) FROM documents WHERE import_id=? AND category=?", (import_id, category)).fetchone()[0]
            version = last + 1
            stored = f"{category}_v{version}_{secrets.token_hex(4)}{Path(original).suffix.lower()}"
            (folder / stored).write_bytes(raw)
            cur = con.execute(
                """INSERT INTO documents(import_id,category,label,original_name,stored_name,version,status,signed,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (import_id, category, b.get("label") or original, original, stored, version, "current", 1 if b.get("signed") else 0, now_iso()),
            )
            con.execute("INSERT INTO events(import_id,event_type,description,created_at) VALUES(?,?,?,?)", (import_id, "document_added", f"Documento anexado: {original}", now_iso()))
        self.send_json({"id": cur.lastrowid, "version": version}, 201)

    def update_document(self, document_id: int) -> None:
        b = self.json_body()
        category = str(b.get("category", "other"))
        label = str(b.get("label", "")).strip()
        with connect() as con:
            row = con.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            if not row:
                raise ValueError("Documento não encontrado")
            con.execute("UPDATE documents SET category=?,label=?,signed=? WHERE id=?", (category, label or row["original_name"], 1 if b.get("signed") else 0, document_id))
            con.execute("INSERT INTO events(import_id,event_type,description,created_at) VALUES(?,?,?,?)", (row["import_id"], "document_edited", f"Documento editado: {row['original_name']}", now_iso()))
        self.send_json({"ok": True})

    def delete_document(self, document_id: int) -> None:
        with connect() as con:
            row = con.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            if not row:
                raise ValueError("Documento não encontrado")
            con.execute("DELETE FROM documents WHERE id=?", (document_id,))
            con.execute("INSERT INTO events(import_id,event_type,description,created_at) VALUES(?,?,?,?)", (row["import_id"], "document_deleted", f"Documento excluído: {row['original_name']}", now_iso()))
        path = DOCS_DIR / f"importacao_{row['import_id']:06d}" / row["stored_name"]
        if path.exists():
            path.unlink()
        self.send_json({"ok": True})

    def import_history(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("Selecione o arquivo ZIP do acervo histórico")
        if length > 1024 * 1024 * 1024:
            raise ValueError("O arquivo selecionado ultrapassa o limite de 1 GB")
        fd, temp_name = tempfile.mkstemp(prefix="historico_importacoes_", suffix=".zip")
        os.close(fd)
        archive = Path(temp_name)
        try:
            remaining = length
            with archive.open("wb") as target:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("O envio do arquivo foi interrompido")
                    target.write(chunk)
                    remaining -= len(chunk)
            if not zipfile.is_zipfile(archive):
                raise ValueError("O arquivo selecionado não é um ZIP válido")
            self._import_history_archive(archive)
        finally:
            archive.unlink(missing_ok=True)

    def _import_history_archive(self, archive: Path) -> None:
        initials_names = {
            "DBZ": "Daniel Breseguello Zocal",
            "CRJ": "Carlos Rossa Junior",
            "ACP": "Ana Cláudia Pavarina",
        }
        def category_for(name: str) -> str:
            n = name.casefold()
            if "form" in n: return "signed_form"
            if "exclus" in n: return "exclusivity"
            if "razo" in n or "preço" in n or "preco" in n: return "price_reasonableness"
            if "merged" in n or "reunid" in n: return "coverage_merged"
            if "invoice" in n or "proforma" in n: return "winner_proposal"
            if "esclare" in n or "clarif" in n: return "clarification"
            return "other"
        imported = documents = 0
        with zipfile.ZipFile(archive) as zf, connect() as con:
            folders = sorted({str(Path(n).parent) for n in zf.namelist() if n.lower().endswith((".pdf", ".xlsx", ".docx")) and str(Path(n).parent) != "IMPORTAÇÕES"})
            for folder in folders:
                if con.execute("SELECT 1 FROM imports WHERE source_folder=?", (folder,)).fetchone():
                    continue
                base = Path(folder).name
                initials = base.split()[0].strip(" -")
                exporter = base[len(initials):].strip(" -") or base
                exporter = exporter.rsplit(" ", 1)[0] if exporter.rsplit(" ", 1)[-1].isdigit() else exporter
                researcher = initials_names.get(initials, f"{initials} — nome a conferir")
                process_number = f"HISTÓRICO-{initials}"
                stamp = now_iso()
                con.execute("INSERT OR IGNORE INTO researchers(name,created_at) VALUES(?,?)", (researcher, stamp))
                researcher_id = con.execute("SELECT id FROM researchers WHERE name=? COLLATE NOCASE", (researcher,)).fetchone()[0]
                con.execute("INSERT OR IGNORE INTO processes(number,researcher_id,notes,created_at) VALUES(?,?,?,?)", (process_number, researcher_id, "Processo provisório do acervo histórico; conferir e corrigir.", stamp))
                process_id = con.execute("SELECT id FROM processes WHERE number=? COLLATE NOCASE", (process_number,)).fetchone()[0]
                cur = con.execute("""INSERT INTO imports(process_id,exporter,currency,value_amount,opened_on,status,next_action,notes,created_at,updated_at,needs_review,source_folder)
                    VALUES(?,?,?,?,?,'completed','Conferir cadastro histórico','Importado automaticamente do acervo enviado; dados cadastrais e classificação dos documentos devem ser conferidos.',?,?,1,?)""",
                    (process_id, exporter, "USD", "0", date.today().isoformat(), stamp, stamp, folder))
                import_id = cur.lastrowid
                target = DOCS_DIR / f"importacao_{import_id:06d}"
                target.mkdir(parents=True, exist_ok=True)
                versions: dict[str, int] = {}
                for member in [n for n in zf.namelist() if str(Path(n).parent) == folder and not n.endswith("/")]:
                    original = Path(member).name
                    category = category_for(original)
                    versions[category] = versions.get(category, 0) + 1
                    version = versions[category]
                    stored = f"{category}_v{version}_{secrets.token_hex(4)}{Path(original).suffix.lower()}"
                    (target / stored).write_bytes(zf.read(member))
                    con.execute("""INSERT INTO documents(import_id,category,label,original_name,stored_name,version,status,signed,created_at)
                        VALUES(?,?,?,?,?,?, 'current',0,?)""", (import_id, category, original, original, stored, version, stamp))
                    documents += 1
                con.execute("INSERT INTO events(import_id,event_type,description,created_at) VALUES(?,?,?,?)", (import_id, "historical_import", "Registro importado do acervo histórico — pendente de conferência", stamp))
                imported += 1
        self.send_json({"imports": imported, "documents": documents}, 201)

    def merge_coverage(self, import_id: int) -> None:
        ids = [int(x) for x in self.json_body().get("document_ids", [])]
        if len(ids) < 2:
            raise ValueError("Selecione ao menos duas propostas de cobertura")
        with connect() as con:
            placeholders = ",".join("?" for _ in ids)
            rows = con.execute(f"SELECT * FROM documents WHERE import_id=? AND id IN ({placeholders})", (import_id, *ids)).fetchall()
            if len(rows) != len(ids) or any(r["category"] != "coverage_proposal" for r in rows):
                raise ValueError("Somente propostas de cobertura podem ser reunidas")
            writer = PdfWriter()
            folder = DOCS_DIR / f"importacao_{import_id:06d}"
            by_id = {r["id"]: r for r in rows}
            for doc_id in ids:
                source = folder / by_id[doc_id]["stored_name"]
                for page in PdfReader(source).pages:
                    writer.add_page(page)
            version = con.execute("SELECT COALESCE(MAX(version),0)+1 FROM documents WHERE import_id=? AND category='coverage_merged'", (import_id,)).fetchone()[0]
            stored = f"propostas_cobertura_reunidas_v{version}_{secrets.token_hex(4)}.pdf"
            with (folder / stored).open("wb") as fh:
                writer.write(fh)
            cur = con.execute(
                """INSERT INTO documents(import_id,category,label,original_name,stored_name,version,status,merged_from,created_at)
                   VALUES(?,?,?,?,?,?,?, ?,?)""",
                (import_id, "coverage_merged", "Propostas de cobertura reunidas", "Propostas_de_Cobertura_Reunidas.pdf", stored, version, "current", json.dumps(ids), now_iso()),
            )
            con.execute("INSERT INTO events(import_id,event_type,description,created_at) VALUES(?,?,?,?)", (import_id, "coverage_merged", "Propostas de cobertura reunidas em PDF", now_iso()))
        self.send_json({"id": cur.lastrowid}, 201)

    def update_status(self, import_id: int) -> None:
        b = self.json_body()
        status = str(b["status"])
        allowed = {"preparation", "waiting_documents", "waiting_signature", "ready", "submitted", "analysis", "diligence", "authorized", "execution", "completed", "cancelled"}
        if status not in allowed:
            raise ValueError("Situação inválida")
        with connect() as con:
            con.execute("UPDATE imports SET status=?,next_action=?,updated_at=? WHERE id=?", (status, b.get("next_action", ""), now_iso(), import_id))
            con.execute("INSERT INTO events(import_id,event_type,description,created_at) VALUES(?,?,?,?)", (import_id, "status", f"Situação alterada para {status}", now_iso()))
        self.send_json({"ok": True})

    def download_document(self, document_id: int) -> None:
        with connect() as con:
            row = con.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if not row:
            return self.send_error(404)
        path = DOCS_DIR / f"importacao_{row['import_id']:06d}" / row["stored_name"]
        if not path.exists():
            return self.send_error(404)
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(row["original_name"])[0] or "application/octet-stream")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{row['original_name']}")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def create_backup(self) -> None:
        target = make_backup()
        self.send_json({"file": target.name, "size": target.stat().st_size}, 201)

    def restore_backup(self) -> None:
        b = self.json_body()
        raw = base64.b64decode(str(b["content_base64"]).split(",")[-1])
        with tempfile.TemporaryDirectory(prefix="controle_importacoes_restore_") as temp_name:
            temp = Path(temp_name)
            archive = temp / "backup.zip"
            archive.write_bytes(raw)
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
                if "controle_importacoes.db" not in names:
                    raise ValueError("Backup inválido: banco de dados ausente")
                for name in names:
                    path = Path(name)
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError("Backup inválido: caminho inseguro")
                zf.extractall(temp / "conteudo")
            candidate_db = temp / "conteudo" / "controle_importacoes.db"
            with sqlite3.connect(candidate_db) as candidate:
                required = {"researchers", "processes", "imports", "documents", "events"}
                found = {r[0] for r in candidate.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not required.issubset(found):
                    raise ValueError("Backup incompatível ou danificado")
            safety = make_backup()
            restored_docs = temp / "conteudo" / "documentos"
            replacement_db = DATA_DIR / "controle_importacoes.restored.db"
            shutil.copy2(candidate_db, replacement_db)
            replacement_db.replace(DB_PATH)
            if restored_docs.exists():
                previous_docs = DATA_DIR / "documentos_antes_da_restauracao"
                if previous_docs.exists():
                    shutil.rmtree(previous_docs)
                if DOCS_DIR.exists():
                    DOCS_DIR.replace(previous_docs)
                shutil.copytree(restored_docs, DOCS_DIR)
            else:
                DOCS_DIR.mkdir(parents=True, exist_ok=True)
        self.send_json({"ok": True, "safety_backup": safety.name})


def make_backup() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = BACKUP_DIR / f"Controle_Importacoes_backup_{stamp}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(DB_PATH, "controle_importacoes.db")
        for path in DOCS_DIR.rglob("*"):
            if path.is_file():
                zf.write(path, Path("documentos") / path.relative_to(DOCS_DIR))
    return target


def save_dialog_path(result: object) -> Path | None:
    """Normaliza o retorno do diálogo do pywebview entre versões/plataformas."""
    if not result:
        return None
    if isinstance(result, (list, tuple)):
        if not result:
            return None
        result = result[0]
    return Path(str(result))


class DesktopApi:
    def __init__(self) -> None:
        self.window = None

    def save_document(self, document_id: int) -> dict[str, object]:
        with connect() as con:
            row = con.execute("SELECT * FROM documents WHERE id=?", (int(document_id),)).fetchone()
        if not row:
            return {"ok": False, "error": "Documento não encontrado."}
        source = DOCS_DIR / f"importacao_{row['import_id']:06d}" / row["stored_name"]
        if not source.exists():
            return {"ok": False, "error": "O arquivo do documento não foi encontrado."}
        try:
            import webview  # type: ignore

            selected = self.window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=Path(row["original_name"]).name,
            )
            destination = save_dialog_path(selected)
            if destination is None:
                return {"ok": False, "cancelled": True}
            shutil.copy2(source, destination)
            return {"ok": True, "path": str(destination)}
        except Exception as exc:
            return {"ok": False, "error": f"Não foi possível salvar o documento: {exc}"}

    def save_backup(self) -> dict[str, object]:
        try:
            import webview  # type: ignore

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            selected = self.window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=f"Controle_Importacoes_backup_{stamp}.zip",
                file_types=("Arquivo ZIP (*.zip)",),
            )
            destination = save_dialog_path(selected)
            if destination is None:
                return {"ok": False, "cancelled": True}
            if destination.suffix.lower() != ".zip":
                destination = destination.with_suffix(".zip")
            source = make_backup()
            if source.resolve() != destination.resolve():
                shutil.copy2(source, destination)
            return {"ok": True, "path": str(destination), "file": destination.name}
        except Exception as exc:
            return {"ok": False, "error": f"Não foi possível salvar o backup: {exc}"}


def run() -> None:
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        import webview  # type: ignore
        threading.Thread(target=server.serve_forever, daemon=True).start()
        desktop_api = DesktopApi()
        window = webview.create_window(
            APP_NAME,
            url,
            width=1280,
            height=820,
            min_size=(980, 650),
            js_api=desktop_api,
        )
        desktop_api.window = window
        webview.start()
        server.shutdown()
    except ImportError:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        print(f"{APP_NAME} disponível em {url}. Pressione Ctrl+C para encerrar.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    run()
