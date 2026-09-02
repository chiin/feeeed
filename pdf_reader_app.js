"use strict";

pdfjsLib.GlobalWorkerOptions.workerSrc =
    "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";

const streamParam = new URLSearchParams(window.location.search).get("stream");
const indexKind = streamParam && streamParam.toUpperCase() === "BOOKS"
    ? "books"
    : "pdf";
const streamKey = streamParam
    && !["NONE", "BOOKS"].includes(streamParam.toUpperCase())
    ? streamParam
    : null;
let activeItems = [];
let canContinue = false;
let outbox = null;
let bookId = null;
let outboxScope = null;
let syncInFlight = false;
let renderVersion = 0;
let renderedPdfId = null;

if (!streamKey) {
    loadReaderIndex(indexKind);
} else {
    loadBatch();
    setInterval(loadBatch, 60000);
    setInterval(() => flushPendingSync(), 30000);
}

async function loadReaderIndex(kind) {
    try {
        const response = await fetch(`config.json?t=${Date.now()}`, {
            cache: "no-store"
        });
        if (!response.ok) throw new Error("Configuration not found");
        const config = await response.json();
        const expectedType = kind === "books" ? "current_book" : "pdf_folder";
        const streams = Object.entries(config.streams || {})
            .filter(([, stream]) => stream.type === expectedType);
        const list = document.getElementById("readerList");
        list.replaceChildren(...streams.map(([id, stream]) => {
            const link = document.createElement("a");
            link.className = "reader-link";
            link.href = `pdf_reader.html?stream=${encodeURIComponent(id)}`;
            link.textContent = stream.feed_title || id;
            const detail = document.createElement("span");
            detail.textContent = kind === "books"
                ? `Current book: ${stream.book_id}`
                : `${stream.batch_size || 5} PDFs · ${formatStrategy(stream.strategy)}`;
            link.appendChild(detail);
            return link;
        }));
        document.querySelector("#statusScreen h2").innerText =
            kind === "books" ? "Book Readers" : "PDF Folder Readers";
        document.getElementById("statusMsg").innerText =
            streams.length ? "Choose a reader." : "No readers configured.";
        list.style.display = streams.length ? "grid" : "none";
    } catch (error) {
        showStatus(`Error loading reader list: ${error.message}`, true);
    }
}

function formatStrategy(strategy) {
    return (strategy || "sequential").replaceAll("_", " ");
}

async function loadBatch() {
    try {
        const response = await fetch(
            `cards/${streamKey}_pdf_batch.json?t=${Date.now()}`,
            { cache: "no-store" }
        );
        if (!response.ok) throw new Error("Batch file not found");
        const data = await response.json();
        configureOutbox(data.book_id || null);
        outbox.acknowledge(data.processed_event_ids);
        activeItems = ReviewerState.reconcilePdfItems(
            data.active || data.batch || [],
            outbox.events()
        );
        canContinue = Boolean(data.can_continue);
        renderCurrentState();
        flushPendingSync();
    } catch (error) {
        if (!activeItems.length) {
            showStatus(`Error loading queue: ${error.message}`, true);
        }
    }
}

function configureOutbox(nextBookId) {
    const nextScope = nextBookId ? `${streamKey}:${nextBookId}` : streamKey;
    if (outbox && outboxScope === nextScope) return;
    bookId = nextBookId;
    outboxScope = nextScope;
    outbox = new ReviewerState.DurableOutbox(
        localStorage,
        nextScope,
        undefined,
        nextBookId ? "pdf_outbox_v3" : "pdf_outbox_v2"
    );
}

function renderCurrentState() {
    if (activeItems.length) {
        showReader();
        if (renderedPdfId !== activeItems[0].id) {
            renderPDF(activeItems[0]);
        } else {
            updateProgress();
        }
        return;
    }

    renderedPdfId = null;
    const continuationPending = outbox.events().some(
        event => event.action === "continue"
    );
    if (continuationPending) {
        showStatus(
            "Requesting the next batch. This page will refresh automatically."
        );
    } else if (canContinue) {
        showStatus("That's all for this batch.", false, true);
    } else {
        showStatus("That's all for today. No additional PDFs are eligible.");
    }
}

function showReader() {
    document.getElementById("statusScreen").classList.remove("active");
    document.getElementById("mainUI").style.display = "block";
    setSyncStatus(outbox.events().length
        ? "Progress is waiting for GitHub acknowledgement."
        : "");
}

