# SETUP — Trakt2Letterboxd Automated Sync

This guide configures the weekly Trakt → Letterboxd sync in GitHub Actions. The
exporter reads movie history from a public Trakt profile, creates Letterboxd-ready
CSV files, and can upload them through a logged-in Letterboxd browser session.

## What you need

| Item | Purpose |
|---|---|
| A GitHub repository containing this project | Runs the scheduled workflow |
| A Trakt account | Supplies the public movie history |
| A Letterboxd account | Receives the imported history |
| A browser session logged in to Letterboxd | Supplies the upload cookie |

---

## Step 1 — Make your Trakt profile public

The history endpoint only works for public profiles. This step is required.

1. Sign in to [Trakt](https://trakt.tv).
2. Open your **Privacy Settings**.
3. Set your profile visibility to **Public**.
4. Save the setting and confirm that your profile can be viewed while signed out
   or in a private browser window.

The workflow only reads public movie history. It does not need a private account
credential or an interactive Trakt login.

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

## Step 3 — Extract your Letterboxd session cookie

The uploader does not use a username/password login. It reuses the `lbx_session`
cookie from a browser where you are already logged in.

1. Open [letterboxd.com](https://letterboxd.com) and make sure you are logged in.
2. Open Developer Tools (`F12`).
   - **Chrome/Edge:** **Application → Cookies → `https://letterboxd.com`**
   - **Firefox:** **Storage → Cookies → `https://letterboxd.com`**
   - **Safari:** **Web Inspector → Storage → Cookies**
3. Find the row named **`lbx_session`** and copy its **Value**.
4. Add another repository secret named `LETTERBOXD_SESSION_COOKIE` containing
   that value.

If you log out of Letterboxd, clear cookies, or the session expires, repeat this
step and update the secret.

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
4. Fetches all pages, retaining movie records only.
5. Writes `letterboxd_history.csv` to `exports/`, splitting large exports into
   numbered parts below Letterboxd's 1 MB limit.
6. Optionally injects `LETTERBOXD_SESSION_COOKIE` and uploads each CSV.
7. Stores the generated CSV files as the `letterboxd-ready-csvs` artifact.

## Local usage

Install the dependencies and set the public profile name:

```bash
pip install -r requirements.txt
export TRAKT_USERNAME=your_trakt_username
```

Export history without opening a browser:

```bash
python Trakt2Letterboxd.py --skip-upload
```

To upload locally, also set `LETTERBOXD_SESSION_COOKIE`:

```bash
export LETTERBOXD_SESSION_COOKIE=your_lbx_session_value
python Trakt2Letterboxd.py
```

The Client ID default is already present in the script. To override it locally,
set `TRAKT_CLIENT_ID` before running the command.

## Troubleshooting

| Symptom | Resolution |
|---|---|
| `TRAKT_USERNAME is required` | Add the public profile name to the environment or the `TRAKT_USERNAME` repository secret. |
| Trakt returns `401`, `403`, or no history | Confirm the profile visibility is **Public**, check the username, and wait briefly after changing privacy settings. |
| `LETTERBOXD_SESSION_COOKIE is required` | Set the cookie secret, or run with `--skip-upload` when only an export is needed. |
| Letterboxd asks for login | Re-extract `lbx_session` from a currently logged-in browser session and replace the secret. |
| Cloudflare or Turnstile appears | Check the `letterboxd-upload-debug` artifact and retry with a current session cookie. |
| No CSV artifact is produced | Inspect the workflow log for the Trakt response or an empty history export. |

## Security notes

- Keep `LETTERBOXD_SESSION_COOKIE` private. It grants access to the associated
  Letterboxd session while valid.
- The Trakt history request is intentionally public and does not send a private
  account credential.
- Do not commit local environment files or session-cookie values.
- Generated CSVs and debug screenshots are ignored locally and are available only
  as short-lived workflow artifacts.
