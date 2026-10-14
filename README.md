# Apprendre - The French Learning App

> [!WARNING]
> Most of the code in the repository is AI-generated slop. Only use it if you
> know what you are doing.

This repository contains code for an app to help me read articles in French.

## Features
* Add an article to the website. A list of articles to read becomes available.
* Articles can be read, marked as read, etc.
* The articles are segmented, and words or multi-word expressions (MWEs) are highlighted.
* Clicking on a word/MWE fetches meaning in-context from an LLM, including conjugations for verbs, etc.
* Words and phrases can be added to an spaced-repetition system (FSRS) for revision.

## Usage
```
APP_PUBLIC_ORIGIN=http://localhost:8000 \
APP_COOKIE_SECURE=false \
uv run uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

## Planned Features
* Multiple users, and their authentication.
* Publishing as a standalone docker container to run on homelab.

