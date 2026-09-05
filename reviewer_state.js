(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    } else {
        root.ReviewerState = api;
    }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
    "use strict";

    const RETRY_DELAY_MS = 2 * 60 * 1000;

    function parseStoredArray(storage, key) {
        const raw = storage.getItem(key);
        if (!raw) return [];
        try {
            const parsed = JSON.parse(raw);
            return Array.isArray(parsed) ? parsed : [];
        } catch (error) {
            console.warn(`[Feeeeed Sync] Resetting invalid local data at ${key}.`, error);
            storage.removeItem(key);
            return [];
        }
    }

    class DurableOutbox {
        constructor(
            storage,
            deckId,
            now = () => Date.now(),
            keyPrefix = "anki_outbox_v2"
        ) {
            this.storage = storage;
            this.deckId = deckId;
            this.now = now;
            this.key = `${keyPrefix}_${deckId}`;
            this.records = parseStoredArray(storage, this.key);
        }

        save() {
            this.storage.setItem(this.key, JSON.stringify(this.records));
        }

        mergeStored() {
            const merged = new Map(
                this.records.map(record => [record.event.event_id, record])
            );
            parseStoredArray(this.storage, this.key).forEach(record => {
                const existing = merged.get(record.event.event_id);
                if (
                    !existing
                    || (!existing.last_attempt_at && record.last_attempt_at)
                ) {
                    merged.set(record.event.event_id, record);
                }
            });
            this.records = [...merged.values()];
        }

        events() {
            this.mergeStored();
            return this.records.map(record => record.event);
        }

        enqueue(event) {
            this.mergeStored();
            if (!this.records.some(record => record.event.event_id === event.event_id)) {
                this.records.push({ event, last_attempt_at: null });
                this.save();
            }
        }

        acknowledge(eventIds) {
            this.mergeStored();
            const acknowledged = new Set(eventIds || []);
            const retained = this.records.filter(
                record => !acknowledged.has(record.event.event_id)
            );
            if (retained.length !== this.records.length) {
                this.records = retained;
                this.save();
            }
        }

        retryable(force = false) {
            this.mergeStored();
            const cutoff = this.now() - RETRY_DELAY_MS;
            const shouldSend = force || this.records.some(record =>
                !record.last_attempt_at
                || Date.parse(record.last_attempt_at) <= cutoff
            );
            return shouldSend
                ? this.records.map(record => record.event)
                : [];
        }

        markAttempt(eventIds) {
            this.mergeStored();
            const attempted = new Set(eventIds);
            const timestamp = new Date(this.now()).toISOString();
            this.records.forEach(record => {
                if (attempted.has(record.event.event_id)) {
                    record.last_attempt_at = timestamp;
                }
            });
            this.save();
        }

        markFailed(eventIds) {
            this.mergeStored();
            const failed = new Set(eventIds);
            this.records.forEach(record => {
                if (failed.has(record.event.event_id)) {
                    record.last_attempt_at = null;
                }
            });
            this.save();
        }
    }

    function createReviewEvent(deckId, cardId, rating, reviewedAt, eventId) {
        return {
            deck_id: deckId,
            card_id: cardId,
            rating,
            reviewed_at: reviewedAt,
            event_id: eventId
        };
    }

    function reconcileCards(serverCards, pendingEvents) {
        const cards = (serverCards || []).map(card => ({ ...card }));
        const sortedEvents = [...(pendingEvents || [])].sort(
            (left, right) => Date.parse(left.reviewed_at) - Date.parse(right.reviewed_at)
        );

        sortedEvents.forEach(event => {
            const index = cards.findIndex(card =>
                card.id === event.card_id
                && (!card.deck_id || card.deck_id === event.deck_id)
            );
            if (index < 0) return;
            const card = cards[index];
            if (Date.parse(card.available_at) > Date.parse(event.reviewed_at)) return;
            cards.splice(index, 1);
            if (event.rating === "again") {
                cards.push({
                    ...card,
                    available_at: new Date(
                        Date.parse(event.reviewed_at) + 10 * 60 * 1000
                    ).toISOString()
                });
            }
        });
        return cards;
    }

    function groupReviewEventsByDeck(events) {
        const groups = new Map();
        (events || []).forEach(event => {
            if (!event || typeof event.deck_id !== "string" || !event.deck_id) {
                throw new Error("Review event is missing its deck ID.");
            }
            if (!groups.has(event.deck_id)) groups.set(event.deck_id, []);
            groups.get(event.deck_id).push(event);
        });
        return groups;
    }

    function reconcilePdfItems(serverItems, pendingEvents) {
        const completedIds = new Set(
            (pendingEvents || [])
                .filter(event => event.action === "complete")
                .map(event => event.pdf_id)
        );
        return (serverItems || [])
            .filter(item => !completedIds.has(item.id))
            .map(item => ({ ...item }));
    }

    return {
        DurableOutbox,
        RETRY_DELAY_MS,
        createReviewEvent,
        groupReviewEventsByDeck,
        reconcileCards,
        reconcilePdfItems
    };
});
