# SETUP — Trakt2Letterboxd Automated Sync

This guide configures the weekly Trakt → Letterboxd sync in GitHub Actions. The
exporter reads movie history and ratings from a public Trakt profile, creates
Letterboxd-ready CSV files, and can upload them through a logged-in Letterboxd
browser session.

## What you need

| Item | Purpose |
|---|---|
| A GitHub repository containing this project | Runs the scheduled workflow |
| A Trakt account | Supplies the public movie history and ratings |
| A Letterboxd account | Receives the imported history |
| A browser session logged in to Letterboxd | Supplies the upload cookie |

---

## Step 1 — Make your Trakt profile public

The history and ratings endpoints only work for public profiles. This step is
required.

1. Sign in to [Trakt](https://trakt.tv).
2. Open your **Privacy Settings**.
3. Set your profile visibility to **Public**.
4. Save the setting and confirm that your profile can be viewed while signed out
   or in a private browser window.

The workflow only reads public movie history and ratings. It does not need a
private account credential or an interactive Trakt login.

---

## Step 2 — Add the Trakt username to GitHub Secrets

Open your repository and go to **Settings → Secrets and variables → Actions → New
repository secret**. Add:

| Secret name | Value |
|---|---|
| `TRAKT_USERNAME` | Your public Trakt profile name, exactly as shown in your profile URL |

The supplied public Trakt Client ID is already configured by the script and the
workflow. You do **not** need to create an API application or add a Client ID
secret for normal use. An optional `TRAKT_CLIENT_ID` repository secret can be
provided only when intentionally overriding the default public key.

---

## Step 3 — Configure mobile cookie refresh

The recommended setup uses a temporary browser hosted by GitHub Actions. When a
session expires, the sync workflow automatically starts the refresh workflow and
links to it in the failed run summary. Open that run on your phone, tap the
temporary browser link, and sign in to Letterboxd once. The runner reads the
httpOnly cookies directly and updates all three repository secrets; the sync is
then started again automatically.

The refresh workflow must be merged into the repository's default branch before
this link can work. GitHub does not expose `workflow_dispatch` workflows from a
feature branch as repository workflow URLs. If the report says the workflow is
only present on the current branch, merge that branch first and rerun the sync.

### Create the one-time automation token

GitHub's built-in `GITHUB_TOKEN` cannot update repository secrets, so create a
fine-grained personal access token at
[GitHub token settings](https://github.com/settings/personal-access-tokens/new):

1. Restrict it to **Only select repositories** and select this repository.
2. Grant **Actions: Read and write** so the refresh can re-run the sync.
3. Grant **Secrets: Read and write** so the refresh can replace the three
   Letterboxd cookie secrets.
4. Copy the token once and add it as a repository secret named `SECRETS_PAT` at
   **Settings → Secrets and variables → Actions → New repository secret**.

The token is used only by GitHub Actions. It is never printed, committed, or
sent to the remote browser.

### Refresh from a phone

1. Open the failed **Trakt to Letterboxd Sync** run from the repository's
   **Actions** tab.
2. In the **Letterboxd session expired** summary section, open the active
   **refresh run** link.
3. Open **Open remote Letterboxd browser** from that run's summary.
4. Sign in to Letterboxd in the temporary browser. Complete Turnstile there if
   it appears.
5. Wait for the refresh run to report that secrets were updated and the sync was
   re-triggered.

The temporary link expires when the refresh run ends. You only need to sign in
again when the Letterboxd session expires, normally weeks or months later.

### Manual fallback

The uploader does not use a username/password login. It reuses three cookies
from a browser where you are already logged in: the session cookie
(`letterboxd.user.CURRENT`), the CSRF cookie (`com.xk72.webparts.csrf`), and
optionally the Cloudflare clearance cookie (`cf_clearance`).

1. Open [letterboxd.com](https://letterboxd.com) and make sure you are logged in.
2. Open Developer Tools (`F12`).
   - **Chrome/Edge:** **Application → Cookies → `https://letterboxd.com`**
   - **Firefox:** **Storage → Cookies → `https://letterboxd.com`**
   - **Safari:** **Web Inspector → Storage → Cookies**
3. Copy the **Value** of each of these rows:
   - **`letterboxd.user.CURRENT`** — the session cookie (required).
   - **`com.xk72.webparts.csrf`** — the CSRF token (required).
   - **`cf_clearance`** — Cloudflare clearance (optional, helps avoid the
     Turnstile challenge).
4. Add repository secrets containing those values:
   - `LETTERBOXD_SESSION_COOKIE` ← `letterboxd.user.CURRENT` value
   - `LETTERBOXD_CSRF_COOKIE` ← `com.xk72.webparts.csrf` value
   - `LETTERBOXD_CF_CLEARANCE` ← `cf_clearance` value (optional)

If mobile refresh is not configured, or if the refresh workflow is unavailable,
repeat this step and update the secrets manually. The session and CSRF cookies
are the ones to watch; `cf_clearance` is long-lived (~1 year).

---

## Step 4 — Run the workflow

1. Open the repository's **Actions** tab.
2. Select **Trakt to Letterboxd Sync**.
3. Choose **Run workflow**.
4. For a safe first test, enable `skip_upload`. This exports the CSV without
   opening Letterboxd.
5. Download the `letterboxd-ready-csvs` artifact and check the history rows.
6. Run again with `skip_upload` disabled to perform the Letterboxd import.

The scheduled workflow runs every Sunday at 00:00 UTC. Each run uploads its CSV
files as an artifact for seven days.

---

## What the workflow does

1. Checks out the project and installs Python and Playwright.
2. Reads `TRAKT_USERNAME` and the built-in `TRAKT_CLIENT_ID` configuration.
3. Requests the public movie history endpoint:
   `https://api.trakt.tv/users/{username}/history/movies`.
4. Requests the public movie ratings endpoint:
   `https://api.trakt.tv/users/{username}/ratings/movies`.
5. Fetches all pages from both endpoints, retaining movie records only, and
   merges the latest `rated_at` rating onto every matching history event.
6. Writes Trakt's integer 1–10 rating to Letterboxd's `Rating10` column;
   watched-but-unrated movies retain a blank value.
7. Writes `letterboxd_history.csv` to `exports/`, splitting large exports into
   numbered parts below Letterboxd's 1 MB limit.
8. Optionally injects the Letterboxd session, CSRF, and clearance cookies and
   uploads each CSV.
9. If Letterboxd asks for login, starts the mobile cookie-refresh workflow and
   links to the active run in the failed job summary.
10. Stores the generated CSV files as the `letterboxd-ready-csvs` artifact.

## Local usage

Install the dependencies and create a local environment file from the template:

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set TRAKT_USERNAME.
```

Export history without opening a browser:

```bash
python Trakt2Letterboxd.py --skip-upload
```

To upload locally, also add the session and CSRF cookies (and optionally
`cf_clearance`) to `.env`:

```bash
# Edit .env and set:
# LETTERBOXD_SESSION_COOKIE=your_session_cookie_value
# LETTERBOXD_CSRF_COOKIE=your_csrf_cookie_value
# LETTERBOXD_CF_CLEARANCE=your_cf_clearance_value
python Trakt2Letterboxd.py
```

Uploads always use headed Chromium. To run a local upload, use:

```bash
python Trakt2Letterboxd.py
```

The local headed run pauses after an upload failure, allowing inspection of the
visible page before the browser closes. The scheduled workflow also launches
Chromium in headed mode, but inside an Xvfb virtual display so it remains
unattended while avoiding the Cloudflare headless-browser challenge. Trakt
requests and Playwright waits use a 15-second timeout. There is no headless
upload mode; use the export-only option when no browser is needed.

`Trakt2Letterboxd.py` loads `.env` automatically for local runs. Existing shell
variables take precedence over `.env`, which keeps CI and one-off overrides
working as before. The `.env` file is ignored by Git and must never be committed.

The Client ID default is already present in the script. To override it locally,
add `TRAKT_CLIENT_ID` to `.env` or set it before running the command.

## Troubleshooting

| Symptom | Resolution |
|---|---|
| `TRAKT_USERNAME is required` | Add the public profile name to the environment or the `TRAKT_USERNAME` repository secret. |
| Trakt returns `401`, `403`, or no history or ratings | Confirm the profile visibility is **Public**, check the username, and wait briefly after changing privacy settings. |
| `LETTERBOXD_SESSION_COOKIE and LETTERBOXD_CSRF_COOKIE are required` | Set both cookie secrets, or run with `--skip-upload` when only an export is needed. |
| Letterboxd asks for login | Open the active mobile cookie-refresh run linked in the failed job summary and sign in once. If `SECRETS_PAT` is not configured, use the manual fallback in Step 3. |
| Cloudflare or Turnstile appears | Check the `letterboxd-upload-debug` artifact and retry with a current `cf_clearance` cookie. |
| No CSV artifact is produced | Inspect the workflow log for the Trakt response or an empty history export. |

## Security notes

- Keep the Letterboxd cookie secrets private. They grant access to the
  associated Letterboxd session while valid.
- Keep `SECRETS_PAT` private. It can update this repository's Actions secrets and
  dispatch workflows; revoke it immediately if exposed.
- The temporary noVNC link grants access to the runner's Letterboxd browser while
  the refresh run is active. Do not share it; it expires when the run ends.
- The phone's browser interaction, including login input, is carried through the
  HTTPS/WSS Cloudflare tunnel to the temporary runner. Letterboxd receives the
  login request directly from the runner, but this design requires trusting the
  short-lived tunnel service.
- GitHub-hosted runners use dynamic IPs. The refreshed session and CSRF cookies
  are reusable across jobs; `cf_clearance` may still require a new Turnstile
  challenge during the later sync if its IP binding does not match.
- The Trakt history and ratings requests are intentionally public and do not
  send a private account credential.
- Do not commit local environment files or session-cookie values.
- Generated CSVs and debug screenshots are ignored locally and are available only
  as short-lived workflow artifacts.
