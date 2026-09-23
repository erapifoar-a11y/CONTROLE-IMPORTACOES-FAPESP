import base64
import importlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter


class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.mkdtemp(prefix="controle_importacoes_test_")
        os.environ["CONTROLE_IMPORTACOES_DATA"] = cls.temp
        cls.app = importlib.import_module("app")
        cls.app.DATA_DIR = Path(cls.temp)
        cls.app.DB_PATH = cls.app.DATA_DIR / "test.db"
        cls.app.DOCS_DIR = cls.app.DATA_DIR / "documentos"
        cls.app.BACKUP_DIR = cls.app.DATA_DIR / "backups"
        cls.app.init_db()
        cls.server = cls.app.ThreadingHTTPServer(("127.0.0.1", 0), cls.app.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.token = cls.request("/api/setup", {"password": "teste123"})["token"]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    @classmethod
    def request(cls, path, data=None, method=None):
        headers = {"Content-Type": "application/json"}
        if getattr(cls, "token", ""):
            headers["Authorization"] = f"Bearer {cls.token}"
        req = urllib.request.Request(
            cls.base + path,
            data=None if data is None else json.dumps(data).encode(),
            headers=headers,
            method=method or ("GET" if data is None else "POST"),
        )
        return json.load(urllib.request.urlopen(req))

    @classmethod
    def raw_request(cls, path, data, content_type="application/octet-stream"):
        req = urllib.request.Request(
            cls.base + path,
            data=data,
            headers={"Content-Type": content_type, "Authorization": f"Bearer {cls.token}"},
            method="POST",
        )
        return json.load(urllib.request.urlopen(req))

    @staticmethod
    def pdf_b64(pages):
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=100, height=100)
        output = io.BytesIO()
        writer.write(output)
        return base64.b64encode(output.getvalue()).decode()

    def test_workflow_and_coverage_merge(self):
        created = self.request(
            "/api/imports",
            {
                "researcher_name": "Pesquisadora Teste",
                "process_number": "2026/12345-6",
                "exporter": "Fornecedor X",
                "currency": "USD",
                "value_amount": "1.234,56",
                "proforma_number": "PF-1",
            },
        )
        import_id = created["id"]
        coverage_ids = []
        for idx, pages in enumerate((1, 2), start=1):
            document = self.request(
                f"/api/imports/{import_id}/documents",
                {
                    "name": f"cobertura_{idx}.pdf",
                    "content_base64": self.pdf_b64(pages),
                    "category": "coverage_proposal",
                    "label": f"Cobertura {idx}",
                },
            )
            coverage_ids.append(document["id"])
        self.request(
            f"/api/imports/{import_id}/documents",
            {
                "name": "vencedora.pdf",
                "content_base64": self.pdf_b64(4),
                "category": "winner_proposal",
                "label": "Vencedora",
            },
        )
        merged_id = self.request(
            f"/api/imports/{import_id}/merge-coverage",
            {"document_ids": coverage_ids},
        )["id"]
        with self.app.connect() as con:
            merged = con.execute("SELECT * FROM documents WHERE id=?", (merged_id,)).fetchone()
        merged_path = self.app.DOCS_DIR / f"importacao_{import_id:06d}" / merged["stored_name"]
        self.assertEqual(len(PdfReader(merged_path).pages), 3)
        dashboard = self.request("/api/dashboard")
        dashboard_import = next(
            item
            for process in dashboard["processes"]
            for item in process["imports"]
            if item["id"] == import_id
        )
        self.assertEqual(dashboard_import["value_amount"], "1234.56")

        self.request(f"/api/documents/{coverage_ids[0]}", {"category": "other", "label": "Corrigido", "signed": True}, "PUT")
        detail = self.request(f"/api/imports/{import_id}")
        edited = next(d for d in detail["documents"] if d["id"] == coverage_ids[0])
        self.assertEqual(edited["category"], "other")
        self.request(f"/api/documents/{coverage_ids[0]}", None, "DELETE")
        detail = self.request(f"/api/imports/{import_id}")
        self.assertFalse(any(d["id"] == coverage_ids[0] for d in detail["documents"]))

    def test_backup(self):
        backup = self.request("/api/backup", {})
        self.assertTrue(backup["file"].endswith(".zip"))
        source = self.app.BACKUP_DIR / backup["file"]
        restored = self.request(
            "/api/restore",
            {"content_base64": base64.b64encode(source.read_bytes()).decode()},
        )
        self.assertTrue(restored["ok"])
        self.assertTrue((self.app.BACKUP_DIR / restored["safety_backup"]).exists())

    def test_historical_zip_upload_is_idempotent(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("IMPORTAÇÕES/DBZ - FORNECEDOR TESTE/Formulário.pdf", self.pdf_b64(1))
            archive.writestr("IMPORTAÇÕES/DBZ - FORNECEDOR TESTE/Invoice.pdf", self.pdf_b64(1))
        first = self.raw_request("/api/historical-import", output.getvalue(), "application/zip")
        second = self.raw_request("/api/historical-import", output.getvalue(), "application/zip")
        self.assertEqual(first, {"imports": 1, "documents": 2})
        self.assertEqual(second, {"imports": 0, "documents": 0})


if __name__ == "__main__":
    unittest.main()
