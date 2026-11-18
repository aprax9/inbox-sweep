# Gmail API setup (about 5 minutes, once)

inbox-sweep uses **your own** Google Cloud OAuth client, so your mail is never
routed through anyone else's app. It's free.

1. Go to <https://console.cloud.google.com/> and create a project (any name,
   e.g. `inbox-sweep`).
2. **APIs & Services → Library**, search for **Gmail API**, click **Enable**.
3. **Google Auth Platform** (formerly "OAuth consent screen"):
   - App name: `inbox-sweep`. Support and developer email: yours.
   - Audience: **External**. Leave the app in **Testing**.
   - Under **Test users**, add the Gmail address you're cleaning.
4. **Clients → Create client**. Application type **Desktop app**.
   Download the JSON and save it as `~/.config/inbox-sweep/credentials.json`
   (or point `[backend] credentials` in `sweep.toml` at it).
5. Run `inbox-sweep auth`. A browser opens. Google will say the app isn't
   verified: that's expected, it's your own app. Choose **Continue** and allow.

The token is saved to `~/.config/inbox-sweep/token.json` with `0600` permissions.

## Notes

- **Scope.** Only `https://www.googleapis.com/auth/gmail.modify` is requested. It
  can read headers, add labels and move to Trash. It cannot delete permanently
  or change settings.
- **Testing mode expires tokens after 7 days.** If a later run says the token
  expired, run `inbox-sweep auth` again. Don't publish the app to "Production"
  for personal use: Gmail scopes are restricted and need Google's review.
- **Quota, and how long the first index takes.** Google Cloud projects created on
  or after 1 May 2026 get 6,000 quota units per user per minute, and reading one
  message's headers costs 20 units. inbox-sweep paces itself at 90% of that, so a
  **first** index reads about 4.5 messages a second: 20,000 messages in about
  75 minutes, 100,000 in about 6 hours. It's resumable, and later runs read only
  new mail. Trashing and labelling are fast either way (1,000 messages per call).
  Projects created before May 2026 keep the older, higher limits: set
  `[gmail] quota = "legacy"` (about 45 messages a second). For a very large
  inbox, [Gmail over IMAP](imap-setup.md) has no per-message quota and is much
  faster to index.
- **Revoke access** any time at <https://myaccount.google.com/permissions>.
- **Google Workspace accounts** may need an admin to allow the app (internal apps
  can use Audience: Internal).
