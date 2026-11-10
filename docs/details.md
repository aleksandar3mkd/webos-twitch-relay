# My Streams: the details

Everything that did not fit in the [README](../README.md).

## Start the server at boot

On Linux with systemd, this keeps it running after you log out and starts it
at boot:

```sh
mkdir -p ~/.config/systemd/user
cp deploy/my-streams.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now my-streams.service
loginctl enable-linger "$USER"
```

The service file expects the folder at `~/apps/my-streams`. If yours is somewhere
else, change the three paths in your copy of the file.

## Using it

- **Arrows and OK** on the remote move between channels and play one. **Back**
  returns to the library while the stream keeps playing behind it. The first
  button press while watching only shows the controls.
- **+ Add channel** takes a Twitch channel name or link. Typing on a TV is
  slow, so add channels from a phone or computer when you can.
- **Past broadcasts:** paste a `twitch.tv/videos/...` link into **+ Add
  channel** and choose **Save & watch**. It plays with seeking, and **Continue
  watching** remembers where you stopped, on every screen. **History** lists
  what you watched.
- **Manage** removes channels: choose one, then choose it again to confirm.
- **Quality** switches between 720p, 1080p and 1080p 60fps. Sources are never
  upscaled. Use 720p if the picture stutters.
- While a past broadcast plays: **Left/Right** skip 10 seconds, **OK** pauses,
  and the remote's play, pause and seek keys work.
- **Every open screen follows along:** start a stream on your phone and the TV
  switches to it. Closing the page does not stop the stream; use **Stop**. A
  stream nobody has watched for 15 minutes stops by itself.

| Quality | Encoding | Video bitrate |
| --- | --- | --- |
| 720p | 1280×720, 30 fps, H.264 | 2.2 Mbps |
| 1080p | 1920×1080, 30 fps, H.264 High | 5 Mbps |
| 1080p 60fps | 1920×1080, 60 fps, H.264 High | 7.5 Mbps |

## Your Twitch follows

Connect your Twitch account and **Live now** shows everyone you follow who is
live, without adding them one by one. Access is read-only and the tokens stay
on your server.

The owner sets this up once, on a computer. It is free:

1. Go to <https://dev.twitch.tv/console>, sign in, choose **Register Your Application**.
2. Name: anything. OAuth Redirect URL: `http://localhost`. Category: *Other*.
   Client type: **Public**. Create it.
3. Open the app and copy its **Client ID**.
4. In My Streams, open **People**, paste it under **Twitch sign-in** and choose **Save**.

After that, each person opens **Accounts → Connect Twitch**, scans the code
with their phone and approves.

## People and signing in

One server can be shared. Everyone has their own library, Twitch account,
quality setting and player. There are no passwords.

1. **People → Invite** adds a person and shows an invite link, a QR code and an
   invite code, valid for 7 days. Opening the link signs that browser in.
2. **A TV** (or any browser that is not signed in) shows a QR code. Scan it with
   a signed-in phone and choose **Approve**.
3. **People → Remove** signs out all of that person's devices and deletes their
   data. **Accounts → Sign out this device** signs out one device.

At most three streams run at the same time; change it with `--max-streams`.

## Extras

- **Chrome extension:** open `chrome://extensions`, turn on **Developer mode**,
  choose **Load unpacked** and select the `extension/` folder. Open it, enter
  your server's address under ⚙ and choose **Save**. On a Twitch page it offers
  **Save to library** and **Play on TV**; right-clicking a Twitch page or link
  offers the same.
- **iPhone or iPad app:** open the player in Safari, tap **Share**, then **Add
  to Home Screen**. The Home Screen app asks once for a sign-in code, shown in
  the banner or under **Accounts → Sign in another device**. Past broadcasts
  then use the iPhone's own player, with AirPlay and picture in picture.

## Questions

**Do I have to root or modify the TV?**
No. It uses LG's own Developer Mode, the official way to install your own app.
The catch is that Developer Mode has to be extended now and then.

**Why does it need a separate computer?**
The TV's built-in browser is from 2014 and Twitch's own player does not run on
it. The server does the fetching and converting; the TV only plays plain video.

