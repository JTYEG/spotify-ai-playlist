import asyncio
import json
import os
import re
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
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")

SPOTIFY_SCOPES = "playlist-modify-public playlist-modify-private user-read-private user-read-email"
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
LASTFM_API_BASE = "https://ws.audioscrobbler.com/2.0/"

app = FastAPI()
signer = URLSafeTimedSerializer(SECRET_KEY)

CLAUDE_SYSTEM_PROMPT = """You are a music ranking engine.

You receive a JSON object describing a seed, tags, a mode, and a list of candidate tracks sourced from Last.fm.

Your task: select and rank the best candidates according to the mode instruction.

Rules:
- ONLY use tracks from candidate_tracks. Never invent or add songs not in the list.
- Do not include the seed track itself.
- Do not repeat the same artist more than twice.
- Return ONLY valid JSON matching the output_schema. No explanations, no markdown, no code blocks.

Example output: {"ranked_tracks": [{"artist": "New Order", "track": "Blue Monday"}]}"""

RANK_MODE_INSTRUCTIONS = {
    "tight_match":        "Rank by closest sonic and mood similarity to the seed. Prioritize tracks that feel nearly identical in style, energy, and production.",
    "adjacent_discovery": "Rank balancing similarity with variety. Mix close matches with interesting adjacent picks across different artists and subgenres.",
    "influence_trail":    "Prioritize tracks that represent the musical lineage and influences behind the seed's style.",
    "left_field":         "Prioritize the most unexpected but musically defensible picks. Favour surprising connections that still make sense to fans of the seed.",
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
# Last.fm helpers
# ---------------------------------------------------------------------------

async def _lastfm(method: str, params: dict) -> dict:
    if not LASTFM_API_KEY:
        return {}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                LASTFM_API_BASE,
                params={"method": method, "api_key": LASTFM_API_KEY, "format": "json", "autocorrect": 1, **params},
            )
        return resp.json() if resp.status_code == 200 else {}
    except Exception:
        return {}


def _normalize_key(title: str, artist: str) -> str:
    """Lowercase + strip punctuation for deduplication."""
    def clean(s):
        return re.sub(r"[^\w\s]", "", s.lower()).strip()
    return f"{clean(artist)}||{clean(title)}"


async def _lfm_track_info(track: str, artist: str) -> dict | None:
    data = await _lastfm("track.getInfo", {"track": track, "artist": artist})
    t = data.get("track")
    if not t:
        return None
    a = t.get("artist", {})
    return {
        "track":  t["name"],
        "artist": a["name"] if isinstance(a, dict) else str(a),
    }


async def _lfm_similar_tracks(track: str, artist: str, limit: int = 25) -> list[dict]:
    data = await _lastfm("track.getSimilar", {"track": track, "artist": artist, "limit": limit})
    tracks = data.get("similartracks", {}).get("track", [])
    return [{"track": t["name"], "artist": t["artist"]["name"]} for t in tracks]


async def _lfm_track_top_tags(track: str, artist: str, limit: int = 6) -> list[str]:
    data = await _lastfm("track.getTopTags", {"track": track, "artist": artist})
    tags = data.get("toptags", {}).get("tag", [])[:limit]
    return [t["name"] for t in tags]


async def _lfm_similar_artists(artist: str, limit: int = 10) -> list[str]:
    data = await _lastfm("artist.getSimilar", {"artist": artist, "limit": limit})
    artists = data.get("similarartists", {}).get("artist", [])
    return [a["name"] for a in artists]


async def _lfm_artist_top_tags(artist: str, limit: int = 6) -> list[str]:
    data = await _lastfm("artist.getTopTags", {"artist": artist})
    tags = data.get("toptags", {}).get("tag", [])[:limit]
    return [t["name"] for t in tags]


async def _lfm_artist_top_tracks(artist: str, limit: int = 3) -> list[dict]:
    data = await _lastfm("artist.getTopTracks", {"artist": artist, "limit": limit})
    tracks = data.get("toptracks", {}).get("track", [])
    return [{"track": t["name"], "artist": artist} for t in tracks]


async def _lfm_tag_top_artists(tag: str, limit: int = 8) -> list[str]:
    data = await _lastfm("tag.getTopArtists", {"tag": tag, "limit": limit})
    artists = data.get("topartists", {}).get("artist", [])
    return [a["name"] for a in artists]


async def _lfm_tag_top_tracks(tag: str, limit: int = 8) -> list[dict]:
    data = await _lastfm("tag.getTopTracks", {"tag": tag, "limit": limit})
    tracks = data.get("tracks", {}).get("track", [])
    return [{"track": t["name"], "artist": t["artist"]["name"]} for t in tracks]


def _unique_by_artist_limit(tracks: list[dict], max_per_artist: int = 2) -> list[dict]:
    """Limit candidates to max_per_artist tracks per artist."""
    counts: dict[str, int] = {}
    result = []
    for t in tracks:
        key = t["artist"].lower().strip()
        if counts.get(key, 0) < max_per_artist:
            counts[key] = counts.get(key, 0) + 1
            result.append(t)
    return result


