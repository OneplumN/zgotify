# Released Android UI Integration

## Boundary

Reuse released zgotify Android source incrementally, while retaining the independent
personal identity API and tested offline state engine. Gotify Server stays at 2.9.1.
This is an internal build, not a production rollout or a stable release.

The legacy checkout and its dirty multi-account worktree remain untouched. Android
package `com.zgotify.app.internal` remains separate from the legacy installation.
No old Gotify tokens, global deletion endpoints or WebSocket services are carried
into personal mode. The existing independent session/cache format is migrated in place.

## This Increment

- [x] Inspect published release, tag and existing independent Android implementation.
- [x] Record exact reused source provenance and licensing.
- [x] Adapt released message row/Markdown rendering to personal state actions.
- [x] Add safe content-type metadata and non-destructive offline cache migration.
- [x] Replace the prototype list with RecyclerView, pull-to-refresh and rendered details.
- [x] Run unit, build/lint and emulator UI acceptance, including narrow layouts.
- [x] Review the intended source and prepare the existing draft GitHub PR synchronization.

Publication and CI completion are recorded in the draft PR after the branch is
updated; preparing this source snapshot does not imply that CI has run.

## Deferred

Legacy settings/log export, feed icon metadata, vendor push SDKs, background delivery,
physical phones, four-node acceptance and migration from the legacy APK remain separate
increments. Reusing UI does not make background delivery or deployment complete.