function showStatus(message, isError = false, showContinue = false) {
    document.getElementById("mainUI").style.display = "none";
    document.getElementById("statusScreen").classList.add("active");
    const statusMessage = document.getElementById("statusMsg");
    statusMessage.innerText = message;
    statusMessage.className = isError ? "error" : "muted";
    document.getElementById("continueBtn").style.display =
        showContinue ? "inline-block" : "none";
}

function updateProgress() {
    document.getElementById("batchProgress").innerText =
        `${activeItems.length} remaining`;
}

function setSyncStatus(message) {
    document.getElementById("syncStatus").innerText = message;
    document.getElementById("statusSyncStatus").innerText = message;
}

async function renderPDF(item) {
    const version = ++renderVersion;
    renderedPdfId = item.id;
    document.getElementById("docTitle").innerText = item.title;
    updateProgress();
    const viewer = document.getElementById("pdfViewer");
    viewer.innerHTML = '<p class="muted rendering">Rendering PDF...</p>';

    try {
        const pdf = await pdfjsLib.getDocument(item.pdf_url).promise;
        if (version !== renderVersion) return;
        viewer.innerHTML = "";
        for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber++) {
            const page = await pdf.getPage(pageNumber);
            if (version !== renderVersion) return;
            const viewport = page.getViewport({ scale: 2 });
            const canvas = document.createElement("canvas");
            canvas.height = viewport.height;
            canvas.width = viewport.width;
            viewer.appendChild(canvas);
            await page.render({
                canvasContext: canvas.getContext("2d"),
                viewport
            }).promise;
        }
    } catch (error) {
        if (version === renderVersion) {
            viewer.innerHTML = "";
            const message = document.createElement("p");
            message.className = "error rendering";
            message.innerText = `Failed to load PDF: ${error.message}`;
            viewer.appendChild(message);
        }
    }
}

function createEventId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function createPdfEvent(action, pdfId = null) {
    const event = {
        event_id: createEventId(),
        stream_id: streamKey,
        action,
        occurred_at: new Date().toISOString()
    };
    if (pdfId) event.pdf_id = pdfId;
    if (bookId) event.book_id = bookId;
    return event;
}

function finishCurrentPDF() {
    if (!activeItems.length) return;
    const finished = activeItems.shift();
    outbox.enqueue(createPdfEvent("complete", finished.id));
    renderedPdfId = null;
    renderCurrentState();
    window.scrollTo(0, 0);
    flushPendingSync();
}

function requestContinuation() {
    if (activeItems.length) return;
    outbox.enqueue(createPdfEvent("continue"));
    renderCurrentState();
    flushPendingSync();
}

function getPAT() {
    const token = localStorage.getItem("feeeed_pat");
    if (!token) document.getElementById("patModal").style.display = "flex";
    return token;
}

function savePATFromModal() {
    const input = document.getElementById("patInput");
    if (!input.value.trim()) return;
    localStorage.setItem("feeeed_pat", input.value.trim());
    input.value = "";
    document.getElementById("patModal").style.display = "none";
    flushPendingSync(true);
}

async function flushPendingSync(force = false) {
    if (!outbox || syncInFlight) return;
    const syncOutbox = outbox;
    const syncBookId = bookId;
    const events = syncOutbox.retryable(force);
    if (!events.length) return;
    const token = getPAT();
    if (!token) return;

    const eventIds = events.map(event => event.event_id);
    syncOutbox.markAttempt(eventIds);
    syncInFlight = true;
    try {
        const response = await fetch(
            "https://api.github.com/repos/chiin/feeeed/dispatches",
            {
                method: "POST",
                headers: {
                    "Accept": "application/vnd.github+json",
                    "Authorization": `Bearer ${token}`,
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    event_type: "pdf_batch_event",
                    client_payload: {
                        stream_id: streamKey,
                        ...(syncBookId ? { book_id: syncBookId } : {}),
                        events
                    }
                }),
                keepalive: true
            }
        );
        if (!response.ok) {
            if (response.status === 401) {
                localStorage.removeItem("feeeed_pat");
                alert("Invalid GitHub Token. Please re-enter your PAT.");
                getPAT();
            }
            throw new Error(`GitHub API returned ${response.status}`);
        }
        setSyncStatus("Progress accepted; waiting for GitHub Pages to publish.");
    } catch (error) {
        syncOutbox.markFailed(eventIds);
        console.error("[Feeeeed PDF Sync]", error);
        setSyncStatus("Sync failed. The durable outbox will retry.");
    } finally {
        syncInFlight = false;
    }
}

document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushPendingSync();
});
