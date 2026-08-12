# SETUP — Trakt2Letterboxd Automated Sync

This guide sets up everything you need to run the weekly, fully-automated
Trakt → Letterboxd sync in GitHub Actions. Once configured, nothing else is
required: the workflow exports your Trakt movie history/watchlist every Sunday,
uploads the CSVs to your Letterboxd account via Playwright, and quietly rotates
the Trakt tokens when they approach their ~3-month expiry.

---

## What you need

| Item | Where to get it |
|---|---|
| A GitHub repository (this fork) | Your own fork of `Trakt2Letterboxd` |
| A Trakt account | [trakt.tv](https://trakt.tv) |
| A Letterboxd account (logged in, in a browser) | [letterboxd.com](https://letterboxd.com) |
| A GitHub Personal Access Token (PAT) | For auto-rotation of the Trakt secrets (see Step 4) |

---

## Step 1 — Create a Trakt API app

1. Go to **[https://trakt.tv/oauth/applications/new](https://trakt.tv/oauth/applications/new)**.
2. Fill in:
   - **Name:** e.g. `Trakt2Letterboxd Sync`
   - **Description:** anything
   - **Redirect URI:** `urn:ietf:wg:oauth:2.0:oob` (this is required for the
     device-code flow we use to mint the initial tokens — it is not a web URL)
3. Save. Note down the two values shown on the app page:
   - **Client ID** → becomes `TRAKT_CLIENT_ID`
   - **Client Secret** → becomes `TRAKT_CLIENT_SECRET`

> The script no longer contains anyone's hardcoded keys — everything comes
> from these two values (originally the upstream repo shipped the author's own
> keys, which is a security problem; you now use yours).

---

## Step 2 — Mint your initial Trakt OAuth tokens (cURL)

The old script used an interactive flow that ends with you pasting tokens into
a local file. With cURL you can run the **same device-code flow** entirely from
your terminal in about 30 seconds.

### 2.1 Request a device code

Replace `YOUR_CLIENT_ID` and run:

```bash
curl -s -X POST "https://api.trakt.tv/oauth/device/code" \
  -H "Content-Type: application/json" \
  -d '{"client_id":"YOUR_CLIENT_ID"}'
```

You get JSON like:

```json
{
  "device_code": "a1b2c3d4e5f6...",
  "user_code": "AB12-CD34",
  "verification_url": "https://trakt.tv/activate",
  "expires_in": 600,
  "interval": 5
}
```

Copy `device_code`, `user_code`, `verification_url`, and `interval` somewhere
handy. **`device_code` is needed for the next call and is single use.**

### 2.2 Approve the connection

1. Open `verification_url` (https://trakt.tv/activate) in your browser.
2. Enter `user_code` (e.g. `AB12-CD34`).
3. Approve / authorise the app.

### 2.3 Exchange the device code for tokens

Replace the placeholders and run (repeat this until it succeeds — it only
returns tokens once you have approved in the browser):

```bash
curl -s -X POST "https://api.trakt.tv/oauth/device/token" \
  -H "Content-Type: application/json" \
  -d '{"code":"DEVICE_CODE","client_id":"YOUR_CLIENT_ID","client_secret":"YOUR_CLIENT_SECRET"}'
```

Copy the whole JSON response into a file named `tokens.json`. It looks like:

```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "expires_in": 7776000,
  "refresh_token": "apXj9m...",
  "scope": "public",
  "created_at": 1712345678
}
```

You now have your `access_token` and `refresh_token`.

### 2.4 Compute `TRAKT_TOKEN_EXPIRES_AT`

The script needs to know *when* the access token dies so it can refresh it
proactively (it refreshes when there are fewer than 7 days left). Trakt gives
you `created_at` + `expires_in` (both in seconds) — the expiry epoch is simply:

```bash
python3 -c "import json; d=json.load(open('tokens.json')); print(d['created_at'] + d['expires_in'])"
```

(Or, if you use `jq`: `jq '.created_at + .expires_in' tokens.json`.)

> The script **does not trust its own estimate** of "3 months" — it uses the
> exact `created_at`/`expires_in` values Trakt returned, and it also refreshes
> on any HTTP 401 just in case. `TRAKT_TOKEN_EXPIRES_AT` is refreshed
> automatically after every rotation, so you only compute it once here.

### 2.4b Postman alternative

The flow is identical in Postman — two `POST` requests to
`https://api.trakt.tv/oauth/device/code` and
`https://api.trakt.tv/oauth/device/token` with the JSON bodies above, then the
browser approval step in between. The cURL route is faster, though.

---

## Step 3 — Extract your Letterboxd session cookie

The uploader does **not** log in with username/password (that triggers a
CAPTCHA). Instead it reuses the `lbx_session` cookie from a browser where you
are already logged in.

1. Open [letterboxd.com](https://letterboxd.com) in Chrome/Edge/Firefox and
   make sure you are logged in.
2. Open **Developer Tools** (`F12`):
   - **Chrome/Edge:** *Application* tab → *Cookies* → `https://letterboxd.com`
   - **Firefox:** *Storage* tab → *Cookies* → `https://letterboxd.com`
   - **Safari:** *Web Inspector* → *Storage* → *Cookies*
3. Find the row named **`lbx_session`** and copy its **Value** (it is a very
   long string).
4. This value becomes `LETTERBOXD_SESSION_COOKIE`.

> ⚠️ If you log out of Letterboxd, clear cookies, or the session expires, the
> upload step will fail with a screenshot. Just re-extract a fresh cookie and
> update the secret — no other step changes.
>
> The session cookie can also be invalidated by unusual activity patterns; the
> workflow runs once a week, so a fresh cookie typically lasts indefinitely. If
> you see repeated failures, obtain a cookie from a browser you actually use
> regularly, or log in once in the browser before extracting it.

---

## Step 4 — Create a GitHub PAT for automatic secret rotation

The default `GITHUB_TOKEN` that GitHub Actions uses **cannot create or update
repository secrets** (GitHub's API restricts secret management to a PAT or
GitHub App). To let the workflow rotate `TRAKT_ACCESS_TOKEN`,
`TRAKT_REFRESH_TOKEN`, and `TRAKT_TOKEN_EXPIRES_AT` by itself, create a PAT:

1. Go to **[https://github.com/settings/tokens/new](https://github.com/settings/tokens/new)**
   (Personal access tokens → *Tokens (classic)* → *Generate new token*).
2. Give it a name like `Trakt2Letterboxd secret rotation`.
3. Select scope: **`repo`** (full control of private repositories; if this
   fork is public, `repo` still covers it — the minimal requirement is the
   **`Secrets`** repository/admin permission available on fine-grained tokens,
   but a classic `repo` token is simplest).
4. *Generate token* → copy the value. It is shown once.
5. This value becomes `GH_TOKEN`.

---

## Step 5 — Add the secrets to your repository

Go to your fork → **Settings → Secrets and variables → Actions → New
repository secret** and add all of these:

| Secret name | Value |
|---|---|
| `TRAKT_CLIENT_ID` | Step 1 — Client ID |
| `TRAKT_CLIENT_SECRET` | Step 1 — Client Secret |
| `TRAKT_ACCESS_TOKEN` | Step 2.3 — `access_token` |
| `TRAKT_REFRESH_TOKEN` | Step 2.3 — `refresh_token` |
| `TRAKT_TOKEN_EXPIRES_AT` | Step 2.4 — computed epoch (e.g. `1747872000`) |
| `LETTERBOXD_SESSION_COOKIE` | Step 3 — `lbx_session` value |
| `GH_TOKEN` | Step 4 — PAT value |

7 secrets total. Leave `TRAKT_TOKEN_EXPIRES_AT` blank (or delete it) only if
you want to rely purely on the 401-driven refresh — the proactive refresh is
better, so please set it.

---

## Step 6 — Test it

1. In your fork, open the **Actions** tab.
2. Select the **Trakt to Letterboxd Sync** workflow.
3. Click **Run workflow** (this is the `workflow_dispatch` trigger) and run
   with defaults.
4. Watch the run. You should see the export steps succeed, the CSVs appear as
   the `letterboxd-ready-csvs` artifact (downloadable for 7 days from the run
   page), and the upload step report each chunk as imported.
5. Confirm the films on [letterboxd.com/import](https://letterboxd.com/import)
   history — or check your diary.

> **Tip for the very first run:** if you want to avoid touching Letterboxd
> while you test, re-run the workflow with the **`skip_upload`** input checked.
> The CSVs still get exported as an artifact and you can inspect them
> (`WatchedDate,tmdbID,imdbID,Title,Year` headers, one movie per row, no TV
> episodes, every file under 1 MB).

---

## What happens every Sunday (00:00 UTC)

1. Workflow checks out the repo, sets up Python, installs deps + Chromium.
2. The script checks `TRAKT_TOKEN_EXPIRES_AT`:
   - still valid → uses it;
   - within 7 days of expiry → refreshes via the refresh token, writes the new
     pair + new expiry to `GITHUB_OUTPUT`;
   - gets a 401 mid-run → refreshes and retries.
3. It fetches `history` and `watchlist` from Trakt (movies only, TV dropped),
   paginating 100 at a time.
4. It writes Letterboxd-ready CSVs into `exports/`, chunked if needed so each
   file stays under 950 KB with the headers repeated per chunk.
5. It uploads each chunk to `https://letterboxd.com/import/` using the
   `lbx_session` cookie, clicks the "Import X films" button, and waits for the
   success confirmation.
6. If a refresh happened, the workflow updates the Trakt secrets for next
   week's run.
7. The CSVs are attached to the run as the `letterboxd-ready-csvs` artifact
   (7-day retention) — your safety net and your manual-upload fallback.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Missing required environment variable(s)` | You skipped one of the 7 secrets in Step 5. |
| `Token refresh failed with HTTP 401` | The refresh token is dead (e.g. inactive > some months). Re-run Step 2 (cURL) to mint a fresh `access_token` / `refresh_token`, recompute `TRAKT_TOKEN_EXPIRES_AT`, update the three secrets. Only this one-time manual step can't be automated. |
| `gh secret set` fails with 403 | `GH_TOKEN` PAT is wrong/expired/insufficient. Recreate it per Step 4 and update the secret. Workflow itself still completes; only rotation fails. |
| `Cloudflare Turnstile challenge detected` | Letterboxd's bot fence tripped. Screenshots land in the `letterboxd-upload-debug` artifact (7 days). Usually re-running the workflow or re-extracting a fresh `lbx_session` cookie resolves it. |
| `Letterboxd is asking for login` | `LETTERBOXD_SESSION_COOKIE` expired / invalidated. Re-extract per Step 3. |
| `The Import X films confirmation button never appeared` | The CSV was accepted by the file picker but didn't parse — usually means the file's headers don't match. The debug screenshot shows the preview page; the export CSVs are still in the `letterboxd-ready-csvs` artifact. |
| Export succeeds but no films imported | The uploaded chunk may have been a fresh continuation of history (nothing new) — normal. If truly missing, your `lbx_session` may belong to a different Letterboxd account. |
| No artifact on the run page | The run failed before any CSVs were produced (check step logs) — e.g. Trakt auth or an empty export. |

---

## Security notes

- **Never commit** access/refresh tokens, the session cookie, or your PAT.
  Everything lives in GitHub Secrets and is only passed via environment
  variables. `exports/`, `debug/`, and `*.csv` are gitignored.
- The PAT (`GH_TOKEN`) can write repository secrets by design — that is its
  only job. Consider a fine-grained PAT limited to this one repository.
- The `lbx_session` cookie grants full access to your Letterboxd account while
  valid. Treat it exactly like a password.
- Deleting a secret does not remove it from historical workflow logs, but since
  tokens are **never echoed in logs** (only written to `GITHUB_OUTPUT`), there
  is nothing sensitive to scrub.

---

## Local (non-CI) usage

You can still run the script on your machine for a quick check:

```bash
pip install -r requirements.txt
export TRAKT_CLIENT_ID=... TRAKT_CLIENT_SECRET=... \
       TRAKT_ACCESS_TOKEN=... TRAKT_REFRESH_TOKEN=... \
       TRAKT_TOKEN_EXPIRES_AT=... LETTERBOXD_SESSION_COOKIE=...

# Export only (no browser involved):
python Trakt2Letterboxd.py --skip-upload

# Export + upload:
python Trakt2Letterboxd.py

# Just one list:
python Trakt2Letterboxd.py --lists watchlist
```

If the script refreshes tokens during a local run it prints them to stdout so
you can update your secrets manually.