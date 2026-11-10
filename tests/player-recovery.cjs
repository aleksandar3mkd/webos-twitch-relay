const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function element(tag) {
  return {
    tagName: (tag || 'div').toUpperCase(), value: '', className: '', style: {}, _text: '',
    events: {}, attributes: {}, childNodes: [], parentNode: null,
    get children() { return this.childNodes; },
    get firstChild() { return this.childNodes[0] || null; },
    get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; },
    set innerHTML(value) { this.childNodes = []; },
    get textContent() { return this._text; },
    set textContent(value) { this._text = value; this.childNodes = []; },
    addEventListener(event, callback) {
      const before = this.events[event];
      this.events[event] = before ? function (e) { before(e); callback(e); } : callback;
    },
    appendChild(child) { return this.insertBefore(child, null); },
    insertBefore(child, before) {
      if (child.parentNode) { child.parentNode.removeChild(child); }
      const index = before ? this.childNodes.indexOf(before) : -1;
      if (index < 0) { this.childNodes.push(child); } else { this.childNodes.splice(index, 0, child); }
      child.parentNode = this;
      return child;
    },
    removeChild(child) { this.childNodes.splice(this.childNodes.indexOf(child), 1); child.parentNode = null; return child; },
    contains(node) { return node === this || this.childNodes.some(child => child.contains(node)); },
    querySelector() { return null; },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; },
    focus() { document.activeElement = this; },
    querySelectorAll() { return []; },
    pause() {}, load() {},
    removeAttribute() { this.src = ''; },
    play() { return { catch() {} }; }
  };
}
let document;

