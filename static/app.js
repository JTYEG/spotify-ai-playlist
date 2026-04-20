// State machine: LOGGED_OUT → LOGGED_IN → LOADING → PREVIEW → SAVING → SUCCESS | ERROR
const State = { LOGGED_OUT: 0, LOGGED_IN: 1, LOADING: 2, PREVIEW: 3, SAVING: 4, SUCCESS: 5, ERROR: 6 };

const $ = (id) => document.getElementById(id);

const els = {
  sectionLoggedOut: $("section-logged-out"),
  sectionLoggedIn:  $("section-logged-in"),
  btnLogin:         $("btn-login"),
  btnGenerate:      $("btn-generate"),
  btnLogout:        $("btn-logout"),
  btnAnother:       $("btn-another"),
  btnMakeAnother:   $("btn-make-another"),
  btnRegenerate:    $("btn-regenerate"),
  btnSave:          $("btn-save"),
  btnRetry:         $("btn-retry"),
  welcomeText:      $("welcome-text"),
  promptInput:      $("prompt-input"),
  promptError:      $("prompt-error"),
  songCount:        $("song-count"),
  songCountLabel:   $("song-count-label"),
  playlistNameInput: $("playlist-name-input"),
  loadingText:      $("loading-text"),
  sectionLoading:   $("section-loading"),
  sectionPreview:   $("section-preview"),
  previewList:      $("preview-list"),
  sectionSuccess:   $("section-success"),
  sectionError:     $("section-error"),
  resultName:       $("result-name"),
  resultCount:      $("result-count"),
  resultLink:       $("result-link"),
  errorMessage:     $("error-message"),
  tasteProfile:     $("section-taste-profile"),
  profileLoading:   $("profile-loading"),
  profileContent:   $("profile-content"),
  genreTags:        $("genre-tags"),
  artistList:       $("artist-list"),
  moodGrid:         $("mood-grid"),
  audioBars:        $("audio-bars"),
  trackList:        $("track-list"),
  btnRefreshProfile: $("btn-refresh-profile"),
  btnUseProfile:    $("btn-use-profile"),
  seedInput:        $("seed-input"),
  blendIndicator:   $("blend-indicator"),
};

let lastPrompt = "";
let currentSongs = [];
let currentUris = [];
let tasteProfile = null;
let usePersonalized = false;
let blendArtists = null;

