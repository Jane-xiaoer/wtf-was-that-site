// Bookmark Capture — listen for new Chrome bookmarks and POST to the local receiver.
// Does not depend on the on-disk Bookmarks file being flushed, so it works
// even when Chrome Sync delays the write.

const ENDPOINT = "http://127.0.0.1:7331/hook";

chrome.bookmarks.onCreated.addListener((id, bookmark) => {
  if (!bookmark || !bookmark.url) return;
  // skip chrome://, chrome-extension://, about:, file:, javascript:
  const u = bookmark.url;
  if (!/^https?:\/\//i.test(u)) return;

  const payload = {
    url: u,
    title: bookmark.title || "",
    parentId: bookmark.parentId || null,
    dateAdded: bookmark.dateAdded || Date.now(),
    source: "chrome-extension",
  };

  fetch(ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then((r) => {
      if (r.ok) {
        console.log("[bookmark-capture] →", u);
      } else {
        console.warn("[bookmark-capture] receiver returned", r.status);
      }
    })
    .catch((e) => {
      // receiver might not be running — fail silently
      console.warn("[bookmark-capture] receiver unreachable:", e.message);
    });
});

console.log("[bookmark-capture] background ready, listening for chrome.bookmarks.onCreated");
