# Privacy

This page is about this website, romanzdk.github.io/verifair, and its static demo. It does not cover a verifair you run yourself, which counts no visits.

The site has no accounts and no forms, and it sets no cookies.

## Who runs it

Roman Žydyk runs this site. Contact details are on [romanzdk.com](https://romanzdk.com/#contact), or open an issue in the [GitHub repository](https://github.com/romanzdk/verifair/issues).

## Other services your browser talks to

| Service | When | What it gets |
|---|---|---|
| [GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages#data-collection) | Every page | GitHub hosts the site and logs the IP address of every visitor for security. |
| [GoatCounter](https://www.goatcounter.com/help/sessions) (gc.zgo.at) | Every page, docs and demo | Counts visits. The script sends the page address and title, the page you came from, and your screen size. GoatCounter uses your IP address and browser name only in memory, for up to 8 hours, to tell one visit from many page loads. Its documentation says it does not store them and keeps totals, not a record per visitor. |
| api.github.com | Docs pages | The header shows the stars and forks of the repository. Your browser asks GitHub for them and keeps the answer until you close the tab. |
| cdn.jsdelivr.net | Docs pages with a diagram | Your browser loads the Mermaid library that draws the diagrams. The demo has its own copy. |
| img.shields.io and github.com | Home page | The badge images at the top of the page. |

Each of these services sees your IP address, as any server you connect to does.

## What stays in your browser

- Your light or dark theme choice, in local storage.
- The GoatCounter script reads the `skipgc` key from local storage. It is set only when you open a page with `#toggle-goatcounter` at the end of the address, and it stops counting your visits in that browser.
