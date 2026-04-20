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

SPOTIFY_SCOPES = "playlist-modify-public playlist-modify-private user-read-private user-read-email user-top-read user-library-read"
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

app = FastAPI()
signer = URLSafeTimedSerializer(SECRET_KEY)

CLAUDE_SYSTEM_PROMPT = """You are an expert music discovery and recommendation engine designed to generate high-quality song recommendations based on a user's musical taste. Your goal is to recommend thoughtful, musically relevant songs, not generic algorithmic suggestions.

When generating recommendations, analyze the request using multiple musical dimensions:
1. Musical Composition — melody, harmony, chord progressions, vocal style, instrumentation, arrangement complexity.
2. Sonic & Production Style — production techniques, synthesizer use, guitar tone, orchestration, rhythm style, sound design.
3. Genre & Subgenre — identify both primary and adjacent genres. Example: Muse → alternative rock, progressive rock, glam rock, electronic rock.
4. Cultural & Historical Context — use artist influences and musical lineage. Example: Billy Joel → Elton John, The Beatles, classical piano traditions.
5. Emotional Tone & Atmosphere — cinematic, dark, euphoric, introspective, energetic, melancholic. Match songs that evoke similar emotional experiences.

Recommendation Rules:
- Avoid generic suggestions. Do not default to the most obvious hits. Prefer deeper cuts, musically similar artists, and thoughtful cross-genre recommendations.
- Include influences and descendants — artists that influenced the original, artists influenced by the original, and contemporaries with similar styles.
- Optimize for discovery: a good recommendation should feel like "I didn't know this song, but it makes perfect sense."

Always internally analyze the input first and determine: genre, era, influences, mood, instrumentation, and comparable artists — then generate recommendations.

Prioritize songs that appeal to listeners who enjoy: intelligent songwriting, strong harmony and melody, cinematic or emotional music, classic 70s-80s songwriting traditions, alternative rock and sophisticated pop.

CRITICAL: You respond with ONLY a JSON array. Each item has 'title' and 'artist' keys. No explanations, no markdown, no code blocks — just the raw JSON array.
Example: [{"title": "Piano Man", "artist": "Billy Joel"}]"""

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
# Artist blend helpers
# ---------------------------------------------------------------------------

async def search_artist(access_token: str, artist_name: str) -> dict | None:
    """Search for an artist and return their top track info."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SPOTIFY_API_BASE}/search",
            params={"q": f"artist:{artist_name}", "type": "track", "limit": 5},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        return None
    tracks = resp.json().get("tracks", {}).get("items", [])
    if not tracks:
        return None
    # Collect all artist URis from top 5 tracks to get audio features
    artist_uris = set()
    for t in tracks:
        for a in t["artists"]:
            if a.get("uri"):
                artist_uris.add(a["uri"])
    return {
        "name": artist_name,
        "artist_uris": list(artist_uris),
        "top_track": tracks[0],
    }


async def fetch_artist_audio_features(access_token: str, artist_uris: list[str]) -> dict | None:
    """Fetch audio features for an artist's tracks."""
    if not artist_uris:
        return None
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SPOTIFY_API_BASE}/audio-features/tracks",
            params={"ids": ",".join(artist_uris)},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        return None
    features = resp.json().get("tracks", [])
    if not features:
        return None
    # Average the features
    n = len(features)
    return {
        "danceability": sum(f.get("danceability", 0) for f in features) / n,
        "energy": sum(f.get("energy", 0) for f in features) / n,
        "valence": sum(f.get("valence", 0) for f in features) / n,
        "acousticness": sum(f.get("acousticness", 0) for f in features) / n,
        "instrumentalness": sum(f.get("instrumentalness", 0) for f in features) / n,
        "liveness": sum(f.get("liveness", 0) for f in features) / n,
        "speechiness": sum(f.get("speechiness", 0) for f in features) / n,
        "tempo": sum(f.get("tempo", 0) for f in features) / n,
        "loudness": sum(f.get("loudness", 0) for f in features) / n,
    }


def blend_features(feat_a: dict, feat_b: dict, weight: float = 0.5) -> dict:
    """Blend two feature dicts. weight=0.5 is equal blend."""
    keys = ["danceability", "energy", "valence", "acousticness", "instrumentalness", "liveness", "speechiness", "tempo", "loudness"]
    blended = {}
    for k in keys:
        va = feat_a.get(k, 0) or 0
        vb = feat_b.get(k, 0) or 0
        blended[k] = va * (1 - weight) + vb * weight
    return blended


def describe_blend(artist_a: str, artist_b: str, feat_a: dict, feat_b: dict, blended: dict) -> str:
    """Build a descriptive string for the blended artist profile."""
    parts = [
        f"Blended artists: {artist_a} + {artist_b}",
        f"Blend profile: danceability={blended['danceability']:.2f}, energy={blended['energy']:.2f}, valence={blended['valence']:.2f}, acousticness={blended['acousticness']:.2f}, instrumentalness={blended['instrumentalness']:.2f}",
        f"Artist A ({artist_a}) features: energy={feat_a.get('energy', 0):.2f}, valence={feat_a.get('valence', 0):.2f}, acousticness={feat_a.get('acousticness', 0):.2f}",
        f"Artist B ({artist_b}) features: energy={feat_b.get('energy', 0):.2f}, valence={feat_b.get('valence', 0):.2f}, acousticness={feat_b.get('acousticness', 0):.2f}",
    ]
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Taste profile analysis
# ---------------------------------------------------------------------------