function player(library, signedIn = true, setup = false) {
  const backend = { instance: 'one', token: 'one', state: 'idle', starts: 0, deny: false, run: 0, url: '', quality: '1080p',
    library: library || [{id: 'z', name: 'somechannel', platform: 'Twitch', url: 'https://www.twitch.tv/somechannel'}],
    followed: [], version: 'v1', kind: 'live', requests: [], signedIn, setup, approved: false, reloads: 0 };
  const saved = {};
  const elements = {};
  let later;
  const intervals = [];
  document = {
    body: element('body'), head: element('head'), documentElement: element('html'), activeElement: null, events: {},
    getElementById: name => (elements[name] = elements[name] || element(name === 'video' ? 'video' : 'button')),
    createElement: tag => element(tag),
    addEventListener(event, callback) { this.events[event] = callback; }
  };
  function XHR() { this.headers = {}; }
  XHR.prototype.open = function (method, path) { this.method = method; this.path = path; };
  XHR.prototype.setRequestHeader = function (name, value) { this.headers[name] = value; };
  XHR.prototype.send = function (data) {
    const request = data ? JSON.parse(data) : {};
    let body = {ok: true};
    this.status = 200;
    if (this.method === 'POST' && (backend.deny || this.headers['X-Playback-Test'] !== backend.token)) {
      this.status = 403; body = {error: 'Reload the player'};
    } else if (this.path === '/api/pair/start') {
      body = {code: 'ABCD-EFGH', secret: 's3cret', link: 'http://tv.local/link?code=ABCDEFGH', qr: '/api/qr?data=x', setup: backend.setup};
    } else if (this.path === '/api/setup/claim') {
      body = {token: 'owner-token'};
    } else if (this.path === '/api/pair/collect') {
      body = backend.approved ? {done: true, token: 'device-token'} : {done: false};
    } else if (!backend.signedIn && this.path !== '/api/session') {
      this.status = 401; body = {error: 'Sign in to continue.', signin: true};
    } else if (this.path === '/api/me') {
      body = {user: {name: 'Me', admin: true}};
    } else if (this.path === '/api/session') {
      body = {token: backend.token, instance_id: backend.instance};
    } else if (this.path === '/api/streams') {
      body = {streams: backend.library, followed: backend.followed, continuing: backend.continuing || [], version: backend.version};
    } else if (this.path === '/api/history') {
      body = {items: backend.history || []};
    } else if (this.path.indexOf('/api/history/') === 0) {
      (backend.historyCalls = backend.historyCalls || []).push([this.path.split('/').pop(), request]);
    } else if (this.path === '/api/start') {
      backend.starts++; backend.run++; backend.state = 'playing'; backend.url = request.url || request.id; backend.playing = backend.quality;
      backend.requests.push(request); backend.kind = request.kind === 'video' ? 'video' : 'live';
      Object.assign(body, {instance_id: backend.instance, run: 'r' + backend.run,
        stream: {name: 'somechannel', platform: 'Twitch', url: backend.url, position: backend.position}});
    } else if (this.path === '/api/settings') {
      backend.quality = request.quality; body.quality = request.quality;
    } else if (this.path === '/api/stop') {
      backend.state = 'idle';
    } else if (this.path === '/api/status') {
      const active = backend.state !== 'idle';
      body = {state: backend.state, instance_id: backend.instance, message: backend.state, run: active ? 'r' + backend.run : null,
        stream: active ? {name: 'stream ' + backend.run, platform: 'Twitch', url: backend.url, position: backend.position} : null,
        library: backend.version,
        kind: active ? backend.kind : null, duration: backend.kind === 'video' ? 600 : null,
        quality: backend.quality, qualities: ['720p', '1080p', '1080p60'], playing_quality: active ? backend.playing : null,
        url: backend.state === 'playing' ? '/media/' + backend.instance + '-' + backend.run + '/index.m3u8' : null};
    }
    this.responseText = JSON.stringify(body);
    this.onload();
  };
  const context = {
    window: {setInterval(callback) { intervals.push(callback); return intervals.length; }, clearInterval() {},
      setTimeout(callback) { later = callback; return 1; }, clearTimeout() {},
      location: {reload() { backend.reloads++; }}},
    navigator: {userAgent: 'Web0S Chrome/38 SmartTV WebAppManager'},
    document, localStorage: {getItem: key => saved[key] || null, setItem(key, value) { saved[key] = value; }, removeItem(key) { delete saved[key]; }},
    XMLHttpRequest: XHR, testToken: 'one'
  };
  vm.runInNewContext(fs.readFileSync('public/player.js', 'utf8'), context);
  // The first library card plays the saved stream, like pressing OK on the TV.
  const play = () => elements['offline-grid'].children[0].onclick();
  return {backend, elements, play, tick: () => intervals.forEach(callback => callback()), later: () => later(), context, saved};
}

const restarted = player();
restarted.play();
assert.equal(restarted.elements.video.src, '/media/one-1/index.m3u8');
assert.match(restarted.elements.watch.className, /^watch(?! hidden)/);
Object.assign(restarted.backend, {instance: 'two', token: 'two', state: 'idle'});
restarted.elements.video.error = {code: 2, message: 'Network connection lost'};
restarted.elements.video.events.error();
restarted.tick();
assert.equal(restarted.context.testToken, 'two');
assert.equal(restarted.backend.starts, 2);
assert.equal(restarted.elements.video.src, '/media/two-2/index.m3u8');
// Another viewer's intentional stop must not be mistaken for a server restart.
restarted.backend.state = 'idle';
restarted.tick(); restarted.tick();
assert.equal(restarted.backend.starts, 2);
assert.match(restarted.elements.watch.className, /hidden/);

const stopped = player();
stopped.play();
stopped.elements.stop.onclick();
Object.assign(stopped.backend, {instance: 'two', token: 'two', state: 'idle'});
stopped.tick();
assert.equal(stopped.backend.starts, 1);

const limited = player();
limited.play();
for (let attempt = 0; attempt < 4; attempt++) {
  limited.elements.video.error = {code: 2};
  limited.elements.video.events.error();
  limited.tick();
}
assert.match(limited.elements.status.textContent, /Playback error 2/);
// Giving up on a run must not immediately re-join that same run.
limited.tick();
assert.match(limited.elements.status.textContent, /Playback error 2/);

