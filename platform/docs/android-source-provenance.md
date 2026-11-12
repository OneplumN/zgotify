# Android Source Provenance

Verified on 2026-11-12. The source reuse in this increment is deliberately limited
to the released message presentation layer. It is not a second copy of the legacy
authentication, deletion or background-service implementation.

## Published Baseline

- Repository: https://github.com/OneplumN/zgotify
- Published prerelease: `zgotify`, asset ID `431878809`, `zgotify.apk`.
- APK size: 8,484,095 bytes.
- APK SHA-256: `1914d29ec2e7ddcf1043898db91cf1693858feee7ab3a8489485e7bae3131b6c`.
- Public tag resolves to `41bcdd1850150f598437b98cca801f2645a7cbaf`.
- Likely build source: `b1433a5e8cd2b2fc9bea23a5da81cade2f8b6082`, development
  build `26562051424`. Its source tree equals local `4e6695aa9afd9da39c6042e5b3e2b30140474126`.
- The APK was uploaded after that build, but the workflow's publish step was
  skipped. Exact source-to-APK provenance is not established by timing alone.

The four presentation files below have identical blobs at the public tag and
`b1433a5e`. APK bytecode/layout inspection is consistent with these sources.

| Original file | Git blob prefix | Personal adaptation |
| --- | --- | --- |
| `messages/ListMessageAdapter.kt` | `1b4dc6236970` | `ListMessageAdapter.kt`, personal callbacks, state and recycling |
| `MarkwonFactory.kt` | `566d3feff1bf` | `MarkwonFactory.kt`, message renderer only |
| `res/layout/message_item.xml` | `4f887000fe1c` | Adapted message row, no global delete button |
| `res/layout/message_item_compact.xml` | `28d599299306` | Inspected, not imported in this increment |

Also reused: `drawable/gotify.png`, `drawable/ic_alarm.xml`. The MIT license is
retained in `android/app/src/main/assets/licenses/gotify-MIT.txt`, packaged in the
APK and accessible through the app's open-source notice.

## Security and Behavior Changes

- Plain text remains plain. Markdown requires exact `content_type=text/markdown`.
- Markdown uses the original Core/Strikethrough/Table plugins, without HTML or
  network image plugins. Remote images and source application icons are deferred
  until authenticated media handling is designed. The bundled fallback icon is
  reset on every row bind; another recycled message's image cannot linger.
- Rendering is bounded to 16,384 characters and 128 ASCII punctuation characters
  before parsing, then 2,000 nodes / 32 levels via an iterative AST check. Messages
  exceeding those conservative limits retain their full body as literal text.
  This prevents deeply nested imported Markdown from crashing the inbox on reopen.
- Titles are always plain text. Link activation only permits HTTP/HTTPS; no
  arbitrary intent, file, content or JavaScript scheme is launched.
- The adapter only emits personal-action callbacks. The companion API owns
  person-scoped state; there is no Gotify message DELETE in this application.
- Existing Room state, pending writes and session isolation are retained.

The link policy uses Markwon's configuration hook, documented at
https://noties.io/Markwon/docs/v4/core/configuration.html#linkresolver .

## Installation Boundary

The published legacy APK is `com.github.gotify.dev`, version code 35, version
2.10.0, debug-signed. Its verified signing certificate SHA-256 is
`0ba4fc3fcf2ab3a7682d274e055ccc92c7000be4c78d91d01fb3ab46f1709492`.

The personal internal app remains `com.zgotify.app.internal` and does not replace
that APK. No private signing key was inspected or copied. A future legacy in-place
upgrade requires the appropriate signing identity, package ID and explicit data
migration; this increment makes no such compatibility claim.
