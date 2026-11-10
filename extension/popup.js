const $ = id => document.getElementById(id);
let pageUrl = '';

function say(text, kind) {
  $('status').textContent = text;
  $('status').className = kind || '';
}

async function busy(task) {
  const buttons = document.querySelectorAll('#main button, #stop');
  buttons.forEach(b => { b.disabled = true; });
  try {
    await task();
  } catch (error) {
    say(error.message, 'error');
  } finally {
    buttons.forEach(b => { b.disabled = !pageUrl && b.id !== 'stop'; });
  }
}

async function refreshNowPlaying() {
  try {
    const state = await relayStatus();
    const on = state.stream && (state.state === 'starting' || state.state === 'playing');
    $('now').hidden = !on;
    if (on) $('now-name').textContent = (state.state === 'starting' ? 'Starting · ' : 'Now playing · ') + state.stream.name;
  } catch (error) {
    $('now').hidden = true;
    say(error.message, 'error');
  }
}

async function showSettings(open) {
  $('settings').hidden = !open;
  $('settings-toggle').setAttribute('aria-expanded', String(open));
  if (open) {
    $('server').value = await getServer();
    $('server').focus();
  }
}

async function init() {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  const server = await getServer();
  $('open-player').href = server ? server + '/' : '#';
  if (tab && TWITCH_PAGE.test(tab.url || '')) {
    pageUrl = tab.url;
    $('page-title').textContent = tab.title || tab.url;
    $('page-url').textContent = tab.url.replace(/^https?:\/\/(www\.)?/, '');
    $('name').value = nameFromTitle(tab.url, tab.title);
  } else {
    $('page-title').textContent = 'Open a Twitch page first';
    $('page-url').textContent = 'A channel or a past broadcast';
    $('save').disabled = $('play').disabled = true;
  }
  if (!server) {
    $('main').hidden = true;
    await showSettings(true);
    say('Enter the address of your My Streams server and choose Save.');
    return;
  }
  if (!(await chrome.permissions.contains({origins: [server + '/*']}))) {
    await showSettings(true);
    say('Choose Save to allow access to your playback server.');
    return;
  }
  try {
    const {user} = await whoAmI();
    $('signin').hidden = true;
    $('main').hidden = false;
    say('');
    $('status').title = 'Signed in as ' + user.name;
    refreshNowPlaying();
  } catch (error) {
    if (!error.signin) throw error;
    await signIn(server);
  }
}

let pairTimer = null;

async function signIn(server) {
  // Same as a TV: show a code that a signed-in phone or browser approves.
  $('main').hidden = true;
  $('now').hidden = true;
  $('signin').hidden = false;
  let {pairing} = await chrome.storage.session.get('pairing');
  if (!pairing || pairing.server !== server || pairing.expires < Date.now()) {
    const result = await api('/api/pair/start', {kind: 'Chrome extension'});
    pairing = {...result, server, expires: Date.now() + 9 * 60 * 1000};
    await chrome.storage.session.set({pairing});
  }
  $('pair-qr').src = server + pairing.qr;
  $('pair-code').textContent = pairing.code;
  $('pair-open').onclick = () => chrome.tabs.create({url: pairing.link});
  clearInterval(pairTimer);
  const check = async () => {
    try {
      const result = await api('/api/pair/collect', {secret: pairing.secret});
      if (!result.done) return;
      clearInterval(pairTimer);
      await chrome.storage.local.set({device: result.token});
      await chrome.storage.session.remove('pairing');
      init();
    } catch (error) {
      clearInterval(pairTimer);
      await chrome.storage.session.remove('pairing');
      signIn(server);   // the code expired: show a new one
    }
  };
  pairTimer = setInterval(check, 2000);
  check();
}

$('settings-toggle').onclick = () => showSettings($('settings').hidden);

$('save-server').onclick = async () => {
  let server;
  try {
    server = cleanServer($('server').value);
  } catch (error) {
    say(error.message || 'Enter an address like ' + EXAMPLE_SERVER, 'error');
    return;
  }
  // Store first: Chrome may close the popup while it shows the permission prompt.
  await chrome.storage.sync.set({server});
  await chrome.storage.local.remove('device');   // a sign-in belongs to one server
  $('open-player').href = server + '/';
  const granted = await chrome.permissions.request({origins: [server + '/*']});
  if (!granted) { say('Access was not allowed, so the extension cannot reach ' + server + '.', 'error'); return; }
  try {
    const health = await (await fetch(server + '/api/health', {cache: 'no-store'})).json();
    if (health.app !== 'my-streams-helper') throw new Error();
    say('Connected to ' + server + '.', 'ok');
    showSettings(false);
    init();
  } catch (error) {
    say('Saved, but nothing answered at ' + server + '. Check the address and the helper.', 'error');
  }
};

$('save').onclick = () => busy(async () => {
  say('Saving…');
  const result = await saveStream(pageUrl, $('name').value.trim());
  say(result.created ? 'Saved “' + result.stream.name + '”. It shows up on the TV in a moment.' : '“' + result.stream.name + '” is already in your library.', 'ok');
});

$('play').onclick = () => busy(async () => {
  say('Starting on the TV…');
  const result = await playStream(pageUrl, $('name').value.trim());
  say('Starting “' + result.stream.name + '”. An open My Streams player joins automatically.', 'ok');
  refreshNowPlaying();
});

$('stop').onclick = () => busy(async () => {
  await stopStream();
  say('Stopped.', 'ok');
  refreshNowPlaying();
});

$('name').onkeydown = event => { if (event.key === 'Enter') $('save').click(); };
$('server').onkeydown = event => { if (event.key === 'Enter') $('save-server').click(); };

init().catch(error => say(error.message, 'error'));
