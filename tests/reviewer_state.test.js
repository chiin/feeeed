"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
    DurableOutbox,
    RETRY_DELAY_MS,
    createReviewEvent,
    reconcileCards
} = require("../reviewer_state.js");

class MemoryStorage {
    constructor() {
        this.values = new Map();
    }
    getItem(key) {
        return this.values.has(key) ? this.values.get(key) : null;
    }
    setItem(key, value) {
        this.values.set(key, value);
    }
    removeItem(key) {
        this.values.delete(key);
    }
}

test("durable outbox survives reload and retries after a network failure", () => {
    const storage = new MemoryStorage();
    let now = Date.parse("2026-08-30T02:00:00Z");
    const event = createReviewEvent(
        "hsk", "hsk-1", "good", new Date(now).toISOString(), "event-1"
    );
    const first = new DurableOutbox(storage, "hsk", () => now);
    first.enqueue(event);
    first.markAttempt(["event-1"]);

    const reloaded = new DurableOutbox(storage, "hsk", () => now);
    assert.equal(reloaded.events().length, 1);
    assert.equal(reloaded.retryable().length, 0);
    reloaded.markFailed(["event-1"]);
    assert.equal(reloaded.retryable().length, 1);

    reloaded.markAttempt(["event-1"]);
    now += RETRY_DELAY_MS;
    assert.equal(reloaded.retryable().length, 1);
});

test("acknowledged events are removed from the durable outbox", () => {
    const storage = new MemoryStorage();
    const outbox = new DurableOutbox(storage, "hsk");
    outbox.enqueue(createReviewEvent(
        "hsk", "hsk-1", "good", "2026-08-30T02:00:00Z", "event-1"
    ));
    outbox.acknowledge(["event-1"]);
    assert.deepEqual(outbox.events(), []);
});

test("concurrent tabs merge events instead of overwriting them", () => {
    const storage = new MemoryStorage();
    const firstTab = new DurableOutbox(storage, "hsk");
    const secondTab = new DurableOutbox(storage, "hsk");
    firstTab.enqueue(createReviewEvent(
        "hsk", "hsk-1", "good", "2026-08-30T02:00:00Z", "event-1"
    ));
    secondTab.enqueue(createReviewEvent(
        "hsk", "hsk-2", "easy", "2026-08-30T02:01:00Z", "event-2"
    ));
    assert.deepEqual(
        firstTab.events().map(event => event.event_id).sort(),
        ["event-1", "event-2"]
    );
});

test("local reconciliation removes ratings and delays Again by ten minutes", () => {
    const cards = [{
        id: "hsk-1",
        available_at: "2026-08-30T02:00:00Z",
        front: {},
        back: {}
    }];
    const again = createReviewEvent(
        "hsk", "hsk-1", "again", "2026-08-30T02:01:00Z", "again-1"
    );
    const delayed = reconcileCards(cards, [again]);
    assert.equal(delayed.length, 1);
    assert.equal(delayed[0].available_at, "2026-08-30T02:11:00.000Z");

    const good = createReviewEvent(
        "hsk", "hsk-1", "good", "2026-08-30T02:11:00Z", "good-1"
    );
    assert.deepEqual(reconcileCards(cards, [again, good]), []);
});
