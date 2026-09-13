# LMS Notifier

Checks your e-GURO (City College of Calamba) dashboard daily and emails you
when something new shows up in DUE TODAY / ASSIGNED / MISSED / UNREAD.

## Status

This is a first version built from the dashboard's summary cards (counts
only). It does **not** yet pull individual assignment names/due dates — that
needs the `course_filter.php` page structure, which we'll add once you can
share what that page looks like when something is actually pending.

## One-time setup

### 1. Get a Gmail App Password
Regular Gmail passwords won't work for this. You need an "App Password":
1. Go to https://myaccount.google.com/apppasswords (you may need 2-Step
   Verification turned on first).
2. Create a new app password (name it anything, e.g. "lms-notifier").
3. Copy the 16-character password it gives you — you'll paste it as a secret
   below.

### 2. Create a GitHub repo
1. Create a new **private** repo on GitHub (keep it private since it touches
   your school login).
2. Upload these files to it (scraper.py, requirements.txt, state.json, and
   the `.github/workflows/check-lms.yml` folder — keep that folder structure
   exactly as-is).

### 3. Add your secrets
In your repo: **Settings -> Secrets and variables -> Actions -> New repository secret**.
Add each of these:

| Secret name | Value |
|---|---|
| `LMS_USERNAME` | your portal username |
| `LMS_PASSWORD` | your portal password |
| `GMAIL_ADDRESS` | the gmail address you made the app password for |
| `GMAIL_APP_PASSWORD` | the 16-character app password from step 1 |
| `NOTIFY_EMAIL` | where you want notifications sent (can be the same Gmail address, or your number's email-to-SMS gateway if your carrier supports it) |

### 4. Test it manually
Go to the **Actions** tab in your repo -> "Check LMS" workflow -> "Run workflow"
button. Watch the run — if it fails, click into it to see the error (it'll be
in the "Run checker" step).

### 5. Let it run
Once a manual run succeeds, it'll run automatically every day at the time set
in `check-lms.yml` (default 7 AM Philippine time). No further action needed.

## Known gaps / next steps

- `AGENTS_VALUE` in `scraper.py` is currently a guess (empty string). If login
  fails, this is the first thing to check.
- Only counts are scraped right now, not item names or due dates. Once you
  send a screenshot of the `course_filter.php` page (reachable by clicking
  a dashboard card), we can extend `scraper.py` to include actual assignment
  names in the email.
- Cron currently runs once a day. Easy to change to run more often by editing
  the `cron:` line in `check-lms.yml`.
