# Dashboard golden screenshots

`golden/` holds the committed baseline for `tools/visual/run.sh` (run by
`check.sh`): one PNG per dashboard view, rendered over fixed fixture data with
a frozen clock. Captures are NOT committed — they go to `logs/visual/`
(gitignored). When a change alters the look on purpose, look at the
golden|now|diff panels in `logs/visual/check-diff/compare/`, then re-bless in
the same diff with `tools/visual/run.sh --update`. See docs/visual-testing.md.
