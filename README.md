# Avenor community bot

Community-only deployment for Python 3.11+. Code contains no credentials.

## Enabled in this version

- Welcome messages and fixed FAQ replies in one configured group.
- URL/forward blocking and a short message-flood filter.
- Owner-only group announcement, mute, unmute, ban, unban, and health commands in private chat.
- Telegram webhook transport on Render, with a secret verification header.
- Durable remote Turso database for stored user records.

## Disabled

`ENABLE_CHANNEL_ACCESS` and `ENABLE_STARS` must remain `false`.
There is no paid-channel sale, grant, join approval, expiry warning, or automatic expiry removal in this version. `/grant` and `/approve` refuse to create entitlements. Channel member events do not remove members. Enabling channel access requires a separately verified external scheduler and a reviewed deployment, not merely changing a setting. Sales also require owner-approved terms, price, payout eligibility, and live payment/refund testing.

## Render configuration

Free Web Service. Build: `pip install -r requirements.txt`. Start: `python bot.py`.
Use one instance and Python 3.11+. Render supplies `PORT` and `RENDER_EXTERNAL_URL`.

Set secrets only in private environment settings:

- `BOT_TOKEN`: BotFather token.
- `TURSO_DATABASE_URL`: URL of a libSQL database hosted by Turso.
- `TURSO_AUTH_TOKEN`: database-scoped credential.
- `ADMIN_ID`: owner's positive Telegram ID. Initially `0` for discovery.
- `GROUP_ID`: the intended group's negative Telegram ID. Initially `0` for discovery.
- `CHANNEL_ID`: optional linked channel ID for allowing its auto-forwards, otherwise `0`.
- `ENABLE_CHANNEL_ACCESS=false`
- `ENABLE_STARS=false`
- `ADMIN_CONTACT`: owner's support username, if supplied.
- `COMMUNITY_URL`: optional approved group URL.

With IDs at zero the bot is in discovery mode. Owner sends `/id` in private, then `/id` in the intended group. Configure the returned IDs and restart. Configure the linked channel ID before relying on its automatic discussion forwards. Test moderation with a second non-admin account, and run `/health` in the owner's private chat.

## Hosting limits

Render free services sleep after 15 minutes without inbound traffic. A new webhook can wake the service, but cold starts delay responses. This is not an uptime guarantee and is not suitable for time-critical trading signals. Remote storage survives Render restarts; local files do not. No local backup job runs in this version. The owner must arrange database exports and a retention/deletion policy before production use. Keep overage billing off and add no payment method.

The webhook secret is derived from the bot token and verified by the Telegram library. Never put the bot token in the webhook path, code repository, logs, or chat.

## Verification

21 offline tests pass for core ledger and access gates. Remote storage, actual Telegram permissions, webhook delivery, sleep recovery, and restart persistence still require live validation before describing deployment as complete.

Official references:
- https://render.com/docs/free
- https://render.com/docs/web-services
- https://docs.turso.tech/sdk/python/quickstart