async def build_seed_pool(seed_str: str) -> dict:
    """Parse 'Artist' or 'Artist - Track' string and build candidate pool."""
    parts = [p.strip() for p in seed_str.split(" - ", 1)]
    artist = parts[0]
    track = parts[1] if len(parts) > 1 else ""
    return await build_candidate_pool(track, artist)


async def build_candidate_pool(seed_track: str, seed_artist: str) -> dict:
    """Full pipeline: normalize → similar tracks + tags → similar artists → their top tracks → dedupe pool."""

    # 1. Normalize via track.getInfo
    info = await _lfm_track_info(seed_track, seed_artist)
    norm_track  = info["track"]  if info else seed_track
    norm_artist = info["artist"] if info else seed_artist

    if seed_track:
        # 2 & 3. Similar tracks + track tags in parallel
        similar_tracks, tags = await asyncio.gather(
            _lfm_similar_tracks(norm_track, norm_artist, limit=25),
            _lfm_track_top_tags(norm_track, norm_artist),
        )
    else:
        similar_tracks = []
        tags = await _lfm_artist_top_tags(norm_artist)

    # 4. Similar artists
    similar_artists = await _lfm_similar_artists(norm_artist, limit=7)

    # 5. Top 2 tracks for each similar artist
    artist_track_lists = await asyncio.gather(
        *[_lfm_artist_top_tracks(a, limit=2) for a in similar_artists]
    )

    # 6. Merge all candidate tracks
    all_candidates: list[dict] = list(similar_tracks)
    for track_list in artist_track_lists:
        all_candidates.extend(track_list)

    # 7. Deduplicate, excluding the seed itself
    seed_key = _normalize_key(norm_track, norm_artist)
    seen = {seed_key}
    unique: list[dict] = []
    for c in all_candidates:
        key = _normalize_key(c["track"], c["artist"])
        if key not in seen:
            seen.add(key)
            unique.append(c)

    # 8. Apply per-artist limit then cap pool at 40
    candidates = _unique_by_artist_limit(unique, max_per_artist=2)[:40]

    return {
        "seed":       {"track": norm_track, "artist": norm_artist},
        "tags":       tags,
        "candidates": candidates,
    }


async def build_mood_pool(description: str) -> dict:
    """Mood-only pipeline: extract tags from description → Last.fm tag endpoints → candidate pool."""
    stop_words = {"but", "more", "and", "or", "style", "feel", "vibe", "vibes", "with",
                  "like", "in", "the", "a", "an", "i", "want", "something", "kind", "of",
                  "music", "songs", "tracks", "please", "give", "me", "some"}
    words = [w.strip(".,!?") for w in description.lower().split()]
    mood_tags = [w for w in words if w and w not in stop_words and len(w) > 2][:3]

    if not mood_tags:
        raise ValueError("Could not extract mood tags from description")

    # Fetch top artists + top tracks for each mood tag in parallel
    results = await asyncio.gather(*[
        asyncio.gather(
            _lfm_tag_top_artists(tag, limit=8),
            _lfm_tag_top_tracks(tag, limit=8),
        )
        for tag in mood_tags
    ])

    all_candidates: list[dict] = []
    artist_names: list[str] = []
    for artist_list, track_list in results:
        all_candidates.extend(track_list)
        artist_names.extend(artist_list)

    # Get top tracks for the discovered artists too
    unique_artists = list(dict.fromkeys(artist_names))[:8]
    artist_track_lists = await asyncio.gather(
        *[_lfm_artist_top_tracks(a, limit=2) for a in unique_artists]
    )
    for track_list in artist_track_lists:
        all_candidates.extend(track_list)

    seen: set[str] = set()
    unique: list[dict] = []
    for c in all_candidates:
        key = _normalize_key(c["track"], c["artist"])
        if key not in seen:
            seen.add(key)
            unique.append(c)

    candidates = _unique_by_artist_limit(unique, max_per_artist=2)[:40]
    return {
        "seed":       {"track": "", "artist": description},
        "tags":       mood_tags,
        "candidates": candidates,
    }


async def build_blend_pool(seed1: str, seed2: str) -> dict:
    """Blend pipeline: run both seeds in parallel, merge candidate pools."""
    pool1, pool2 = await asyncio.gather(build_seed_pool(seed1), build_seed_pool(seed2))

    all_candidates = list(pool1["candidates"]) + list(pool2["candidates"])
    seen: set[str] = set()
    unique: list[dict] = []
    for c in all_candidates:
        key = _normalize_key(c["track"], c["artist"])
        if key not in seen:
            seen.add(key)
            unique.append(c)

    candidates = _unique_by_artist_limit(unique, max_per_artist=2)[:40]
    merged_tags = list(dict.fromkeys(pool1["tags"] + pool2["tags"]))[:6]

    return {
        "seed":       {"track": "", "artist": f"{pool1['seed']['artist']} + {pool2['seed']['artist']}"},
        "tags":       merged_tags,
        "candidates": candidates,
        "blend":      True,
        "seed1":      pool1["seed"],
        "seed2":      pool2["seed"],
    }


# ---------------------------------------------------------------------------
# Claude helper — ranks provided candidates, never invents songs
# ---------------------------------------------------------------------------