**Will it work on my TV?**
It was written for and tested on one LG B7 with webOS 3.5. Other LG webOS TVs
may work. If you try one, please report what happened.

**Does it cost anything?**
No.

**Is there chat?**
No. It plays the video and nothing else.

**Can I watch away from home?**
Through a VPN into your home network, yes. Do not open the server's port to the
internet; see [Security](#security).

**Is it in the LG Content Store?**
No, and it is not meant to be. You install it yourself.

**Is this allowed?**
It is a tool for watching, at home, what you could already watch. Twitch's
terms are yours to follow, and you should not run it as a service for others.

## How it works

```
TV app ──opens──▶ player page ◀──same page── phone, computer
                      │
                      ▼
              server.py on your computer
   Streamlink (live) / yt-dlp (past broadcasts) ──▶ FFmpeg ──▶ HLS files
                      │
                      ▼
            the TV's own video player
```

- Live channels are fetched with Streamlink and re-encoded by FFmpeg to
  H.264/AAC HLS at the chosen quality.
- Past broadcasts are downloaded with yt-dlp and repackaged without re-encoding
  when they already fit the chosen quality, which costs very little CPU.
- The TV's browser is Chromium 38, so the player is plain ES5 JavaScript and
  CSS without variables or grid. Newer browsers use the bundled HLS.js.
- **The server only works while the app is in use.** Live checks and followed
  channels pause ten minutes after the last screen closes and catch up the
  moment one opens.
- Everything is stored as JSON files in `data/` next to the server. There is
  no database.

| File | Purpose |
| --- | --- |
| `server.py` | HTTP server, library, live checks, previews, live and video relay |
| `accounts.py` | Twitch sign-in and followed live channels |
| `users.py` | People, invites, signed-in devices, sign-in codes |
| `history.py` | Watch history shared by a person's screens |
| `public/` | The player: page, styles, script, HLS.js |
| `webos/` | The TV app that opens the player |
| `extension/` | Chrome extension |
| `tools/` | Build and install the TV app, deploy, print an invite |
| `tests/` | Player and server tests |

## Settings

The server needs no configuration file. Options:

| Option | Meaning |
| --- | --- |
| `--port 8765` | Port to listen on |
| `--max-streams 3` | How many people can watch at the same time |
| `--allow-ipv6` | Also use IPv6 for streams (only for a server without IPv4) |
| `--public` | Accept connections from the internet. Only behind an HTTPS reverse proxy |
| `MY_STREAMS_DATA` (environment) | Where to keep `data/` |

Timings are constants at the top of `server.py` and `accounts.py`, each with a
comment: how often live status is checked, how long the app counts as in use.

The tools read `.env` (see `.env.example`): your server's address, and
optionally an SSH destination and your TV's name.

## Security

- By default the server only answers private-network addresses, and only
  signed-in devices can use it. Other websites cannot send it commands.
- It serves plain HTTP. **Do not forward its port to the internet.** To reach
  it from outside, use a VPN, or put it behind an HTTPS reverse proxy and start
  it with `--public`.
- On a home network, the first device to choose **Set up as owner** becomes the
  owner. Do that yourself right after installing.
- Twitch tokens and sign-in tokens are stored in `data/` on the server,
  readable only by the user that runs it.

## Troubleshooting

- **The TV app says it cannot reach the server:** check the address, that the
  server is running, and that the TV and server are on the same network. The
  footer of the library has two **Playback check** samples that test the TV's
  player without Twitch.
- **A stream will not start:** the per-person logs are in `.runtime/logs/`.
  Twitch changes now and then; updating Streamlink and yt-dlp in
  `requirements.txt` is the usual fix.
- **The TV app disappeared:** Developer Mode ran out. Turn it on again and run
  `npm run install:tv`.

## Changing it

```sh
npm ci
npm test
```

The tests run the player script against a fake page and the server against
temporary data; they need the `.venv` from the server install on the same
computer.

Player and server changes take effect when the server restarts and the page is
reopened; the TV app does not need reinstalling. If you edit on one computer
and run on another, set `MY_STREAMS_SSH` in `.env` and use `npm run deploy`: it
sends the committed source over SSH, updates the Python packages, restarts the
service and checks that it answers.
