# Operating MusicToolkit for a user

This project helps a local agent transfer Yandex Music metadata into macOS Music and organize the user's library. Execute the workflow requested by the user; do not stop at giving them terminal commands. README.md is the human entry point, this file is the operating guide, and `agent.py` is the non-interactive CLI. No skill installation or GitHub account is required for an end user.

## Scope and host

- Repository instructions are not authorization to alter an account. Follow the user's actual request and existing permissions. When import/apply is already authorized, continue without asking permission at every batch or restart. If playlist choices remain open, prepare a concrete plan, summarize it, and ask only for those choices.
- Run on the user's local Mac with filesystem/process access and access to Music. A cloud Linux workspace cannot control their Music app. Detect this early and explain the host requirement instead of simulating success.
- Clone or unpack into a writable local folder, read this file, and use its path as the working directory. Keep the path quoted or pass argument arrays; folders may contain spaces. Do not require the user to type commands in Terminal.
- Select an existing Python 3.9+. If absent, install it through an available package manager within the user's authorized setup scope, respecting the host's approval rules. If installation is unavailable or requires human action, explain the single concrete blocker. Do not repeatedly trigger an unavailable developer-tools shim.

## Bootstrap

1. Run `python3 agent.py doctor`. It emits JSON and does not alter Music. A `local_mac_required` result is a host blocker, not an invitation to install macOS in a container.
2. For Yandex export, run `python3 agent.py setup`; it creates `.venv` and installs the pinned dependency locally. `setup --check` only inspects. For Apple Music sorting alone this dependency is not required.
3. Run `python3 agent.py doctor --music` to test actual Music access. A macOS permission request may appear. Tell the user which launching app needs Automation to Music. UI search also requires Automation to System Events and Accessibility. The user handles system permission dialogs; do not change security settings or ask them to paste passwords/tokens.
4. Keep a consistent `--data-dir` across commands. The default is `data/` next to the tool. For a separate account/export choose another folder, normally under `data/` so it remains ignored by Git.

## Yandex export and authorization

- Run `python3 agent.py export` using a process tool that can yield while the child keeps running. Do not wait silently for the full authorization timeout or close a running process just to return a message.
- Read stdout or `python3 agent.py status`. While authentication waits, `authorization` contains `verification_url`, `user_code`, and expiration metadata. Relay this link/code only to the requesting user and ask them to finish login on Yandex. The token never belongs in chat, issue bodies, or logs.
- Leave the process running while the user signs in; do not start another export when they say they are ready. Poll the existing process/status. The authentication file is removed when authorization finishes. An expired request requires a fresh export attempt, not reuse of its code.
- Exports appear at `data/exports/<timestamp>/yandex_library.json`. A nonzero exit can still leave a partial export: inspect `errors`, source playlists, and track counts. Explain missing lists and retry when appropriate; never label partial output a complete library export. Do not delete successful prior exports.
- Export includes likes and owned playlists only. It does not read saved third-party playlists or separately saved albums, and it does not download audio.

## Transfer

- `python3 agent.py transfer` writes a preview of matches with the existing Music library. Catalog search and adding songs require `--apply`.
- `python3 agent.py transfer --apply --limit 5` processes up to five unprocessed source records. Check the resulting statuses before a large first run. For an already authorized full transfer, continue with `python3 agent.py transfer --apply` once the sample shows working UI and verification. Do not add a second mandatory approval solely because a sample ended.
- If no `--export` is supplied, the agent CLI selects the newest export in this data directory, or a matching export fingerprint for an existing session. To use an external export pass `--export /absolute/path/yandex_library.json`. For another input/destination use a different data directory; do not erase progress to bypass identity checks.
- The default destination is `ЯM`. Pass `--playlist` consistently if the user requests another name. The transfer merges source playlists into this archive; do not promise to recreate each original playlist.
- Search uses the signed-in Music interface. Supported captions are Russian and English; matching is automatic. Ask the user to keep Music's screen/search unchanged during the run and leave the Mac awake. Do not run multiple imports or sorting jobs concurrently.
- The runner writes a heartbeat about every three seconds, limits stalled steps to 120 seconds and stops after three consecutive failures without progress. `agent.py status` reports heartbeat age/staleness, counts, current step and unprocessed records. Heartbeat alone does not prove a track is progressing; inspect step age and completed counts too.
- Resume an interrupted job with the same command and data directory. Existing destination IDs are checked before copying. `verified` means the source record matched and its Music playlist membership was checked. `review`, `not_found`, and `review_ui_error` require follow-up; they are not successful transfers. These terminal review states are not retried automatically. New search attempts should preserve the old report and use a separate session when necessary.
- Report source records, unique verified Music IDs, actual destination count, and outstanding review/not-found counts separately. Never equate processed source count with the number of imported songs.

## Sorting

