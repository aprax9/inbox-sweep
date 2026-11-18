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
- **Quota.** inbox-sweep throttles itself under Gmail's per-user limit and retries
  rate-limit errors. Reading 20,000 message headers takes a few minutes.
- **Revoke access** any time at <https://myaccount.google.com/permissions>.
- **Google Workspace accounts** may need an admin to allow the app (internal apps
  can use Audience: Internal).
