# Apprendre - The French Learning App

> [!WARNING]
> Almost all of the code in this repository is AI-generated slop, and this
> entire thing was created over one afternoon. Use with _caution_!

This repository contains code for a web-app to help me read articles in French.

## Features
* Add an article to the website. A list of articles to read becomes available.
* Articles can be read, marked as read, etc.
* The articles are segmented, and words or multi-word expressions (MWEs) are highlighted.
* Clicking on a word/MWE fetches meaning in-context from an LLM, including conjugations for verbs, etc.
* Words and phrases can be added to an spaced-repetition system (FSRS) for revision.
* Multiple users, and their authentication.
* Publishing as a standalone docker container to run on homelab.

## Usage

For local test usage:
```
APP_PUBLIC_ORIGIN=http://localhost:8000 \
APP_COOKIE_SECURE=false \
uv run uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

## Planned Features
* Deleting/trashing articles.
* Markdown viewer for reading articles.
* Automatic URL-based ingestion of articles.