async def fetch_user_top_tracks(access_token: str, limit: int = 50) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SPOTIFY_API_BASE}/me/top/tracks",
            params={"limit": limit},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        return []
    items = resp.json().get("items", [])
    return [{"name": t["name"], "artists": [a["name"] for a in t["artists"]], "uri": t["uri"]} for t in items]


async def fetch_user_top_artists(access_token: str, limit: int = 20) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SPOTIFY_API_BASE}/me/top/artists",
            params={"limit": limit},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        return []
    items = resp.json().get("items", [])
    return [{"name": a["name"], "genres": a.get("genres", []), "uri": a["uri"]} for a in items]


async def fetch_user_saved_tracks(access_token: str, limit: int = 50) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SPOTIFY_API_BASE}/me/tracks",
            params={"limit": limit},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        return []
    items = resp.json().get("items", [])
    return [{"track": {
        "name": it["track"]["name"],
        "artists": [a["name"] for a in it["track"]["artists"]],
        "uri": it["track"]["uri"],
    }} for it in items]


async def fetch_track_audio_features(access_token: str, track_uris: list[str]) -> dict:
    if not track_uris:
        return {}
    features_map = {}
    for i in range(0, len(track_uris), 100):
        batch = track_uris[i:i+100]
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{SPOTIFY_API_BASE}/audio-features/tracks",
                params={"ids": ",".join(batch)},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if resp.status_code == 200:
            for f in resp.json().get("audio_features", []):
                if f and f.get("uri"):
                    features_map[f["uri"]] = f
    return features_map


