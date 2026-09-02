"use strict";

const deckParam = new URLSearchParams(window.location.search).get("deck");
const deckId = deckParam && deckParam.toUpperCase() !== "NONE"
    ? deckParam
    : null;
const ratingNames = { 1: "again", 2: "hard", 3: "good", 4: "easy" };
let cards = [];
let outbox = null;
let waitTimer = null;
let syncInFlight = false;

if (!deckId) {
    loadDeckIndex();
} else {
    outbox = new ReviewerState.DurableOutbox(localStorage, deckId);
    loadDeck();
    setInterval(loadDeck, 60000);
    setInterval(() => flushPendingSync(), 30000);
}

async function loadDeckIndex() {
    try {
        const response = await fetch(`config.json?t=${Date.now()}`, {
            cache: "no-store"
        });
        if (!response.ok) throw new Error("Configuration not found");
        const config = await response.json();
        const decks = Object.entries(config.streams || {})
            .filter(([, stream]) => stream.type === "anki_deck");
        const list = document.getElementById("deckList");
        list.replaceChildren(...decks.map(([id, stream]) => {
            const link = document.createElement("a");
            link.className = "reader-link";
            link.href = `reviewer.html?deck=${encodeURIComponent(id)}`;
            link.textContent = stream.feed_title || id;
            const detail = document.createElement("span");
            detail.textContent = `${stream.new_cards_per_day || 0} new cards per day`;
            link.appendChild(detail);
            return link;
        }));
        if (!decks.length) {
            list.innerHTML = '<p class="muted">No Anki decks configured.</p>';
        }
        showScreen("homeScreen");
    } catch (error) {
        document.getElementById("loadingScreen").innerHTML =
            `<p class="error">Error loading deck list: ${error.message}</p>`;
    }
}

async function loadDeck() {
    try {
        const response = await fetch(`cards/${deckId}_deck.json?t=${Date.now()}`, {
            cache: "no-store"
        });
        if (!response.ok) throw new Error("Deck file not found");
        const data = await response.json();
        document.documentElement.style.setProperty(
            "--front-text-scale",
            String(data.front_text_scale || 1)
        );
        document.getElementById("deckTitle").innerText = data.title || deckId;
        outbox.acknowledge(data.processed_event_ids);
        cards = ReviewerState.reconcileCards(data.cards, outbox.events());
        renderNextCard();
        flushPendingSync();
    } catch (error) {
        if (!cards.length) {
            document.getElementById("loadingScreen").innerHTML =
                `<p class="error">Error loading deck: ${error.message}</p>`;
        }
    }
}

function showScreen(id) {
    document.querySelectorAll(".screen").forEach(
        screen => screen.classList.remove("active")
    );
    document.getElementById(id).classList.add("active");
}

function availableTime(card) {
    const parsed = Date.parse(card.available_at);
    return Number.isNaN(parsed) ? 0 : parsed;
}

function renderNextCard() {
    clearTimeout(waitTimer);
    if (!cards.length) {
        showCompletion("All Caught Up!", "No cards are currently due.");
        return;
    }

    const now = Date.now();
    const readyIndex = cards.findIndex(card => availableTime(card) <= now);
    if (readyIndex < 0) {
        const nextTime = Math.min(...cards.map(availableTime));
        const seconds = Math.max(1, Math.ceil((nextTime - now) / 1000));
        showCompletion(
            "Waiting for Again",
            `The next card returns in ${Math.ceil(seconds / 60)} minute(s).`
        );
        waitTimer = setTimeout(renderNextCard, Math.min(seconds * 1000, 30000));
        return;
    }

    if (readyIndex > 0) {
        cards.unshift(cards.splice(readyIndex, 1)[0]);
    }
    const card = cards[0];
    showScreen("reviewScreen");
    document.getElementById("progress").innerText = `${cards.length} remaining`;
    document.getElementById("answerBox").style.display = "none";
    document.getElementById("showBtn").style.display = "block";
    document.getElementById("gradeGrid").style.display = "none";
    document.getElementById("frontText").innerText = card.front.text || "";
    document.getElementById("backText").innerText = card.back.text || "";
    document.getElementById("backNotes").innerText = card.back.notes || "";
    renderMedia("front", card.front);
    renderMedia("back", card.back);

    const previews = card.schedule_previews || {};
    ["again", "hard", "good", "easy"].forEach((rating, index) => {
        document.getElementById(`int-${index + 1}`).innerText =
            previews[rating] || (rating === "again" ? "10m" : "");
    });
}

function renderMedia(side, content) {
    const image = document.getElementById(`${side}Image`);
    image.style.display = content.image ? "block" : "none";
    if (content.image) image.src = content.image;
    const audioButton = document.getElementById(`${side}AudioBtn`);
    audioButton.style.display = content.audio ? "inline-block" : "none";
    if (content.audio) document.getElementById(`${side}Audio`).src = content.audio;
}

function showCompletion(title, message) {
    showScreen("finishScreen");
    document.getElementById("finishTitle").innerText = title;
    document.getElementById("finishMessage").innerText = message;
    document.getElementById("syncBtn").style.display =
        outbox.events().length ? "block" : "none";
}

function showAnswer() {
    document.getElementById("answerBox").style.display = "block";
    document.getElementById("showBtn").style.display = "none";
    document.getElementById("gradeGrid").style.display = "grid";
    if (cards[0].back.audio) playAudio("backAudio");
}

function playAudio(elementId) {
    const audio = document.getElementById(elementId);
    if (audio && audio.src) audio.play().catch(() => {});
}

function createEventId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function gradeCard(grade) {
    const card = cards.shift();
    const rating = ratingNames[grade];
    const reviewedAt = new Date().toISOString();
    outbox.enqueue(
        ReviewerState.createReviewEvent(
            deckId, card.id, rating, reviewedAt, createEventId()
        )
    );

    if (rating === "again") {
        cards.push({
            ...card,
            available_at: new Date(
                Date.parse(reviewedAt) + 10 * 60 * 1000
            ).toISOString()
        });
    }
    renderNextCard();
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
    if (syncInFlight) return;
    const events = outbox.retryable(force);
    if (!events.length) return;
    const token = getPAT();
    if (!token) return;

    const eventIds = events.map(event => event.event_id);
    outbox.markAttempt(eventIds);
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
                    event_type: "anki_review",
                    client_payload: { deck_id: deckId, events }
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
        document.getElementById("syncStatus").innerText =
            "Events accepted; waiting for GitHub Pages to publish.";
    } catch (error) {
        outbox.markFailed(eventIds);
        console.error("[Feeeeed Sync]", error);
        document.getElementById("syncStatus").innerText =
            "Sync failed. The durable outbox will retry.";
    } finally {
        syncInFlight = false;
    }
}

function syncWithGitHub() {
    flushPendingSync(true);
}

document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushPendingSync();
});
