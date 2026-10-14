const state = {
  messages: [],
  conversationId: null,
  conversationTitle: "New conversation",
  model: "qwen3:4b",
  models: [],
  isStreaming: false,
  abortController: null,
  selectedTheme: "light",
  requestToken: 0,
  searchTimer: null,
  searchQuery: "",
  pendingFiles: [],
  isPreparing: false,
};

const elements = {
  chatMessages: document.getElementById("chat-messages"),
  userInput: document.getElementById("user-input"),
  chatForm: document.getElementById("chat-form"),
  sendButton: document.getElementById("send-button"),
  modelSelect: document.getElementById("model-select"),
  newChatButton: document.getElementById("new-chat"),
  clearChatButton: document.getElementById("clear-chat"),
  deleteChatButton: document.getElementById("delete-chat"),
  themeToggle: document.getElementById("theme-toggle"),
  statusBar: document.getElementById("status-bar"),
  stopGenerationButton: document.getElementById("stop-generation"),
  conversationTitle: document.getElementById("conversation-title"),
  conversationList: document.getElementById("conversation-list"),
  historySearch: document.getElementById("history-search"),
  historyCount: document.getElementById("history-count"),
  fileInput: document.getElementById("file-input"),
  attachFilesButton: document.getElementById("attach-files"),
  attachmentPreview: document.getElementById("attachment-preview"),
};

function escapeHtml(value) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function renderMarkdown(text) {
  let html = escapeHtml(text);
  html = html.replace(/```([\w+-]*)\n?([\s\S]*?)```/g, (_, language, code) => (
    `<pre><code${language ? ` class="language-${language}"` : ""}>${code.trim()}</code></pre>`
  ));
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\n\n/g, "</p><p>");
  html = html.replace(/\n/g, "<br>");
  html = `<p>${html}</p>`;
  html = html.replace(/<p><pre>/g, "<pre>");
  html = html.replace(/<\/pre><\/p>/g, "</pre>");
  return html;
}

function formatFileSize(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(0)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function renderAttachmentCards(container, attachments = []) {
  if (!attachments.length) return;
  const list = document.createElement("div");
  list.className = "message-attachments";
  for (const attachment of attachments) {
    const card = document.createElement("div");
    card.className = "message-attachment";
    const isImage = attachment.image_base64 || attachment.preview_url;
    if (isImage) {
      const image = document.createElement("img");
      image.className = "attachment-thumbnail";
      image.alt = attachment.name || "Attached image";
      image.src = attachment.preview_url
        || `data:${attachment.mime_type};base64,${attachment.image_base64}`;
      card.appendChild(image);
    } else {
      const icon = document.createElement("span");
      icon.className = "attachment-file-icon";
      icon.textContent = (attachment.name || "FILE").split(".").pop().slice(0, 5).toUpperCase();
      card.appendChild(icon);
    }
    const details = document.createElement("span");
    details.className = "attachment-details";
    const name = document.createElement("strong");
    name.textContent = attachment.name || "Attached file";
    const size = document.createElement("small");
    size.textContent = formatFileSize(attachment.size || 0);
    details.append(name, size);
    card.appendChild(details);
    list.appendChild(card);
  }
  container.appendChild(list);
}

function renderPendingFiles() {
  elements.attachmentPreview.innerHTML = "";
  elements.attachmentPreview.classList.toggle("hidden", !state.pendingFiles.length);
  for (const [index, item] of state.pendingFiles.entries()) {
    const card = document.createElement("div");
    card.className = "pending-attachment";
    if (item.previewUrl) {
      const image = document.createElement("img");
      image.src = item.previewUrl;
      image.alt = "";
      card.appendChild(image);
    } else {
      const icon = document.createElement("span");
      icon.className = "attachment-file-icon";
      icon.textContent = (item.file.name.split(".").pop() || "FILE").slice(0, 5).toUpperCase();
      card.appendChild(icon);
    }
    const name = document.createElement("span");
    name.textContent = item.file.name;
    name.title = item.file.name;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-attachment";
    remove.textContent = "×";
    remove.setAttribute("aria-label", `Remove ${item.file.name}`);
    remove.addEventListener("click", () => removePendingFile(index));
    card.append(name, remove);
    elements.attachmentPreview.appendChild(card);
  }
}

function removePendingFile(index) {
  const [removed] = state.pendingFiles.splice(index, 1);
  if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
  renderPendingFiles();
}

function clearPendingFiles() {
  for (const item of state.pendingFiles) {
    if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
  }
  state.pendingFiles = [];
  renderPendingFiles();
  elements.fileInput.value = "";
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(new Error(`Could not read ${file.name}.`));
    reader.readAsDataURL(file);
  });
}