const denied = player();
denied.backend.deny = true;
denied.play();
assert.match(denied.elements.status.textContent, /Reload/);
assert.equal(denied.backend.starts, 0);

// A stream started from another screen or the Chrome extension is joined automatically.
const followed = player();
assert.equal(followed.elements.video.src, undefined);
Object.assign(followed.backend, {state: 'playing', run: 7, url: 'https://www.twitch.tv/someone'});
followed.tick();
assert.equal(followed.elements.video.src, '/media/one-7/index.m3u8');
assert.equal(followed.elements['watch-title'].textContent, 'stream 7');
// ...and when the relay switches stream, the title follows too.
followed.backend.run = 8;
followed.tick();
assert.equal(followed.elements.video.src, '/media/one-8/index.m3u8');
assert.equal(followed.elements['watch-title'].textContent, 'stream 8');
assert.equal(followed.backend.starts, 0);

// Live and offline streams are listed in separate sections; live ones show a preview image.
const sections = player([
  {id: 'a', name: 'alpha', platform: 'Twitch', url: 'https://www.twitch.tv/alpha', live: false, checked: 1},
  {id: 'b', name: 'beta', platform: 'Twitch', url: 'https://www.twitch.tv/beta', live: true, checked: 1, thumb: '/api/img/s-b?v=5', title: 'Speedrun', category: 'Retro'}
]);
const liveCards = sections.elements['live-grid'].children;
const offlineCards = sections.elements['offline-grid'].children;
assert.deepEqual([liveCards.length, offlineCards.length], [1, 1]);
assert.equal(liveCards[0].attributes['data-id'], 'b');
const text = node => node.textContent + node.children.map(text).join('|');
assert.match(text(liveCards[0]), /Speedrun.*Retro/);
assert.equal(liveCards[0].children[0].children.find(child => child.tagName === 'IMG').src, '/api/img/s-b?v=5');
assert.match(text(offlineCards[0]), /alpha.*Offline/);
assert.equal(sections.elements['live-count'].textContent, '1');

// Followed channels join the live row after saved ones; a past broadcast in progress gets its own row.
const home = player();
const vod = 'https://www.twitch.tv/videos/123';
Object.assign(home.backend, {version: 'v2',
  followed: [{id: 'tw-gamma', name: 'Gamma', platform: 'Twitch', url: 'https://www.twitch.tv/gamma', live: true, viewers: 12300, category: 'Chess'}],
  continuing: [{id: 'h-vod', name: 'Gamma', platform: 'Twitch', kind: 'video', source: 'history', url: vod,
    title: 'A broadcast', duration: 754, position: 100, progress: 0.13, thumb: '/api/img/h-vod?v=1'}]});
home.tick();
const followedCard = home.elements['live-grid'].children[0];
assert.match(text(followedCard), /Gamma.*Chess · 12\.3K watching/);
const videoCard = home.elements['continue-grid'].children[0];
assert.match(text(videoCard), /12:34.*A broadcast.*Gamma/);
assert.equal(home.elements['continue-section'].className, 'section');
// Refreshing with unchanged data keeps the same card nodes (no rebuild, no image reload).
home.backend.version = 'v3';
home.tick();
assert.equal(home.elements['live-grid'].children[0], followedCard);
assert.equal(home.elements['continue-grid'].children[0], videoCard);
// Playing a video asks the server for video mode and shows the position bar.
videoCard.onclick();
assert.deepEqual(home.backend.requests.pop(), {url: vod, name: 'Gamma', kind: 'video', title: 'A broadcast'});
assert.match(home.elements.watch.className, /is-video/);
home.tick();
assert.equal(home.elements['seek-time'].textContent, '0:00 / 10:00');
followedCard.onclick();
assert.equal(home.backend.requests.pop().kind, 'live');
assert.doesNotMatch(home.elements.watch.className, /is-video/);

