# Releasing

Owner: `<owner: to be named>`. Deputy: `<deputy: to be named>`.

## Versions

- `v1.x.y` tags are immutable. Protect them with a tag ruleset on `v*.*.*` that blocks updates and deletions. A bad release
  stays tagged; the fix is a new `v1.x.(y+1)`.
- `v1` is the floating major tag customers use. It moves only to a `v1.x.y` that passed the canary. It is the one tag the
  ruleset leaves movable, together with `v1-canary`.
- One SDK pin per release: `release.json` `sdk.version`, the version the release is tested with and installs when a project
  has none. Projects may hold any SDK of the pin's `major.minor` at or above the pin. Changing the pin is a release. A new
  minor turns every lockfile on the old minor red (exit 65), so it ships with the plugin that writes the new minor.
  `managed_rev` moves with the plugin's template revision.

## Cut a release

1. Merge the change into `main` through a reviewed PR with the fixture workflow green.
2. Tag `main` as the immutable `v1.x.y`: `git tag -s v1.x.y <sha> && git push origin v1.x.y`.
3. Move `v1-canary` to the same commit (`git tag -f v1-canary <sha> && git push -f origin v1-canary`). Our dogfood repositories
   reference `plurai-ai/simulate-action@v1-canary`.
4. Wait for the canary to pass on the dogfood repositories: at least one pull request run (smoke), one run on `push` to main
   (full) and one scheduled or dispatched run, each with the expected exit code and a summary that reads correctly. A renderer
   or exit-code bug here would otherwise reach every customer at once. The range starts at the pin, so the canary run
   with the pin covers the lowest SDK a project may hold. The action sends only flags that `X.Y.0` of the pin's minor accepts,
   so a later pin of the same minor stays compatible with SDKs held under an earlier one.
5. Move `v1`: `git tag -f v1 <sha> && git push -f origin v1`.
6. Record the release: the tag, the SDK pin, the canary runs and the date, in the release notes.

Creating a GitHub release or a tag is a human step; nothing in this repository does it.

## Revert

When a release misbehaves after `v1` moved:

1. Decide: the owner (or deputy) calls it. Do not wait for a fix.
2. Move `v1` back to the last good immutable tag: `git tag -f v1 v1.<x>.<y-good> && git push -f origin v1`. Do the same for
   `v1-canary`.
3. Leave the bad `v1.x.y` tag in place. Never delete or retag an immutable tag.
4. Tell the dogfood repositories' owners and note the bad tag, the cause and the revert time in the release notes.
5. Fix forward as `v1.x.(y+1)` and go through the canary again.

A revert changes what `@v1` resolves to on the next job start; runs already going keep the version they started with.