async function encodeAttachments(files) {
  return Promise.all(files.map(async ({ file }) => ({
    name: file.name,
    size: file.size,
    mime_type: file.type,
    data_base64: await readFileAsBase64(file),
  })));
}

async function apiRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(result.error || `Request failed (${response.status}).`);
  }
  return result;
}

function setStatus(message, isError = false) {
  elements.statusBar.textContent = message;
  elements.statusBar.classList.toggle("hidden", !message);
  elements.statusBar.setAttribute("aria-invalid", String(isError));
}

function updateConversationTitle() {
  elements.conversationTitle.textContent = state.conversationTitle;
  const hasConversation = Boolean(state.conversationId);
  elements.clearChatButton.disabled = !hasConversation;
  elements.deleteChatButton.disabled = !hasConversation;
}

function renderWelcome() {
  const welcome = document.createElement("div");
  welcome.className = "welcome-card";
  welcome.innerHTML = `
    <span class="welcome-spark" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none">
        <path d="M12 3.5 14 9l5.5 2-5.5 2-2 5.5L10 13l-5.5-2L10 9l2-5.5Z" fill="currentColor" />
      </svg>
    </span>
    <h2>What can I help with?</h2>
    <p>Ask a question, explore an idea, or get help with a task. Your conversation stays on this device.</p>
    <div class="suggestion-grid">
      <button class="suggestion-card" type="button">Explain a tricky concept in simple terms</button>
      <button class="suggestion-card" type="button">Help me brainstorm a new project</button>
      <button class="suggestion-card" type="button">Review or improve a piece of code</button>
      <button class="suggestion-card" type="button">Summarize a topic I want to understand</button>
    </div>
  `;
  welcome.querySelectorAll(".suggestion-card").forEach((button) => {
    button.addEventListener("click", () => {
      elements.userInput.value = button.textContent.trim();
      elements.userInput.focus();
      elements.chatForm.requestSubmit();
    });
  });
  elements.chatMessages.appendChild(welcome);
}

function renderMessages() {
  elements.chatMessages.innerHTML = "";
  if (!state.messages.length) {
    renderWelcome();
    return;
  }

  for (const message of state.messages) {
    const row = document.createElement("div");
    row.className = `message-row ${message.role}`;

    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    const messageContent = document.createElement("div");
    messageContent.className = "message-content";
    messageContent.innerHTML = renderMarkdown(message.content);
    renderAttachmentCards(messageContent, message.attachments);
    bubble.appendChild(messageContent);

    const meta = document.createElement("div");
    meta.className = "message-meta";
    const author = document.createElement("span");
    author.textContent = message.role === "user" ? "You" : "Assistant";
    meta.appendChild(author);

    if (message.role === "assistant") {
      const copyButton = document.createElement("button");
      copyButton.type = "button";
      copyButton.className = `copy-button${message.content ? "" : " hidden"}`;
      copyButton.textContent = "Copy";
      copyButton.setAttribute("aria-label", "Copy assistant response");
      copyButton.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(message.content);
          copyButton.textContent = "Copied";
          window.setTimeout(() => { copyButton.textContent = "Copy"; }, 1300);
        } catch (error) {
          setStatus(`Could not copy response: ${error.message}`, true);
        }
      });
      meta.appendChild(copyButton);
    }

    bubble.appendChild(meta);
    row.appendChild(bubble);
    elements.chatMessages.appendChild(row);
  }
  elements.chatMessages.scrollTop = elements.chatMessages.scrollHeight;
}

function renderConversationList(conversations) {
  elements.conversationList.innerHTML = "";
  elements.historyCount.textContent = conversations.length ? String(conversations.length) : "";
  if (!conversations.length) {
    const empty = document.createElement("p");
    empty.className = "history-empty";
    empty.textContent = state.searchQuery ? "No matching conversations." : "Your saved conversations will appear here.";
    elements.conversationList.appendChild(empty);
    return;
  }

  for (const conversation of conversations) {
    const item = document.createElement("div");
    item.className = `history-item${conversation.id === state.conversationId ? " active" : ""}`;

    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "history-open";
    openButton.textContent = conversation.title || "New conversation";
    openButton.title = conversation.title || "New conversation";
    openButton.setAttribute("aria-current", conversation.id === state.conversationId ? "page" : "false");
    openButton.addEventListener("click", () => loadConversation(conversation.id));

    const renameButton = document.createElement("button");
    renameButton.type = "button";
    renameButton.className = "history-rename";
    renameButton.setAttribute("aria-label", `Rename ${conversation.title || "conversation"}`);
    renameButton.title = "Rename conversation";
    renameButton.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14 5 5 5M4 20l4.2-.8L19 8.4 15.6 5 4.8 15.8 4 20Z" /></svg>';
    renameButton.addEventListener("click", () => renameConversation(conversation.id, conversation.title));

    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "history-remove";
    removeButton.setAttribute("aria-label", `Delete ${conversation.title || "conversation"}`);
    removeButton.title = "Delete conversation";
    removeButton.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14M10 11v6m4-6v6M7 7l1 13h8l1-13M9 7V4h6v3" /></svg>';
    removeButton.addEventListener("click", () => deleteConversation(conversation.id, conversation.title));

    item.append(openButton, renameButton, removeButton);
    elements.conversationList.appendChild(item);
  }
}

