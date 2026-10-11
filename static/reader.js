(() => {
    "use strict";

    // ------------------------------------------------------------
    // Elements
    // ------------------------------------------------------------

    const body = document.getElementById("read-body");
    const popup = document.getElementById("lexical-popup");

    const title = document.getElementById("lexical-popup-title");
    const canonical = document.getElementById(
        "lexical-popup-canonical"
    );
    const srsBadge = document.getElementById(
        "lexical-popup-srs-state"
    );

    const heart = document.getElementById("lexical-popup-heart");
    const saveStatus = document.getElementById(
        "lexical-popup-status"
    );
    const saveRetry = document.getElementById(
        "lexical-popup-retry"
    );

    const lookupStatus = document.getElementById(
        "lexical-popup-lookup-status"
    );
    const lookupRetry = document.getElementById(
        "lexical-popup-lookup-retry"
    );
    const details = document.getElementById(
        "lexical-popup-details"
    );

    const closeButton = document.getElementById(
        "lexical-popup-close"
    );

    const requiredElements = {
        "read-body": body,
        "lexical-popup": popup,
        "lexical-popup-title": title,
        "lexical-popup-canonical": canonical,
        "lexical-popup-srs-state": srsBadge,
        "lexical-popup-heart": heart,
        "lexical-popup-status": saveStatus,
        "lexical-popup-retry": saveRetry,
        "lexical-popup-lookup-status": lookupStatus,
        "lexical-popup-lookup-retry": lookupRetry,
        "lexical-popup-details": details,
        "lexical-popup-close": closeButton,
    };

    for (const [id, element] of Object.entries(requiredElements)) {
        if (!element) {
            throw new Error(
                `Reader initialization failed: missing #${id}.`
            );
        }
    }

    // ------------------------------------------------------------
    // State
    // ------------------------------------------------------------

    let article = null;
    let selected = null;
    let generation = 0;
    let enrichment = null;

    const segmentsById = new Map();

    // Completed and in-flight meaning requests.
    // Persistent caching is handled separately by the backend.
    const meanings = new Map();
    const MAX_MEANINGS = 200;

    // Explicit save attempts.
    const saves = new Map();

    // Latest known scheduling information, keyed by lookup identity.
    // This also protects against an older article response overwriting
    // a state update that arrived from a completed review.
    const knownSRS = new Map();

    const srsLabels = {
        new: "New",
        learning: "Learning",
        learnt: "Learnt · scheduled review",
    };

    // ------------------------------------------------------------
    // HTTP
    // ------------------------------------------------------------

    async function requestJSON(url, options = {}) {
        const response = await fetch(url, options);
        const text = await response.text();

        let data;

        try {
            data = JSON.parse(text);
        } catch {
            const error = new Error(
                response.ok
                    ? "The server returned an invalid JSON response."
                    : `Request failed (${response.status})`
            );

            error.status = response.status;
            throw error;
        }

        if (!response.ok) {
            const error = new Error(
                typeof data?.detail === "string"
                    ? data.detail
                    : `Request failed (${response.status})`
            );

            // Used by review.js to distinguish stale-card conflicts
            // from ambiguous network failures.
            error.status = response.status;
            throw error;
        }

        return data;
    }

    function occurrencePayload(sourceArticle, segment) {
        return {
            article_id: sourceArticle.unique_id,
            segment_id: segment.id,
            segmentation_version: sourceArticle.segmentation_version,
        };
    }

    // ------------------------------------------------------------
    // Selection and popup positioning
    // ------------------------------------------------------------

    function selectedSegment() {
        if (!selected) return null;

        return segmentsById.get(
            Number(selected.dataset.segmentId)
        ) || null;
    }

    function isCurrent(segment, token) {
        return (
            token === generation &&
            !popup.hidden &&
            selectedSegment()?.lookup_key === segment.lookup_key
        );
    }

    function close(restoreFocus = false) {
        const previous = selected;

        // Invalidate UI updates from older asynchronous operations.
        generation += 1;

        if (previous) {
            previous.classList.remove("is-selected");
            previous.setAttribute("aria-expanded", "false");
        }

        selected = null;
        enrichment = null;
        popup.hidden = true;

        if (restoreFocus && previous?.isConnected) {
            previous.focus({ preventScroll: true });
        }
    }

    function positionPopup() {
        if (!selected || popup.hidden) return;

        const anchor = selected.getBoundingClientRect();
        const rect = popup.getBoundingClientRect();
        const margin = 12;

        const left = Math.max(
            margin,
            Math.min(
                anchor.left,
                window.innerWidth - rect.width - margin
            )
        );

        let top = anchor.bottom + 8;

        if (top + rect.height > window.innerHeight - margin) {
            top = anchor.top - rect.height - 8;
        }

        top = Math.max(
            margin,
            Math.min(
                top,
                window.innerHeight - rect.height - margin
            )
        );

        popup.style.left = `${left}px`;
        popup.style.top = `${top}px`;
    }

    // ------------------------------------------------------------
    // SRS state display
    // ------------------------------------------------------------

    function scheduleVersion(value) {
        return Number.isInteger(value?.version)
            ? value.version
            : -1;
    }

    function rememberSRS(key, information) {
        if (!key || !information) return;

        const previous = knownSRS.get(key);

        if (
            previous &&
            scheduleVersion(previous) > scheduleVersion(information)
        ) {
            return;
        }

        knownSRS.set(key, {
            id: information.id,
            lookup_key: key,
            state: information.state,
            due_at: information.due_at,
            version: information.version,
            review_count: information.review_count,
            lapses: information.lapses,
        });
    }

    function syncSegmentSRS(segment) {
        const key = segment.lookup_key;
        if (!key) return;

        if (segment.srs) {
            rememberSRS(key, segment.srs);
        }

        const known = knownSRS.get(key);

        if (
            known &&
            scheduleVersion(known) >= scheduleVersion(segment.srs)
        ) {
            segment.srs = { ...known };
            segment.in_srs = true;
        }
    }

    function dueDescription(information) {
        if (!Number.isFinite(information?.due_at)) return "";

        return (
            "Next review: " +
            new Date(information.due_at * 1000).toLocaleString()
        );
    }

    function decorateSRS(button, segment) {
        const state = segment.srs?.state;

        if (!state) {
            delete button.dataset.srsState;
            button.title = segment.in_srs
                ? "In your learning list"
                : "Not in your learning list";
            return;
        }

        button.dataset.srsState = state;

        const label = srsLabels[state] || state;
        const due = dueDescription(segment.srs);

        button.title = due ? `${label}. ${due}` : label;
    }

    function refreshSRSBadge() {
        const segment = selectedSegment();
        if (!segment || popup.hidden) return;

        const state = segment.srs?.state;

        if (state) {
            srsBadge.textContent = srsLabels[state] || state;
            srsBadge.dataset.state = state;
            srsBadge.title = dueDescription(segment.srs);
        } else {
            srsBadge.textContent = segment.in_srs
                ? "In learning list"
                : "Not in learning list";

            delete srsBadge.dataset.state;
            srsBadge.removeAttribute("title");
        }
    }

    function updateCard(card) {
        if (!card?.lookup_key) return;

        const key = card.lookup_key;

        // Record updates even if no article is currently displayed.
        rememberSRS(key, card);

        const save = saves.get(key);
        if (save) {
            save.saved = true;
            save.error = null;
        }

        if (article) {
            for (const segment of article.segments) {
                if (segment.lookup_key !== key) continue;

                syncSegmentSRS(segment);
                segment.in_srs = true;

                const button = body.querySelector(
                    `[data-segment-id="${segment.id}"]`
                );

                if (button) {
                    decorateSRS(button, segment);
                }
            }
        }

        refreshSaveUI();
    }

    // ------------------------------------------------------------
    // Heart and saving status
    // ------------------------------------------------------------

    function saveState(segment) {
        const key = segment.lookup_key;

        if (!saves.has(key)) {
            saves.set(key, {
                saved: Boolean(
                    segment.in_srs || segment.srs || knownSRS.has(key)
                ),
                pending: false,
                error: null,
                created: null,
            });
        }

        const state = saves.get(key);

        if (segment.in_srs || segment.srs || knownSRS.has(key)) {
            state.saved = true;
            state.error = null;
        }

        return state;
    }

    function refreshSaveUI() {
        const segment = selectedSegment();
        if (!segment || popup.hidden) return;

        const state = saveState(segment);
        const ready = Boolean(
            segment.lookup_key &&
            enrichment?.lookup_key === segment.lookup_key
        );

        // Fill immediately while saving, then revert on failure.
        const filled = state.saved || state.pending;

        heart.classList.toggle("is-saved", filled);
        heart.setAttribute("aria-pressed", String(filled));
        heart.setAttribute("aria-busy", String(state.pending));
        heart.disabled = state.pending || state.saved || !ready;

        // The SVG is controlled by CSS; never replace its contents.
        const label = state.pending
            ? "Adding to learning list"
            : state.saved
                ? "Already in learning list"
                : ready
                    ? "Add to learning list"
                    : "Load the meaning before saving";

        heart.setAttribute("aria-label", label);
        heart.title = label;

        saveRetry.hidden = (
            !state.error ||
            state.saved ||
            state.pending ||
            !ready
        );

        if (state.pending) {
            saveStatus.textContent = "Adding to your learning list…";
        } else if (state.saved) {
            saveStatus.textContent = state.created
                ? "Added as a new learning card."
                : "Already in your learning list.";
        } else if (state.error) {
            saveStatus.textContent = `Not saved: ${state.error}`;
        } else {
            saveStatus.textContent = ready
                ? "Click the heart to save this card."
                : "";
        }

        refreshSRSBadge();
        positionPopup();
    }

    // ------------------------------------------------------------
    // Meaning lookup and browser cache
    // ------------------------------------------------------------

    function trimMeanings() {
        if (meanings.size <= MAX_MEANINGS) return;

        for (const [key, entry] of meanings) {
            if (meanings.size <= MAX_MEANINGS) break;

            // Keep pending requests so reopening them does not
            // create duplicate requests.
            if (entry.settled) {
                meanings.delete(key);
            }
        }
    }

    function lookupMeaning(sourceArticle, segment) {
        const key = segment.lookup_key;
        const existing = meanings.get(key);

        if (existing) {
            return existing.promise;
        }

        const entry = {
            settled: false,
            promise: null,
        };

        entry.promise = requestJSON("/api/lexical/lookup", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify(
                occurrencePayload(sourceArticle, segment)
            ),
        }).then(result => {
            if (result?.lookup_key !== key) {
                throw new Error(
                    "Vocabulary changed; reload the article."
                );
            }

            if (!result.data || typeof result.data !== "object") {
                throw new Error(
                    "The server returned incomplete lexical data."
                );
            }

            return result;
        }).catch(error => {
            // Failed lookups must remain retryable.
            if (meanings.get(key) === entry) {
                meanings.delete(key);
            }

            throw error;
        }).finally(() => {
            entry.settled = true;
            trimMeanings();
        });

        meanings.set(key, entry);

        return entry.promise;
    }

    async function loadMeaning(sourceArticle, segment, token) {
        if (!isCurrent(segment, token)) return;

        enrichment = null;

        details.replaceChildren();
        details.setAttribute("aria-busy", "true");

        lookupStatus.textContent = "Looking up meaning and grammar…";
        lookupRetry.hidden = true;

        refreshSaveUI();

        try {
            if (!window.LexicalDetails?.render) {
                throw new Error(
                    "The lexical renderer is missing. " +
                    "Load /static/lexical-details.js before reader.js."
                );
            }

            const result = await lookupMeaning(sourceArticle, segment);

            // Never display an older response in another word's popup.
            if (!isCurrent(segment, token)) return;

            window.LexicalDetails.render(details, result.data);

            // Allow saving only once the result renders successfully.
            enrichment = result;
            canonical.textContent = `spaCy: ${result.spacy_pos}`;
            lookupStatus.textContent = "";
        } catch (error) {
            if (!isCurrent(segment, token)) return;

            enrichment = null;
            details.replaceChildren();

            lookupStatus.textContent =
                `Could not load meaning: ${error.message}`;

            lookupRetry.hidden = false;
        } finally {
            if (isCurrent(segment, token)) {
                details.setAttribute("aria-busy", "false");
                refreshSaveUI();
                positionPopup();
            }
        }
    }

    // ------------------------------------------------------------
    // Explicit saving to SRS
    // ------------------------------------------------------------

    async function saveSegment(sourceArticle, segment, token) {
        if (!isCurrent(segment, token)) return;

        if (enrichment?.lookup_key !== segment.lookup_key) {
            return;
        }

        const state = saveState(segment);

        if (state.pending || state.saved) {
            return;
        }

        state.pending = true;
        state.error = null;

        refreshSaveUI();

        try {
            const result = await requestJSON("/api/srs/cards", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify(
                    occurrencePayload(sourceArticle, segment)
                ),
            });

            if (
                !result?.card ||
                result.card.lookup_key !== segment.lookup_key
            ) {
                throw new Error(
                    "The server did not confirm the saved card."
                );
            }

            state.saved = true;
            state.created = Boolean(result.created);

            // Update the captured article too, even if the reader
            // navigated to a different article during the request.
            rememberSRS(segment.lookup_key, result.card);

            for (const occurrence of sourceArticle.segments) {
                if (occurrence.lookup_key !== segment.lookup_key) continue;

                occurrence.in_srs = true;
                syncSegmentSRS(occurrence);
            }

            // Update visible words, the popup badge, and known schedule.
            updateCard(result.card);
        } catch (error) {
            state.error = error.message;
        } finally {
            state.pending = false;

            // Refresh the currently open popup, not an old one.
            refreshSaveUI();
        }
    }

    // ------------------------------------------------------------
    // Open vocabulary popup
    // ------------------------------------------------------------

    function open(button) {
        if (selected === button) {
            close();
            return;
        }

        close();

        selected = button;
        selected.classList.add("is-selected");
        selected.setAttribute("aria-expanded", "true");

        const sourceArticle = article;
        const segment = selectedSegment();
        const token = generation;

        if (!segment) {
            close();
            return;
        }

        syncSegmentSRS(segment);
        enrichment = null;

        title.textContent = segment.text;
        canonical.textContent = segment.canonical || "";

        details.replaceChildren();
        details.setAttribute("aria-busy", "false");

        lookupStatus.textContent = "";
        lookupRetry.hidden = true;

        saveStatus.textContent = "";
        saveRetry.hidden = true;

        popup.hidden = false;
        popup.scrollTop = 0;

        heart.onclick = () => {
            saveSegment(sourceArticle, segment, token);
        };

        saveRetry.onclick = () => {
            saveSegment(sourceArticle, segment, token);
        };

        lookupRetry.onclick = () => {
            loadMeaning(sourceArticle, segment, token);
        };

        refreshSaveUI();

        if (!segment.lookup_key) {
            lookupStatus.textContent =
                "This article response has no lookup key. " +
                "Update the backend and reload the article.";

            heart.disabled = true;
            positionPopup();
        } else {
            // Opening vocabulary looks it up.
            // Only clicking the heart saves it to SRS.
            loadMeaning(sourceArticle, segment, token);
        }

        closeButton.focus({ preventScroll: true });
    }

    // ------------------------------------------------------------
    // Article rendering
    // ------------------------------------------------------------

    function render(data) {
        if (
            !data ||
            !Array.isArray(data.segments) ||
            typeof data.unique_id !== "string" ||
            typeof data.segmentation_version !== "string"
        ) {
            throw new Error("The server returned an invalid article.");
        }

        close();

        article = data;
        segmentsById.clear();

        const fragment = document.createDocumentFragment();

        for (const segment of data.segments) {
            segmentsById.set(Number(segment.id), segment);

            if (segment.kind === "text") {
                fragment.append(
                    document.createTextNode(segment.text)
                );
                continue;
            }

            syncSegmentSRS(segment);

            const button = document.createElement("button");

            button.type = "button";
            button.className = "lexical-unit";
            button.textContent = segment.text;
            button.dataset.segmentId = String(segment.id);

            button.setAttribute("aria-haspopup", "dialog");
            button.setAttribute("aria-controls", popup.id);
            button.setAttribute("aria-expanded", "false");

            decorateSRS(button, segment);

            fragment.append(button);
        }

        body.replaceChildren(fragment);
    }

    // ------------------------------------------------------------
    // Events
    // ------------------------------------------------------------

    body.addEventListener("click", event => {
        if (!(event.target instanceof Element)) return;

        const button = event.target.closest(".lexical-unit");

        if (button && body.contains(button)) {
            open(button);
        }
    });

    closeButton.addEventListener("click", () => {
        close(true);
    });

    document.addEventListener("click", event => {
        if (popup.hidden) return;
        if (!(event.target instanceof Element)) return;

        if (
            !popup.contains(event.target) &&
            !event.target.closest(".lexical-unit")
        ) {
            close();
        }
    });

    document.addEventListener("keydown", event => {
        if (event.key === "Escape" && !popup.hidden) {
            event.preventDefault();
            close(true);
        }
    });

    window.addEventListener("resize", positionPopup);

    // Capture scrolling in the article's scrollable content area.
    document.addEventListener("scroll", positionPopup, true);

    // Expanding conjugation sections changes the popup's height.
    popup.addEventListener("toggle", positionPopup, true);

    // ------------------------------------------------------------
    // Public API used by index.html and review.js
    // ------------------------------------------------------------

    window.ArticleReader = {
        render,
        close,
        requestJSON,
        updateCard,
    };
})();
