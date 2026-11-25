# Review: Phase 4.3 vehicles Excel export

- **Branch reviewed:** `feat/backend-vehicle-export` (062752c, fixes in a81a2d5)
- **Reviewer:** independent review agent (Opus 5.5), read-only, probing a copy of the code
- **Integration check:** Sonnet 5.5, over real HTTP
- **Date:** 2026-11-25

## Scope

`GET /reports/vehicles.xlsx` exports one row per vehicle.

- **Columns:**
  - Registration, category, customer and owner name.
  - Mobile, chassis and engine numbers, all masked.
  - The responsible employee.
  - Each of the five validity dates with days left.
  - Challan status and whether the vehicle is active.
  - An "Open WhatsApp" link.
- **Filters:** `due_within_days`, `include_inactive`, `customer_id` and `include_whatsapp`.
- **Access:** employees get only the vehicles they may see.
- **Audit:** every export is recorded.
- **Limit:** 20,000 rows at most.
- **Formula injection:** cells are protected against it.
- **New dependency:** openpyxl 3.1.5, plus et-xmlfile.

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| H1 | High | One record containing a control character (e.g. `\x01`, `\x0b` from a paste or an OCR reading) made every export fail with 500, for admins too. | **Fixed.** Characters a spreadsheet can't hold are removed. Covered by a test. |
| M1 | Medium | The masked mobile column is undone by the WhatsApp hyperlink, which carries the full number. | **Kept as the owner asked.** `include_whatsapp=false` leaves the links out, and the choice is audited. |
| M2 | Medium | At the 20,000-row cap the workbook took about 15 s and about 163 MB. | **Fixed.** The workbook is now built in write-only (streaming) mode, about 6× faster. |
| L1 | Low | The masked mobile column could show a different number from the one the link used. | **Fixed.** It now shows the number the link uses. |
| L2 | Low | The message's 30-day window ignored a wider `due_within_days`. | **Fixed.** |
| L3 | Low | The leading apostrophe is visible in Excel. | **Kept on purpose.** It protects against formula injection if the file is re-saved as CSV. |
| L4 | Low | The footer sat inside the data rows. | **Fixed.** It moved to an "About" sheet. |
| L5 | Low | An export refused for being over the row cap isn't audited. | **Recorded.** |
| L6 | Low | The 422 response documented only one shape. | **Fixed.** Both shapes are now documented. |
| L7 | Low | Test gaps. | **Added:** control characters, a raw-XML check for formulas, the About sheet, the WhatsApp opt-out, the number shown, the wider window, and scope that can't be widened. |

## Integration check (real HTTP)

All seven steps passed.

1. **Download headers:** content type, attachment filename and no-store were all correct.
2. **Sheets:** the file had a Vehicles sheet and an About sheet. Inactive vehicles were excluded by default.
3. **Masking:** the mobile showed as `********3210`, and chassis and engine as `****3456` and `****4567`.
4. **WhatsApp link:** "…PUCC expired on 22-11-2026; Insurance expires on 15-12-2026 (20 days left)… - Integration CSC". It did not mention the permit, which was 200 days away.
5. **Injected names:** a customer named `=HYPERLINK(...)` and an owner named `+cmd|...` were stored as text with a leading apostrophe. No `<f>` appears in any worksheet XML.
6. **Filters and access:** filters behaved as expected. An employee only ever got their own vehicles, whatever filters they used. Anonymous requests got 401.
7. **Audit and speed:** there was one `vehicles_exported` audit row per download, with no phone numbers in its details. A download took about 0.01 s.
8. **Contract:** the only change is one added path.

## Checks

- **Test suite:** `scripts/check.sh backend` gives 697 passed, with the contract in sync on the owner branch.
- **Deliberate breaks:** 12 of 13 were caught. The one missed is forcing text cells to type `s`. That is defence in depth: the leading apostrophe already stops formulas, and the raw-XML test guards the result.