async function refreshConversationList() {
  try {
    const endpoint = state.searchQuery
      ? `/api/search?q=${encodeURIComponent(state.searchQuery)}`
      : "/api/conversations";
    const result = await apiRequest(endpoint);
    renderConversationList(result.conversations || []);
  } catch (error) {
    setStatus(`Could not load chat history: ${error.message}`, true);
  }
}

async function loadConversation(conversationId) {
  if (state.abortController) state.abortController.abort();
  state.requestToken += 1;
  state.abortController = null;
  state.isStreaming = false;
  elements.stopGenerationButton.classList.add("hidden");
  try {
    const result = await apiRequest(`/api/conversations/${encodeURIComponent(conversationId)}`);
    state.conversationId = result.conversation.id;
    state.conversationTitle = result.conversation.title;
    state.model = result.conversation.model || "qwen3:4b";
    elements.modelSelect.value = state.model;
    state.messages = result.messages.map(({ role, content, attachments }) => ({ role, content, attachments }));
    updateConversationTitle();
    renderMessages();
    setStatus("");
    await refreshConversationList();
  } catch (error) {
    setStatus(`Could not open conversation: ${error.message}`, true);
  }
}

function setTheme(theme) {
  const dark = theme === "dark";
  document.body.classList.toggle("dark", dark);
  state.selectedTheme = theme;
  const nextTheme = dark ? "light" : "dark";
  elements.themeToggle.setAttribute("aria-label", `Switch to ${nextTheme} theme`);
  elements.themeToggle.title = `Switch to ${nextTheme} theme`;
  elements.themeToggle.innerHTML = dark
    ? '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4" /><path d="M12 2v2m0 16v2M4.93 4.93l1.41 1.41m11.32 11.32 1.41 1.41M2 12h2m16 0h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" /></svg>'
    : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.2 15.1A8.4 8.4 0 0 1 8.9 3.8a8.5 8.5 0 1 0 11.3 11.3Z" /></svg>';
  localStorage.setItem("chat-theme", theme);
}

async function fetchModels() {
  const result = await apiRequest("/api/models");
  state.models = result.models || [];
  state.model = result.default_model || "qwen3:4b";
  elements.modelSelect.innerHTML = "";
  for (const model of state.models) {
    const option = document.createElement("option");
    option.value = model;
    option.textContent = model;
    elements.modelSelect.appendChild(option);
  }
  elements.modelSelect.value = state.model;
}

function createNewConversation() {
  if (state.abortController) state.abortController.abort();
  state.requestToken += 1;
  state.abortController = null;
  state.isStreaming = false;
  state.conversationId = null;
  state.conversationTitle = "New conversation";
  state.messages = [];
  state.model = "qwen3:4b";
  clearPendingFiles();
  elements.modelSelect.value = state.model;
  elements.userInput.value = "";
  elements.userInput.style.height = "auto";
  elements.stopGenerationButton.classList.add("hidden");
  updateConversationTitle();
  renderMessages();
  setStatus("");
  refreshConversationList();
}

async function createConversationForMessage() {
  const result = await apiRequest("/api/conversations", {
    method: "POST",
    body: JSON.stringify({ model: state.model }),
  });
  state.conversationId = result.conversation.id;
  state.conversationTitle = result.conversation.title;
  updateConversationTitle();
}

