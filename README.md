# Spotify AI Playlist Generator

Generate Spotify playlists from natural language prompts using Claude AI. Describe a mood, artist, vibe, or any musical idea — Claude picks the songs, Spotify builds the playlist.

<p align="center">
  <img src="docs/screenshot.png" alt="App screenshot" />
</p>

## What it does

1. You describe a sound, mood, or artist in plain text (e.g. *"songs like Radiohead's OK Computer"*)
2. You pick a discovery mode to control how adventurous the recommendations are
3. Claude AI selects songs based on the mode and your description
4. The app searches Spotify to verify each track exists
5. You preview the list, name the playlist, and save it directly to your Spotify account

## Discovery modes

A slider lets you control how Claude interprets your prompt:

| Mode | Behaviour |
|---|---|
| **Similar** | Songs that sound very close — same mood, instrumentation, and energy. Accuracy over discovery. |
| **Explore** | Starts close, then branches into adjacent artists, subgenres, and scenes. |
| **Influences** | Traces the musical lineage — artists and songs that shaped the described sound. |
| **Surprise** | Unexpected connections that still make musical sense. Anything goes. |

## Tech stack

- **Backend** — Python, [FastAPI](https://fastapi.tiangolo.com/), [httpx](https://www.python-httpx.org/)
- **AI** — [Anthropic Claude](https://www.anthropic.com/) (`claude-sonnet-4-6`) for song recommendations
- **Auth** — Spotify OAuth 2.0, signed session cookies (no database required)
- **Frontend** — Vanilla HTML/CSS/JS (no framework)

## Prerequisites

- Python 3.11+
- A [Spotify Developer](https://developer.spotify.com/dashboard) account and app
- An [Anthropic API](https://console.anthropic.com/) key

## Local setup

1. **Clone the repo**

   ```bash
   git clone https://github.com/JTYEG/spotify-ai-playlist.git
   cd spotify-ai-playlist
   ```

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables**

   Copy `.env.example` to `.env` and fill in your keys:

   ```bash
   cp .env.example .env
   ```

   | Variable | Where to get it |
   |---|---|
   | `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com/) |
   | `SPOTIFY_CLIENT_ID` | Spotify Developer Dashboard |
   | `SPOTIFY_CLIENT_SECRET` | Spotify Developer Dashboard |
   | `SPOTIFY_REDIRECT_URI` | Set to `http://localhost:8000/callback` locally |
   | `SECRET_KEY` | Any long random string (used to sign session cookies) |

4. **Add the redirect URI to your Spotify app**

   In your Spotify Developer Dashboard → your app → Edit Settings → Redirect URIs, add:
   ```
   http://localhost:8000/callback
   ```

5. **Run the server**

   ```bash
   uvicorn main:app --reload
   ```

   Open [http://localhost:8000](http://localhost:8000) in your browser.

## Deployment

See [DEPLOY.md](DEPLOY.md) for instructions on deploying to Render.

## How the AI recommendation works

Each discovery mode sends Claude a structured task prompt that defines exactly what kind of recommendation to make. Claude prioritizes:

1. Sonic similarity (production, instrumentation, tempo, texture)
2. Emotional similarity (mood, atmosphere, intensity)
3. Musical similarity (melody, harmony, structure)
4. Genre/subgenre fit
5. Era and historical context (only when it improves accuracy)

So *"songs like Bohemian Rhapsody"* in **Similar** mode returns tracks that feel operatic and layered, while **Influences** mode traces back to the artists that shaped Queen's sound.

## Project structure

```
├── main.py              # FastAPI app — all routes and Claude/Spotify logic
├── static/
│   ├── index.html       # Single-page UI
│   ├── app.js           # Frontend logic
│   └── style.css        # Styles
├── requirements.txt
├── .env.example         # Environment variable template
└── Procfile             # For Render/Heroku deployment
```

## License

MIT
