import json
import os
import secrets
import time
from urllib.parse import urlencode

import anthropic
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import URLSafeTimedSerializer, BadSignature
from pydantic import BaseModel

load_dotenv()

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SPOTIFY_CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
SPOTIFY_CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
SPOTIFY_REDIRECT_URI = os.environ["SPOTIFY_REDIRECT_URI"]
SECRET_KEY = os.environ["SECRET_KEY"]

SPOTIFY_SCOPES = "playlist-modify-public playlist-modify-private user-read-private user-read-email"
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

app = FastAPI()
signer = URLSafeTimedSerializer(SECRET_KEY)

CLAUDE_SYSTEM_PROMPT = """You are a music recommendation engine.

Recommend songs based on the user's requested mode and seed.

Prioritize:
1. sonic similarity
2. mood and atmosphere
3. instrumentation and production
4. genre/subgenre fit
5. historical or stylistic lineage only when it improves the musical match

Rules:
- Avoid duplicates.
- Avoid more than 2 songs by the same artist unless requested.
- Prefer musically credible recommendations over generic obvious picks.
- Return ONLY a JSON array.
- Each item must contain:
 

Then return the best matches.

CRITICAL: Respond with ONLY a JSON array. Each item has 'title' and 'artist' keys. No explanations, no markdown, no code blocks — just the raw JSON array.
Example: [{"title": "Piano Man", "artist": "Billy Joel"}]"""

MODE_PROMPTS = {
    "tight_match": (
        "DISCOVERY MODE: tight_match — Stay as close as possible to the input. "
        "Recommend songs that are near-identical in sound, production, mood, tempo, and genre. "
        "Same instrumentation style, same emotional register, same general era. Minimal deviation."
    ),
    "adjacent_discovery": (
        "DISCOVERY MODE: adjacent_discovery — Balance familiarity with discovery. "
        "Recommend songs that clearly share musical DNA with the input but introduce the listener "
        "to adjacent artists, subgenres, or eras they may not know."
    ),
    "left_field": (
        "DISCOVERY MODE: left_field — Be adventurous. Recommend songs that share underlying musical or emotional DNA "
        "with the input but come from unexpected genres, eras, or subcultures. "
        "Make connections that aren't obvious but are musically defensible. Push well beyond the expected."
    ),
}

# ---------------------------------------------------------------------------
# Session helpers (signed cookie, no database)
# ---------------------------------------------------------------------------

SESSION_COOKIE = "spotify_session"
SESSION_MAX_AGE = 60 * 60 * 24  # 24 hours


def get_session(request: Request) -> dict:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return {}
    try:
        return signer.loads(cookie, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return {}


def set_session(response, data: dict):
    value = signer.dumps(data)
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def clear_session(response):
    response.delete_cookie(SESSION_COOKIE)


# ---------------------------------------------------------------------------
# Spotify token helpers
# ---------------------------------------------------------------------------

async def refresh_token_if_needed(session: dict) -> dict:
    """Return updated session with a fresh access_token if the current one is expiring."""
    if time.time() < session.get("expires_at", 0) - 60:
        return session  # still valid

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": session["refresh_token"],
            },
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        )
    resp.raise_for_status()
    data = resp.json()
    session["access_token"] = data["access_token"]
    session["expires_at"] = time.time() + data["expires_in"]
    if "refresh_token" in data:
        session["refresh_token"] = data["refresh_token"]
    return session


# ---------------------------------------------------------------------------
# Claude helper
# ---------------------------------------------------------------------------