async function streamChatRequest(requestToken, content, attachments) {
  state.isStreaming = true;
  elements.stopGenerationButton.classList.remove("hidden");
  setStatus("Generating...");
  state.abortController = new AbortController();

  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: state.model,
      conversation_id: state.conversationId,
      content,
      attachments,
    }),
    signal: state.abortController.signal,
  });

  if (!response.ok || !response.body) {
    const result = await response.json().catch(() => ({}));
    throw new Error(result.error || `Chat request failed (${response.status}).`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let assistantText = "";
  let completed = false;
  const assistantMessage = { role: "assistant", content: "" };
  state.messages.push(assistantMessage);
  renderMessages();
  const assistantRow = elements.chatMessages.lastElementChild;
  const assistantContent = assistantRow.querySelector(".message-content");
  const copyButton = assistantRow.querySelector(".copy-button");

  const processEvent = (frame) => {
    const data = frame.split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim())
      .join("\n");
    if (!data) return;
    const event = JSON.parse(data);
    if (event.type === "content") {
      assistantText += event.content || "";
      assistantMessage.content = assistantText;
      if (requestToken === state.requestToken) {
        assistantContent.innerHTML = renderMarkdown(assistantText);
        copyButton.classList.toggle("hidden", !assistantText);
        elements.chatMessages.scrollTop = elements.chatMessages.scrollHeight;
      }
    } else if (event.type === "error") {
      throw new Error(event.error || "The model returned an error.");
    } else if (event.type === "done") {
      completed = true;
    }
  };

  while (!completed) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let frameEnd = buffer.indexOf("\n\n");
    while (frameEnd !== -1) {
      const frame = buffer.slice(0, frameEnd);
      buffer = buffer.slice(frameEnd + 2);
      processEvent(frame);
      frameEnd = buffer.indexOf("\n\n");
    }
  }
  if (buffer.trim()) processEvent(buffer);
  if (requestToken !== state.requestToken) return;
  if (!assistantText.trim()) {
    state.messages.pop();
    assistantRow.remove();
  }
  else if (completed) state.conversationTitle = state.messages[0]?.content.split("\n")[0].slice(0, 60) || "Conversation";
  updateConversationTitle();
}

async function handleSubmit(event) {
  event.preventDefault();
  const input = elements.userInput.value.trim();
  if ((!input && !state.pendingFiles.length) || state.isStreaming || state.isPreparing) return;

  state.isPreparing = true;
  elements.sendButton.disabled = true;
  const requestToken = ++state.requestToken;
  try {
    const pendingFiles = [...state.pendingFiles];
    const attachments = await encodeAttachments(pendingFiles);
    if (requestToken !== state.requestToken) return;
    state.messages.push({
      role: "user",
      content: input,
      attachments: attachments.map(({ name, size, mime_type, data_base64 }) => ({
        name,
        size,
        mime_type,
        ...(mime_type.startsWith("image/") ? { image_base64: data_base64 } : {}),
      })),
    });
    clearPendingFiles();
    renderMessages();
    elements.userInput.value = "";
    elements.userInput.style.height = "auto";
    elements.userInput.focus();
    setStatus("");

    if (!state.conversationId) await createConversationForMessage();
    if (requestToken !== state.requestToken) return;
    await streamChatRequest(requestToken, input, attachments);
  } catch (error) {
    if (requestToken !== state.requestToken) return;
    if (error.name === "AbortError") {
      setStatus("Generation stopped.");
      const lastMessage = state.messages[state.messages.length - 1];
      if (lastMessage?.role === "assistant" && !lastMessage.content) state.messages.pop();
    } else {
      setStatus(error.message || "The request failed.", true);
      const lastMessage = state.messages[state.messages.length - 1];
      if (lastMessage?.role === "assistant" && !lastMessage.content) state.messages.pop();
    }
    renderMessages();
  } finally {
    state.isPreparing = false;
    elements.sendButton.disabled = false;
    if (requestToken === state.requestToken) {
      state.isStreaming = false;
      state.abortController = null;
      elements.stopGenerationButton.classList.add("hidden");
      updateConversationTitle();
      await refreshConversationList();
    }
  }
}

async function clearCurrentConversation() {
  if (!state.conversationId) return;
  if (state.abortController) state.abortController.abort();
  state.requestToken += 1;
  state.abortController = null;
  state.isStreaming = false;
  elements.stopGenerationButton.classList.add("hidden");
  try {
    await apiRequest(`/api/conversations/${encodeURIComponent(state.conversationId)}/messages`, {
      method: "DELETE",
    });
    state.messages = [];
    state.conversationTitle = "New conversation";
    updateConversationTitle();
    renderMessages();
    setStatus("Messages cleared.");
    await refreshConversationList();
  } catch (error) {
    setStatus(`Could not clear conversation: ${error.message}`, true);
  }
}

