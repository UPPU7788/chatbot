from __future__ import annotations

import json
import base64
import binascii
import csv
import io
import mimetypes
import sqlite3
import uuid
import xml.etree.ElementTree as ET
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List

import requests
from flask import Flask, Response, jsonify, render_template, request
from pypdf import PdfReader
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 24 * 1024 * 1024

OLLAMA_BASE_URL = "http://localhost:11434"
ALLOWED_MODELS = {"qwen3:4b", "llama3.2:3b"}
DEFAULT_MODEL = "qwen3:4b"
DATABASE_PATH = Path(app.instance_path) / "chat_history.sqlite3"
MAX_ATTACHMENTS = 4
MAX_ATTACHMENT_BYTES = 6 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 16 * 1024 * 1024
MAX_EXTRACTED_CHARS = 30_000
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".xml",
    ".html", ".htm", ".yaml", ".yml", ".toml", ".ini", ".log", ".py",
    ".js", ".jsx", ".ts", ".tsx", ".css", ".scss", ".sh", ".bat", ".ps1",
    ".sql", ".java", ".c", ".h", ".cpp", ".cs", ".go", ".rs", ".php", ".rb",
    ".r", ".ipynb", ".tex", ".diff", ".patch", ".properties", ".env",
    ".dockerfile", ".conf", ".cfg", ".gitignore", ".makefile",
}
IMAGE_MIME_BY_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
SYSTEM_PROMPT = (
    "You are a helpful, intelligent, friendly AI assistant powered by the local Ollama model. "
    "Answer questions accurately and clearly. Provide step-by-step explanations when useful. "
    "Use Markdown for readability and fenced code blocks for programming examples. "
    "When an attachment is provided, analyze only the extracted document text or image data "
    "included in the conversation, and be clear about any format or extraction limitations. "
    "Stay professional, respectful, and helpful."
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'New conversation',
                model TEXT NOT NULL DEFAULT 'qwen3:4b',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                model TEXT,
                attachments_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_conversations_updated
                ON conversations(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages(conversation_id, id);
            """
        )
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(messages)")
        }
        if "attachments_json" not in columns:
            connection.execute(
                "ALTER TABLE messages ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'"
            )


def conversation_json(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "model": row["model"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def find_conversation(connection: sqlite3.Connection, conversation_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT id, title, model, created_at, updated_at FROM conversations WHERE id = ?",
        (conversation_id,),
    ).fetchone()


def ollama_installed_models() -> Dict[str, set[str]]:
    try:
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError):
        return {}
    models: Dict[str, set[str]] = {}
    for model in payload.get("models", []):
        if not isinstance(model, dict) or not isinstance(model.get("name"), str):
            continue
        capabilities = model.get("capabilities", [])
        models[model["name"]] = set(capabilities) if isinstance(capabilities, list) else set()
    return models


def available_models() -> List[str]:
    installed = ollama_installed_models()
    vision_models = {
        name for name, capabilities in installed.items() if "vision" in capabilities
    }
    return sorted(ALLOWED_MODELS | vision_models)


def supported_model(model: Any) -> bool:
    return isinstance(model, str) and (
        model in ALLOWED_MODELS
        or "vision" in ollama_installed_models().get(model, set())
    )


def extract_zip_text(data: bytes, suffix: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(entry.file_size for entry in entries) > 32 * 1024 * 1024:
            raise ValueError("Office document expands beyond the safe processing limit.")

        if suffix == ".docx":
            names = ["word/document.xml"]
        elif suffix == ".pptx":
            names = sorted(
                (
                    name for name in archive.namelist()
                    if name.startswith("ppt/slides/slide") and name.endswith(".xml")
                ),
                key=lambda name: int(Path(name).stem.removeprefix("slide"))
                if Path(name).stem.removeprefix("slide").isdigit() else 0,
            )
        else:
            names = ["xl/sharedStrings.xml"] + sorted(
                name for name in archive.namelist()
                if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
            )

        pieces: List[str] = []
        shared_strings: List[str] = []
        for name in names:
            if name not in archive.namelist():
                continue
            root = ET.fromstring(archive.read(name))
            if suffix == ".xlsx" and name == "xl/sharedStrings.xml":
                shared_strings = [
                    "".join(element.itertext())
                    for element in root
                ]
                continue
            if suffix == ".xlsx":
                rows: List[str] = []
                for row in root.iter():
                    if row.tag.rsplit("}", 1)[-1] != "row":
                        continue
                    values = []
                    for cell in row:
                        if cell.tag.rsplit("}", 1)[-1] != "c":
                            continue
                        value = next(
                            (child.text or "" for child in cell
                             if child.tag.rsplit("}", 1)[-1] == "v"),
                            "",
                        )
                        if cell.attrib.get("t") == "s" and value.isdigit():
                            index = int(value)
                            value = shared_strings[index] if index < len(shared_strings) else value
                        elif cell.attrib.get("t") == "inlineStr":
                            inline_string = next(
                                (child for child in cell
                                 if child.tag.rsplit("}", 1)[-1] == "is"),
                                None,
                            )
                            value = "".join(inline_string.itertext()) if inline_string is not None else ""
                        values.append(value)
                    if any(values):
                        rows.append("\t".join(values))
                pieces.extend(rows)
            else:
                text = [
                    element.text.strip()
                    for element in root.iter()
                    if element.tag.rsplit("}", 1)[-1] in {"t", "p"}
                    and element.text and element.text.strip()
                ]
                pieces.extend(text)
    return "\n".join(pieces)


def decode_attachment(raw_attachment: Any) -> Dict[str, Any]:
    if not isinstance(raw_attachment, dict):
        raise ValueError("Each attachment must be a file object.")
    original_name = raw_attachment.get("name")
    data_base64 = raw_attachment.get("data_base64")
    if not isinstance(original_name, str) or not isinstance(data_base64, str):
        raise ValueError("Attachment name or file data is invalid.")
    safe_name = secure_filename(original_name.replace("\\", "/").rsplit("/", 1)[-1])
    if not safe_name:
        raise ValueError("Attachment filename is invalid.")
    try:
        data = base64.b64decode(data_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"Could not decode {safe_name}.") from exc
    if not data:
        raise ValueError(f"{safe_name} is empty.")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"{safe_name} exceeds the 6 MiB per-file limit.")

    suffix = Path(safe_name).suffix.lower()
    attachment: Dict[str, Any] = {
        "name": safe_name,
        "size": len(data),
    }
    if suffix in IMAGE_MIME_BY_EXTENSION:
        mime_type = IMAGE_MIME_BY_EXTENSION[suffix]
        valid_image = (
            (mime_type == "image/jpeg" and data.startswith(b"\xff\xd8\xff"))
            or (mime_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
            or (
                mime_type == "image/webp"
                and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
            )
        )
        if not valid_image:
            raise ValueError(f"{safe_name} is not a valid {mime_type} image.")
        attachment.update(
            {
                "mime_type": mime_type,
                "image_base64": base64.b64encode(data).decode("ascii"),
            }
        )
        return attachment

    if suffix == ".pdf":
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            if len(reader.pages) > 500:
                raise ValueError("PDF files are limited to 500 pages.")
            extracted = "\n".join(
                page.extract_text() or "" for page in reader.pages
            )
        except Exception as exc:
            raise ValueError(f"Could not read PDF file {safe_name}: {exc}") from exc
    elif suffix in {".docx", ".xlsx", ".pptx"}:
        try:
            extracted = extract_zip_text(data, suffix)
        except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError) as exc:
            raise ValueError(f"Could not read Office file {safe_name}: {exc}") from exc
    else:
        if suffix and suffix not in TEXT_EXTENSIONS:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"{safe_name} is an unsupported binary format. "
                    "Supported documents include PDF, DOCX, XLSX, PPTX, and text-based files."
                ) from exc
        else:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                try:
                    text = data.decode("cp1252")
                except UnicodeDecodeError as exc:
                    raise ValueError(f"{safe_name} is not readable as a text file.") from exc
        if "\x00" in text:
            raise ValueError(f"{safe_name} appears to be a binary file type not supported.")
        if suffix not in TEXT_EXTENSIONS and any(
            ord(character) < 32 and character not in "\t\n\r\f" for character in text
        ):
            raise ValueError(
                f"{safe_name} is an unsupported binary format. "
                "Supported documents include PDF, DOCX, XLSX, PPTX, and text-based files."
            )
        if suffix == ".csv":
            try:
                text = "\n".join(
                    "\t".join(row)
                    for row in csv.reader(io.StringIO(text))
                )
            except csv.Error as exc:
                raise ValueError(f"Could not read CSV file {safe_name}: {exc}") from exc
        extracted = text

    extracted = extracted.strip()
    if not extracted:
        raise ValueError(f"No readable text was found in {safe_name}.")
    if len(extracted) > MAX_EXTRACTED_CHARS:
        extracted = extracted[:MAX_EXTRACTED_CHARS] + "\n[Text truncated at 30,000 characters.]"
    attachment.update(
        {
            "mime_type": mimetypes.guess_type(safe_name)[0] or "application/octet-stream",
            "extracted_text": extracted,
        }
    )
    return attachment


def ollama_message(row: sqlite3.Row) -> Dict[str, Any]:
    attachments = json.loads(row["attachments_json"] or "[]")
    text_sections = [
        f"[Attached file: {item['name']}]\n{item['extracted_text']}"
        for item in attachments if item.get("extracted_text")
    ]
    content = row["content"]
    if text_sections:
        content = f"{content}\n\n" + "\n\n".join(text_sections)
    message: Dict[str, Any] = {"role": row["role"], "content": content}
    images = [
        item["image_base64"] for item in attachments
        if item.get("image_base64")
    ]
    if images:
        message["images"] = images
    return message


def create_conversation(model: str = DEFAULT_MODEL, title: str = "New conversation") -> str:
    conversation_id = str(uuid.uuid4())
    timestamp = utc_now()
    with get_db() as connection:
        connection.execute(
            """
            INSERT INTO conversations (id, title, model, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (conversation_id, title, model, timestamp, timestamp),
        )
    return conversation_id


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.errorhandler(413)
def request_too_large(_: Exception) -> Any:
    return jsonify({"error": "The request exceeds the 24 MiB upload limit."}), 413


@app.get("/api/health")
def health() -> Any:
    try:
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        response.raise_for_status()
        ollama_ok = True
    except requests.RequestException:
        ollama_ok = False

    return jsonify(
        {
            "status": "ok" if ollama_ok else "degraded",
            "default_model": DEFAULT_MODEL,
            "allowed_models": sorted(ALLOWED_MODELS),
            "ollama_url": OLLAMA_BASE_URL,
            "ollama_connected": ollama_ok,
            "database": "ok" if DATABASE_PATH.exists() else "not_initialized",
        }
    )


@app.get("/api/models")
def list_models() -> Any:
    models = available_models()
    installed = ollama_installed_models()
    return jsonify(
        {
            "default_model": DEFAULT_MODEL,
            "models": models,
            "vision_models": sorted(
                name for name, capabilities in installed.items()
                if "vision" in capabilities
            ),
        }
    )


@app.get("/api/conversations")
def list_conversations() -> Any:
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT c.id, c.title, c.model, c.created_at, c.updated_at,
                   (SELECT content FROM messages m
                    WHERE m.conversation_id = c.id AND m.role = 'user'
                    ORDER BY m.id LIMIT 1) AS first_message
            FROM conversations c
            ORDER BY c.updated_at DESC, c.created_at DESC
            """
        ).fetchall()
    result = [conversation_json(row) for row in rows]
    for item, row in zip(result, rows):
        item["preview"] = row["first_message"] or ""
    return jsonify({"conversations": result})


@app.post("/api/conversations")
def new_conversation() -> Any:
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"error": "A JSON object is required."}), 400
    model = payload.get("model", DEFAULT_MODEL)
    title = payload.get("title", "New conversation")
    if not supported_model(model):
        return jsonify({"error": "Unsupported model."}), 400
    if not isinstance(title, str) or len(title) > 120:
        return jsonify({"error": "Title must be a string of at most 120 characters."}), 400

    conversation_id = create_conversation(model=model, title=title.strip() or "New conversation")
    with get_db() as connection:
        row = find_conversation(connection, conversation_id)
    return jsonify({"conversation": conversation_json(row)}), 201


@app.get("/api/conversations/<conversation_id>")
def get_conversation(conversation_id: str) -> Any:
    with get_db() as connection:
        row = find_conversation(connection, conversation_id)
        if row is None:
            return jsonify({"error": "Conversation not found."}), 404
        messages = connection.execute(
            """
            SELECT id, role, content, model, attachments_json, created_at
            FROM messages WHERE conversation_id = ? ORDER BY id
            """,
            (conversation_id,),
        ).fetchall()
    serialized_messages = []
    for message in messages:
        item = dict(message)
        item["attachments"] = json.loads(item.pop("attachments_json") or "[]")
        serialized_messages.append(item)
    return jsonify(
        {
            "conversation": conversation_json(row),
            "messages": serialized_messages,
        }
    )


@app.patch("/api/conversations/<conversation_id>")
def update_conversation(conversation_id: str) -> Any:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A JSON object is required."}), 400

    updates: Dict[str, str] = {}
    if "title" in payload:
        title = payload["title"]
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
            return jsonify({"error": "Title must contain 1 to 120 characters."}), 400
        updates["title"] = title.strip()
    if "model" in payload:
        model = payload["model"]
        if not supported_model(model):
            return jsonify({"error": "Unsupported model."}), 400
        updates["model"] = model
    if not updates:
        return jsonify({"error": "Provide a title or model to update."}), 400

    updates["updated_at"] = utc_now()
    assignments = ", ".join(f"{column} = ?" for column in updates)
    with get_db() as connection:
        cursor = connection.execute(
            f"UPDATE conversations SET {assignments} WHERE id = ?",
            (*updates.values(), conversation_id),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "Conversation not found."}), 404
        row = find_conversation(connection, conversation_id)
    return jsonify({"conversation": conversation_json(row)})


@app.delete("/api/conversations/<conversation_id>")
def delete_conversation(conversation_id: str) -> Any:
    with get_db() as connection:
        cursor = connection.execute(
            "DELETE FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "Conversation not found."}), 404
    return jsonify({"deleted": True})


@app.delete("/api/conversations/<conversation_id>/messages")
def clear_conversation_messages(conversation_id: str) -> Any:
    with get_db() as connection:
        if find_conversation(connection, conversation_id) is None:
            return jsonify({"error": "Conversation not found."}), 404
        connection.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        connection.execute(
            "UPDATE conversations SET title = 'New conversation', updated_at = ? WHERE id = ?",
            (utc_now(), conversation_id),
        )
    return jsonify({"cleared": True})


@app.get("/api/search")
def search_conversations() -> Any:
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"conversations": []})
    if len(query) > 200:
        return jsonify({"error": "Search query must be at most 200 characters."}), 400

    escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped_query}%"
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT c.id, c.title, c.model, c.created_at, c.updated_at
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            WHERE c.title LIKE ? ESCAPE '\\'
               OR m.content LIKE ? ESCAPE '\\'
            ORDER BY c.updated_at DESC
            LIMIT 50
            """,
            (pattern, pattern),
        ).fetchall()
    return jsonify({"conversations": [conversation_json(row) for row in rows]})


@app.post("/api/chat")
def chat() -> Response | tuple[Any, int]:
    payload = request.get_json(silent=True)
    if payload is None and request.form:
        payload = request.form.to_dict()
        try:
            payload["attachments"] = json.loads(payload.get("attachments", "[]"))
        except json.JSONDecodeError:
            return jsonify({"error": "Attachment metadata must be valid JSON."}), 400
    if payload is None:
        return jsonify({"error": "A JSON object is required."}), 400
    if not isinstance(payload, dict):
        return jsonify({"error": "A JSON object is required."}), 400

    model = payload.get("model") or DEFAULT_MODEL
    if not supported_model(model):
        return jsonify(
            {
                "error": (
                    f"Unsupported model '{model}'. Choose an allowed local model from /api/models."
                )
            }
        ), 400

    user_content = payload.get("content")
    if user_content is None:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return jsonify({"error": "'content' must be a string."}), 400
        user_messages = [
            item for item in messages
            if isinstance(item, dict) and item.get("role") == "user"
            and isinstance(item.get("content"), str)
        ]
        user_content = user_messages[-1]["content"] if user_messages else None
    if not isinstance(user_content, str) or len(user_content) > 100_000:
        return jsonify({"error": "Message must be text of at most 100,000 characters."}), 400
    user_content = user_content.strip()

    raw_attachments = payload.get("attachments", [])
    if not isinstance(raw_attachments, list):
        return jsonify({"error": "'attachments' must be a list."}), 400
    if len(raw_attachments) > MAX_ATTACHMENTS:
        return jsonify({"error": f"Attach no more than {MAX_ATTACHMENTS} files per message."}), 400

    attachments: List[Dict[str, Any]] = []
    try:
        for raw_attachment in raw_attachments:
            attachments.append(decode_attachment(raw_attachment))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    total_attachment_bytes = sum(item["size"] for item in attachments)
    if total_attachment_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
        return jsonify({"error": "Attachments must total no more than 16 MiB per message."}), 400
    if not user_content and not attachments:
        return jsonify({"error": "Enter a message or attach at least one file."}), 400

    image_attachments = [item for item in attachments if item.get("image_base64")]
    if image_attachments:
        installed = ollama_installed_models()
        if "vision" not in installed.get(model, set()):
            return jsonify(
                {
                    "error": (
                        f"{model} cannot analyze image pixels. Select an installed vision-capable "
                        "Ollama model; see README.md for local setup instructions."
                    )
                }
            ), 400

    conversation_id = payload.get("conversation_id")
    if conversation_id is not None and not isinstance(conversation_id, str):
        return jsonify({"error": "'conversation_id' must be a string."}), 400
    if conversation_id:
        with get_db() as connection:
            conversation = find_conversation(connection, conversation_id)
            if conversation is None:
                return jsonify({"error": "Conversation not found."}), 404
            connection.execute(
                "UPDATE conversations SET model = ?, updated_at = ? WHERE id = ?",
                (model, utc_now(), conversation_id),
            )
            history = connection.execute(
                """
                SELECT role, content, attachments_json
                FROM messages WHERE conversation_id = ? ORDER BY id
                """,
                (conversation_id,),
            ).fetchall()
            if not history and conversation["title"] == "New conversation":
                title = (user_content.splitlines()[0].strip() if user_content else attachments[0]["name"])[:60]
                connection.execute(
                    "UPDATE conversations SET title = ? WHERE id = ?",
                    (title, conversation_id),
                )
            connection.execute(
                """
                INSERT INTO messages
                    (conversation_id, role, content, model, attachments_json, created_at)
                VALUES (?, 'user', ?, ?, ?, ?)
                """,
                (
                    conversation_id, user_content, model,
                    json.dumps(attachments, ensure_ascii=False), utc_now(),
                ),
            )
    else:
        title = (user_content.splitlines()[0].strip() if user_content else attachments[0]["name"])[:60]
        conversation_id = create_conversation(model=model, title=title)
        history = []
        with get_db() as connection:
            connection.execute(
                """
                INSERT INTO messages
                    (conversation_id, role, content, model, attachments_json, created_at)
                VALUES (?, 'user', ?, ?, ?, ?)
                """,
                (
                    conversation_id, user_content, model,
                    json.dumps(attachments, ensure_ascii=False), utc_now(),
                ),
            )

    current_message = {
        "role": "user",
        "content": user_content,
        "attachments_json": json.dumps(attachments, ensure_ascii=False),
    }
    final_messages = [ollama_message(row) for row in history]
    final_messages.append(ollama_message(current_message))

    def stream_chat() -> Iterable[str]:
        assistant_text: List[str] = []
        try:
            with requests.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + final_messages,
                    "stream": True,
                    "think": False,
                    "options": {"temperature": 0.7},
                },
                timeout=(10, 180),
                stream=True,
            ) as ollama_response:
                if ollama_response.status_code != 200:
                    error_detail = ollama_response.text.strip() or "Unknown Ollama error"
                    yield f"data: {json.dumps({'type': 'error', 'error': error_detail})}\n\n"
                    return

                for line in ollama_response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        response_payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if "error" in response_payload:
                        yield f"data: {json.dumps({'type': 'error', 'error': response_payload['error']})}\n\n"
                        return

                    message = response_payload.get("message") or {}
                    content = message.get("content", "")
                    if content:
                        assistant_text.append(content)
                        yield f"data: {json.dumps({'type': 'content', 'content': content})}\n\n"

            response_text = "".join(assistant_text).strip()
            if response_text:
                timestamp = utc_now()
                with get_db() as connection:
                    connection.execute(
                        """
                        INSERT INTO messages (conversation_id, role, content, model, created_at)
                        VALUES (?, 'assistant', ?, ?, ?)
                        """,
                        (conversation_id, response_text, model, timestamp),
                    )
                    connection.execute(
                        "UPDATE conversations SET updated_at = ? WHERE id = ?",
                        (timestamp, conversation_id),
                    )
            yield f"data: {json.dumps({'type': 'done', 'conversation_id': conversation_id})}\n\n"
        except requests.RequestException as exc:
            yield f"data: {json.dumps({'type': 'error', 'error': f'Unable to connect to Ollama at {OLLAMA_BASE_URL}: {exc}'})}\n\n"

    return Response(stream_chat(), mimetype="text/event-stream")


initialize_database()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
