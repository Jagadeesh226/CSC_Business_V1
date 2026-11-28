# Review: Phase 6.1 frontend foundation

- **Branch reviewed:** `feat/frontend-foundation` (770ccb9, fixes in adb9c0a and 94ad323)
- **Builder:** frontend agent (Sonnet 5.5)
- **Reviewer:** independent review agent (Opus 5.5), read-only, running mutations on a copy of the code
- **Integration check:** Sonnet 5.5, in a real browser against the real backend; the final fix was confirmed in a real browser by the orchestrator
- **Date:** 2026-11-28

## Scope

`frontend/` only. Stack: React 19, Vite, TypeScript (strict), TanStack Query, React Hook Form with Zod, and react-router 7.

- **API types:** regenerated from `docs/contracts/openapi.json`. The output is byte-identical to the generator's.
- **API client:** a typed fetch wrapper.
  - It sends cookies and attaches `X-CSRF-Token` to every request that changes data.
  - Path parameters are typed and must be present.
  - Every error status has a plain message.
  - A Vite `/api` proxy is included.
- **Auth:**
  - Sign-in, and the forced password change, which cannot be bypassed in the app (the backend enforces it as well).
  - Admin-only routes.
  - When a session ends, the app goes to sign-in and returns afterwards to a safe path within the app.
  - Signing out keeps a warning and a retry button if the server cannot be reached.
- **App shell:** header, user menu, and navigation for each role. Unbuilt screens show placeholders. It works at phone width.
- **Dashboard:** built from `/dashboard` and `/work-queue`.
- **Ctrl/Cmd+K command palette:** searches customers, jobs and vehicles.
- **Design settings:** design tokens in `src/styles/tokens.css`.
- **Tests:** Vitest with Testing Library, 81 tests. These are dev-only dependencies: vitest, jsdom, @testing-library/react, @testing-library/user-event and @testing-library/dom.

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| F1 | Medium | Signing out counted any error as success, so the server session could stay valid. On a shared counter PC, the next person to reload would be signed in as the previous employee. | **Fixed.** Only a 401 counts as already signed out. Any other error clears local data, warns, and offers a retry. |
| F2 | Medium | At phone width (360px) the header overflowed and the user menu was off-screen. | **Fixed.** The header wraps and the labels shorten. Checked in a real browser at 320, 360 and 1024px with no sideways scroll. |
| F3 | Low | The return path after sign-in accepted `/\evil.example`. | **Fixed.** Backslashes are rejected, and the path must resolve to the same origin. Unit tests added. |
| F4 | Low | When a session ended, the cached data stayed, and the next user could land on the previous user's page. | **Fixed.** The cache is cleared, and a different user lands on the dashboard. |
| F5 | Low | A stale CSRF token showed "You don't have access". | **Fixed.** It now shows "Your session needs refreshing" and is handled as an ended session. |
| F6 | Low | Behind the proxy, a backend that was down showed a generic error. | **Fixed.** A 502, 503 or 504 without JSON shows "Can't reach the server". |
| F7 | Low | The palette did not scroll the active option into view, and the page behind it stayed interactive. | **Fixed.** The active option scrolls into view, and the background is `inert`. |
| F8 | Low | The user menu announced itself as a menu, and Escape lost focus. | **Fixed.** It uses the disclosure pattern, and Escape returns focus to the toggle. |
| F9 | Low | A missing path parameter produced a URL ending in `/`. | **Fixed.** Parameters are typed from the contract, and a missing one throws a clear error. |
| F10 | Low | Test gaps: 15 of 33 mutations survived. `scripts/check.sh` does not run `npm test`. | **Fixed:** tests added for each gap, taking the count from 27 to 81. **For the owner:** add `npm test` to `scripts/check.sh`, which is an owner-owned file. |
| B1 | Low | Found in the browser check: in development mode (React StrictMode), Escape in the palette lost focus. Production builds were fine. | **Fixed.** The opener is captured before the palette opens, and focus is restored once the background is no longer inert. StrictMode tests were added, and the fix was confirmed in a real browser: focus returns to the Search button. |

Note: jsdom does not implement `inert`, so the StrictMode tests do not reproduce B1 exactly. On the old code, 2 of the 4 new tests fail. The real-browser confirmation covers the rest.

## Checked and found sound

- **CSRF header:** sent on every request that changes data, and never on reads.
- **No unsafe content:** no `dangerouslySetInnerHTML`, nothing in `localStorage` or `sessionStorage`, no `console` logging, and no secrets in environment variables.
- **Masking:** the palette shows the masked phone number only, and never chassis or engine numbers.
- **Cached data:** cleared on sign-in, sign-out and session end.
- **Error messages:** match the backend's real responses, including 422 detail arrays and the login 401.
- **Dates:** in Indian format and time, including the case where UTC is still the previous day.
- **Colours:** every text colour pair is at least 4.5:1, the readability standard.
- **Keyboard and focus:** a skip link, a visible focus ring on every control, and 44px click targets.
- **Contrast at phone width:** no sideways scrolling at 320, 360 and 1024px.

## Integration check (real browser, real backend)

9 of 10 checks passed. The failure was B1, which is now fixed.

1. **Sign-in:** a wrong password shows a clear message, and the session cookie is HttpOnly.
2. **Dashboard:** the counts match the API, and dates and times are in Indian time.
3. **Roles:** the admin and employee menus are correct. An employee is forced to change their password first, a typed URL does not get around it, and admin pages show "Admins only".
4. **Search:** grouped results with a masked phone number. Arrow keys and Enter navigate.
5. **Session end:** the app goes to sign-in, then back to the same page.
6. **Sign-out with the server down:** the warning shows, and the retry works once the server is back.
7. **Browsing with the server down:** "Can't reach the server" with Try again.
8. **Phone width (360px):** no sideways scroll, and the user menu is fully visible.
9. **Keyboard only:** the skip link works, and focus is always visible.
10. **Browser console:** no errors.

## Checks run

- `bash scripts/check.sh frontend`: npm ci, TypeScript build and ESLint all PASS.
- `npm test`: 9 files, **81 passed**.
- No contract change. No files outside `frontend/` changed.