def rank_candidates_with_claude(
    seed: dict,
    tags: list[str],
    mode: str,
    description: str,
    candidates: list[dict],
    count: int = 20,
    blend_context: str = "",
    show_reasons: bool = False,
) -> list[dict]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    mode_instruction = RANK_MODE_INSTRUCTIONS.get(mode, RANK_MODE_INSTRUCTIONS["adjacent_discovery"])
    if blend_context:
        mode_instruction = blend_context + " " + mode_instruction
    if tags:
        mode_instruction += f" Tags: {', '.join(tags[:4])}."
    if description:
        mode_instruction += f" Context: {description}"

    if show_reasons:
        output_schema = {"ranked_tracks": [{"artist": "string", "track": "string", "reason": "3-5 words"}]}
        system = CLAUDE_SYSTEM_PROMPT.replace(
            "- Return ONLY valid JSON",
            "- For each track include a \"reason\" field: 3-5 words explaining why it fits.\n- Return ONLY valid JSON",
        )
    else:
        output_schema = {"ranked_tracks": [{"artist": "string", "track": "string"}]}
        system = CLAUDE_SYSTEM_PROMPT

    payload = {
        "task": f"Rank and return the best {count} candidate tracks in order of best fit to the seed.",
        "seed": {"artist": seed["artist"], "track": seed["track"]},
        "mode": mode_instruction,
        "candidate_tracks": [{"artist": c["artist"], "track": c["track"]} for c in candidates],
        "output_schema": output_schema,
    }

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": json.dumps(payload)}],
    )
    content = response.content[0].text.strip()
    if content.startswith("```"):
        parts = content.split("```")
        content = parts[1]
        if content.startswith("json"):
            content = content[4:]
    result = json.loads(content.strip())
    return result.get("ranked_tracks", result) if isinstance(result, dict) else result


# ---------------------------------------------------------------------------
# Spotify API helpers
# ---------------------------------------------------------------------------

async def search_track(track: str, artist: str, access_token: str) -> dict | None:
    query = f"track:{track} artist:{artist}"
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
    item = items[0]
    return {
        "uri":          item["uri"],
        "spotify_id":   item["id"],
        "external_url": item["external_urls"]["spotify"],
    }


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
    seed: str                          # "Artist" or "Artist - Track"
    prompt: str = ""                   # optional extra context for ranking
    song_count: int = 15
    mode: str = "adjacent_discovery"
    show_reasons: bool = False


class SaveRequest(BaseModel):
    playlist_name: str = ""
    uris: list[str]


@app.post("/api/get-songs")
async def get_songs(request: Request, body: GenerateRequest):
    """Pipeline: Last.fm candidates → Claude ranking → Spotify verification."""
    session = get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in")

    seed = body.seed.strip()
    description = body.prompt.strip()

    if not seed and not description:
        raise HTTPException(status_code=400, detail="Please enter a seed artist/track or a mood description")

    try:
        session = await refresh_token_if_needed(session)
    except Exception:
        raise HTTPException(status_code=401, detail="Could not refresh Spotify token — please log in again")

    access_token = session["access_token"]
    song_count = max(5, min(50, body.song_count))
    mode = body.mode if body.mode in RANK_MODE_INSTRUCTIONS else "adjacent_discovery"

    # Route to the right pipeline
    try:
        if "+" in seed:
            parts = [p.strip() for p in seed.split("+", 1)]
            pool = await build_blend_pool(parts[0], parts[1])
        elif seed:
            pool = await build_seed_pool(seed)
        else:
            pool = await build_mood_pool(description)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Last.fm error: {str(e)}")

    if not pool["candidates"]:
        raise HTTPException(status_code=502, detail="No candidates found for this seed")

    rank_count = min(20, len(pool["candidates"]))
    blend_context = (
        f"Blend request: find tracks that appeal to fans of both "
        f"{pool['seed1']['artist']} and {pool['seed2']['artist']}."
        if pool.get("blend") else ""
    )

    try:
        ranked = rank_candidates_with_claude(
            pool["seed"], pool["tags"], mode, description, pool["candidates"],
            count=rank_count, blend_context=blend_context, show_reasons=body.show_reasons,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Claude error: {str(e)}")

    # Verify ranked tracks on Spotify sequentially until song_count found
    found = []
    for song in ranked:
        result = await search_track(song["track"], song["artist"], access_token)
        if result:
            entry = {
                "title":        song["track"],
                "artist":       song["artist"],
                "uri":          result["uri"],
                "spotify_id":   result["spotify_id"],
                "external_url": result["external_url"],
            }
            if body.show_reasons and song.get("reason"):
                entry["reason"] = song["reason"]
            found.append(entry)
        if len(found) >= song_count:
            break

    if not found:
        raise HTTPException(status_code=502, detail="No tracks found on Spotify from the ranked candidates")

    response = JSONResponse({
        "songs": found,
        "debug": {
            "seed":             pool["seed"],
            "tags":             pool["tags"],
            "candidates_built": len(pool["candidates"]),
            "ranked_count":     len(ranked),
            "verified_count":   len(found),
        },
    })
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
