# Manual browser check

`isolation-probe.html` + `data.json` prove the preview sandbox in a real browser. Create a project in LocalBolt, add both files (keep the names `index.html` and `data.json`, paste the probe as `index.html`), and open the preview.

Expected (all green, run 2026-10-29 in headless Chromium):

    window.origin:  null  (opaque, not 127.0.0.1:8878)
    parent.document / document.cookie / top navigation: blocked (SecurityError)
    localStorage: in-memory shim
    fetch to the UI origin and to the internet: blocked by CSP connect-src
    remote image: blocked by CSP img-src
    fetch own file: ok