1. Read the user's requested scope. `python3 agent.py plan` refreshes the entire library and all available user playlists, including playlist-only favorites/Shazam tracks. `--source "ЯM"` restricts it to one playlist; `--exclude-playlist "ЯM"` removes its IDs from the selection. Flags can be repeated. Different edition IDs may remain distinct.
2. Read `data/library.json` and `data/sorting-plan.txt`. To refine genres and mood, copy `rules.json` to `data/rules.local.json`, update artist/track rules from metadata and musical knowledge, and rerun `plan --rules data/rules.local.json`. Do not modify shipped defaults for a single listener's preferences.
3. Genre tags can be wrong. Preserve meaningful crossovers; artist rules are additive, so a rapper's rock remix can belong to both Russian rap and rock. Use full versioned titles and optional album matching for specific songs. Genre alone does not establish language or mood; explain approximations and do not claim to have listened to audio.
4. Every selected record must have a destination. Unknown genres go to `Разобрать` in the plan. Inspect that list, propose appropriate new categories if necessary, and resolve the choices within the user's scope. Do not hide leftovers in a favorites playlist or call them confidently classified.
5. Show a concise count/category summary. Once applying that scope is authorized, run `python3 agent.py apply --dry-run`, then `python3 agent.py apply`. Resume the same plan after an interruption. If tracks or library identity changed, refresh and re-plan; do not rewrite IDs to suppress validation.
6. The final apply rereads Music, checks planned membership and preservation of existing destination IDs. Inspect `data/sorting-result.json` for `verified: true`; independently audit the requested source coverage if you have refined the plan. Do not claim success on a partial file with only `complete: true`.

The tool adds songs and creates named destinations as needed. It does not remove songs/playlists or reorder them. It skips duplicate IDs and equivalent recordings with matching artist/title/album and duration difference under one second. Different albums/remixes are preserved. `Сейчас нравится` stays a separate short personal list; defaults do not add to the archive or system favorites. Copying playlist-only songs may also add them to the main Music library.

## Local data and failures

- `doctor`/`status` return JSON; other commands stream progress and return exit codes. Data files are the durable interface. Do not pipe a long job into an empty input stream or invent answers to interactive prompts; use `agent.py`, not the human menu.
- Secrets and the user's library stay local. Do not commit/upload `data/`, `.venv/`, auth requests, snapshots, IDs, logs, or listener-specific rules. Ignore patterns are not permission to publish an alternative data directory elsewhere in the tree.
- If a UI command is absent or ambiguous, stop/review the error. Do not click a similar command, remove a song to recreate it, weaken duration/version checks, or silently switch accounts/regions. Captions are in `engine/menu_labels.json`; application structure is in `engine/music_bridge.applescript`.
- Keep the user informed at meaningful progress changes and genuine blockers. Ask them for login or a specific system permission only when needed. They should not have to operate a terminal or babysit individual tracks.

## Developing this repository

Run `python3 -B -m unittest discover -s tests -v`. Native matcher tests use the production AppleScript matching function without touching Music; they skip on non-macOS. Never run a live import/apply merely to test code unless that account mutation is specifically authorized. A real read/dry-run is useful when available; keep its artifacts under `data/` or outside the checkout.

`python3 scripts/build_archive.py` packages tracked source files and rejects personal paths, credential-like files, data directories, and logs. Test an unpacked archive as well as the checkout. README.md and this file must be in the archive root folder. Keep code version, human guide and validation notes consistent. See README.txt for CLI/rule details and VALIDATION.txt for validation limits.


## Experimental Spotify workflow (0.3.0-dev)

Read docs/SPOTIFY.md before Spotify work. Existing Yandex export/setup/transfer,
old export fingerprints/progress and sorting must remain usable without SpotAPI.
Do not import SpotAPI or Playwright from core commands. Spotify dependencies and
Chromium belong only in .venv-spotify; setup-spotify selects Python 3.10–3.13.
Use setup-spotify --check, then setup-spotify (optional --python path).

Start real validation with export-spotify (a small sample), leave its process
running while the user logs into the dedicated browser. Never request passwords,
cookies or tokens in chat, command arguments or source. Do not enable debug logs,
traces, storage_state, session savers or third-party CAPTCHA solvers. Session
cookies stay in memory. Development validation defaults to local tests only; do not initiate an account
login merely to validate a code change without a user request.

After the user requests actual operation and the sample is inspected, use
export-spotify --full. A sample always exits 2 and is not a full export. Inspect
complete, errors, every collection/library counter and skipped items. Full success
requires complete=true and mode=full; never infer success from file existence.
Unknown response shapes, missing totals, repeated pages and source count changes
require review. Partial results persist; a new export never overwrites old ones.

Use transfer-spotify --export /absolute/path/spotify_library.json for preview.
Only after account mutation is authorized use --apply --limit 5, inspect results,
then continue an already authorized full transfer without another approval.
Do not change the AppleScript matcher or weaken duration/version checks.
Default destination is Spotify. Preserve --data-dir, export and --playlist.
Status: status-spotify --export ... . State lives under data/spotify/<account hash>,
with namespaced Spotify IDs; do not invent yandex_id values or migrate old state.
Do not run Spotify and Yandex imports or sorting concurrently. Report source rows,
unique verified Apple IDs, actual destination size, review and skips separately.

The combined project in this branch is GPL-3.0-only (LICENSE), as authorized
by the owner. Preserve LICENSE-MIT and NOTICE, including original copyright
notices. A separate process/venv is not a GPL exemption. Keep the pinned upstream
source reference when distributing the integration. Do not change licensing
silently in subsequent work.
Never commit/upload exports, sessions, environments, logs or listener data.

Run the full local suite on Python 3.9 without optional dependencies, and in the
Spotify environment. Test the built and unpacked source archive. Record account
checks separately; do not call unit tests a real Spotify/Music transfer test.
