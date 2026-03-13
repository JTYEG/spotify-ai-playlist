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
};

let lastPrompt = "";
let currentSongs = [];
let currentUris = [];

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
// Core actions
// ---------------------------------------------------------------------------

async function fetchSongs(prompt) {
  setState(State.LOADING, { message: "Claude is picking songs and checking Spotify\u2026" });
  const song_count = parseInt(els.songCount.value, 10);
  try {
    const resp = await fetch("/api/get-songs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, song_count }),
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

els.btnGenerate.addEventListener("click", () => {
  const prompt = els.promptInput.value.trim();
  if (!prompt) {
    els.promptError.classList.remove("hidden");
    els.promptInput.focus();
    return;
  }
  els.promptError.classList.add("hidden");
  lastPrompt = prompt;
  fetchSongs(prompt);
});

els.btnSave.addEventListener("click", () => savePlaylist());

els.btnRegenerate.addEventListener("click", () => {
  if (lastPrompt) fetchSongs(lastPrompt);
});

els.btnAnother.addEventListener("click", () => {
  els.promptInput.value = "";
  currentSongs = [];
  currentUris = [];
  setState(State.LOGGED_IN);
  els.promptInput.focus();
});

els.btnMakeAnother.addEventListener("click", () => {
  els.promptInput.value = "";
  currentSongs = [];
  currentUris = [];
  setState(State.LOGGED_IN);
  els.promptInput.focus();
});

els.btnRetry.addEventListener("click", () => setState(State.LOGGED_IN));

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
