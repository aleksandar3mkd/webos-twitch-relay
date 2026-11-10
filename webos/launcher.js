/* Hosted webOS application entry point; ES5 for webOS 3.5. */
(function () {
  var address = document.getElementById('address');
  var status = document.getElementById('status');
  var connect = document.getElementById('connect');
  var edit = document.getElementById('edit');
  var timer = null;
  var request = null;
  var attempt = 0;
  // The address this TV used last, else the one built into the app (tools/build_tv_app.py --server).
  var builtIn = typeof MY_STREAMS_SERVER === 'string' ? MY_STREAMS_SERVER : '';
  try { address.value = localStorage.getItem('my-streams-helper') || builtIn; } catch (e) { address.value = builtIn; }
  function cancel() {
    attempt++;
    if (timer) { window.clearTimeout(timer); timer = null; }
    if (request) { request.abort(); request = null; }
    connect.disabled = false;
  }
  function openPlayer() {
    cancel();
    var currentAttempt = attempt;
    var base = address.value.replace(/^\s+|\s+$/g, '').replace(/\/+$/, '');
    if (!/^http:\/\/[a-zA-Z0-9.-]+(?::[0-9]{1,5})?$/.test(base)) {
      status.textContent = 'Enter the address of your My Streams server, for example http://192.168.1.50:8765';
      address.focus(); return;
    }
    status.textContent = 'Connecting to your playback server...';
    connect.disabled = true;
    var xhr = new XMLHttpRequest();
    request = xhr;
    xhr.open('GET', base + '/api/health', true);
    xhr.timeout = 6000;
    function failed() {
      if (attempt !== currentAttempt) { return; }
      request = null; connect.disabled = false;
      status.textContent = 'Cannot reach the playback server. Check the helper and address, then choose Open player.';
      connect.focus();
    }
    xhr.onerror = xhr.ontimeout = failed;
    xhr.onload = function () {
      if (attempt !== currentAttempt) { return; }
      var health;
      try { health = JSON.parse(xhr.responseText); } catch (e) { failed(); return; }
      if (xhr.status !== 200 || health.app !== 'my-streams-helper') { failed(); return; }
      try { localStorage.setItem('my-streams-helper', base); } catch (e) {}
      request = null;
      // This navigation stays inside this installed webOS app, as in LG's hosted-app template.
      window.location.href = base + '/?tvapp=1';
    };
    xhr.send();
  }
  connect.onclick = openPlayer;
  edit.onclick = function () { cancel(); status.textContent = 'Update the address, then choose Open player.'; address.focus(); };
  address.onfocus = function () { cancel(); status.textContent = 'Choose Open player when the address is ready.'; };
  address.onkeydown = function (event) { if (event.keyCode === 13) { openPlayer(); } };
  document.addEventListener('keydown', function (event) {
    var target = event.target;
    if (target === address) { return; }
    if (event.keyCode === 37 || event.keyCode === 39) {
      event.preventDefault(); (document.activeElement === connect ? edit : connect).focus();
    } else if (event.keyCode === 38) { event.preventDefault(); address.focus(); }
  });
  if (address.value) {
    status.textContent = 'Opening your player in 3 seconds...';
    connect.focus();
    timer = window.setTimeout(openPlayer, 3000);
  } else {
    address.focus();   // (focusing writes its own hint; the first-time one goes after it)
    status.textContent = 'Enter the address of your My Streams server, then choose Open player.';
  }
}());
