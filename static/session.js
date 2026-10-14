(() => {
    "use strict";

    const userId = document.querySelector(
        'meta[name="apprendre-user"]'
    )?.content;

    const username = document.querySelector(
        'meta[name="apprendre-username"]'
    )?.content;

    if (!userId) {
        throw new Error("Missing authenticated page identity");
    }

    const nativeFetch = window.fetch.bind(window);

    window.fetch = function(input, init) {
        const request = new Request(input, init);
        const url = new URL(request.url);

        if (
            url.origin !== window.location.origin ||
            !url.pathname.startsWith("/api/")
        ) {
            return nativeFetch(request);
        }

        const headers = new Headers(request.headers);
        headers.set("X-Apprendre-User", userId);

        return nativeFetch(new Request(request, {
            headers,
            credentials: "same-origin",
            cache: "no-store",
        }));
    };

    async function requestJSON(url, options = {}, timeoutMs = 20000) {
        const controller = new AbortController();
        const upstreamSignal = options.signal;

        const abort = () => controller.abort();

        if (upstreamSignal?.aborted) {
            controller.abort();
        } else {
            upstreamSignal?.addEventListener("abort", abort, { once: true });
        }

        const timer = setTimeout(abort, timeoutMs);

        try {
            const response = await fetch(url, {
                ...options,
                signal: controller.signal,
            });

            let body;
            try {
                body = await response.json();
            } catch (error) {
                if (controller.signal.aborted) throw error;
                throw new Error("Server returned an invalid JSON response");
            }

            if (!response.ok) {
                const detail = typeof body.detail === "string"
                    ? body.detail
                    : `Request failed (${response.status})`;

                const error = new Error(detail);
                error.status = response.status;

                if (response.status === 401) {
                    window.location.assign("/login");
                }

                throw error;
            }

            return body;
        } catch (error) {
            if (controller.signal.aborted) {
                throw new Error(
                    "Request timed out or was cancelled. Please retry."
                );
            }
            throw error;
        } finally {
            clearTimeout(timer);
            upstreamSignal?.removeEventListener("abort", abort);
        }
    }

    window.AppSession = Object.freeze({
        userId,
        requestJSON,
    });

    document.addEventListener("DOMContentLoaded", () => {
        const nav = document.querySelector("nav");

        if (nav) {
            const label = document.createElement("span");
            label.textContent = username || "Signed in";
            label.style.marginLeft = "auto";

            const form = document.createElement("form");
            form.method = "post";
            form.action = "/logout";

            const button = document.createElement("button");
            button.type = "submit";
            button.textContent = "Sign out";
            button.className = "secondary";

            form.append(button);
            nav.append(label, form);
        }

        if (typeof window.loadFlashcards !== "function") {
            const front = document.getElementById("fc-front");
            const status = document.getElementById("review-status");

            if (front) {
                front.textContent =
                    "Review interface failed to initialize.";
            }
            if (status) {
                status.textContent =
                    "Reload the page and check the browser console.";
            }
        }
    });
})();
