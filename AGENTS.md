# Installing My Streams: instructions for an AI agent

You are installing My Streams for the person you are working with. This file is
the whole procedure. `README.md` is the short version for people and
`docs/details.md` is the reference.

My Streams puts Twitch on an old LG webOS TV. It has two parts:

- **The server** (`server.py`, Python). It runs on a computer in the person's
  home that stays on, and listens on port 8765.
- **The TV app** (`webos/`). It is installed on an LG webOS TV over the home
  network with LG's command-line tools, from any computer that has Node.js.

## Rules

- Steps marked **Person** happen on the TV, a phone or a browser. Ask, then
  wait for the answer. Do not guess a TV address or a passphrase.
- Do not open the owner sign-in link yourself. Whoever opens it becomes the
  owner on that device, and that must be the person's own browser.
- Never start the server with `--public`, forward a port, or change router or
  firewall settings. The server is meant for the home network only.
- Do not use `sudo` unless the person agrees to the exact command.
- Do not commit `.env`, `data/` or `webos/server.js`. They are already ignored.

## 0. Ask first

1. Is this the computer that will stay on and run the server? If the server
   goes on another computer, ask how to reach it (SSH) and run step 1 there.
2. Is the TV an LG with webOS, on the same network as this computer?

## 1. Install and start the server

Check `python3 --version` is 3.11 or newer. Then:

```sh
git clone <repository address> my-streams    # skip if you are already in it
cd my-streams
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-server.txt
```

**On Linux with systemd**, run it as a user service so it survives logout and
reboot. `deploy/my-streams.service` expects the folder at `~/apps/my-streams`.
If the folder is somewhere else, change the three paths in the copy you
install, not in the repository.

```sh
mkdir -p ~/.config/systemd/user
cp deploy/my-streams.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now my-streams.service
loginctl enable-linger "$USER"
journalctl --user -u my-streams.service -n 20 --no-pager
```

**On anything else**, start `.venv/bin/python server.py` in the background and
tell the person it stops when the computer restarts. Ask before you set up any
other way of starting it at boot.

The log has two lines you need:

```
My Streams is running: http://192.168.1.50:8765
Sign in as the owner: http://192.168.1.50:8765/join/...
```

Check it answers:

```sh
curl -s http://127.0.0.1:8765/api/health
# {"app":"my-streams-helper","ok":true}
```

Call the address from the first line `SERVER` below. Tell the person to give
that computer a fixed address in their router, so the TV keeps finding it.

## 2. Person: become the owner

Give the person the `Sign in as the owner` link and ask them to open it on
their phone or computer. The link is printed only while the owner has no
device yet. `.venv/bin/python tools/invite.py` on the server prints a new one.

## 3. Person: put the TV in Developer Mode

Ask the person to do this on the TV and to tell you the two values at the end:

1. Install **Developer Mode** from the LG Content Store and sign in with a free
   LG developer account (created at webostv.developer.lge.com).
2. Turn on **Dev Mode Status**. The TV restarts.
3. Open Developer Mode again and turn on **Key Server**.
4. Read out the **IP address** and the **passphrase** the app shows.

## 4. Install the TV app

Check `node --version` works. In the repository folder:

```sh
npm ci
printf 'MY_STREAMS_URL=%s\n' "SERVER" > .env
node_modules/.bin/ares-setup-device --add tv -i host=TV_ADDRESS -i port=9922 -i username=prisoner
node_modules/.bin/ares-novacom --device tv --getkey --passphrase PASSPHRASE
npm run install:tv
```

`npm run install:tv` builds the app with the server's address inside, installs
it and starts it. Check it is there:

```sh
node_modules/.bin/ares-install --device tv --list    # shows com.local.mystreams
```

If the TV cannot be reached: Key Server must be on, the TV must be awake and on
the same network, and `node_modules/.bin/ares-setup-device --list` must show the
right address.

## 5. Person: sign the TV in

The TV now shows a QR code. Ask the person to scan it with the phone they
signed in on in step 2 and choose **Approve**.

## 6. Finish

Tell the person, in a few lines:

- the server's address, and where the folder is;
- how to restart it (`systemctl --user restart my-streams.service` on Linux);
- that Developer Mode runs out: open the Developer Mode app on the TV now and
  then and press **EXTEND**, otherwise LG removes the app and
  `npm run install:tv` has to be run again;
- that **+ Add channel** on their phone adds the first Twitch channel;
- that connecting their Twitch account (to see followed channels) is optional
  and described under "Your Twitch follows" in `docs/details.md`.

## When something fails

- Server logs: `journalctl --user -u my-streams.service`, and one log per
  person in `.runtime/logs/`.
- A stream does not start: Twitch changes now and then. Updating Streamlink and
  yt-dlp in `requirements.txt`, then reinstalling the packages, is the usual fix.
- `npm test` runs the player and server tests. It needs the `.venv` from step 1.
