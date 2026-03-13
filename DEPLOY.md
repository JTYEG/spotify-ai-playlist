# Deploying to Render

## Prerequisites
- Code pushed to GitHub
- Spotify Developer app created at developer.spotify.com
- Anthropic API key

## Steps

### 1. Create a Render account
Go to render.com and sign in with GitHub.

### 2. Create a new Web Service
- Click **New** → **Web Service**
- Connect GitHub and select the `spotify-ai-playlist` repo
- If repo doesn't appear: GitHub → Settings → Applications → Render → Configure → grant repo access

### 3. Configure the service
- **Runtime:** Python 3
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
- **Instance Type:** Free

### 4. Add environment variables
In the **Environment** section add:

```
ANTHROPIC_API_KEY=your_anthropic_key
SPOTIFY_CLIENT_ID=your_spotify_client_id
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
SPOTIFY_REDIRECT_URI=https://<your-render-domain>.onrender.com/callback
SECRET_KEY=some-long-random-string
```

### 5. Deploy
Click **Create Web Service** — build takes ~2-3 minutes.

### 6. Update Spotify Dashboard
Go to developer.spotify.com → your app → Edit Settings → Redirect URIs → add:
```
https://<your-render-domain>.onrender.com/callback
```
Click **Save**.

### 7. Done
Open your Render URL in any browser. First load may take 30-60 seconds (free tier sleeps after inactivity).

## Updating the app
Push changes to GitHub — Render auto-deploys on every push to `master`.
