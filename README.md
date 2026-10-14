# Local Ollama Chatbot

This project is a Flask + vanilla JavaScript chatbot that connects to the local Ollama runtime and uses the default model `qwen3:4b`. Conversations and completed messages are saved in a local SQLite database at `instance/chat_history.sqlite3`.

## Features

- ChatGPT-inspired interface
- Qwen3 default model with `llama3.2:3b` fallback option
- Streaming responses from Ollama
- Dark and light theme
- Clear and delete conversation actions
- SQLite-backed conversation history that survives restarts
- Search saved conversations and restore them with their selected model
- Attach and analyze PDF, DOCX, XLSX, PPTX, CSV, text, and common code files
- Attach JPG, PNG, and WebP images when a vision-capable local Ollama model is installed
- Copy assistant response
- Safe error handling for missing Ollama or unsupported models

## Project layout

- `app.py` — Flask backend and Ollama integration
- `templates/index.html` — HTML interface
- `static/style.css` — site styling
- `static/script.js` — frontend behavior and streaming logic
- `requirements.txt` — Python dependencies
- `instance/chat_history.sqlite3` — automatically initialized local conversation database

The database is created on first startup using non-destructive `CREATE TABLE IF NOT EXISTS` initialization. Do not delete this file if you want to keep saved chats.

## Windows setup

1. Open a terminal in the project folder.
2. Create and activate a virtual environment:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. Install dependencies:

   ```powershell
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   ```

4. Verify Ollama is running and the models are available:

   ```powershell
   curl.exe http://localhost:11434/api/tags
   ollama list
   ```

5. Start the app:

   ```powershell
   python app.py
   ```

6. Open the site at:

   - http://127.0.0.1:5000

## Test the backend manually

```powershell
curl.exe http://127.0.0.1:5000/api/conversations
```

Chat messages are stored as they are submitted, and a completed assistant response is saved when Ollama finishes streaming. If generation is stopped or fails, the submitted user message remains in history.

Useful history endpoints:

- `GET /api/conversations` — list conversations
- `POST /api/conversations` — create a conversation
- `GET /api/conversations/<id>` — restore conversation and messages
- `PATCH /api/conversations/<id>` — rename or change selected model
- `DELETE /api/conversations/<id>` — delete conversation and its messages
- `DELETE /api/conversations/<id>/messages` — clear messages but keep the conversation
- `GET /api/search?q=...` — search conversation titles and message contents

## Notes

- `qwen3:4b` remains the default; no model is selected automatically as a fallback.
- The installed `qwen3:4b` and `llama3.2:3b` models are text-only. To analyze image pixels, install a local Ollama vision model yourself, for example:

  ```powershell
  ollama pull qwen3-vl:4b
  ```

  The app discovers installed Ollama models marked with the `vision` capability and adds them to the model selector. Confirm the model tag is available in your Ollama installation; images are rejected with an explanation if the selected model is not vision-capable.
- Supported document formats are PDF, DOCX, XLSX, PPTX, CSV, and readable text/code. Other files are accepted only when they decode as text; unsupported binary formats (such as legacy DOC/XLS, archives, or audio/video) are rejected rather than sent as if analyzed.
- Attachments are stored locally in SQLite with conversation messages. Limits are 4 files per message, 6 MiB per file, 16 MiB total, and up to 30,000 extracted text characters per file. Deleting a conversation deletes its stored attachments.
- Ollama must already be installed and running locally.
- The app does not use cloud APIs or paid services.
