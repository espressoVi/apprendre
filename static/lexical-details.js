(() => {
    const persons = [
        ["je", "je"],
        ["tu", "tu"],
        ["il", "il / elle / on"],
        ["nous", "nous"],
        ["vous", "vous"],
        ["ils", "ils / elles"],
    ];

    const tenses = [
        ["present_indicative", "Présent de l’indicatif"],
        ["imperfect_indicative", "Imparfait"],
        ["future_indicative", "Futur simple"],
        ["present_conditional", "Conditionnel présent"],
        ["present_subjunctive", "Subjonctif présent"],
        ["past_simple", "Passé simple"],
        ["imperfect_subjunctive", "Subjonctif imparfait"],
    ];

    function node(tag, text, className) {
        const element = document.createElement(tag);
        if (text !== undefined) element.textContent = text;
        if (className) element.className = className;
        return element;
    }

    function paragraph(parent, label, value) {
        if (!value) return;

        const p = node("p");
        p.append(node("strong", `${label}: `), node("span", value));
        parent.append(p);
    }

    function table(forms, rows) {
        const element = node("table", undefined, "conjugation-table");
        const body = node("tbody");

        for (const [key, label] of rows) {
            const row = node("tr");
            const heading = node("th", label);
            heading.scope = "row";

            row.append(heading, node("td", forms[key] ?? "—"));
            body.append(row);
        }

        element.append(body);
        return element;
    }

    function render(container, data) {
        const fragment = document.createDocumentFragment();
        const intro = node("div", undefined, "lexical-details");

        intro.append(
            node(
                "p",
                `${data.base_form} · ${data.part_of_speech}`,
                "lexical-meta"
            ),
            node("p", data.meaning_en, "lexical-meaning"),
            node("p", data.example_fr, "lexical-example"),
            node("p", data.example_en, "lexical-example-translation")
        );

        paragraph(intro, "Usage", data.usage_notes_en);
        fragment.append(intro);

        for (const verb of data.verbs) {
            const details = node("details", undefined, "lexical-conjugations");
            details.append(
                node("summary", `Conjugaison : ${verb.infinitive}`)
            );

            paragraph(details, "Auxiliary", verb.auxiliary);
            paragraph(details, "Past participle", verb.past_participle);
            paragraph(details, "Present participle", verb.present_participle);

            paragraph(
                details,
                "Future / conditional stems",
                verb.future_conditional_stems.join(", ")
            );
            paragraph(
                details,
                "Imperfect stems",
                verb.imperfect_stems.join(", ")
            );
            paragraph(
                details,
                "Subjunctive stems",
                verb.present_subjunctive_stems.join(", ")
            );

            for (const [key, title] of tenses) {
                const section = node("details");
                section.append(
                    node("summary", title),
                    table(verb[key], persons)
                );
                details.append(section);
            }

            const imperative = node("details");
            imperative.append(
                node("summary", "Impératif"),
                table(verb.imperative, [
                    ["tu", "tu"],
                    ["nous", "nous"],
                    ["vous", "vous"],
                ])
            );

            details.append(imperative);
            paragraph(details, "Notes", verb.notes_en);
            fragment.append(details);
        }

        fragment.append(
            node(
                "p",
                "AI-generated learning aid; check unusual or irregular forms.",
                "lexical-generated-note"
            )
        );

        container.replaceChildren(fragment);
    }

    window.LexicalDetails = { render };
})();
