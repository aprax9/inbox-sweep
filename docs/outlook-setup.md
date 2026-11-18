# Outlook setup (Outlook.com, Hotmail, Live, Microsoft 365)

inbox-sweep talks to Outlook through Microsoft Graph, using **your own** app
registration. Your mail is never routed through anyone else's app, and it's free.

1. Sign in at <https://entra.microsoft.com> (or <https://portal.azure.com>), then go to
   **Identity, Applications, App registrations, New registration**.
   - Name: `inbox-sweep`.
   - Supported account types: **Accounts in any organizational directory and
     personal Microsoft accounts**. Choose "Personal Microsoft accounts only" if
     you only clean an Outlook.com or Hotmail mailbox.
   - Redirect URI: platform **Public client/native (mobile & desktop)**, value
     `https://login.microsoftonline.com/common/oauth2/nativeclient`.
2. In the new app, open **Authentication**. Under *Advanced settings*, set
   **Allow public client flows** to **Yes**. This enables the device-code sign-in.
3. **API permissions, Add a permission, Microsoft Graph, Delegated**: add
   `Mail.ReadWrite` (`User.Read` is there by default). Work or school tenants may
   need an admin to grant consent.
4. Copy the **Application (client) ID** from the Overview page into `sweep.toml`:

   ```toml
   [backend]
   type = "outlook"

   [outlook]
   client_id = "00000000-0000-0000-0000-000000000000"
   authority = "common"     # or "consumers" for personal accounts only, or your tenant id
   ```

5. `pip install 'inbox-sweep[outlook]'` (or `uv tool install "inbox-sweep[outlook] @ git+https://github.com/aprax9/inbox-sweep"`).
6. Run `inbox-sweep auth`. It prints a code and a link
   (<https://microsoft.com/devicelogin>). Enter the code, sign in, and accept the permissions.

The token cache is saved to `~/.config/inbox-sweep/outlook-token.json` with `0600`
permissions and refreshes itself.

## How Outlook mode behaves

- **Reads headers only.** The request lists exactly the fields used (sender,
  recipients, subject, date, folder, categories, flag, list headers). Message
  bodies and previews are never requested.
- **Scans every folder** except Deleted Items, Junk Email, Drafts, Outbox and
  conversation history. Folder names (e.g. `Taxes/2015`) act as labels for
  `[protect] labels`. Flagged mail counts as starred. Outlook conversations are threads.
- **The keep label is an Outlook category** (`Keep` by default), added alongside
  any categories a message already has.
- **Trash means Deleted Items.** Undo moves each message back to the folder it came
  from, or to the Inbox if that folder no longer exists.
- Requests use immutable ids, and Graph throttling is handled with `Retry-After`.

## Notes

- **Permissions** are delegated, so inbox-sweep acts only as you, only on your mailbox.
  `Mail.ReadWrite` can move and categorise mail. inbox-sweep never calls the
  permanent-delete endpoints.
- **Revoke access** any time at <https://account.live.com/consent/Manage> (personal)
  or <https://myapps.microsoft.com> (work or school).
- **No Entra directory?** Registering an app needs one. Work and school accounts
  already have one. With only a personal account, the portal may offer to create a free one.
- **Unsubscribing** works the same as for Gmail: one-click (RFC 8058) URLs from the
  sender's own headers.