def analyze_taste_profile(top_tracks: list, top_artists: list, saved_tracks: list, audio_features_map: dict) -> dict:
    # Aggregate audio features across all tracks
    all_features = []
    for track in top_tracks:
        for uri in [track["uri"]]:
            if uri in audio_features_map:
                all_features.append(audio_features_map[uri])
    for track in saved_tracks:
        uri = track["track"]["uri"]
        if uri in audio_features_map:
            all_features.append(audio_features_map[uri])

    # Calculate average audio features
    if all_features:
        avg_features = {
            "danceability": sum(f.get("danceability", 0) for f in all_features) / len(all_features),
            "energy": sum(f.get("energy", 0) for f in all_features) / len(all_features),
            "valence": sum(f.get("valence", 0) for f in all_features) / len(all_features),
            "acousticness": sum(f.get("acousticness", 0) for f in all_features) / len(all_features),
            "instrumentalness": sum(f.get("instrumentalness", 0) for f in all_features) / len(all_features),
            "liveness": sum(f.get("liveness", 0) for f in all_features) / len(all_features),
            "speechiness": sum(f.get("speechiness", 0) for f in all_features) / len(all_features),
            "tempo": sum(f.get("tempo", 0) for f in all_features) / len(all_features),
            "loudness": sum(f.get("loudness", 0) for f in all_features) / len(all_features),
        }
    else:
        avg_features = {}

    # Get top genres from top artists
    genre_counts = {}
    for artist in top_artists:
        for genre in artist.get("genres", []):
            genre_counts[genre] = genre_counts.get(genre, 0) + 1
    top_genres = sorted(genre_counts.items(), key=lambda x: x[1], reverse=True)[:8]

    # Get top artists
    artist_counts = {}
    for track in top_tracks:
        for artist_name in track.get("artists", []):
            artist_counts[artist_name] = artist_counts.get(artist_name, 0) + 1
    top_artist_names = sorted(artist_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    # Mood interpretation
    mood = "balanced"
    if avg_features:
        if avg_features["valence"] >= 0.7:
            mood = "positive/energetic"
        elif avg_features["valence"] <= 0.3:
            mood = "melancholic/introspective"
        else:
            mood = "mixed/moderate"

        if avg_features["energy"] >= 0.7:
            mood += "/high-energy"
        elif avg_features["energy"] <= 0.3:
            mood += "/chill"

    # Tempo preference
    tempo_pref = "mixed"
    if avg_features.get("tempo"):
        if avg_features["tempo"] > 120:
            tempo_pref = "fast (120+ BPM)"
        elif avg_features["tempo"] > 100:
            tempo_pref = "moderate (100-120 BPM)"
        else:
            tempo_pref = "slow (<100 BPM)"

    return {
        "avg_features": avg_features,
        "top_genres": top_genres,
        "top_artists": top_artist_names,
        "mood": mood,
        "tempo_preference": tempo_pref,
        "total_tracks_analyzed": len(all_features),
        "top_track_names": [t["name"] for t in top_tracks[:5]],
    }


def build_taste_profile_prompt(profile: dict) -> str:
    if not profile or not profile.get("top_genres"):
        return ""

    parts = []
    parts.append(f"User's top genres: {', '.join(g[0] + f' ({g[1]}x)' for g in profile['top_genres'][:5])}")
    parts.append(f"Favorite artists: {', '.join(a[0] for a in profile['top_artists'][:8])}")
    parts.append(f"Overall mood preference: {profile['mood']}")
    parts.append(f"Tempo preference: {profile['tempo_preference']}")

    if profile.get("avg_features"):
        af = profile["avg_features"]
        parts.append(f"Audio feature profile: danceability={af['danceability']:.2f}, energy={af['energy']:.2f}, valence={af['valence']:.2f}, acousticness={af['acousticness']:.2f}")

    parts.append(f"Current favorite tracks: {', '.join(profile.get('top_track_names', [])[:3])}")
    parts.append(f"Basis: {profile['total_tracks_analyzed']} tracks analyzed from user's listening history")

    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Claude helper
# ---------------------------------------------------------------------------

def get_songs_from_claude(prompt: str, song_count: int = 15, taste_profile: str = "", blend_info: str = "") -> list[dict]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    user_message = (
        f'Generate {song_count} song recommendations for a playlist described as: "{prompt}"\n\n'
        f"Return exactly {song_count} songs as a JSON array with 'title' and 'artist' keys only. "
        "Make them well-known enough to be findable on Spotify. "
        "Vary the artists — do not repeat the same artist more than twice."
    )
    if blend_info:
        user_message += f'\n\nArtist blend data: {blend_info}\nUse this to find songs that bridge both artists\' styles, tempos, and production aesthetics.'
    if taste_profile:
        user_message += f'\n\nYour personal taste profile: {taste_profile}\nUse this to tailor your recommendations to the user\'s actual listening preferences.'
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
    use_personalization: bool = False


class SaveRequest(BaseModel):
    playlist_name: str = ""
    uris: list[str]


class TasteProfileRequest(BaseModel):
    limit: int = 50


@app.post("/api/taste-profile")
async def get_taste_profile(request: Request, body: TasteProfileRequest):
    """Fetch user's listening history and return a taste profile."""
    session = get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in")

    try:
        session = await refresh_token_if_needed(session)
    except Exception:
        raise HTTPException(status_code=401, detail="Could not refresh Spotify token — please log in again")

    access_token = session["access_token"]

    # Fetch user data in parallel
    try:
        top_tracks = await fetch_user_top_tracks(access_token, limit=body.limit)
        top_artists = await fetch_user_top_artists(access_token, limit=20)
        saved_tracks = await fetch_user_saved_tracks(access_token, limit=body.limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch user data: {str(e)}")

    # Fetch audio features for all tracks
    all_uris = [t["uri"] for t in top_tracks] + [t["track"]["uri"] for t in saved_tracks]
    audio_features_map = await fetch_track_audio_features(access_token, all_uris)

    # Analyze and return profile
    profile = analyze_taste_profile(top_tracks, top_artists, saved_tracks, audio_features_map)
    profile["top_tracks"] = top_tracks[:10]
    profile["top_artists_detail"] = top_artists[:10]

    response = JSONResponse(profile)
    set_session(response, session)
    return response


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

    # Handle artist blending (e.g. "Radiohead + Aphex Twin")
    blend_prompt = ""
    if " + " in prompt:
        parts = prompt.split(" + ", 1)
        artist_a = parts[0].strip()
        artist_b = parts[1].strip()
        if artist_a and artist_b:
            artist_info_a = await search_artist(access_token, artist_a)
            artist_info_b = await search_artist(access_token, artist_b)
            if artist_info_a and artist_info_b:
                feat_a = await fetch_artist_audio_features(access_token, artist_info_a["artist_uris"])
                feat_b = await fetch_artist_audio_features(access_token, artist_info_b["artist_uris"])
                if feat_a and feat_b:
                    blended = blend_features(feat_a, feat_b)
                    blend_prompt = describe_blend(artist_a, artist_b, feat_a, feat_b, blended)
                    # Replace the "+" prompt with a blend-aware description
                    prompt = f"A musical blend of {artist_a} and {artist_b} — mix their styles, tempos, and sonic textures"

    # Fetch user's taste profile for personalization
    taste_profile_prompt = ""
    if body.use_personalization:
        try:
            top_tracks = await fetch_user_top_tracks(access_token, limit=50)
            top_artists = await fetch_user_top_artists(access_token, limit=20)
            saved_tracks = await fetch_user_saved_tracks(access_token, limit=50)
            all_uris = [t["uri"] for t in top_tracks] + [t["track"]["uri"] for t in saved_tracks]
            audio_features_map = await fetch_track_audio_features(access_token, all_uris)
            profile = analyze_taste_profile(top_tracks, top_artists, saved_tracks, audio_features_map)
            taste_profile_prompt = build_taste_profile_prompt(profile)
        except Exception:
            pass  # Proceed without personalization if profile fetch fails

    try:
        songs = get_songs_from_claude(prompt, song_count, taste_profile_prompt, blend_prompt)
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
