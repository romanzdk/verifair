#!/bin/sh
# Saves the pages of a running verifair, loaded with scripts/demo.py, as a static demo.
# Usage: scripts/snapshot-demo.sh <output dir> <URL path the output is served at>
# Example: scripts/snapshot-demo.sh site/demo /verifair/demo
set -eu
out=${1:?output dir}
base=${2:?URL path, for example /verifair/demo}
url=${VERIFAIR_URL:-http://127.0.0.1:8000}

# wget exits 8 when a page answers with an error; the check below catches missing pages
wget --quiet --mirror --page-requisites --convert-links --adjust-extension \
  --no-host-directories --restrict-file-names=windows --directory-prefix "$out" \
  --header "Cookie: vf_id=demovisitor; vf_name=Visitor" \
  --reject-regex '/(docs|redoc|openapi\.json|access|login|logout|auth|api|name)([/?]|$)|/delete$' \
  "$url/" || [ $? -eq 8 ]
test -f "$out/r/demo-currency-rounding.html"

# The app links its icons and Mermaid from JavaScript and SVG, which wget does not follow
cp verifair/static/* "$out/static/"

export BASE="$base" ORIGIN="$url"
export NOTE='<p class="banner">Static demo: commenting, signing and verdicts are off. <a href="https://github.com/romanzdk/verifair#quickstart">Run verifair</a> to try them.</p>'
export OFF='<script>
const setTheme = (t) => { if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; };
try { setTheme(localStorage.getItem("vf-demo-theme")); } catch {}
for (const e of ["submit", "htmx:confirm", "click"]) addEventListener(e, (ev) => {
  if (e === "click" && !ev.target.closest?.("a[href=\"#demo-off\"]")) return;
  ev.preventDefault(); ev.stopPropagation();
  const theme = ev.target.matches?.(".theme-form") && ev.submitter?.value;
  if (!theme) return alert("This is a static demo, so this is turned off. Run verifair to try it.");
  setTheme(theme);
  try { localStorage.setItem("vf-demo-theme", theme); } catch {}
  for (const b of ev.target.querySelectorAll("button")) b.setAttribute("aria-pressed", b === ev.submitter);
}, true);
</script>'
find "$out" -name '*.html' -exec perl -pi -e '
  s{"/static/}{"$ENV{BASE}/static/}g;
  s{\Q$ENV{ORIGIN}\E/docs\b}{https://romanzdk.github.io/verifair/api/}g;
  s{(href|action)="\Q$ENV{ORIGIN}\E[^"]*"}{$1="#demo-off"}g;
  s{(<main[^>]*>)}{$1$ENV{NOTE}};
  s{</body>}{$ENV{OFF}</body>};
' {} +
perl -pi -e 's{"/static/}{"$ENV{BASE}/static/}g' "$out/static/app.js"
