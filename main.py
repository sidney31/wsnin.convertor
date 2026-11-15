"""K3-Mebel MDB -> .wsnin converter. FastAPI backend.

Run:  uvicorn main:app --reload
Open: http://127.0.0.1:8000
"""
import json
import os
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from wsnin import Converter, load_tables

MAX_UPLOAD_MB = 200
BASE_DIR = Path(__file__).parent
WORK_DIR = Path(tempfile.gettempdir()) / "mdb2wsnin"
WORK_DIR.mkdir(exist_ok=True)

app = FastAPI(title="MDB to WSNIN")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def convert(mdb_path: str, input_name: str) -> dict:
    tables = load_tables(mdb_path)
    if not tables.get("TPanels") or not tables.get("TElems"):
        raise ValueError("в базе нет таблиц деталей K3-Мебель (TPanels / TElems)")
    return Converter(tables, input_name).build()


def explain_error(exc: Exception) -> str:
    """Turn parser/converter failures into a Russian message with a next step."""
    text = str(exc)
    if "DB file header" in text or "valid access database" in text:
        return "Это не база Access: файл повреждён или сохранён в другом формате. Выгрузите заказ из К3 заново."
    if "TPanels" in text:
        return "В базе нет деталей К3-Мебель. Проверьте, что выгружена база заказа, а не другая таблица."
    return f"Не удалось прочитать базу заказа: {text}"


def remove_file(path: Path) -> None:
    path.unlink(missing_ok=True)


@app.get("/", response_class=HTMLResponse)
async def index():
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


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
            raise HTTPException(422, explain_error(exc))

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
