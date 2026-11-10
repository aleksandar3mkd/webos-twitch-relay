/* Shared by the popup and the background worker. Talks to the My Streams server on your network. */
const EXAMPLE_SERVER = 'http://192.168.1.50:8765';

async function getServer() {
  // Set once under the gear in the popup; empty until then.
  const {server} = await chrome.storage.sync.get('server');
  return server || '';
}

function cleanServer(value) {
  let text = String(value || '').trim().replace(/\/+$/, '');
  if (!/^https?:\/\//i.test(text)) text = 'http://' + text;
  const url = new URL(text);
  if (url.pathname !== '/' || url.search || url.hash) throw new Error('Use just the address, like ' + EXAMPLE_SERVER);
  return url.origin;
}

async function ensureAccess(server) {
  if (!(await chrome.permissions.contains({origins: [server + '/*']}))) {
    throw new Error('Allow access to ' + server + ': open ⚙ in the extension popup and choose Save.');
  }
}

async function getDevice() {
  const {device} = await chrome.storage.local.get('device');
  return device || '';
}

async function api(path, body) {
  const server = await getServer();
  if (!server) throw new Error('Set your My Streams server first: open the extension and choose ⚙.');
  await ensureAccess(server);
  // This browser's sign-in for the extension, approved once from a signed-in phone or browser.
  const headers = {'X-Device-Token': await getDevice()};
  let response;
  try {
    if (!body) {
      response = await fetch(server + path, {cache: 'no-store', headers});
    } else {
      // The control token changes whenever the helper restarts, so fetch it each time.
      const session = await fetch(server + '/api/session', {cache: 'no-store'});
      const {token} = await session.json();
      response = await fetch(server + path, {
        method: 'POST', cache: 'no-store', body: JSON.stringify(body),
        headers: {...headers, 'Content-Type': 'application/json', 'X-Playback-Test': token}
      });
    }
  } catch (error) {
    throw new Error("Can't reach My Streams at " + server + '. Is the server running and reachable?');
  }
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(response.status === 401 ? 'Sign in first: open the My Streams extension.' : result.error || 'Request failed (' + response.status + ')');
    error.signin = response.status === 401;
    throw error;
  }
  return result;
}

const saveStream = (url, name) => api('/api/streams/add', {url, name: name || ''});
const playStream = (url, name) => api('/api/start', {url, name: name || ''});
const stopStream = () => api('/api/stop', {});
const relayStatus = () => api('/api/status');
const whoAmI = () => api('/api/me');

const TWITCH_PAGE = /^https?:\/\/(www\.|m\.)?twitch\.tv\/./;

function nameFromTitle(url, title) {
  // A channel's address already names it; the server picks that up. A past broadcast is named after its page.
  if (!/twitch\.tv\/videos\/\d+/.test(url || '')) return '';
  return String(title || '').replace(/^\(\d+\)\s*/, '').replace(/\s*[-|–·]\s*Twitch\s*$/i, '').trim().slice(0, 60);
}