// A device that is not signed in shows a QR code and waits for a phone to approve it.
const fresh = player(undefined, false);
assert.equal(fresh.elements.signin.className, 'signin');
assert.equal(fresh.elements['signin-code'].textContent, 'ABCD-EFGH');
assert.equal(fresh.elements['signin-qr'].src, '/api/qr?data=x');
fresh.tick();
assert.equal(fresh.backend.reloads, 0);
fresh.backend.approved = true;
fresh.tick();
assert.equal(fresh.saved['my-streams-device'], 'device-token');
assert.equal(fresh.backend.reloads, 1);
// First start: the owner sets up from the sign-in screen with one button.
const first = player(undefined, false, true);
assert.equal(first.elements['signin-setup'].className, 'setup');
assert.equal(first.context.document.activeElement, first.elements['signin-claim']);
first.elements['signin-claim'].onclick();
assert.equal(first.saved['my-streams-device'], 'owner-token');
assert.equal(first.backend.reloads, 1);
assert.equal(fresh.elements['signin-setup'].className, 'setup hidden');   // normal sign-in: no setup offer
// Signed in: the owner sees People; the sign-in screen stays hidden.
const owner = player();
assert.equal(owner.elements.people.className, 'btn ghost');
assert.equal(owner.elements.signin.className, 'signin hidden');

// Quality: presses cycle the label; only the final choice is saved, and it restarts the stream.
const tuned = player();
assert.equal(tuned.elements.quality.textContent, 'Quality · 1080p');
tuned.play();
tuned.elements.quality.onclick();
tuned.elements.quality.onclick();
assert.equal(tuned.elements.quality.textContent, 'Quality · 720p');
assert.equal(tuned.backend.quality, '1080p');
tuned.later();
assert.equal(tuned.backend.quality, '720p');
assert.equal(tuned.backend.starts, 2);
assert.equal(tuned.backend.playing, '720p');

// A past broadcast pasted into Add a channel is played, not saved as a channel.
const pasted = player();
pasted.elements.add.onclick();
pasted.elements.channel.value = ' https://www.twitch.tv/videos/987?t=1h ';
pasted.elements.save.onclick();
assert.match(pasted.elements.status.textContent, /played, not saved/);
assert.equal(pasted.backend.starts, 0);
pasted.elements.start.onclick();
assert.deepEqual(pasted.backend.requests.pop(), {url: 'https://www.twitch.tv/videos/987?t=1h', name: 'Twitch video', kind: 'video', title: ''});
assert.match(pasted.elements.watch.className, /is-video/);
assert.equal(pasted.elements.dialog.className, 'dialog hidden');

// When a past broadcast ends, it is reported as finished and the library comes back.
const over = player();
Object.assign(over.backend, {version: 'v5', continuing: [{id: 'h-end', name: 'Gamma', platform: 'Twitch', kind: 'video',
  source: 'history', url: 'https://www.twitch.tv/videos/5', title: 'Almost done', duration: 300, position: 280, progress: 0.93}]});
over.tick();
over.elements['continue-grid'].children[0].onclick();
over.tick();
assert.doesNotMatch(over.elements.watch.className, /hidden/);
over.elements.video.events.ended();
assert.equal(over.backend.historyCalls.pop()[1].event, 'ended');
assert.match(over.elements.watch.className, /hidden/);

// Watch history: Continue watching resumes where any screen stopped, and every screen reports how far it got.
const talk = 'https://www.twitch.tv/videos/talk';
const resume = player();
Object.assign(resume.backend, {version: 'v7', position: 200, continuing: [{id: 'h-talk', name: 'Talks', platform: 'Twitch',
  kind: 'video', source: 'history', url: talk, title: 'Long talk', duration: 600, position: 200, progress: 0.33}]});
