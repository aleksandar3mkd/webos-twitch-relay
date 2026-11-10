importScripts('api.js');

// Offered on Twitch pages, and on links to Twitch from any page.
const TWITCH = ['*://*.twitch.tv/*'];
const MENUS = [
  {id: 'save-link', title: 'Save link to My Streams', contexts: ['link'], targetUrlPatterns: TWITCH},
  {id: 'play-link', title: 'Play link on TV', contexts: ['link'], targetUrlPatterns: TWITCH},
  {id: 'save-page', title: 'Save this channel to My Streams', contexts: ['page'], documentUrlPatterns: TWITCH},
  {id: 'play-page', title: 'Play this channel on TV', contexts: ['page'], documentUrlPatterns: TWITCH}
];

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    for (const menu of MENUS) {
      chrome.contextMenus.create(menu);
    }
  });
});

async function flash(text, color, title) {
  await chrome.action.setBadgeBackgroundColor({color});
  await chrome.action.setBadgeText({text});
  await chrome.action.setTitle({title});
  setTimeout(() => {
    chrome.action.setBadgeText({text: ''});
    chrome.action.setTitle({title: 'Send to My Streams'});
  }, 4000);
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const isLink = info.menuItemId.endsWith('-link');
  const url = isLink ? info.linkUrl : info.pageUrl;
  // A bare link has no page title to offer.
  const name = isLink ? '' : nameFromTitle(url, tab && tab.title);
  try {
    if (info.menuItemId.startsWith('play')) {
      const result = await playStream(url, name);
      await flash('▶', '#7c5cff', 'Playing ' + result.stream.name + ' on the TV');
    } else {
      const result = await saveStream(url, name);
      await flash('✓', '#1f9d55', (result.created ? 'Saved ' : 'Already saved: ') + result.stream.name);
    }
  } catch (error) {
    await flash('!', '#d93848', error.message);
  }
});
