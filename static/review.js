(() => {
    "use strict";

    const ids = [
        "flashcard", "fc-front", "fc-back",
        "review-summary", "review-state", "review-status",
        "review-flip", "review-refresh", "review-retry",
        "revise-view",
    ];

    const elements = Object.fromEntries(
        ids.map(id => [id, document.getElementById(id)])
    );

    for (const [id, element] of Object.entries(elements)) {
        if (!element) throw new Error(`Missing review element: ${id}`);
    }

    if (!window.AppSession) {
        throw new Error("Session client did not initialize");
    }

    const flashcard = elements.flashcard;
    const front = elements["fc-front"];
    const back = elements["fc-back"];
    const summary = elements["review-summary"];
    const stateBadge = elements["review-state"];
    const status = elements["review-status"];
    const flipButton = elements["review-flip"];
    const refreshButton = elements["review-refresh"];
    const retryButton = elements["review-retry"];
    const gradeButtons = [
        ...document.querySelectorAll("[data-rating]"),
    ];

    const labels = {
        new: "New",
        learning: "Learning",
        learnt: "Learnt · scheduled review",
    };

    const storageKey =
        `apprendre:pending-review:${AppSession.userId}`;

    let current = null;
    let flipped = false;
    let loading = false;
    let grading = false;
    let timer = null;
    let pendingSubmission = null;

    try {
        pendingSubmission = JSON.parse(
            sessionStorage.getItem(storageKey) || "null"
        );

        if (
            pendingSubmission &&
            (
                typeof pendingSubmission.cardId !== "string" ||
                !pendingSubmission.payload ||
                typeof pendingSubmission.payload.request_id !== "string" ||
                !["again", "hard", "good", "easy"].includes(
                    pendingSubmission.payload.rating
                ) ||
                !Number.isInteger(
                    pendingSubmission.payload.expected_version
                )
            )
        ) {
            throw new Error("Invalid pending review");
        }
    } catch {
        pendingSubmission = null;
        try {
            sessionStorage.removeItem(storageKey);
        } catch {
            // Storage may be unavailable.
        }
    }

    function persistPending() {
        if (pendingSubmission) {
            // If this fails, do not send a new review: its retry identity
            // must be recoverable after a reload.
            sessionStorage.setItem(
                storageKey,
                JSON.stringify(pendingSubmission)
            );
        } else {
            try {
                sessionStorage.removeItem(storageKey);
            } catch {
                // A stale saved request remains safe to replay.
            }
        }
    }

    function active() {
        return elements["revise-view"].classList.contains("active");
    }

    function intervalLabel(seconds) {
        if (seconds < 60) return "<1 min";
        if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
        if (seconds < 86400) return `${Math.round(seconds / 3600)} h`;
        return `${Math.round(seconds / 86400)} d`;
    }

    function updateControls() {
        const busy = loading || grading;

        flipButton.disabled =
            busy || !current || Boolean(pendingSubmission);
        refreshButton.disabled = busy || Boolean(pendingSubmission);
        retryButton.hidden = !pendingSubmission || grading;
        retryButton.disabled = busy;

        for (const button of gradeButtons) {
            const rating = button.dataset.rating;
            const label = rating[0].toUpperCase() + rating.slice(1);

            button.disabled = (
                busy || !current || !flipped || Boolean(pendingSubmission)
            );

            button.textContent = current
                ? `${label} · ${intervalLabel(current.intervals[rating])}`
                : label;
        }
    }

    function showCard(card) {
        // Render before making this card gradable.
        const fragment = document.createDocumentFragment();

        if (card) {
            if (
                typeof card.id !== "string" ||
                typeof card.front !== "string" ||
                !Number.isInteger(card.version) ||
                !card.data ||
                !card.intervals ||
                !["again", "hard", "good", "easy"].every(
                    rating => Number.isFinite(card.intervals[rating])
                )
            ) {
                throw new Error("Server returned an invalid review card");
            }

            if (!window.LexicalDetails) {
                throw new Error("Vocabulary renderer is unavailable");
            }

            LexicalDetails.render(fragment, card.data);
        }

        current = card;
        flipped = false;
        flashcard.classList.remove("flipped");
        flipButton.textContent = "Reveal answer";
        back.replaceChildren(fragment);

        front.textContent = card
            ? card.front
            : "All caught up for now.";

        stateBadge.textContent = card
            ? labels[card.state] || card.state
            : "";

        if (card) {
            stateBadge.dataset.state = card.state;
        } else {
            stateBadge.removeAttribute("data-state");
        }

        updateControls();
    }

    function scheduleRefresh(queue) {
        clearTimeout(timer);
        timer = null;

        if (current || queue.next_due == null) return;

        const delay = Math.max(
            1000,
            (queue.next_due - queue.server_now) * 1000 + 250
        );

        timer = setTimeout(() => {
            if (active() && !pendingSubmission) {
                void loadFlashcards();
            }
        }, Math.min(delay, 60000));
    }

    async function loadFlashcards() {
        if (loading || grading) return;

        if (pendingSubmission) {
            front.textContent = "A previous review needs confirmation.";
            status.textContent =
                "Use “Retry saving review” to confirm it safely.";
            updateControls();
            return;
        }

        clearTimeout(timer);
        loading = true;

        try {
            status.textContent = "Loading due cards…";
            updateControls();

            const queue = await AppSession.requestJSON(
                "/api/flashcards?limit=1"
            );

            if (
                !Array.isArray(queue.cards) ||
                !queue.counts ||
                !["new", "learning", "learnt"].every(
                    key => Number.isFinite(queue.counts[key])
                ) ||
                !Number.isFinite(queue.due_total) ||
                !Number.isFinite(queue.server_now) ||
                (
                    queue.next_due !== null &&
                    !Number.isFinite(queue.next_due)
                )
            ) {
                throw new Error("Server returned an invalid review queue");
            }

            showCard(queue.cards[0] || null);

            summary.textContent =
                `${queue.counts.new} new · ` +
                `${queue.counts.learning} learning · ` +
                `${queue.counts.learnt} learnt · ` +
                `${queue.due_total} due now`;

            status.textContent = "";

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
            console.error("Could not load reviews", error);
            current = null;
            flipped = false;
            flashcard.classList.remove("flipped");
            back.replaceChildren();
            stateBadge.textContent = "";
            stateBadge.removeAttribute("data-state");
            front.textContent = "Could not load due cards.";
            status.textContent = error.message;
        } finally {
            loading = false;
            updateControls();

            if (current && active()) {
                flashcard.focus({ preventScroll: true });
            }
        }
    }

    function flipCard() {
        if (!current || loading || grading || pendingSubmission) return;

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

            if (typeof crypto.randomUUID !== "function") {
                status.textContent =
                    "Reviews require HTTPS. Open the HTTPS proxy URL.";
                return;
            }

            pendingSubmission = {
                cardId: current.id,
                payload: {
                    rating,
                    expected_version: current.version,
                    request_id: crypto.randomUUID(),
                },
            };

            try {
                persistPending();
            } catch {
                pendingSubmission = null;
                status.textContent =
                    "Browser session storage is unavailable. " +
                    "Enable it before reviewing.";
                updateControls();
                return;
            }
        }

        grading = true;
        status.textContent = "Saving review…";
        updateControls();

        let confirmed = false;

        try {
            const submission = pendingSubmission;

            const result = await AppSession.requestJSON(
                `/api/srs/cards/${encodeURIComponent(submission.cardId)}/review`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(submission.payload),
                }
            );

            if (!result.card || typeof result.card.id !== "string") {
                throw new Error("Invalid review confirmation");
            }

            pendingSubmission = null;
            persistPending();
            confirmed = true;

            // Rendering is not part of confirming the database write.
            try {
                window.ArticleReader?.updateCard?.(result.card);
            } catch (error) {
                console.error("Reader update failed after saved review", error);
            }
        } catch (error) {
            if (error.status === 404 || error.status === 422 || error.status === 409) {
                pendingSubmission = null;
                persistPending();
                current = null;
                front.textContent = "The pending review cannot be applied.";
                status.textContent =
                    `${error.message} Use “Refresh due cards”.`;
            } else if (error.status === 412) {
                // Preserve the request on a possible account-switch
                // conflict. Reloading restores the correct page identity.
                status.textContent =
                    `${error.message} Reload the page, then retry if offered.`;
            } else {
                status.textContent =
                    `Could not confirm the review: ${error.message} ` +
                    "Use “Retry saving review”.";
            }
        } finally {
            grading = false;
            updateControls();
        }

        if (confirmed) await loadFlashcards();
    }

    flipButton.addEventListener("click", flipCard);
    refreshButton.addEventListener("click", () => void loadFlashcards());
    retryButton.addEventListener("click", () => void submitReview());

    for (const button of gradeButtons) {
        button.addEventListener("click", () => {
            void submitReview(button.dataset.rating);
        });
    }

    flashcard.addEventListener("click", event => {
        if (
            event.target instanceof Element &&
            event.target.closest("details, summary, button, a, input")
        ) return;

        flipCard();
    });

    document.addEventListener("keydown", event => {
        if (
            !active() ||
            event.defaultPrevented ||
            event.repeat ||
            event.isComposing ||
            event.ctrlKey ||
            event.metaKey ||
            event.altKey ||
            event.shiftKey
        ) return;

        const target = event.target instanceof Element
            ? event.target
            : null;

        if (
            target &&
            (
                target.isContentEditable ||
                target.closest("input, textarea, select, [role='textbox']")
            )
        ) return;

        const rating = {
            "1": "again",
            "2": "hard",
            "3": "good",
            "4": "easy",
        }[event.key];

        if (rating) {
            if (!current || loading || grading || pendingSubmission) return;

            event.preventDefault();

            if (!flipped) {
                status.textContent =
                    "Reveal the answer before grading the card.";
                return;
            }

            void submitReview(rating);
            return;
        }

        if (event.code === "Space" || event.key === " ") {
            if (
                target?.closest(
                    "button, a[href], summary, [role='button'], [role='link']"
                )
            ) return;

            if (!current) return;

            event.preventDefault();
            flipCard();
        }

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
        ) {
            void loadFlashcards();
        }
    });

    window.loadFlashcards = loadFlashcards;
    updateControls();
})();