async function deleteConversation(conversationId, conversationTitle = "this conversation") {
  const selected = conversationId === state.conversationId;
  const title = selected ? state.conversationTitle : conversationTitle;
  if (!window.confirm(`Delete "${title}" and its saved messages? This cannot be undone.`)) return;
  if (selected && state.abortController) state.abortController.abort();
  if (selected) {
    state.requestToken += 1;
    state.abortController = null;
    state.isStreaming = false;
    elements.stopGenerationButton.classList.add("hidden");
  }
  try {
    await apiRequest(`/api/conversations/${encodeURIComponent(conversationId)}`, { method: "DELETE" });
    if (selected) createNewConversation();
    else await refreshConversationList();
    setStatus("Conversation deleted.");
  } catch (error) {
    setStatus(`Could not delete conversation: ${error.message}`, true);
  }
}

async function renameConversation(conversationId, currentTitle) {
  const title = window.prompt("Rename conversation", currentTitle || "");
  if (title === null || !title.trim()) return;
  try {
    const result = await apiRequest(`/api/conversations/${encodeURIComponent(conversationId)}`, {
      method: "PATCH",
      body: JSON.stringify({ title: title.trim() }),
    });
    if (conversationId === state.conversationId) {
      state.conversationTitle = result.conversation.title;
      updateConversationTitle();
    }
    await refreshConversationList();
    setStatus("Conversation renamed.");
  } catch (error) {
    setStatus(`Could not rename conversation: ${error.message}`, true);
  }
}

function addAutoResize() {
  elements.userInput.addEventListener("input", () => {
    elements.userInput.style.height = "auto";
    elements.userInput.style.height = `${Math.min(elements.userInput.scrollHeight, 180)}px`;
  });
  elements.userInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      elements.chatForm.requestSubmit();
    }
  });
}

function searchHistory() {
  window.clearTimeout(state.searchTimer);
  state.searchTimer = window.setTimeout(() => {
    state.searchQuery = elements.historySearch.value.trim();
    refreshConversationList();
  }, 200);
}

document.addEventListener("DOMContentLoaded", async () => {
  setTheme(localStorage.getItem("chat-theme") || "light");
  addAutoResize();
  try {
    await fetchModels();
    renderMessages();
    updateConversationTitle();
    await refreshConversationList();
  } catch (error) {
    setStatus(`Could not initialize the app: ${error.message}`, true);
  }

  elements.newChatButton.addEventListener("click", createNewConversation);
  elements.clearChatButton.addEventListener("click", clearCurrentConversation);
  elements.deleteChatButton.addEventListener("click", () => {
    if (state.conversationId) deleteConversation(state.conversationId, state.conversationTitle);
  });
  elements.modelSelect.addEventListener("change", async () => {
    state.model = elements.modelSelect.value;
    if (!state.conversationId) return;
    try {
      const result = await apiRequest(`/api/conversations/${encodeURIComponent(state.conversationId)}`, {
        method: "PATCH",
        body: JSON.stringify({ model: state.model }),
      });
      state.conversationTitle = result.conversation.title;
      updateConversationTitle();
      await refreshConversationList();
      setStatus(`Using ${state.model} for this conversation.`);
    } catch (error) {
      setStatus(`Could not update model: ${error.message}`, true);
    }
  });
  elements.themeToggle.addEventListener("click", () => {
    setTheme(state.selectedTheme === "dark" ? "light" : "dark");
  });
  elements.chatForm.addEventListener("submit", handleSubmit);
  elements.stopGenerationButton.addEventListener("click", () => {
    if (state.abortController) state.abortController.abort();
  });
  elements.historySearch.addEventListener("input", searchHistory);
  elements.attachFilesButton.addEventListener("click", () => elements.fileInput.click());
  elements.fileInput.addEventListener("change", () => {
    const selected = [...elements.fileInput.files];
    if (state.pendingFiles.length + selected.length > 4) {
      setStatus("Attach up to 4 files at a time.", true);
      elements.fileInput.value = "";
      return;
    }
    const totalSize = state.pendingFiles.reduce((total, item) => total + item.file.size, 0)
      + selected.reduce((total, file) => total + file.size, 0);
    const oversized = selected.find((file) => file.size > 6 * 1024 * 1024);
    if (oversized) {
      setStatus(`${oversized.name} exceeds the 6 MiB per-file limit.`, true);
      elements.fileInput.value = "";
      return;
    }
    if (totalSize > 16 * 1024 * 1024) {
      setStatus("Attachments must total no more than 16 MiB per message.", true);
      elements.fileInput.value = "";
      return;
    }
    for (const file of selected) {
      state.pendingFiles.push({
        file,
        previewUrl: file.type.startsWith("image/") ? URL.createObjectURL(file) : "",
      });
    }
    renderPendingFiles();
    setStatus("");
    elements.fileInput.value = "";
  });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      createNewConversation();
      elements.userInput.focus();
    }
  });
});
