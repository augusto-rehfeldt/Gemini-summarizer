# Gemini Book Summarizer — Calibre Plugin

Summarizes books in your Calibre library using Google's Gemini API and stores the result in a custom column.

## Features

- Summarizes EPUB, TXT, HTML, and MOBI books
- Configurable Gemini model, prompt template, and word limit (default: 2000 words)
- Writes summaries to any custom Long Text column (default: `#summary`)
- Batch-processes multiple selected books with a progress dialog

---

## Installation

### 1. Prerequisite — install the Google GenAI SDK

```bash
pip install -q -U google-genai
```

### 2. Install the plugin

In Calibre: **Preferences → Plugins → Load plugin from file** → select `GeminiSummarizer.zip`

Restart Calibre.

### 3. Create the custom column

**Preferences → Add your own columns → Add column**

| Field | Value |
|-------|-------|
| Column id | `summary` |
| Column heading | `Summary` |
| Type | Long text / HTML |

Calibre stores it as `#summary`. Restart Calibre after adding.

### 4. Configure

**Preferences → Plugins → Gemini Book Summarizer → Customize plugin**

- Paste your [Gemini API key](https://aistudio.google.com/app/apikey)
- Select model (`gemini-3-flash-preview` by default)
- Verify column name is `#summary`

### 5. Use

Select one or more books → click **Gemini Summarize** in the toolbar.

---

## GitHub Releases — How to Release & Update

### Repository structure

```
GeminiSummarizer/          ← this folder becomes the plugin source
├── __init__.py
├── action.py
├── config.py
├── jobs.py
├── summarizer.py
├── images/
│   └── icon.png
├── plugin-import-name-gemini_summarizer.txt
└── README.md
.github/
└── workflows/
    └── release.yml        ← auto-builds the zip on every version tag
```

### One-time setup

```bash
git init
git remote add origin https://github.com/YOUR_USERNAME/calibre-gemini-summarizer.git
git add .
git commit -m "Initial release"
git push -u origin main
```

Add `.github/workflows/release.yml` (see below) then push.

### How to publish a new release

**1. Bump the version** in `__init__.py`:
```python
version = (1, 1, 0)   # change this
```

**2. Commit and tag:**
```bash
git add __init__.py
git commit -m "Release v1.1.0"
git tag v1.1.0
git push origin main --tags
```

**3. GitHub Actions automatically:**
- Zips the plugin source into `GeminiSummarizer.zip`
- Creates a GitHub Release named `v1.1.0`
- Attaches the zip as a downloadable asset

Users can then download the zip directly from the **Releases** page.

---

### `.github/workflows/release.yml`

```yaml
name: Build & Release Plugin

on:
  push:
    tags:
      - 'v*'          # triggers on any tag like v1.0.0, v2.3.1

jobs:
  release:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Build plugin zip
        run: |
          cd GeminiSummarizer
          zip -r ../GeminiSummarizer.zip . \
            --exclude "*.pyc" \
            --exclude "__pycache__/*" \
            --exclude "*.DS_Store"

      - name: Create GitHub Release
        uses: softprops/action-gh-release@v2
        with:
          files: GeminiSummarizer.zip
          generate_release_notes: true   # auto-generates changelog from commits
```

### Users updating the plugin

1. Download the new `GeminiSummarizer.zip` from GitHub Releases
2. In Calibre: **Preferences → Plugins → find Gemini Book Summarizer → Remove**
3. **Load plugin from file** → select new zip
4. Restart Calibre — settings are preserved in `calibre/plugins/gemini_summarizer.json`

---

## Models

| Model | Speed | Quality | Notes |
|-------|-------|---------|-------|
| `gemini-3-flash-preview` | Fast | ★★★★☆ | **Default** — best balance |
| `gemini-3.1-pro-preview` | Slow | ★★★★★ | Highest quality |
| `gemini-2.5-flash` | Very fast | ★★★☆☆ | Stable release |
| `gemini-2.5-pro` | Medium | ★★★★☆ | Large context window |

## License

Apache
