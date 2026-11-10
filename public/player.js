/* ES5 syntax for the Chromium 38 engine on webOS 3.5. */
(function () {
  function $(id) { return document.getElementById(id); }
  var video = $('video');
  var status = $('status');
  var toastBox = $('toast');
  var channel = $('channel');
  var nameInput = $('name');
  var libraryEl = $('library');
  var watchEl = $('watch');
  var addDialog = $('dialog');
  var accountsDialog = $('accounts-dialog');
  var peopleDialog = $('people-dialog');
  var signinEl = $('signin');
  var liveGrid = $('live-grid');
  var historyDialog = $('history-dialog');
  var continueGrid = $('continue-grid');
  var offlineGrid = $('offline-grid');
  var seekButton = $('seek');
  var isTV = /webos|web0s|netcast|smarttv/i.test(navigator.userAgent);
  var rootClass = ' ' + document.documentElement.className + ' ';
  var isIOS = rootClass.indexOf(' ios ') >= 0;    // iPhone/iPad: Apple's own player handles these streams best
  var isApp = rootClass.indexOf(' app ') >= 0;    // opened from the Home Screen
  var touch = 'ontouchstart' in window;
  var unlocked = false;
  var hist = window.history && window.history.pushState ? window.history : null;

  // Playback
  var current = null;
  var pending = false;
  var lastState = '';
  var hlsPlayer = null;
  var sourceGeneration = 0;
  var activeTarget = null;      // what /api/start needs to restart this screen's stream
  var activeInstance = null;
  var commandGeneration = 0;
  var networkRetries = 0;
  var seenRun = null;           // relay run this screen started, followed or gave up on
  var nowStream = null;         // {name, platform, url, title} shown in the player
  var nowKind = 'live';         // 'video' when the relay plays a seekable video
  var duration = 0;             // video length from the server (the browser may not know it yet)
  var startApplied = false;     // a video's start or resume position has been set
  var resumeAt = null;          // where to continue, once the server has downloaded that far
  var lastSecond = -1;
  var lastStatus = null;

  // Interface
  var streams = [];             // saved library, with live information
  var followed = [];            // live channels from connected accounts
  var continuing = [];          // started but unfinished videos, from the watch history
  var watched = [];             // the watch history, while its dialog is open
  var clearing = false;         // Clear history was pressed once and waits for confirmation
  var lastReport = 0;           // when this screen last told the server how far the video is
  var cardCache = {};           // id -> {sig, node}: unchanged cards are reused, not rebuilt
  var libraryVersion = null;
  var libraryLoading = false;
  var live = null;              // stream the server is currently relaying
  var managing = false;
  var confirmId = null;
  var view = 'library';
  var dialog = null;            // the open dialog element, if any
  var ready = false;
  var failed = false;
  var idle = false;
  var idleTimer = null;
  var pops = 0;
  var reuseEntry = false;       // replace the dialog's history entry instead of adding one
  var autoMuted = false;        // desktop browsers block sound until the page is clicked
  var quality = null;           // shared quality setting, e.g. '1080p'
  var qualities = ['720p', '1080p', '1080p60'];
  var qualityTimer = null;      // waits for the last press before applying a new quality
  var accountsLoading = false;
  var quickPoll = null;
  var me = null;                // the signed-in person: {name, admin}
  var deviceToken = null;       // sent with requests, in case this device loses its cookie
  var pairing = null;           // {code, secret} while this device waits to be approved
  var pairingTimer = null;
  var toastText = '';
  var toastAt = 0;
  var usedKeys = false;         // computers highlight the chosen card only once the keyboard is used
  var lastMove = 0;

  function say(text, quiet) {
    status.textContent = text;
    toast(quiet ? '' : text);
  }
  function toast(text) {
    // A new element each time restarts the style sheet's show-then-fade animation: no timer to keep.
    var now = new Date().getTime();
    if (text && text === toastText && now - toastAt < 4000) { return; }   // repeated while reconnecting: no flicker
    toastText = text; toastAt = now;
    toastBox.textContent = '';
    if (text) { toastBox.appendChild(el('div', 'toast', text)); }
  }
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text) { node.textContent = text; }
    return node;
  }
  function shortUrl(url) { return String(url || '').replace(/^https?:\/\/(www\.)?/i, '').replace(/\/$/, ''); }
  function pad(number) { return number < 10 ? '0' + number : String(number); }
  function clock(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor(seconds / 60) % 60;
    return (hours ? hours + ':' + pad(minutes) : minutes) + ':' + pad(seconds % 60);
  }
  function compact(number) {
    if (number >= 1000000) { return (number / 1000000).toFixed(1).replace(/\.0$/, '') + 'M'; }
    if (number >= 1000) { return (number / 1000).toFixed(1).replace(/\.0$/, '') + 'K'; }
    return String(number);
  }
  function ago(timestamp) {
    var minutes = Math.max(1, Math.floor((new Date().getTime() / 1000 - timestamp) / 60));
    if (minutes < 60) { return minutes + ' min ago'; }
    if (minutes < 1440) { return Math.floor(minutes / 60) + ' h ago'; }
    var days = Math.floor(minutes / 1440);
    return days === 1 ? 'yesterday' : days + ' days ago';
  }
  function store(key, value) {
    try { if (value === null) { localStorage.removeItem(key); } else { localStorage.setItem(key, value); } } catch (e) {}
  }
  function recall(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }

  function request(method, path, data, callback, retried) {
    var xhr = new XMLHttpRequest();
    xhr.open(method, path, true);
    xhr.timeout = 15000;
    if (deviceToken) { xhr.setRequestHeader('X-Device-Token', deviceToken); }
    if (method === 'POST') {
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.setRequestHeader('X-Playback-Test', testToken);
    }
    xhr.onload = function () {
      var result;
      try { result = JSON.parse(xhr.responseText); } catch (e) { callback('Invalid server response'); return; }
      if (xhr.status === 401 && result.signin) {
        // Signed out (removed by the owner, or a new device): show the sign-in screen.
        if (me) { store('my-streams-device', null); window.location.reload(); return; }
        callback(result.error);
        return;
      }
      if (xhr.status === 403 && method === 'POST' && !retried) {
        request('GET', '/api/session', null, function (error, session) {
          if (error) { callback(error); return; }
          testToken = session.token;
          request(method, path, data, callback, true);
        });
        return;
      }
      callback(xhr.status >= 400 ? result.error || 'Request failed' : null, result);
    };
    xhr.onerror = xhr.ontimeout = function () { callback('Cannot reach the playback server. Check the network and keep the helper running.'); };
    xhr.send(data ? JSON.stringify(data) : null);
  }
  function report(event, detail) {
    request('POST', '/api/event', {event: event, detail: detail || ''}, function () {});
  }

  /* ---------- Views and dialogs ---------- */

  function render() {
    libraryEl.className = 'library' + (managing ? ' managing' : '') + (view === 'library' ? '' : ' hidden');
    signinEl.className = view === 'signin' ? 'signin' : 'signin hidden';
    peopleDialog.className = dialog === peopleDialog ? 'dialog' : 'dialog hidden';
    watchEl.className = 'watch' + (view === 'watch' ? '' : ' hidden') + (ready ? ' ready' : '') +
      (failed ? ' failed' : '') + (idle ? ' idle' : '') + (nowKind === 'video' ? ' is-video' : '');
    addDialog.className = dialog === addDialog ? 'dialog' : 'dialog hidden';
    accountsDialog.className = dialog === accountsDialog ? 'dialog' : 'dialog hidden';
    historyDialog.className = dialog === historyDialog ? 'dialog' : 'dialog hidden';
    document.body.className = current ? 'has-video' : '';
  }
  function wake() {
    idle = false;
    if (idleTimer) { window.clearTimeout(idleTimer); idleTimer = null; }
    if (view === 'watch' && ready && window.setTimeout) {
      // While it waits to continue, the bar stays up: it says where it will continue.
      idleTimer = window.setTimeout(function () { idle = view === 'watch' && ready && resumeAt === null; render(); }, 4000);
    }
    render();
  }
  function watchFocus() { return nowKind === 'video' ? seekButton : $('back'); }
  function showWatch() {
    if (view !== 'watch') {
      view = 'watch';
      if (hist && reuseEntry) { hist.replaceState({view: 'watch'}, ''); }
      else if (hist) { hist.pushState({view: 'watch'}, ''); }
      render();
      focus(watchFocus());
    }
    reuseEntry = false;
    wake();
  }
  function showLibrary() {
    if (view === 'library') { return; }
    if (hist && hist.state && hist.state.view === 'watch') { hist.back(); return; }
    enterLibrary();
  }
  function enterLibrary() {
    view = 'library'; idle = false;
    render();
    focusLibrary();
  }
  function openDialog(node, first) {
    dialog = node;
    if (hist) { hist.pushState({dialog: true}, ''); }
    render();
    focus(first);
  }
  function dialogClosed(node) {
    focus(node === accountsDialog ? $('accounts') : node === peopleDialog ? $('people')
      : node === historyDialog ? $('history') : $('add'));
  }
  function closeDialog(next) {
    if (!dialog) { return; }
    var closing = dialog;
    dialog = null;
    if (hist && hist.state && hist.state.dialog) {
      // Continue into the player without leaving a stale dialog entry behind Back.
      if (next === 'watch') { reuseEntry = true; } else { hist.back(); }
    }
    render();
    if (next !== 'watch') { dialogClosed(closing); }
  }
  if (window.addEventListener) {
    window.addEventListener('popstate', function (event) {
      pops++;
      var state = event.state || {};
      if (dialog && !state.dialog) { var closing = dialog; dialog = null; render(); dialogClosed(closing); }
      if (state.view === 'watch' && view !== 'watch') { view = 'watch'; render(); focus(watchFocus()); wake(); }
      else if (state.view !== 'watch' && view === 'watch') { enterLibrary(); }
    });
  }
  function back() {
    if (dialog) { closeDialog(); }
    else if (view === 'watch') { showLibrary(); }
    else if (managing) { toggleManage(); }
  }

  /* ---------- Library ---------- */

  function hue(text) {
    var hash = 0;
    for (var i = 0; i < text.length; i++) { hash = (hash * 31 + text.charCodeAt(i)) % 360; }
    return hash;
  }
  function offlineText(stream) {
    if (stream.live === false) { return 'Offline'; }
    return stream.checked ? 'Offline or unavailable' : 'Checking...';
  }
  function liveDetails(item) {
    var parts = [];
    if (item.category) { parts.push(item.category); }
    if (item.viewers) { parts.push(compact(item.viewers) + ' watching'); }
    return parts.join(' · ');
  }
  function art(item, withImage, before) {
    var node = el('span', 'art');
    var h = hue(item.name + item.platform);
    var colours = 'linear-gradient(135deg, hsl(' + h + ',62%,50%), hsl(' + ((h + 40) % 360) + ',68%,26%))';
    var hasImage = withImage && item.thumb;
    // A card redrawn with a newer preview keeps showing the previous one until the new one has arrived.
    var keep = hasImage && before && /^[\w\/?=&.:-]+$/.test(before);
    node.style.backgroundImage = keep ? 'url("' + before + '"), ' + colours : colours;
    if (!keep) {
      node.appendChild(el('span', 'mono', (item.name.replace(/^[^A-Za-z0-9]+/, '').charAt(0) || '?').toUpperCase()));
    }
    if (hasImage) {
      var image = el('img', 'thumb');
      image.alt = '';
      // Newer browsers fetch previews further down the page only when you scroll to them.
      image.setAttribute('loading', 'lazy');
      image.setAttribute('decoding', 'async');
      image.onload = function () { image.className = 'thumb loaded'; };
      image.onerror = function () { if (image.parentNode) { image.parentNode.removeChild(image); } };
      image.src = item.thumb;
      node.appendChild(image);
    }
    return node;
  }
  function watchedBar(picture, item) {
    // How far this person got, on any of their screens.
    if (item.finished) { picture.appendChild(el('span', 'badge seen', 'Watched')); }
    if (!item.progress) { return; }
    var bar = el('span', 'watched-bar');
    var fillPart = el('span');
    fillPart.style.width = Math.max(2, Math.round(item.progress * 100)) + '%';
    bar.appendChild(fillPart);
    picture.appendChild(bar);
    picture.className += ' has-progress';
  }
  function section(item) {
    if (item.kind === 'video' && item.source !== 'library') { return 'video'; }
    return item.live === true ? 'live' : 'offline';
  }
  function signature(item) {
    var playing = live && live.url === item.url;
    // Rounded viewer counts: a few viewers more or less should not rebuild a card.
    return [item.id, section(item), item.name, item.title, item.category, item.viewers && compact(item.viewers), item.thumb,
      item.duration, item.checked ? 1 : 0, item.live,
      playing, confirmId === item.id, item.finished, item.progress && Math.round(item.progress * 50)].join('|');
  }
  function buildCard(item, before) {
    var kind = section(item);
    var button = el('button', 'card ' + kind + '-card' + (confirmId === item.id ? ' confirm' : '') +
      (item.source === 'library' ? '' : ' fixed'));
    var label = el('span', 'label');
    if (kind === 'video') {
      var picture = art(item, true, before);
      if (item.duration) { picture.appendChild(el('span', 'duration', clock(item.duration))); }
      watchedBar(picture, item);
      button.appendChild(picture);
      label.appendChild(el('span', 'name two-lines', item.title || item.name));
      label.appendChild(el('span', 'sub', item.name));
    } else if (kind === 'live') {
      var preview = art(item, true, before);
      var playing = live && live.url === item.url;
      preview.appendChild(el('span', playing ? 'on-air playing' : 'on-air', playing ? 'ON TV' : 'LIVE'));
      button.appendChild(preview);
      label.appendChild(el('span', 'name', item.name));
      label.appendChild(el('span', 'title', item.title || shortUrl(item.url)));
      label.appendChild(el('span', 'sub', liveDetails(item)));
    } else {
      button.appendChild(art(item, false));
      label.appendChild(el('span', 'name', item.name));
      label.appendChild(el('span', 'sub', offlineText(item)));
    }
    button.appendChild(label);
    if (item.source === 'library') {
      button.appendChild(el('span', 'remove', confirmId === item.id ? 'Press again to remove' : 'Remove'));
    }
    button.setAttribute('data-id', item.id);
    return button;
  }
  function fill(grid, items, used) {
    // Reuse unchanged cards and only move nodes whose position changed: cheap on the TV.
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      var sig = signature(item);
      var entry = cardCache[item.id];
      if (!entry || entry.sig !== sig) { entry = {sig: sig, node: buildCard(item, entry && entry.thumb), thumb: item.thumb}; }
      used[item.id] = entry;
      entry.node.onclick = choose(item);
      entry.node.onfocus = clearConfirm(item.id);
      if (grid.childNodes[i] !== entry.node) { grid.insertBefore(entry.node, grid.childNodes[i] || null); }
    }
    while (grid.childNodes.length > items.length) { grid.removeChild(grid.childNodes[grid.childNodes.length - 1]); }
  }
  function clearConfirm(id) {
    return function () { if (confirmId && confirmId !== id) { confirmId = null; drawLibrary(); } };
  }
  function drawLibrary() {
    var active = document.activeElement;
    var activeId = active && active.getAttribute ? active.getAttribute('data-id') : null;
    var hadFocus = !active || active === document.body || activeId !== null;
    var liveItems = [];
    var offlineItems = [];
    var checked = false;
    for (var i = 0; i < streams.length; i++) {
      streams[i].source = 'library';
      if (streams[i].checked) { checked = true; }
      (streams[i].live === true ? liveItems : offlineItems).push(streams[i]);
    }
    liveItems = liveItems.concat(followed);
    var used = {};
    fill(liveGrid, liveItems, used);
    fill(continueGrid, continuing, used);
    fill(offlineGrid, offlineItems, used);
    cardCache = used;
    $('live-count').textContent = liveItems.length ? String(liveItems.length) : '';
    $('offline-count').textContent = offlineItems.length ? String(offlineItems.length) : '';
    $('live-empty').textContent = !streams.length && !followed.length
      ? 'Add your favourite channels, or connect Twitch under Accounts.'
      : checked || followed.length ? 'Nobody you follow is live right now.' : 'Checking who is live...';
    $('live-empty').className = liveItems.length ? 'empty hidden' : 'empty';
    $('continue-section').className = 'section' + (continuing.length ? '' : ' hidden');
    $('offline-section').className = 'section' + (offlineItems.length ? '' : ' hidden');
    if (view !== 'library' || dialog) { return; }
    if (activeId && used[activeId]) {
      // A moved card can lose focus; give it back.
      if (document.activeElement !== used[activeId].node) { focus(used[activeId].node); }
    } else if (hadFocus) {
      focusLibrary();
    }
  }
  function focusLibrary() {
    // Focus is for remotes and keyboards; on a touchscreen it would only highlight a random card,
    // and with a mouse it waits until an arrow key is pressed.
    if (!isTV && (touch || !usedKeys)) { return; }
    focus(libraryStart());
  }
  function libraryStart() {
    var target = nowStream && cardForUrl(nowStream.url);
    return target || liveGrid.firstChild || continueGrid.firstChild || offlineGrid.firstChild || $('add');
  }
  function cardForUrl(url) {
    var lists = [streams, followed, continuing];
    for (var l = 0; l < lists.length; l++) {
      for (var i = 0; i < lists[l].length; i++) {
        var item = lists[l][i];
        if (item.url === url && cardCache[item.id]) { return cardCache[item.id].node; }
      }
    }
    return null;
  }
  function loadLibrary() {
    if (libraryLoading) { return; }
    libraryLoading = true;
    request('GET', '/api/streams', null, function (error, result) {
      libraryLoading = false;
      if (error) { say(error); if (libraryVersion === null) { drawLibrary(); } return; }
      streams = result.streams || [];
      followed = result.followed || [];
      for (var i = 0; i < followed.length; i++) { followed[i].source = 'followed'; }
      continuing = result.continuing || [];
      libraryVersion = result.version;
      drawLibrary();
    });
  }
  function choose(item) {
    return function () {
      if (managing) {
        if (item.source !== 'library') { say('Only streams you saved can be removed here.'); return; }
        if (confirmId === item.id) { removeStream(item); }
        else { confirmId = item.id; drawLibrary(); }
        return;
      }
      if (pending && nowStream && nowStream.url === item.url) { showWatch(); return; }
      if (item.source === 'library') { startStream({id: item.id}, false, item); return; }
      startStream({url: item.url, name: item.name, kind: item.kind || 'live', title: item.title || ''}, false, item);
    };
  }
  function removeStream(stream) {
    var index = 0;
    for (var i = 0; i < streams.length; i++) { if (streams[i].id === stream.id) { index = i; } }
    request('POST', '/api/streams/remove', {id: stream.id}, function (error) {
      if (error) { say(error); return; }
      confirmId = null;
      streams.splice(index, 1);
      drawLibrary();
      var next = streams[Math.min(index, streams.length - 1)];
      focus(next && cardCache[next.id] ? cardCache[next.id].node : $('manage'));
      say('Removed ' + stream.name + '.');
    });
  }
  function toggleManage() {
    managing = !managing; confirmId = null;
    $('manage').textContent = managing ? 'Done' : 'Manage';
    $('hint').textContent = managing ? 'Choose a saved stream to remove it. Choose it again to confirm.' : hint();
    drawLibrary(); render();
    if (managing) { focus(offlineGrid.firstChild || liveGrid.firstChild || $('manage')); }
  }
  function hint() {
    return isTV ? 'Choose with the arrows and press OK. Back returns here while watching.'
      : (touch ? 'Tap' : 'Choose') + ' a stream to watch it here and on the TV. Every open screen follows along.';
  }
  function showLive(state) {
    var next = (state.state === 'starting' || state.state === 'playing') && state.stream ? state.stream : null;
    var changed = (next && next.url) !== (live && live.url);
    live = next;
    $('now-playing').className = live ? 'chip' : 'chip hidden';
    $('now-playing-name').textContent = live ? 'Now playing · ' + live.name : '';
    if (changed) { drawLibrary(); }
  }
  function openAdd() {
    channel.value = ''; nameInput.value = '';
    openDialog(addDialog, channel);
  }
  function addFromDialog(play) {
    var value = channel.value.replace(/^\s+|\s+$/g, '');
    if (!value) { say('Paste a Twitch link or a channel name.'); focus(channel); return; }
    if (/twitch\.tv\/videos\/\d+/i.test(value)) {
      // A past broadcast is played, not saved: it then shows under Continue watching and in History.
      if (!play) { say('A past broadcast is played, not saved. Choose Save & watch.'); focus($('start')); return; }
      var title = nameInput.value.replace(/^\s+|\s+$/g, '') || 'Twitch video';
      closeDialog('watch');
      startStream({url: value, name: title, kind: 'video', title: ''}, false, {name: title, platform: 'Twitch', url: value});
      return;
    }
    say('Saving...');
    request('POST', '/api/streams/add', {url: value, name: nameInput.value}, function (error, result) {
      if (error) { say(error); focus(channel); return; }
      var stream = result.stream;
      stream.source = 'library';
      if (result.created) { streams.push(stream); }
      if (play) {
        closeDialog('watch');
        drawLibrary();
        startStream({id: stream.id}, false, stream);
      } else {
        closeDialog();
        drawLibrary();
        if (cardCache[stream.id]) { focus(cardCache[stream.id].node); }
        say(result.created ? 'Saved ' + stream.name + '.' : stream.name + ' is already in your library.');
      }
    });
  }

  /* ---------- Watch history ---------- */

  function openHistory() {
    clearing = false;
    drawHistory();
    openDialog(historyDialog, $('history-close'));
    $('history-empty').textContent = 'Loading...';
    request('GET', '/api/history', null, function (error, result) {
      if (dialog !== historyDialog) { return; }
      if (error) { $('history-empty').textContent = error; return; }
      watched = result.items || [];
      drawHistory();
      focus($('history-list').firstChild ? $('history-list').firstChild.firstChild : $('history-close'));
    });
  }
  function historyText(item) {
    var parts = [item.name];
    if (item.watched) { parts.push(ago(item.watched)); }
    if (item.finished) { parts.push('watched'); }
    else if (item.position && item.duration) { parts.push(clock(item.position) + ' of ' + clock(item.duration)); }
    else if (item.duration) { parts.push(clock(item.duration)); }
    return parts.join(' · ');
  }
  function drawHistory() {
    var list = $('history-list');
    list.innerHTML = '';
    for (var i = 0; i < watched.length; i++) {
      var item = watched[i];
      var row = el('div', 'history-row');
      var open = el('button', 'history-item');
      var picture = art(item, true);
      watchedBar(picture, item);
      open.appendChild(picture);
      var text = el('span', 'history-text');
      text.appendChild(el('span', 'name', item.title || item.name));
      text.appendChild(el('span', 'sub', historyText(item)));
      open.appendChild(text);
      open.onclick = playFromHistory(item);
      var remove = el('button', 'btn ghost slim', 'Remove');
      remove.setAttribute('aria-label', 'Remove ' + (item.title || item.name) + ' from history');
      remove.onclick = removeFromHistory(item, i);
      row.appendChild(open);
      row.appendChild(remove);
      list.appendChild(row);
    }
    $('history-empty').textContent = 'Nothing yet. Past broadcasts you watch show up here.';
    $('history-empty').className = watched.length ? 'empty hidden' : 'empty';
    $('history-clear').className = watched.length ? 'btn danger' : 'btn danger hidden';
    $('history-clear').textContent = clearing ? 'Press again to clear everything' : 'Clear history';
  }
  function playFromHistory(item) {
    return function () {
      closeDialog('watch');
      startStream({url: item.url, name: item.name, kind: 'video', title: item.title || ''}, false, item);
    };
  }
  function removeFromHistory(item, index) {
    return function () {
      request('POST', '/api/history/remove', {url: item.url}, function (error) {
        if (error) { say(error); return; }
        watched.splice(index, 1);
        drawHistory();
        var rows = $('history-list').childNodes;
        var next = rows[Math.min(index, rows.length - 1)];
        focus(next ? next.lastChild : $('history-close'));
        say('Removed ' + (item.title || item.name) + ' from your history.');
      });
    };
  }
  function clearHistory() {
    if (!clearing) { clearing = true; drawHistory(); return; }
    request('POST', '/api/history/clear', {}, function (error) {
      if (error) { say(error); return; }
      clearing = false; watched = [];
      drawHistory();
      focus($('history-close'));
      say('Your watch history is cleared.');
    });
  }

  /* ---------- Accounts ---------- */

  function openAccounts() {
    openDialog(accountsDialog, $('accounts-done'));
    loadAccounts();
  }
  function loadAccounts() {
    if (accountsLoading) { return; }
    accountsLoading = true;
    request('GET', '/api/accounts', null, function (error, result) {
      accountsLoading = false;
      if (error) { say(error); return; }
      drawAccount(result.twitch);
    });
  }
  function accountAction(action) {
    var box = $('account-twitch');
    request('POST', '/api/accounts/' + action, {}, function (error, result) {
      if (error) { box.sig = null; say(error); loadAccounts(); return; }
      drawAccount(result.twitch);
      focus(box.querySelector('.primary') || box.querySelector('button'));
    });
  }
  function drawAccount(info) {
    var box = $('account-twitch');
    var sig = [info.configured, info.connected, info.user && info.user.name, info.pending && info.pending.code,
      info.error].join('|');
    if (box.sig === sig) { return; }   // keep focus while nothing changed
    var hadFocus = box.contains && box.contains(document.activeElement);
    box.sig = sig;
    box.textContent = '';
    var head = el('div', 'account-head');
    head.appendChild(el('strong', '', 'Twitch'));
    head.appendChild(el('span', info.connected ? 'state on' : 'state',
      info.connected ? 'Connected as ' + (info.user && info.user.name) : 'Not connected'));
    box.appendChild(head);
    var row = el('div', 'row');
    if (info.pending) {
      var code = el('div', 'device-code');
      var qr = el('img', 'qr');
      qr.alt = '';
      qr.src = '/api/qr?data=' + encodeURIComponent(info.pending.url);
      code.appendChild(qr);
      var steps = el('div', 'steps');
      steps.appendChild(el('span', '', 'Scan with your phone, or open '));
      steps.appendChild(el('b', '', shortUrl(info.pending.url.split('?')[0])));
      steps.appendChild(el('span', '', ' and enter'));
      steps.appendChild(el('span', 'code', info.pending.code));
      steps.appendChild(el('span', 'muted small', 'Waiting for you to approve...'));
      code.appendChild(steps);
      box.appendChild(code);
      row.appendChild(button('Cancel', '', function () { accountAction('cancel'); }));
    } else if (info.connected) {
      row.appendChild(button('Disconnect', 'danger', function () { accountAction('disconnect'); }));
    } else if (info.configured) {
      row.appendChild(button('Connect Twitch', 'primary', function () { accountAction('connect'); }));
    } else {
      box.appendChild(el('p', 'muted small', me && me.admin
        ? 'Set up Twitch sign-in once under People, then connect here.'
        : 'The server owner has not set up Twitch sign-in yet.'));
    }
    box.appendChild(row);
    if (info.error) { box.appendChild(el('p', 'account-error', info.error)); }
    if (hadFocus) { focus(box.querySelector('.primary') || box.querySelector('button') || $('accounts-done')); }
  }
  function button(text, kind, action) {
    var node = el('button', 'btn' + (kind ? ' ' + kind : ''), text);
    node.onclick = action;
    return node;
  }

  /* ---------- People (owner) ---------- */

  function openPeople() {
    openDialog(peopleDialog, $('people-done'));
    $('person-name').value = '';
    peopleRequest('GET', '/api/people', null);
  }
  function peopleRequest(method, path, data, after) {
    request(method, path, data, function (error, result) {
      $('people-message').textContent = error || '';
      if (error) { return; }
      drawPeople(result);
      if (after) { after(result); }
    });
  }
  function drawPeople(data) {
    var list = $('people-list');
    list.textContent = '';
    for (var i = 0; i < data.people.length; i++) { list.appendChild(personRow(data.people[i])); }
    $('people-usage').textContent = data.watching + ' of ' + data.max_streams + ' streams in use right now.';
    var invite = $('invite');
    invite.className = data.invite ? 'invite' : 'invite hidden';
    if (data.invite) {
      $('invite-name').textContent = 'Invite for ' + data.invite.name;
      $('invite-qr').src = data.invite.qr;
      $('invite-code').textContent = data.invite.code;
      $('invite-link').textContent = data.invite.link;
    }
    var app = data.apps.twitch;
    $('twitch-app-state').textContent = app.configured ? 'Ready: people can connect Twitch.' : 'Not set up yet.';
    $('twitch-app-state').className = app.configured ? 'state on' : 'state';
    if (document.activeElement !== $('twitch-client-id')) { $('twitch-client-id').value = app.client_id || ''; }
  }
  function personRow(person) {
    var row = el('div', 'person');
    var label = el('span', 'person-name', person.name + (person.admin ? ' (owner)' : ''));
    row.appendChild(label);
    row.appendChild(el('span', 'muted small', person.devices === 1 ? '1 device' : person.devices + ' devices'));
    row.appendChild(button('Invite', '', function () {
      peopleRequest('POST', '/api/people/invite', {id: person.id}, function () { focus($('invite-done')); });
    }));
    if (!person.admin) {
      var remove = button('Remove', 'danger', function () {
        if (remove.textContent !== 'Press again') { remove.textContent = 'Press again'; return; }
        peopleRequest('POST', '/api/people/remove', {id: person.id}, function () { focus($('person-name')); });
      });
      remove.onblur = function () { remove.textContent = 'Remove'; };
      row.appendChild(remove);
    }
    return row;
  }
  function addPerson() {
    var name = $('person-name').value.replace(/^\s+|\s+$/g, '');
    if (!name) { $('people-message').textContent = 'Enter a name.'; focus($('person-name')); return; }
    peopleRequest('POST', '/api/people/add', {name: name}, function () {
      $('person-name').value = '';
      focus($('invite-done'));
    });
  }
  function saveApp() {
    peopleRequest('POST', '/api/app/setup', {client_id: $('twitch-client-id').value}, function () {
      $('people-message').textContent = 'Twitch sign-in is ready.';
    });
  }

  /* ---------- Other devices and the iPhone app ---------- */

  function showDeviceCode() {
    request('POST', '/api/devices/code', {}, function (error, result) {
      if (error) { say(error); return; }
      $('device-code').className = 'device-code';
      $('device-code-qr').src = result.qr;
      $('device-code-text').textContent = result.code;
      $('device-code-show').textContent = 'Show a new code';
    });
  }
  function offerInstall() {
    // In Safari on iPhone/iPad: explain Add to Home Screen, with the code the app will ask for.
    if (!isIOS || isApp || recall('my-streams-install-hidden')) { return; }
    request('POST', '/api/devices/code', {}, function (error, result) {
      if (error) { return; }
      $('install-code').textContent = result.code;
      $('install').className = 'install';
    });
  }

  /* ---------- Signing this device in ---------- */

  function showSignIn() {
    view = 'signin';
    render();
    startPairing();
  }
  function startPairing() {
    if (pairingTimer) { window.clearInterval(pairingTimer); pairingTimer = null; }
    request('POST', '/api/pair/start', {kind: isTV ? 'TV' : 'browser'}, function (error, result) {
      if (error) { $('signin-message').textContent = error; window.setTimeout(startPairing, 5000); return; }
      pairing = result;
      $('signin-setup').className = result.setup ? 'setup' : 'setup hidden';
      $('signin-title').className = result.setup ? 'hidden' : '';
      $('signin-qr').src = result.qr;
      $('signin-code').textContent = result.code;
      $('signin-link').textContent = shortUrl(result.link.split('?')[0]);
      $('signin-message').textContent = '';
      pairingTimer = window.setInterval(collect, 2000);
      // On the TV, scanning is the main way; focusing the field would open the on-screen keyboard.
      focus(result.setup ? $('signin-claim') : isTV ? $('signin-use') : $('signin-invite'));
    });
  }
  function collect() {
    if (!pairing || away()) { return; }
    request('POST', '/api/pair/collect', {secret: pairing.secret}, function (error, result) {
      if (error) { startPairing(); return; }   // the code expired: show a fresh one
      if (result.done) { signedIn(result.token); }
    });
  }
  function signedIn(token) {
    if (pairingTimer) { window.clearInterval(pairingTimer); pairingTimer = null; }
    store('my-streams-device', token);
    window.location.reload();
  }
  function useInvite() {
    // An invite code or a sign-in code from another device, typed here, signs this device in directly.
    var code = $('signin-invite').value;
    if (!code) { focus($('signin-invite')); return; }
    request('POST', '/api/devices/redeem', {code: code, kind: isApp ? 'iPhone app' : isTV ? 'TV' : 'Browser'},
      function (error, result) {
        if (error) { $('signin-message').textContent = error; return; }
        signedIn(result.token);
      });
  }
  function claimOwner() {
    request('POST', '/api/setup/claim', {}, function (error, result) {
      if (error) { $('signin-message').textContent = error; startPairing(); return; }
      signedIn(result.token);
    });
  }
  function signOut() {
    request('POST', '/api/logout', {}, function () {
      store('my-streams-device', null);
      window.location.reload();
    });
  }

  /* ---------- Quality ---------- */

  function qualityLabel(value) { return value === '1080p60' ? '1080p 60fps' : value; }
  function drawQuality() {
    var text = quality ? 'Quality · ' + qualityLabel(quality) : 'Quality';
    $('quality').textContent = text;
    $('quality-lib').textContent = text;
  }
  function cycleQuality() {
    var index = 0;
    for (var i = 0; i < qualities.length; i++) { if (qualities[i] === quality) { index = i; } }
    quality = qualities[(index + 1) % qualities.length];
    drawQuality();
    say('Quality ' + qualityLabel(quality) + '. Press again for another option.');
    if (qualityTimer) { window.clearTimeout(qualityTimer); }
    // Restarting a stream takes a while, so apply only once the presses stop.
    qualityTimer = window.setTimeout(applyQuality, 1500);
  }
  function applyQuality() {
    var chosen = quality;
    request('POST', '/api/settings', {quality: chosen}, function (error) {
      qualityTimer = null;
      if (error) { say(error); return; }
      var running = lastStatus && lastStatus.playing_quality;
      if (activeTarget && (pending || failed) && running !== chosen) {
        startStream(activeTarget, false);
        say('Switching to ' + qualityLabel(chosen) + '. This takes a few seconds...');
      } else {
        say('Quality set to ' + qualityLabel(chosen) + (running && running !== chosen ? '. It applies to the next stream you start.' : '.'));
      }
    });
  }

  /* ---------- Playback ---------- */

  function setNow(stream) {
    nowStream = stream;
    $('watch-title').textContent = stream.name;
    var details = [stream.platform];
    if (stream.category) { details.push(stream.category); }
    details.push(stream.title || shortUrl(stream.url));
    $('watch-sub').textContent = details.join(' · ');
  }
  function play() {
    var generation = sourceGeneration;
    var promise = video.play();
    if (promise && promise.catch) {
      promise.catch(function () {
        if (generation !== sourceGeneration || !current || video.error || resumeAt !== null) { return; }
        if (!isTV && !video.muted) {
          // Joined without a click (e.g. sent from the Chrome extension): start muted instead.
          autoMuted = true; video.muted = true;
          play();
          return;
        }
        say('Video ready. Press Retry or use the video Play control.');
      });
    }
  }
  function resetPlayer() {
    sourceGeneration++;
    current = null; ready = false; startApplied = false; resumeAt = null; lastSecond = -1;
    if (hlsPlayer) { hlsPlayer.destroy(); hlsPlayer = null; }
    video.pause(); video.removeAttribute('src'); video.load();
    render();
  }
  function ensureHls(callback) {
    if (window.Hls) { callback(true); return; }
    var script = document.createElement('script');
    script.src = '/vendor/hls.light.min.js';
    script.onload = function () { callback(!!window.Hls); };
    script.onerror = function () { callback(false); };
    document.head.appendChild(script);
  }
  function load(url, text) {
    resetPlayer();
    current = url; failed = false;
    render();
    say(text);
    var generation = sourceGeneration;
    function nativePlayback() {
      report('player-mode', /\.m3u8/.test(url) ? 'native-hls' : 'native-mp4');
      video.src = url; video.load(); play();
    }
    // Native HLS on the LG and on iPhone/iPad (AirPlay, picture in picture); other browsers use hls.js.
    if (isTV || isIOS || !/\.m3u8/.test(url)) { nativePlayback(); return; }
    ensureHls(function (available) {
      if (generation !== sourceGeneration) { return; }
      if (!available || !window.Hls.isSupported()) { nativePlayback(); return; }
      report('player-mode', 'hls.js');
      var options = {maxBufferLength: 20};
      if (nowKind === 'video') {
        options.maxBufferLength = 30; options.startPosition = resumePosition(); startApplied = true;
        resumeAt = options.startPosition || null;   // hls.js starts earlier when that part is not there yet
      }
      hlsPlayer = new window.Hls(options);
      hlsPlayer.on(window.Hls.Events.MANIFEST_PARSED, function () {
        if (generation === sourceGeneration) { play(); }
      });
      hlsPlayer.on(window.Hls.Events.ERROR, function (event, data) {
        if (generation !== sourceGeneration || !data.fatal) { return; }
        if (data.type === 'networkError' && recoverNetwork()) { return; }
        fail('Playback failed: ' + data.details + '. Choose Retry to reconnect.');
        report('hls-error', data.type + ': ' + data.details);
      });
      hlsPlayer.loadSource(url);
      hlsPlayer.attachMedia(video);
    });
  }
  function fail(text) {
    pending = false; failed = true;
    say(text);
    wake();
  }
  function sample(url) {
    commandGeneration++;
    activeTarget = null; activeInstance = null;
    pending = false; nowKind = 'live';
    request('POST', '/api/stop', {}, function () {});
    setNow({name: 'Sample video', platform: 'Playback check', url: ''});
    load(url, 'Loading sample video...');
    showWatch();
  }
  function startStream(target, recovering, info) {
    var command = ++commandGeneration;
    activeTarget = target;
    if (!recovering) { networkRetries = 0; }
    pending = false; failed = false;
    nowKind = target.kind === 'video' ? 'video' : 'live';
    resetPlayer();
    if (info) { setNow(info); }
    showWatch();
    say(recovering ? 'Server restarted. Reconnecting to your stream...'
      : 'Connecting to ' + (nowStream ? nowStream.name : 'the stream') + '...');
    request('POST', '/api/start', target, function (error, result) {
      if (command !== commandGeneration) { return; }
      if (error) { fail(error); return; }
      activeInstance = result.instance_id;
      seenRun = result.run;
      if (result.stream) { setNow(merge(result.stream, info)); }
      pending = true; lastState = ''; poll();
    });
  }
  function merge(stream, info) {
    // Keep what the card already knew (title, category) when the server's copy is shorter.
    if (!info) { return stream; }
    return {name: stream.name, platform: stream.platform, url: stream.url, position: stream.position,
      title: stream.title || info.title, category: info.category};
  }
  function targetFor(stream) {
    var target = {url: stream.url, name: stream.name};
    if (stream.kind) { target.kind = stream.kind; }
    if (stream.title) { target.title = stream.title; }
    return target;
  }
  function follow(state) {
    // Another screen (or the Chrome extension) started a stream: join it.
    commandGeneration++;
    seenRun = state.run;
    activeTarget = targetFor(state.stream);
    activeInstance = state.instance_id;
    networkRetries = 0; failed = false;
    nowKind = state.kind === 'video' ? 'video' : 'live';
    resetPlayer();
    setNow(state.stream);
    pending = true; lastState = '';
    showWatch();
    handle(state);
  }
  function retry() {
    failed = false;
    if (!activeTarget) { if (current) { play(); } return; }
    if (lastStatus && lastStatus.run === seenRun && lastStatus.state === 'playing') {
      // The relay is still healthy; reconnect this screen only.
      commandGeneration++;
      networkRetries = 0; resetPlayer();
      pending = true; lastState = '';
      say('Reconnecting...');
      poll();
      return;
    }
    startStream(activeTarget, false);
  }
  function stop() {
    if (!video.paused) { reportProgress('pause'); }
    commandGeneration++;
    activeTarget = null; activeInstance = null;
    pending = false; failed = false; resetPlayer();
    request('POST', '/api/stop', {}, function (error) { say(error || 'Stopped.'); });
    showLibrary();
  }
  function recoverNetwork() {
    if (!activeTarget || networkRetries >= 3) { return false; }
    networkRetries++;
    resetPlayer(); pending = true;
    say('Connection interrupted. Reconnecting (' + networkRetries + '/3)...');
    return true;
  }

  /* ---------- Videos: seeking, pause, resume ---------- */

  function positionKey() { return nowStream && nowStream.url ? 'position:' + nowStream.url : null; }
  function resumePosition() {
    // The server knows where any of this person's screens stopped; this device's own note is the fallback.
    var known = nowStream && typeof nowStream.position === 'number';
    var saved = known ? nowStream.position : parseFloat(recall(positionKey()) || '0');
    return saved > 30 && (!duration || saved < duration - 60) ? saved : 0;
  }
  function seekable() {
    var ranges = video.seekable;
    return ranges && ranges.length ? ranges.end(ranges.length - 1) : (video.duration || 0);
  }
  function seekTo(seconds) {
    var end = seekable();
    // Only the downloaded part can be played; it grows quickly.
    var target = Math.max(0, Math.min(seconds, end ? end - 2 : seconds));
    try { video.currentTime = target; } catch (e) { return; }
    drawProgress();
  }
  function seekBy(seconds) {
    if (nowKind !== 'video' || !current) { return; }
    skipResume();
    seekTo(video.currentTime + seconds);
    wake();
  }
  function togglePause() {
    if (nowKind !== 'video' || !current) { return; }
    skipResume();
    if (video.paused) { play(); } else { video.pause(); }
    wake();
  }
  function applyStart() {
    // Some players open a still-growing video at its newest part; start at the beginning or the saved spot.
    if (startApplied || nowKind !== 'video') { return; }
    startApplied = true;
    var target = resumePosition();
    if (target) { resumeAt = target; drawProgress(); }   // draws 'continuing at' and continues if it can
    else if (video.currentTime > 3) { seekTo(0); }
  }
  function checkResume() {
    // Each start downloads the video from its beginning: wait (paused) until the spot to continue at is there.
    if (resumeAt === null || !current || nowKind !== 'video') { return; }
    if (Math.abs((video.currentTime || 0) - resumeAt) < 3) { resumeAt = null; return; }
    if (seekable() >= resumeAt + 2) {
      var target = resumeAt;
      resumeAt = null;
      seekTo(target);
      play();
      say('Continuing where you stopped.');
      return;
    }
    if (!video.paused) { video.pause(); }
  }
  function skipResume() {
    // Pressing play or skipping while it waits: they would rather not wait.
    if (resumeAt === null) { return; }
    resumeAt = null;
    drawProgress();
  }
  function drawProgress() {
    if (nowKind !== 'video') { return; }
    var total = duration || video.duration || 0;
    if (!isFinite(total)) { total = 0; }
    var position = video.currentTime || 0;
    $('seek-fill').style.width = total ? Math.min(100, position / total * 100) + '%' : '0';
    $('seek-ready').style.width = total ? Math.min(100, seekable() / total * 100) + '%' : '0';
    var loaded = seekable();
    // While the video is still downloading, say how far you can skip.
    $('seek-time').textContent = clock(position) + ' / ' + (total ? clock(total) : '--:--') +
      (total && loaded && loaded < total - 5 ? '  ·  loaded to ' + clock(loaded) : '') +
      (resumeAt !== null ? '  ·  continuing at ' + clock(resumeAt) + ' when loaded' : '');
    checkResume();
  }
  video.addEventListener('timeupdate', function () {
    var second = Math.floor(video.currentTime || 0);
    if (second === lastSecond) { return; }   // repaint at most once per second
    lastSecond = second;
    drawProgress();
    if (second % 10 === 0 && second > 0 && positionKey()) { store(positionKey(), String(second)); }
    if (!video.paused && new Date().getTime() - lastReport > 10000) { reportProgress('tick'); }
  });
  function reportProgress(event) {
    if (nowKind !== 'video' || !current || !activeTarget || !nowStream || !nowStream.url) { return; }
    // Until the player has jumped to the resume position it is still at 0: that is not progress.
    if (event !== 'ended' && (!startApplied || resumeAt !== null || !(video.currentTime > 0))) { return; }
    lastReport = new Date().getTime();
    var position = Math.floor(video.currentTime || 0);
    nowStream.position = event === 'ended' ? 0 : position;
    request('POST', '/api/history/progress', {url: nowStream.url, position: position,
      duration: Math.floor(duration || video.duration || 0) || null, event: event}, function () {});
  }
  video.addEventListener('loadedmetadata', applyStart);
  video.addEventListener('progress', drawProgress);   // more downloaded: maybe enough to continue now
  video.addEventListener('pause', function () {
    if (nowKind === 'video' && current && positionKey() && video.currentTime > 0) {
      store(positionKey(), String(Math.floor(video.currentTime)));
    }
    if (!video.ended) { reportProgress('pause'); }
  });
  function drawControls() {
    $('pause').innerHTML = video.paused ? '&#9654;' : '&#10074;&#10074;';
    $('pause').setAttribute('aria-label', video.paused ? 'Play' : 'Pause');
    $('mute').textContent = video.muted || !video.volume ? 'Sound off' : 'Sound on';
    $('volume').value = String(video.muted ? 0 : video.volume);
  }
  video.addEventListener('play', drawControls);
  video.addEventListener('pause', drawControls);
  video.addEventListener('volumechange', drawControls);
  video.addEventListener('playing', function () {
    ready = true; failed = false;
    applyStart();
    // Shown over the video only when there is something to do about it (this also fires after every skip).
    say('Playing ' + (nowStream ? nowStream.name : 'video') +
      (autoMuted ? ' muted. ' + (touch ? 'Tap' : 'Click') + ' anywhere to turn on sound.' : '.'), !autoMuted);
    wake();
    report('playing', current);
  });
  video.addEventListener('error', function () {
    if (!current) { return; }
    var code = video.error ? video.error.code : 'unknown';
    report('video-error', String(code) + ': ' + (video.error && video.error.message || ''));
    if (code === 2 && recoverNetwork()) { return; }
    fail('Playback error ' + code + '. Choose Retry, or try a playback check sample.');
  });
  video.addEventListener('ended', function () {
    ready = false;
    if (activeTarget && nowKind === 'video') {
      store(positionKey(), null);
      reportProgress('ended');
      pending = false;
      say('Finished ' + (nowStream ? nowStream.name : 'the video') + '.');
      showLibrary();
    } else if (activeTarget) {
      fail('Stream ended. Choose Retry to reconnect.');
    } else { say('Sample finished.'); showLibrary(); }
  });

  /* ---------- Server status ---------- */

  function away() {
    // A tab or app nobody is looking at, with nothing playing in it, has no reason to ask every two seconds.
    return document.hidden === true && !pending && !current;
  }
  function poll() {
    var command = commandGeneration;
    if (dialog === accountsDialog) { loadAccounts(); }
    request('GET', '/api/status', null, function (error, state) {
      if (command !== commandGeneration) { return; }
      if (error) { if (pending) { say(error); } return; }
      lastStatus = state;
      if (state.quality && !qualityTimer && state.quality !== quality) {
        quality = state.quality;
        qualities = state.qualities || qualities;
        drawQuality();
      }
      if (state.library && state.library !== libraryVersion) { loadLibrary(); }
      showLive(state);
      if (pending) { handle(state); return; }
      if ((state.state === 'starting' || state.state === 'playing') && state.stream && state.run && state.run !== seenRun) {
        follow(state);
      }
    });
  }
  function handle(state) {
    if (activeInstance && state.instance_id !== activeInstance) {
      activeInstance = state.instance_id;
      if (state.state === 'idle' && activeTarget) {
        startStream(activeTarget, true);
        return;
      }
    }
    if (state.state === 'idle') {
      pending = false; resetPlayer();
      say('Playback was stopped. Choose a stream to start again.');
      showLibrary();
      return;
    }
    if (state.run && state.run !== seenRun && state.stream) {
      // Someone switched the shared relay to another stream while this screen was watching.
      seenRun = state.run; networkRetries = 0;
      activeTarget = targetFor(state.stream);
      setNow(state.stream);
      resetPlayer();
      showWatch();
    }
    if (state.kind && state.kind !== nowKind) {
      // Only the server knows whether a link turned out to be a live broadcast or a video.
      nowKind = state.kind;
      render();
      if (view === 'watch') { focus(watchFocus()); }
    }
    duration = state.duration || 0;
    if (state.state === 'error') { fail(state.message); return; }
    if (state.url && current !== state.url) { load(state.url, 'Ready. Starting playback...'); }
    else if (!state.url && state.message !== lastState) { say(state.message); }
    if (state.state === 'starting' && !quickPoll && window.setTimeout) {
      // Check often while a stream starts, so playback begins the moment it is ready.
      quickPoll = window.setTimeout(function () { quickPoll = null; if (pending) { poll(); } }, 500);
    }
    lastState = state.message;
    if (nowKind === 'video' && view === 'watch') { drawProgress(); }
  }

  /* ---------- Remote control and keyboard ---------- */

  function focus(node) {
    if (!node || !node.focus) { return false; }
    node.focus();
    return true;
  }
  function layer() { return dialog || (view === 'watch' ? watchEl : view === 'signin' ? signinEl : libraryEl); }
  function moveFocus(direction) {
    var nodes = layer().querySelectorAll('button, input, a[href]');
    var active = document.activeElement;
    var from = null;
    var items = [];
    for (var i = 0; i < nodes.length; i++) {
      var rect = nodes[i].getBoundingClientRect();
      if (!rect.width || !rect.height || nodes[i].disabled) { continue; }
      if (nodes[i] === active) { from = rect; } else { items.push({node: nodes[i], rect: rect}); }
    }
    if (!from) {
      // Nothing chosen yet (a computer before its first arrow key): start where the TV starts.
      if (layer() === libraryEl) { focus(libraryStart()); } else if (items.length) { focus(items[0].node); }
      return;
    }
    var fx = from.left + from.width / 2;
    var fy = from.top + from.height / 2;
    var best = null;
    var bestScore = Infinity;
    for (var j = 0; j < items.length; j++) {
      var r = items[j].rect;
      var cx = r.left + r.width / 2;
      var cy = r.top + r.height / 2;
      var ahead, aside;
      if (direction === 'left') { ahead = fx - cx; aside = r.bottom > from.top && r.top < from.bottom ? 0 : Math.abs(cy - fy); }
      else if (direction === 'right') { ahead = cx - fx; aside = r.bottom > from.top && r.top < from.bottom ? 0 : Math.abs(cy - fy); }
      else if (direction === 'up') { ahead = fy - cy; aside = Math.abs(cx - fx); }
      else { ahead = cy - fy; aside = Math.abs(cx - fx); }
      if (ahead < 4) { continue; }
      var score = ahead + aside * 2;
      if (score < bestScore) { bestScore = score; best = items[j].node; }
    }
    if (best) { focus(best); reveal(best); }
  }
  function reveal(node) {
    var box = layer() === libraryEl ? libraryEl : null;
    if (!box) { return; }
    var rect = node.getBoundingClientRect();
    var margin = 80;
    if (rect.top < margin) { box.scrollTop -= margin - rect.top; }
    else if (rect.bottom > window.innerHeight - margin) { box.scrollTop += rect.bottom - window.innerHeight + margin; }
  }
  var directions = {37: 'left', 38: 'up', 39: 'right', 40: 'down'};
  // webOS remote media keys: Play, Pause, Fast-forward, Rewind, Stop.
  var mediaKeys = {415: 'play', 19: 'pause', 417: 'forward', 412: 'rewind', 413: 'stop'};
  function mediaKey(action) {
    if (action === 'stop') { if (activeTarget) { stop(); } return; }
    if (nowKind !== 'video' || !current) { return; }
    if (action === 'play') { play(); }
    else if (action === 'pause') { video.pause(); }
    else { seekBy(action === 'forward' ? 30 : -10); }
    wake();
  }
  document.addEventListener('keydown', function (event) {
    var key = event.keyCode;
    var target = document.activeElement;
    usedKeys = true;
    if (view === 'watch' && !dialog) {
      if (mediaKeys[key]) { event.preventDefault(); mediaKey(mediaKeys[key]); return; }
      var wasIdle = idle;
      wake();
      if (nowKind === 'video' && (wasIdle || target === seekButton) && (key === 37 || key === 39 || key === 13)) {
        // Seeking and pausing are harmless, so they work even while the controls are hidden.
        event.preventDefault();
        focus(seekButton);
        if (key === 13) { togglePause(); } else { seekBy(key === 39 ? 10 : -10); }
        return;
      }
      // The first press only brings back the controls, so OK never stops a stream by surprise.
      if (wasIdle && (directions[key] || key === 13)) { event.preventDefault(); return; }
    }
    if (key === 27) { event.preventDefault(); back(); return; }
    if (key === 461) {
      // webOS Back normally triggers history.back(); fall back if it did not.
      var before = pops;
      window.setTimeout(function () { if (pops === before) { back(); } }, 300);
      return;
    }
    if (key === 13 && (target === channel || target === nameInput)) { event.preventDefault(); addFromDialog(true); return; }
    if (key === 13 && target === $('signin-invite')) { event.preventDefault(); useInvite(); return; }
    if (key === 13 && target === $('person-name')) { event.preventDefault(); addPerson(); return; }
    var direction = directions[key];
    if (!direction) { return; }
    if (target && target.tagName === 'INPUT' && (direction === 'left' || direction === 'right')) { return; }
    event.preventDefault();
    moveFocus(direction);
  });
  document.addEventListener('mousemove', function () {
    if (view !== 'watch') { return; }
    // The pointer reports many times a second; while the controls already show, twice a second keeps them up.
    var now = new Date().getTime();
    if (!idle && now - lastMove < 500) { return; }
    lastMove = now;
    wake();
  });
  function unmute() {
    if (!autoMuted) { return; }
    autoMuted = false; video.muted = false;
    if (ready) { say('Playing ' + (nowStream ? nowStream.name : 'video') + '.', true); }
  }
  document.addEventListener('click', unmute);
  function unlock() {
    // iPhone only lets a page start sound after a tap. Starting the (empty) player during the first tap
    // allows the stream to play with sound later, when it is ready.
    if (unlocked || !isIOS) { return; }
    unlocked = true;
    try { var attempt = video.play(); if (attempt && attempt.catch) { attempt.catch(function () {}); } } catch (e) {}
  }
  document.addEventListener('touchend', unlock, true);
  document.addEventListener('click', unlock, true);
  document.addEventListener('keyup', unmute);
  if (isTV) {
    // Magic Remote pointer: highlight what the pointer is over, like arrow focus.
    document.addEventListener('mouseover', function (event) {
      var node = event.target;
      while (node && node !== document.body) {
        if (node.tagName === 'BUTTON' || node.tagName === 'A' || node.tagName === 'INPUT') {
          if (node !== document.activeElement && node.tagName !== 'INPUT') { node.focus(); }
          return;
        }
        node = node.parentNode;
      }
    });
  }

  /* ---------- Wiring ---------- */

  function closeOnBackdrop(node) {
    // A click on the dimmed page beside a dialog closes it, unless the press began inside (selecting text in a field).
    var pressed = null;
    node.onmousedown = function (event) { pressed = event ? event.target : null; };
    node.onclick = function (event) {
      if (event && event.target === node && pressed === node && dialog === node) { closeDialog(); }
      pressed = null;
    };
  }
  closeOnBackdrop(addDialog);
  closeOnBackdrop(historyDialog);
  closeOnBackdrop(accountsDialog);
  closeOnBackdrop(peopleDialog);

  $('add').onclick = openAdd;
  $('accounts').onclick = openAccounts;
  $('people').onclick = openPeople;
  $('people-done').onclick = function () { closeDialog(); };
  $('person-add').onclick = addPerson;
  $('invite-done').onclick = function () { $('invite').className = 'invite hidden'; focus($('person-name')); };
  $('twitch-app-save').onclick = saveApp;
  $('signin-use').onclick = useInvite;
  $('device-code-show').onclick = showDeviceCode;
  $('install-close').onclick = function () { $('install').className = 'install hidden'; store('my-streams-install-hidden', '1'); };
  if (isApp) {
    $('signin-invite-label').innerHTML = '<b>Enter your sign-in code.</b> In Safari or on another signed-in device, ' +
      'open <b>Accounts &rarr; Sign in another device</b> to get one.';
  }
  $('signin-claim').onclick = claimOwner;
  $('sign-out').onclick = signOut;
  $('accounts-done').onclick = function () { closeDialog(); };
  $('manage').onclick = toggleManage;
  $('start').onclick = function () { addFromDialog(true); };
  $('save').onclick = function () { addFromDialog(false); };
  $('cancel').onclick = function () { closeDialog(); };
  $('back').onclick = showLibrary;
  $('play').onclick = retry;
  $('quality').onclick = cycleQuality;
  $('quality-lib').onclick = cycleQuality;
  $('stop').onclick = stop;
  seekButton.onclick = function (event) {
    // OK on the remote or Enter pauses; a click or tap on the bar jumps to that spot.
    if (!event || !event.detail) { togglePause(); return; }
    skipResume();
    var rect = seekButton.getBoundingClientRect();
    var total = duration || video.duration;
    if (!rect.width || !total || !isFinite(total)) { return; }
    seekTo((event.clientX - rect.left) / rect.width * total);
    wake();
  };
  $('history').onclick = openHistory;
  $('history-close').onclick = function () { closeDialog(); };
  $('history-clear').onclick = clearHistory;
  $('history-clear').onblur = function () { if (clearing) { clearing = false; drawHistory(); } };
  $('pause').onclick = togglePause;
  $('mute').onclick = function () {
    var off = !(video.muted || !video.volume);
    autoMuted = false;
    video.muted = off;
    if (!off && !video.volume) { video.volume = 1; }
    wake();
  };
  $('volume').oninput = function () {
    autoMuted = false;
    video.volume = Number($('volume').value);
    video.muted = !video.volume;
    wake();
  };
  // Our own bar replaces the browser's controls (which used to show as a second player underneath).
  video.onclick = function () { if (!isTV && !autoMuted && view === 'watch') { togglePause(); } };   // a muted start's first click turns on sound
  video.ondblclick = function () { if (!isTV && !isIOS) { $('fullscreen').onclick(); } };
  $('skip-back').onclick = function () { seekBy(-10); };
  $('skip-forward').onclick = function () { seekBy(30); };
  $('sample').onclick = function () { sample('/media/sample/index.m3u8'); };
  $('mp4').onclick = function () { sample('/media/sample/sample.mp4'); };
  $('now-playing').onclick = function () {
    if (pending && nowStream && live && nowStream.url === live.url) { showWatch(); }
    else if (lastStatus && lastStatus.stream) { follow(lastStatus); }
  };
  $('fullscreen').onclick = function () {
    if (isIOS && video.webkitEnterFullscreen) { video.webkitEnterFullscreen(); return; }   // iPhone: Apple's player
    var root = document.documentElement;
    if (document.fullscreenElement || document.webkitFullscreenElement) {
      (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    } else {
      var enter = root.requestFullscreen || root.webkitRequestFullscreen;
      if (enter) { enter.call(root); }
    }
  };
  video.controls = false;
  drawControls();
  $('hint').textContent = hint();
  deviceToken = recall('my-streams-device');
  render();
  request('GET', '/api/me', null, function (error, result) {
    if (error || !result || !result.user) { showSignIn(); return; }
    me = result.user;
    $('people').className = me.admin ? 'btn ghost' : 'btn ghost hidden';
    $('signed-in-as').textContent = 'Signed in as ' + me.name + '.';
    offerInstall();
    loadLibrary();
    poll();
    window.setInterval(function () { if (!away()) { poll(); } }, 2000);
    report('page-open', navigator.userAgent);
  });
  // Back in view: catch up at once instead of on the next two-second tick.
  document.addEventListener('visibilitychange', function () { if (me && !document.hidden) { poll(); } });
}());