resume.tick();
assert.match(resume.elements['continue-section'].className, /^section$/);
const continueCard = resume.elements['continue-grid'].children[0];
assert.match(text(continueCard), /Long talk/);
const hasBar = node => node.className === 'watched-bar' || node.childNodes.some(hasBar);
assert.ok(hasBar(continueCard));
continueCard.onclick();
assert.deepEqual(resume.backend.requests.pop(), {url: talk, name: 'Talks', kind: 'video', title: 'Long talk'});
resume.tick();
resume.elements.video.currentTime = 0;
let loadedTo = 100;
resume.elements.video.seekable = {length: 1, end: () => loadedTo};
resume.elements.video.events.loadedmetadata();
// Each start downloads from the beginning: it waits until the spot to continue at has arrived.
assert.equal(resume.elements.video.currentTime, 0);
assert.match(resume.elements['seek-time'].textContent, /continuing at 3:20 when loaded/);
resume.elements.video.currentTime = 4;
resume.elements.video.events.timeupdate();                         // not progress: it has not continued yet
assert.equal(resume.backend.historyCalls, undefined);
loadedTo = 300;
resume.elements.video.events.progress();
assert.equal(resume.elements.video.currentTime, 200);             // the server's spot, not this device's
assert.doesNotMatch(resume.elements['seek-time'].textContent, /continuing/);
resume.elements.video.currentTime = 260;
resume.elements.video.events.timeupdate();
resume.elements.video.events.pause();
assert.deepEqual(resume.backend.historyCalls.map(([kind, body]) => [kind, body.position, body.event]),
  [['progress', 260, 'tick'], ['progress', 260, 'pause']]);
assert.equal(resume.backend.historyCalls[0][1].url, talk);
resume.elements.video.ended = true;
resume.elements.video.events.pause();                              // the pause that comes with the end is not reported
resume.elements.video.events.ended();
assert.deepEqual(resume.backend.historyCalls.pop()[1].event, 'ended');
assert.equal(resume.backend.historyCalls.length, 2);
// Pressing play while it waits plays from where it is instead.
const impatient = player();
Object.assign(impatient.backend, {version: 'v9', position: 300, continuing: [{id: 'h-talk', name: 'Talks', platform: 'Twitch',
  kind: 'video', source: 'history', url: talk, title: 'Long talk', duration: 600, position: 300, progress: 0.5}]});
impatient.tick();
impatient.elements['continue-grid'].children[0].onclick();
impatient.tick();
impatient.elements.video.currentTime = 0;
impatient.elements.video.seekable = {length: 1, end: () => 20};
impatient.elements.video.events.loadedmetadata();
assert.match(impatient.elements['seek-time'].textContent, /continuing at 5:00/);
impatient.elements.pause.onclick();
assert.doesNotMatch(impatient.elements['seek-time'].textContent, /continuing/);
impatient.elements.video.seekable = {length: 1, end: () => 400};
impatient.elements.video.events.progress();
assert.equal(impatient.elements.video.currentTime, 0);
// Without anything on the server, a video starts from the beginning even if this device has an old note.
const unseen = player();
Object.assign(unseen.backend, {version: 'v8', position: 0, continuing: [{id: 'h-new', name: 'New', platform: 'Twitch', kind: 'video',
  source: 'history', url: 'https://www.twitch.tv/videos/new', title: 'New one', duration: 900, progress: 0.5}]});
unseen.saved['position:https://www.twitch.tv/videos/new'] = '400';
unseen.tick();
unseen.elements['continue-grid'].children[0].onclick();
unseen.tick();
unseen.elements.video.currentTime = 0;
unseen.elements.video.events.loadedmetadata();
assert.equal(unseen.elements.video.currentTime, 0);
// The History dialog lists everything; open, remove and clear.
const past = player();
past.backend.history = [
  {id: 'h-a', name: 'Chan A', platform: 'Twitch', kind: 'video', url: 'https://www.twitch.tv/videos/aaa', title: 'Video A',
    duration: 600, position: 120, progress: 0.2, watched: 1},
  {id: 'h-b', name: 'Chan B', platform: 'Twitch', kind: 'video', url: 'https://www.twitch.tv/videos/bbb', title: 'Video B',
    duration: 300, finished: true, progress: 1, watched: 1}];