function setState(state, payload = {}) {
  els.sectionLoggedOut.classList.add("hidden");
  els.sectionLoggedIn.classList.add("hidden");
  els.sectionLoading.classList.add("hidden");
  els.sectionPreview.classList.add("hidden");
  els.sectionSuccess.classList.add("hidden");
  els.sectionError.classList.add("hidden");
  els.promptError.classList.add("hidden");
  els.btnGenerate.disabled = false;

  if (state === State.LOGGED_OUT) {
    els.sectionLoggedOut.classList.remove("hidden");
  }

  if (state === State.LOGGED_IN) {
    els.sectionLoggedIn.classList.remove("hidden");
    if (payload.displayName) {
      els.welcomeText.textContent = `Logged in as ${payload.displayName}`;
    }
  }

  if (state === State.LOADING) {
    els.sectionLoggedIn.classList.remove("hidden");
    els.sectionLoading.classList.remove("hidden");
    els.loadingText.textContent = payload.message || "Claude is picking songs\u2026";
    els.btnGenerate.disabled = true;
  }

  if (state === State.PREVIEW) {
    els.sectionLoggedIn.classList.remove("hidden");
    els.sectionPreview.classList.remove("hidden");
    els.previewList.innerHTML = "";
    (payload.songs || []).forEach(song => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="song-title">${song.title}</span><span class="song-artist">${song.artist}</span>`;
      els.previewList.appendChild(li);
    });
  }

  if (state === State.SUCCESS) {
    els.sectionLoggedIn.classList.remove("hidden");
    els.sectionSuccess.classList.remove("hidden");
    els.resultName.textContent = payload.playlistName || "";
    const count = payload.tracksFound || 0;
    els.resultCount.textContent = `${count} tracks added`;
    els.resultLink.href = payload.playlistUrl || "#";
  }

  if (state === State.ERROR) {
    els.sectionLoggedIn.classList.remove("hidden");
    els.sectionError.classList.remove("hidden");
    els.errorMessage.textContent = payload.message || "Something went wrong. Please try again.";
  }
}

// ---------------------------------------------------------------------------
// Taste profile rendering
// ---------------------------------------------------------------------------

function renderTasteProfile(profile) {
  tasteProfile = profile;

  // Show/hide elements
  els.profileLoading.classList.add("hidden");
  els.profileContent.classList.remove("hidden");
  els.tasteProfile.classList.remove("hidden");

  // Render genres
  els.genreTags.innerHTML = "";
  (profile.top_genres || []).forEach(g => {
    const tag = document.createElement("span");
    tag.className = "genre-tag";
    tag.textContent = `${g[0]} (${g[1]}x)`;
    els.genreTags.appendChild(tag);
  });

  // Render artists
  els.artistList.innerHTML = "";
  (profile.top_artists || []).forEach(a => {
    const span = document.createElement("span");
    span.textContent = a[0];
    els.artistList.appendChild(span);
  });

  // Render mood
  els.moodGrid.innerHTML = "";
  const moodItem = (label, value) => {
    const div = document.createElement("div");
    div.className = "mood-item";
    div.innerHTML = `<span class="mood-item-label">${label}</span><span class="mood-item-value">${value}</span>`;
    els.moodGrid.appendChild(div);
  };
  moodItem("Mood", profile.mood || "Unknown");
  moodItem("Tempo", profile.tempo_preference || "Unknown");
  moodItem("Tracks Analyzed", profile.total_tracks_analyzed || 0);

  // Render audio bars
  els.audioBars.innerHTML = "";
  const features = profile.avg_features || {};
  if (Object.keys(features).length > 0) {
    const barLabels = {
      danceability: "Dance",
      energy: "Energy",
      valence: "Positivity",
      acousticness: "Acoustic",
      instrumentalness: "Instrumental",
      speechiness: "Speech",
    };
    const barKeys = Object.keys(barLabels);
    barKeys.forEach(key => {
      const val = features[key];
      const pct = Math.round(val * 100);
      const row = document.createElement("div");
      row.className = "audio-bar-row";
      row.innerHTML = `
        <span class="audio-bar-label">${barLabels[key]}</span>
        <div class="audio-bar-track">
          <div class="audio-bar-fill" style="width: ${pct}%"></div>
        </div>
        <span class="audio-bar-value">${Math.round(val * 100)}</span>
      `;
      els.audioBars.appendChild(row);
    });
  }

  // Render track list
  els.trackList.innerHTML = "";
  (profile.top_track_names || []).slice(0, 5).forEach((name, i) => {
    const div = document.createElement("div");
    div.className = "track-item";
    div.innerHTML = `<span class="track-number">${i + 1}.</span><span>${name}</span>`;
    els.trackList.appendChild(div);
  });
}

async function fetchTasteProfile() {
  els.profileLoading.classList.remove("hidden");
  els.profileContent.classList.add("hidden");
  els.tasteProfile.classList.remove("hidden");

  try {
    const resp = await fetch("/api/taste-profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit: 50 }),
    });
    if (!resp.ok) {
      els.tasteProfile.classList.add("hidden");
      return;
    }
    const data = await resp.json();
    renderTasteProfile(data);
  } catch {
    els.tasteProfile.classList.add("hidden");
  }
}

// ---------------------------------------------------------------------------
// Artist blend detection
// ---------------------------------------------------------------------------

function updateBlendIndicator() {
  const seed = els.seedInput.value.trim();
  if (seed && " + " in seed) {
    const parts = seed.split(" + ", 1);
    if (parts[0].trim() && parts.length > 1) {
      const artistA = parts[0].trim();
      const artistB = parts[1].trim();
      if (artistB) {
        blendArtists = { a: artistA, b: artistB };
        els.blendIndicator.innerHTML = `
          <span class="blend-artist">${artistA}</span>
          <span class="blend-operator">+</span>
          <span class="blend-artist">${artistB}</span>
          <span class="blend-status">→ <span>Blending styles</span></span>
        `;
        els.blendIndicator.classList.remove("hidden");
        return;
      }
    }
  }
  blendArtists = null;
  els.blendIndicator.classList.add("hidden");
}

// ---------------------------------------------------------------------------
// Core actions
// ---------------------------------------------------------------------------

async function fetchSongs(prompt) {
  setState(State.LOADING, { message: "Claude is picking songs and checking Spotify\u2026" });
  const song_count = parseInt(els.songCount.value, 10);
  try {
    const resp = await fetch("/api/get-songs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, song_count, use_personalization: usePersonalized }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      const msg = Array.isArray(data.detail)
        ? data.detail.map(e => e.msg).join(", ")
        : (data.detail || `Error ${resp.status}`);
      setState(State.ERROR, { message: msg });
      return;
    }
    currentSongs = data.songs;
    currentUris = data.songs.map(s => s.uri);
    setState(State.PREVIEW, { songs: currentSongs });
  } catch {
    setState(State.ERROR, { message: "Network error — please check your connection and try again." });
  }
}

async function savePlaylist() {
  setState(State.LOADING, { message: "Saving to Spotify\u2026" });
  const nameSuffix = els.playlistNameInput.value.trim();
  const playlist_name = nameSuffix ? `AI Mix: ${nameSuffix}` : `AI Mix: ${lastPrompt.slice(0, 50)}`;
  try {
    const resp = await fetch("/api/save-playlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uris: currentUris, playlist_name }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      const msg = Array.isArray(data.detail)
        ? data.detail.map(e => e.msg).join(", ")
        : (data.detail || `Error ${resp.status}`);
      setState(State.ERROR, { message: msg });
      return;
    }
    setState(State.SUCCESS, {
      playlistName: data.playlist_name,
      playlistUrl: data.playlist_url,
      tracksFound: data.tracks_found,
    });
  } catch {
    setState(State.ERROR, { message: "Network error — please check your connection and try again." });
  }
}

// ---------------------------------------------------------------------------
// Event handlers
// ---------------------------------------------------------------------------

els.btnLogin.addEventListener("click", () => { window.location.href = "/auth/login"; });
els.btnLogout.addEventListener("click", () => { window.location.href = "/auth/logout"; });

els.songCount.addEventListener("input", () => {
  els.songCountLabel.textContent = els.songCount.value;
});

els.seedInput.addEventListener("input", () => {
  updateBlendIndicator();
});

els.btnGenerate.addEventListener("click", () => {
  const prompt = els.promptInput.value.trim();
  if (!prompt) {
    els.promptError.classList.remove("hidden");
    els.promptInput.focus();
    return;
  }
  els.promptError.classList.add("hidden");
  lastPrompt = prompt;
  if (blendArtists) {
    fetchSongs(prompt);
  } else {
    fetchSongs(prompt);
  }
});

els.btnSave.addEventListener("click", () => savePlaylist());

els.btnRegenerate.addEventListener("click", () => {
  if (lastPrompt) fetchSongs(lastPrompt);
});

els.seedInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    els.btnGenerate.click();
  }
});

els.btnAnother.addEventListener("click", () => {
  els.promptInput.value = "";
  els.seedInput.value = "";
  currentSongs = [];
  currentUris = [];
  blendArtists = null;
  els.blendIndicator.classList.add("hidden");
  setState(State.LOGGED_IN);
  els.promptInput.focus();
});

els.btnMakeAnother.addEventListener("click", () => {
  els.promptInput.value = "";
  els.seedInput.value = "";
  currentSongs = [];
  currentUris = [];
  blendArtists = null;
  els.blendIndicator.classList.add("hidden");
  setState(State.LOGGED_IN);
  els.promptInput.focus();
});

els.btnRetry.addEventListener("click", () => setState(State.LOGGED_IN));

els.btnRefreshProfile.addEventListener("click", () => fetchTasteProfile());

els.btnUseProfile.addEventListener("click", () => {
  usePersonalized = true;
  els.btnUseProfile.textContent = "Taste Profile Active \u2713";
  els.btnUseProfile.style.background = "var(--green)";
  els.btnUseProfile.style.color = "#000";
  els.btnUseProfile.style.opacity = "1";
  els.promptInput.focus();
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function init() {
  const params = new URLSearchParams(window.location.search);
  const oauthError = params.get("error");

  try {
    const resp = await fetch("/auth/status");
    const data = await resp.json();

    if (data.logged_in) {
      setState(State.LOGGED_IN, { displayName: data.display_name });
      if (oauthError) {
        setState(State.ERROR, { message: `Spotify login failed: ${oauthError}` });
      }
      fetchTasteProfile();
    } else {
      setState(State.LOGGED_OUT);
      if (oauthError) {
        const errEl = document.createElement("p");
        errEl.style.cssText = "color:#e74c3c;margin-top:1rem;font-size:.9rem;";
        errEl.textContent = `Login error: ${oauthError}`;
        $("section-logged-out").appendChild(errEl);
      }
    }
  } catch {
    setState(State.LOGGED_OUT);
  }
}

init();
