#!/bin/sh
# Saves the pages of a running verifair, loaded with scripts/demo.py, as a static demo.
# Run it against an empty database that holds only the demo: every page Alice can see is published.
# Usage: scripts/snapshot-demo.sh <output dir> <URL path the output is served at>
# Example: scripts/snapshot-demo.sh site/demo /verifair/demo
set -eu
out=${1:?output dir}
base=${2:?URL path, for example /verifair/demo}
url=${VERIFAIR_URL:-http://127.0.0.1:8000}

wget --quiet --mirror --page-requisites --convert-links --adjust-extension \
  --no-host-directories --restrict-file-names=windows --directory-prefix "$out" \
  --timeout 10 --tries 2 \
  --header "Cookie: vf_id=alice-demo; vf_name=Alice" \
  --reject-regex '/(docs|redoc|openapi\.json|access|login|logout|auth|api|name)([/?]|$)|/delete$' \
  "$url/"

# The app links its icons and Mermaid from JavaScript and SVG, which wget does not follow
cp -R verifair/static/. "$out/static/"

export BASE="$base" ORIGIN="$url"
# The demo counts visits like the docs site; a self-hosted verifair never does
export HEAD='<script data-goatcounter="https://verifair.goatcounter.com/count" async src="https://gc.zgo.at/count.v4.js" crossorigin="anonymous" integrity="sha384-nRw6qfbWyJha9LhsOtSb2YJDyZdKvvCFh0fJYlkquSFjUxp9FVNugbfy8q1jdxI+"></script><script>try { const t = localStorage.getItem("vf-demo-theme"); if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; } catch {}</script>'
export NOTE='<p class="banner" role="status">Static demo: commenting, signing and verdicts are off. <a href="https://github.com/romanzdk/verifair#quickstart">Run verifair</a> to try them. Visits are counted with GoatCounter, without cookies.</p>'
export OFF='<script>
const pressTheme = (form, theme) => { for (const b of form.querySelectorAll("button")) b.setAttribute("aria-pressed", b.value === theme); };
for (const f of document.querySelectorAll(".theme-form")) pressTheme(f, document.documentElement.dataset.theme || "system");
for (const e of ["submit", "htmx:confirm", "click"]) addEventListener(e, (ev) => {
  if (e === "click" && !ev.target.closest?.("a[href=\"#demo-off\"]")) return;
  ev.preventDefault(); ev.stopPropagation();
  const theme = ev.target.matches?.(".theme-form") && ev.submitter?.value;
  if (!theme) return alert("This is a static demo, so this is turned off. Run verifair to try it.");
  if (theme === "system") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = theme;
  pressTheme(ev.target, theme);
  try { localStorage.setItem("vf-demo-theme", theme); } catch {}
}, true);
</script>'
find "$out" -name '*.html' -exec perl -pi -e '
  s{"/static/}{"$ENV{BASE}/static/}g;
  s{\Q$ENV{ORIGIN}\E/docs\b}{https://romanzdk.github.io/verifair/api/}g;
  s{(href|action)="\Q$ENV{ORIGIN}\E[^"]*"}{$1="#demo-off"}g;
  s{</head>}{$ENV{HEAD}</head>};
  s{(<main[^>]*>)}{$1$ENV{NOTE}};
  s{</body>}{$ENV{OFF}</body>};
' {} +
perl -pi -e 's{"/static/}{"$ENV{BASE}/static/}g' "$out/static/app.js"

# Fail instead of publishing a demo with missing reviews, live-looking forms or broken assets
for id in $(sed -n 's/.*"id": "\(demo-[a-z-]*\)".*/\1/p' scripts/demo.py | sort -u); do
  ls "$out/r/$id"*.html > /dev/null
done
bad=$(
  find "$out" -name '*.html' -exec grep -L 'Static demo:' {} + || true
  find "$out" -name '*.html' -exec grep -L 'vf-demo-theme' {} + || true
  grep -rlF -e '"/static/' -e "$url" --include '*.html' --include '*.js' --include '*.css' "$out" || true
)
if [ -n "$bad" ]; then
  echo "snapshot-demo: banner or script missing, or links to the app left in:" "$bad" >&2
  exit 1
fi