past.elements.history.onclick();
assert.equal(past.elements['history-dialog'].className, 'dialog');
const rows = () => past.elements['history-list'].children;
assert.equal(rows().length, 2);
assert.match(text(rows()[0]), /Video A.*Chan A.*2:00 of 10:00/);
assert.match(text(rows()[1]), /Video B.*watched/);
rows()[1].lastChild.onclick();                                     // Remove
assert.deepEqual(past.backend.historyCalls.pop(), ['remove', {url: 'https://www.twitch.tv/videos/bbb'}]);
assert.equal(rows().length, 1);
past.elements['history-clear'].onclick();
assert.equal(past.backend.historyCalls.length, 0);             // the first press only asks to confirm
assert.match(past.elements['history-clear'].textContent, /again/);
past.elements['history-clear'].onclick();
assert.equal(past.backend.historyCalls.pop()[0], 'clear');
assert.equal(rows().length, 0);
past.backend.history = [{id: 'h-c', name: 'Chan C', platform: 'Twitch', kind: 'video', url: 'https://www.twitch.tv/videos/ccc',
  title: 'Video C', duration: 600, position: 90, progress: 0.15, watched: 1}];
past.elements.history.onclick();
rows()[0].firstChild.onclick();                                    // plays it, and closes the dialog
assert.equal(past.backend.requests.pop().url, 'https://www.twitch.tv/videos/ccc');
assert.equal(past.elements['history-dialog'].className, 'dialog hidden');

// Messages show over whatever is on screen (they used to be written at the bottom of the library).
const told = player();
const toastOf = p => (p.elements.toast.children[0] || {}).textContent;
told.play();
told.elements.stop.onclick();
assert.equal(toastOf(told), 'Stopped.');
told.play();
told.elements.video.events.playing();
assert.equal(toastOf(told), undefined);                          // plain "Playing ..." does not cover the video
assert.match(told.elements.status.textContent, /^Playing/);

// A tab nobody looks at, with nothing playing, stops asking the server; it catches up when it is shown again.
const tab = player();
tab.context.document.hidden = true;
Object.assign(tab.backend, {state: 'playing', run: 3, url: 'https://www.twitch.tv/someone'});
tab.tick();
assert.equal(tab.elements.video.src, undefined);
tab.context.document.hidden = false;
tab.context.document.events.visibilitychange();
assert.equal(tab.elements.video.src, '/media/one-3/index.m3u8');
tab.context.document.hidden = true;                              // hidden while it plays: it keeps following
tab.backend.run = 4;
tab.tick();
assert.equal(tab.elements.video.src, '/media/one-4/index.m3u8');

// A card redrawn with a newer preview keeps showing the previous one until the new one has arrived.
const fresher = player([{id: 'b', name: 'beta', platform: 'Twitch', url: 'https://www.twitch.tv/beta', live: true, checked: 1, thumb: '/api/img/s-b?v=5'}]);
const firstArt = fresher.elements['live-grid'].children[0].children[0];
assert.doesNotMatch(firstArt.style.backgroundImage, /url/);
assert.equal(firstArt.children.find(child => child.tagName === 'IMG').attributes.loading, 'lazy');
fresher.backend.library[0].thumb = '/api/img/s-b?v=6';
fresher.backend.version = 'v2';
fresher.tick();
const nextArt = fresher.elements['live-grid'].children[0].children[0];
assert.match(nextArt.style.backgroundImage, /^url\("\/api\/img\/s-b\?v=5"\), linear-gradient/);
assert.equal(nextArt.children.find(child => child.tagName === 'IMG').src, '/api/img/s-b?v=6');

console.log('PASS: messages, hidden tabs, preview refresh, server restart + token refresh, intentional stops, bounded network recovery, rejected credentials, following remote starts, quality switching, live/offline sections, followed channels, past broadcasts, watch history, card reuse, device sign-in.');
