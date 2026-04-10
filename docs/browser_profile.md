# Browser Profile Management

Stock Triage uses a persistent Chromium profile (`data/browser/`) for
browser-backed discovery (IDX company profiles, financial report listings,
disclosure pages). The profile stores cookies, local storage, and TLS
session tickets so that subsequent runs can skip Cloudflare challenges that
have already been solved.

## Directory layout

```
data/browser/
  Default/
    Cookies
    Local Storage/
    ...
```

The path is controlled by `browser.profile_dir` in `config.yaml` (default:
`data/browser`).

## When the profile breaks

Chromium profile corruption or Cloudflare fingerprint changes can cause
discovery runs to fail with `BrowserBlocked` or crash on startup. Symptoms:

- `discover` exits immediately with `BrowserBlocked: challenge loop detected`
- Chromium crashes with `Profile too new` or segment-fault on launch
- TLS errors / certificate pinning mismatches after a Chromium upgrade

## Recovery steps

1. **Back up the existing profile** (optional, for forensics):

   ```bash
   mv data/browser data/browser.bak.$(date +%s)
   ```

2. **Create a fresh profile** by running any discovery command:

   ```bash
   python -m stock_triage discover company_universe
   ```

   Playwright will create a new profile directory automatically. You will
   need to solve a Cloudflare challenge interactively on the first run
   (unless running in headless mode with a challenge-solving proxy).

3. **Verify the new profile works**:

   ```bash
   python -m stock_triage canary-run --source idx_disclosures
   ```

   A successful canary run confirms the profile can reach IDX endpoints.

## Upgrading Chromium / Playwright

When you upgrade Playwright (and therefore the bundled Chromium), the
profile format may change. Playwright handles forward-migration
automatically in most cases. If the profile is incompatible:

1. Delete the profile directory (`rm -rf data/browser`).
2. Re-run discovery to recreate it (see step 2 above).
3. The first run will be slower because Cloudflare challenges must be
   re-solved.

## Sharing profiles across machines

Browser profiles are **not portable** across OS versions or Chromium builds.
Do not copy `data/browser/` between machines. Each machine should maintain
its own profile.

## Headless mode considerations

When `browser.headless` is `true` in `config.yaml`, Cloudflare challenges
cannot be solved interactively. Options:

- Run the first discovery pass with `headless: false` to solve the
  challenge, then switch back to headless for subsequent runs.
- Use a challenge-solving proxy (not included in Stock Triage).

## Configuration reference

```yaml
browser:
  headless: false                 # true = no visible window
  profile_dir: data/browser       # persistent Chromium profile
  artifacts_dir: data/artifacts   # screenshots & HAR files
  challenge_timeout_s: 180        # max wait for Cloudflare challenge
  page_settle_ms: 5000            # wait after page load for JS rendering
  channel: ""                     # Chromium channel override (e.g. "chrome")
```
