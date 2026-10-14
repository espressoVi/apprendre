(() => {
    "use strict";

    const flashcard = document.getElementById("flashcard");
    const front = document.getElementById("fc-front");
    const back = document.getElementById("fc-back");

    const summary = document.getElementById("review-summary");
    const stateBadge = document.getElementById("review-state");
    const status = document.getElementById("review-status");

    const flipButton = document.getElementById("review-flip");
    const refreshButton = document.getElementById("review-refresh");
    const retryButton = document.getElementById("review-retry");
    const gradeButtons = [
        ...document.querySelectorAll("[data-rating]"),
    ];

    const labels = {
        new: "New",
        learning: "Learning",
        learnt: "Learnt · scheduled review",
    };

    let current = null;
    let flipped = false;
    let loading = false;
    let grading = false;
    let sequence = 0;
    let timer = null;

    // Retain the same request ID after an ambiguous network failure.
    let pendingSubmission = null;

    function active() {
        return document.getElementById("revise-view")
            .classList.contains("active");
    }

    function intervalLabel(seconds) {
        if (seconds < 60) return "<1 min";
        if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
        if (seconds < 86400) return `${Math.round(seconds / 3600)} h`;
        return `${Math.round(seconds / 86400)} d`;
    }

    function updateControls() {
        const busy = loading || grading;

        flipButton.disabled = busy || !current || Boolean(pendingSubmission);
        refreshButton.disabled = busy || Boolean(pendingSubmission);

        retryButton.hidden = !pendingSubmission || grading;
        retryButton.disabled = busy;

        for (const button of gradeButtons) {
            button.disabled = (
                busy ||
                !current ||
                !flipped ||
                Boolean(pendingSubmission)
            );

            const rating = button.dataset.rating;
            const label = rating[0].toUpperCase() + rating.slice(1);

            button.textContent = current
                ? `${label} · ${intervalLabel(current.intervals[rating])}`
                : label;
        }
    }

    function showCard(card) {
        current = card;
        flipped = false;
        flipButton.textContent = "Reveal answer";
        flashcard.classList.remove("flipped");
        back.replaceChildren();

        if (!card) {
            front.textContent = "All caught up for now.";
            stateBadge.textContent = "";
            stateBadge.removeAttribute("data-state");
        } else {
            front.textContent = card.front;
            LexicalDetails.render(back, card.data);

            stateBadge.textContent = labels[card.state] || card.state;
            stateBadge.dataset.state = card.state;
        }

        updateControls();
    }

    function scheduleRefresh(queue) {
        clearTimeout(timer);
        timer = null;

        if (current || queue.next_due == null) return;

        // Use a server-relative delay rather than trusting the client clock.
        const delay = Math.max(
            1000,
            (queue.next_due - queue.server_now) * 1000 + 250
        );

        timer = setTimeout(() => {
            if (active() && !pendingSubmission) loadFlashcards();
        }, Math.min(delay, 60000));
    }

    async function loadFlashcards() {
        if (grading || pendingSubmission) return;

        const token = ++sequence;

        clearTimeout(timer);
        loading = true;
        status.textContent = "";
        updateControls();

        try {
            const queue = await ArticleReader.requestJSON(
                "/api/flashcards?limit=1"
            );

            if (token !== sequence) return;

            summary.textContent =
                `${queue.counts.new} new · ` +
                `${queue.counts.learning} learning · ` +
                `${queue.counts.learnt} learnt · ` +
                `${queue.due_total} due now`;

            showCard(queue.cards[0] || null);

            if (!current && queue.next_due != null) {
                status.textContent =
                    "Next review: " +
                    new Date(queue.next_due * 1000).toLocaleString();
            } else if (!current) {
                status.textContent =
                    "Save vocabulary with the heart while reading.";
            }

            scheduleRefresh(queue);
        } catch (error) {
            if (token !== sequence) return;

            showCard(null);
            front.textContent = "Could not load due cards.";
            status.textContent = error.message;
        } finally {
            if (token === sequence) {
                loading = false;
                updateControls();
                if (current && active()) {
                    flashcard.focus({ preventScroll: true });
                }
            }
        }
    }

    function flipCard() {
        if (
            !current ||
            loading ||
            grading ||
            pendingSubmission
        ) return;

        flipped = !flipped;
        flashcard.classList.toggle("flipped", flipped);

        flipButton.textContent = flipped
            ? "Hide answer"
            : "Reveal answer";

        updateControls();
    }

    async function submitReview(rating) {
        if (loading || grading) return;

        if (!pendingSubmission) {
            if (!current || !flipped) return;

            pendingSubmission = {
                cardId: current.id,
                payload: {
                    rating,
                    expected_version: current.version,
                    request_id: crypto.randomUUID(),
                },
            };
        }

        grading = true;
        status.textContent = "Saving review…";
        updateControls();

        let succeeded = false;

        try {
            const submission = pendingSubmission;

            const result = await ArticleReader.requestJSON(
                `/api/srs/cards/${encodeURIComponent(submission.cardId)}/review`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(submission.payload),
                }
            );

            ArticleReader.updateCard?.(result.card);

            pendingSubmission = null;
            succeeded = true;
        } catch (error) {
            if (error.status === 404 || error.status === 409) {
                // A stale card cannot be submitted as a new review.
                pendingSubmission = null;
                showCard(null);
                front.textContent = "The review queue changed.";
                status.textContent =
                    `${error.message} Use “Refresh due cards”.`;
            } else {
                // The server may already have committed the review.
                // Retrying must reuse its original idempotency key.
                status.textContent =
                    `Could not confirm the review: ${error.message} ` +
                    "Use “Retry saving review”.";
            }
        } finally {
            grading = false;
            updateControls();
        }

        if (succeeded) {
            await loadFlashcards();
        }
    }

    flipButton.addEventListener("click", flipCard);
    refreshButton.addEventListener("click", loadFlashcards);
    retryButton.addEventListener("click", () => submitReview());

    for (const button of gradeButtons) {
        button.addEventListener("click", () => {
            submitReview(button.dataset.rating);
        });
    }

    flashcard.addEventListener("click", event => {
        if (
            event.target instanceof Element &&
            event.target.closest("details, button, a, input")
        ) return;

        flipCard();
    });

    document.addEventListener("keydown", event => {
        if ( !active() || event.defaultPrevented || event.repeat || event.isComposing || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) { return; }
        const target = event.target instanceof Element
            ? event.target
            : null;
    
        if ( target && ( target.isContentEditable || target.closest( "input, textarea, select, [role='textbox']"))) { return; }
        const ratings = { "1": "again", "2": "hard", "3": "good", "4": "easy", };
        const rating = ratings[event.key];
        // Number shortcuts work even while a button has focus.
        if (rating) {
            if ( !current || loading || grading || pendingSubmission) { return; }
            event.preventDefault();
            if (!flipped) {
                status.textContent = "Reveal the answer before grading the card.";
                return;
            }
            submitReview(rating);
            return;
        }
        if (event.code === "Space" || event.key === " ") {
            if (
                target?.closest(
                    "button, a[href], summary, " +
                    "[role='button'], [role='link']"
                )
            ) {
                return;
            }
    
            if (!current) return;
    
            event.preventDefault();
            flipCard();
            return;
        }
        // Enter also flips when the card container itself has focus.
        if (event.key === "Enter" && target === flashcard) {
            event.preventDefault();
            flipCard();
        }
    });

    document.addEventListener("visibilitychange", () => {
        if (
            !document.hidden &&
            active() &&
            !current &&
            !loading &&
            !grading &&
            !pendingSubmission
        ) loadFlashcards();
    });

    window.loadFlashcards = loadFlashcards;
})();
