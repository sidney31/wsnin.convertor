"""K3-Mebel MDB -> .wsnin converter. FastAPI backend.

Run:  uvicorn main:app --reload
Open: http://127.0.0.1:8000
"""
import html
import json
import os
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from wsnin import Converter, load_tables

MAX_UPLOAD_MB = 200
BASE_DIR = Path(__file__).parent
WORK_DIR = Path(tempfile.gettempdir()) / "mdb2wsnin"
WORK_DIR.mkdir(exist_ok=True)

app = FastAPI(title="MDB to WSNIN")


def convert(mdb_path: str, input_name: str) -> dict:
    tables = load_tables(mdb_path)
    if not tables.get("TPanels") or not tables.get("TElems"):
        raise ValueError("в базе нет таблиц деталей K3-Мебель (TPanels / TElems)")
    return Converter(tables, input_name).build()


def remove_file(path: Path) -> None:
    path.unlink(missing_ok=True)


@app.get("/", response_class=HTMLResponse)
async def index():
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


FILES_DIR = BASE_DIR / "test_wsnin"


@app.get("/files", response_class=HTMLResponse)
async def files_page():
    """Simple download page for the .wsnin files in test_wsnin/.

    Links are relative so the app also works behind a reverse-proxy prefix
    (e.g. nginx `location /services/test/`).
    """
    files = sorted(FILES_DIR.glob("*.wsnin")) if FILES_DIR.is_dir() else []
    rows = "".join(
        f'<li><a href="files/{html.escape(f.name)}" download>{html.escape(f.name)}</a>'
        f" <span>{f.stat().st_size / 1024:,.0f} КБ</span></li>"
        for f in files
    ) or "<li>Файлов нет</li>"
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Файлы .wsnin</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 16px}}
li{{margin:8px 0}} span{{color:#888;font-size:.9em}} a{{font-weight:600}}</style></head>
<body><h1>Файлы .wsnin</h1><ul>{rows}</ul><p><a href="./">← Конвертер</a></p></body></html>"""


@app.get("/files/{name}")
async def download_file(name: str):
    path = (FILES_DIR / name).resolve()
    if path.parent != FILES_DIR.resolve() or path.suffix != ".wsnin" or not path.is_file():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(path, media_type="application/json", filename=path.name)


@app.post("/api/convert")
async def api_convert(file: UploadFile = File(...)):
    filename = file.filename or "database.mdb"
    if not filename.lower().endswith((".mdb", ".accdb")):
        raise HTTPException(400, "Нужен файл .mdb или .accdb")
    input_name = os.path.splitext(os.path.basename(filename))[0]

    src = WORK_DIR / f"{uuid.uuid4().hex}.mdb"
    dst = WORK_DIR / f"{uuid.uuid4().hex}.wsnin"
    size = 0
    try:
        with open(src, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_MB * 1024 * 1024:
                    raise HTTPException(413, f"Файл больше {MAX_UPLOAD_MB} МБ")
                out.write(chunk)

        try:
            data = await run_in_threadpool(convert, str(src), input_name)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(422, f"Не удалось сконвертировать файл: {exc}")

        with open(dst, "w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
    except BaseException:
        remove_file(dst)
        raise
    finally:
        remove_file(src)

    return FileResponse(
        dst,
        media_type="application/json",
        filename=input_name + ".wsnin",
        background=BackgroundTask(remove_file, dst),
    )