def get_songs_from_claude(prompt: str, song_count: int = 15, mode: str = "adjacent_discovery") -> list[dict]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    mode_instruction = MODE_PROMPTS.get(mode, MODE_PROMPTS["adjacent_discovery"])
    user_message = (
        f"{mode_instruction}\n\n"
        f'Generate {song_count} song recommendations for a playlist described as: "{prompt}"\n\n'
        f"Return exactly {song_count} songs as a JSON array with 'title' and 'artist' keys only. "
        "Make them well-known enough to be findable on Spotify. "
        "Vary the artists — do not repeat the same artist more than twice."
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=CLAUDE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    content = response.content[0].text.strip()
    # Strip markdown fences if present
    if content.startswith("```"):
        parts = content.split("```")
        content = parts[1]
        if content.startswith("json"):
            content = content[4:]
    return json.loads(content.strip())


# ---------------------------------------------------------------------------
# Spotify API helpers
# ---------------------------------------------------------------------------

async def search_track(title: str, artist: str, access_token: str) -> str | None:
    query = f"track:{title} artist:{artist}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SPOTIFY_API_BASE}/search",
            params={"q": query, "type": "track", "limit": 1},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        return None
    items = resp.json().get("tracks", {}).get("items", [])
    if not items:
        return None
    return items[0]["uri"]


async def create_playlist(user_id: str, name: str, access_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SPOTIFY_API_BASE}/users/{user_id}/playlists",
            json={
                "name": name,
                "description": "Generated by AI Playlist Generator (Claude + Spotify)",
                "public": False,
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
    resp.raise_for_status()
    data = resp.json()
    return {"id": data["id"], "url": data["external_urls"]["spotify"]}


async def add_tracks_to_playlist(playlist_id: str, uris: list[str], access_token: str):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/tracks",
            json={"uris": uris},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/auth/login")
async def auth_login(request: Request):
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "scope": SPOTIFY_SCOPES,
        "state": state,
    }
    # Store state in a short-lived cookie for CSRF check
    redirect_url = f"{SPOTIFY_AUTH_URL}?{urlencode(params)}"
    response = RedirectResponse(redirect_url)
    response.set_cookie("oauth_state", state, max_age=300, httponly=True, samesite="lax")
    return response


@app.get("/callback")
async def auth_callback(request: Request, code: str = None, state: str = None, error: str = None):
    if error:
        return RedirectResponse(f"/?error={error}")

    stored_state = request.cookies.get("oauth_state")
    # Only reject if the cookie is present but doesn't match (cookie can be lost in HTTPS proxies)
    if stored_state and state != stored_state:
        return RedirectResponse("/?error=state_mismatch")

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": SPOTIFY_REDIRECT_URI,
            },
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to exchange code for token")

    token_data = resp.json()
    access_token = token_data["access_token"]
    expires_at = time.time() + token_data["expires_in"]

    # Fetch user profile
    async with httpx.AsyncClient() as client:
        profile_resp = await client.get(
            f"{SPOTIFY_API_BASE}/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    profile = profile_resp.json()

    session = {
        "access_token": access_token,
        "refresh_token": token_data["refresh_token"],
        "expires_at": expires_at,
        "user_id": profile["id"],
        "display_name": profile.get("display_name") or profile["id"],
    }

    response = RedirectResponse("/")
    response.delete_cookie("oauth_state")
    set_session(response, session)
    return response


@app.get("/auth/status")
async def auth_status(request: Request):
    session = get_session(request)
    if not session:
        return JSONResponse({"logged_in": False})
    return JSONResponse({"logged_in": True, "display_name": session.get("display_name", "")})


@app.get("/auth/logout")
async def auth_logout(request: Request):
    response = RedirectResponse("/")
    clear_session(response)
    return response


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    prompt: str
    song_count: int = 15
    mode: str = "adjacent_discovery"


class SaveRequest(BaseModel):
    playlist_name: str = ""
    uris: list[str]


@app.post("/api/get-songs")
async def get_songs(request: Request, body: GenerateRequest):
    """Step 1: Ask Claude for songs, verify on Spotify, return only found tracks."""
    import asyncio

    session = get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in")

    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    try:
        session = await refresh_token_if_needed(session)
    except Exception:
        raise HTTPException(status_code=401, detail="Could not refresh Spotify token — please log in again")

    access_token = session["access_token"]
    song_count = max(5, min(50, body.song_count))

    mode = body.mode if body.mode in ("tight_match", "adjacent_discovery", "left_field") else "adjacent_discovery"

    try:
        songs = get_songs_from_claude(prompt, song_count, mode)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Claude error: {str(e)}")

    search_results = await asyncio.gather(
        *[search_track(s["title"], s["artist"], access_token) for s in songs],
        return_exceptions=True,
    )

    found = []
    for song, uri in zip(songs, search_results):
        if uri and isinstance(uri, str):
            found.append({"title": song["title"], "artist": song["artist"], "uri": uri})

    if not found:
        raise HTTPException(status_code=502, detail="No tracks found on Spotify for any of Claude's suggestions")

    response = JSONResponse({"songs": found})
    set_session(response, session)
    return response


@app.post("/api/save-playlist")
async def save_playlist(request: Request, body: SaveRequest):
    """Step 2: Create the playlist with already-verified URIs."""
    session = get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in")

    try:
        session = await refresh_token_if_needed(session)
    except Exception:
        raise HTTPException(status_code=401, detail="Could not refresh Spotify token — please log in again")

    access_token = session["access_token"]
    user_id = session["user_id"]
    playlist_name = body.playlist_name.strip() or "AI Mix"

    try:
        playlist = await create_playlist(user_id, playlist_name, access_token)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to create playlist: {str(e)}")

    try:
        await add_tracks_to_playlist(playlist["id"], body.uris, access_token)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to add tracks: {str(e)}")

    response = JSONResponse({
        "playlist_url": playlist["url"],
        "playlist_name": playlist_name,
        "tracks_found": len(body.uris),
    })
    set_session(response, session)
    return response


# ---------------------------------------------------------------------------
# Serve static assets and index page
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")
