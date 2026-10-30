# Recording QuickBooks payloads for fixtures

Two tools turn a connected QuickBooks company into test fixtures without ever
touching the Intuit sandbox:

1. **Recorder** (`rgnr8_qbo.recorder`) reads a real connected company, read-only, and
   writes the raw JSON Intuit returns.
2. **Scrubber** (`rgnr8_qbo.scrub`) turns that raw output into anonymized fixtures
   that are safe to commit.

## Raw output must never be committed

Raw recordings contain a real company's customers, vendors, employees and amounts.
This repository is public. `qbo-recordings/` and `**/raw/` are git-ignored, but the
ignore rule is a seatbelt, not a review. Do not copy raw output anywhere that is
committed, and do not paste it into issues, PRs or chat.

## 1. Record

```
py -m rgnr8_qbo.recorder --tenant <tenant-id> --out <dir> [--since 2025-01-01]
```

- `--out` defaults to `qbo-recordings/` at the repo root. A previous `raw/` in that
  directory is replaced on each run.
- `--since` is the first month of the monthly TrialBalance and ProfitAndLoss reports
  (default `2025-01-01`).
- Environment, read at runtime only (never printed, never written):
  - `RGNR8_DATABASE_URL` (or `DATABASE_URL`): where the connection rows live (`postgres://` or `sqlite://`). On the Render web service it is already set.
  - `RGNR8_SECRET_KEY`: the Fernet key that decrypts the stored tokens.
  - `RGNR8_QBO_CLIENT_ID`, `RGNR8_QBO_CLIENT_SECRET`, `RGNR8_QBO_ENVIRONMENT`: needed to
    refresh an expired access token, as the app does.

The recorder builds the connection the same way the app does (the same connection
store, cipher, connect service and API client).

### What it reads

- Every entity: Account, Customer, Vendor, Employee, Item, Class, Department (the
  API entity behind QuickBooks' Location), Term, PaymentMethod, TaxCode, TaxRate,
  Invoice, Payment, SalesReceipt, CreditMemo, RefundReceipt, Deposit, Transfer, Bill,
  BillPayment, VendorCredit, Purchase (checks, expenses and card charges),
  JournalEntry, Estimate, PurchaseOrder, TimeActivity, Attachable (metadata only).
  Paginated 1,000 rows a page with `STARTPOSITION`/`MAXRESULTS`.
- CompanyInfo and Preferences.
- A change data capture (`cdc`) call for the last 30 days.
- TrialBalance and ProfitAndLoss for each month since `--since`.

### The read-only guard

Every Accounting API request goes through `ReadOnlyHttp`, which refuses:

- any method other than `GET`, and
- any path outside this allowlist, relative to `/v3/company/{realm}/`, for the
  connected company's realm and the environment's host over https:

  | Path | Use |
  |---|---|
  | `query` | `select * from <Entity> ...` |
  | `reports/<Name>` | TrialBalance, ProfitAndLoss |
  | `cdc` | change data capture |
  | `companyinfo[/<id>]` | company info |
  | `preferences` | preferences |
  | `<Entity>/<id>` | one entity by id |

The token refresh (which the app also performs, and which keeps the tokens alive)
goes through a separate seam that may POST only to Intuit's token endpoint. The
recorder never revokes a token and never calls the connect or disconnect paths.
Note that a refresh rotates the stored tokens in the database, exactly as the app
does. The recorder writes nothing else to the database.

The guard is covered by `packages/qbo/tests/test_qbo_recorder.py`.

### Output

```
<out>/raw/<Entity>/0001.json           one query page per file
<out>/raw/CompanyInfo/0001.json
<out>/raw/Preferences/0001.json
<out>/raw/CDC/0001.json
<out>/raw/reports/<Report>/<YYYY-MM>.json
<out>/manifest.json                    counts per record, request URLs (realm replaced
                                       by {realm}), timestamp, minorversion, api host
```

No token and no realm id is written. The manifest carries no secrets.

## 2. Scrub

```
py -m rgnr8_qbo.scrub <in_dir> packages/qbo/tests/fixtures/recorded
```

`<in_dir>` is the recorder's output directory. Set `RGNR8_SCRUB_KEY` to a private
value and keep it. The same key always gives the same pseudonyms and the same amount
factor. If it is unset, a random key is generated and printed once to stderr, and
the next run will differ.

Nothing is written unless the leak check passes. The scrubber refuses to write any
output if a replaced personal value still appears in a kept field, and names the
field path (the value itself is never printed).

### Replaced (pseudonymized)

Deterministic: the same original value (case-insensitively for names) always maps
to the same replacement, across every entity and reference, so links survive.

| Field | Replacement |
|---|---|
| `DisplayName`, `CompanyName`, `LegalName`, `GivenName`, `FamilyName`, `MiddleName`, `PrintOnCheckName` | `Name-<hash8>` |
| `Name` on Customer, Vendor, Employee, Item, attachments, and any other entity not listed under "kept" | `Name-<hash8>` |
| `FullyQualifiedName` (each `:` segment, except Account, Class, Department, Term, PaymentMethod, TaxCode, TaxRate) | segments as `Name-<hash8>` |
| `name` inside any reference (`CustomerRef`, `VendorRef`, `EntityRef`, `ItemRef`, ...) | `Name-<hash8>` |
| `PrivateNote`, `Memo`, `Description`, `CustomerMemo` (including `{"value": ...}`), `Note`, `Notes` | `memo <hash8>` |
| `PrimaryEmailAddr`, `BillEmail`, `EmailAddr`, `Email` (`Address`) | `user-<hash8>@example.com` |
| `PrimaryPhone`, `Mobile`, `Fax`, `AlternatePhone` (`FreeFormNumber`) | `555-NNNN` |
| `WebAddr` (`URI`) | `https://example.com/<hash8>` |
| Address lines `Line1`..`Line5`, `City` (in `*Addr` blocks) | `NNN Fixture Way`, `City-<hash8>` |
| `PostalCode` | five digits from the hash |
| `Lat`, `Long` | `0` |
| `TaxIdentifier`, `SSN`, `EIN`, `AcctNum` (not on Account), `BankAccountNumber`, `RoutingNumber`, `AccountNumber`, `ResaleNum`, `TaxNumber` | `ID-<hash8>` |
| `FileName` (Attachable) | `file-<hash8>` |
| `TempDownloadUri` | dropped |
| Any string under a key not listed under "kept" | `x-<hash8>` (listed in the run summary for review) |

### Kept

- Ids, `SyncToken`, `MetaData` timestamps, `TxnDate`, `DueDate`, every `*Date`,
  `*Time` and `*Status` field, and every `*Id` field.
- Types and statuses: `Type`, `type`, `TxnType`, `AccountType`, `AccountSubType`,
  `Classification`, `Status`, `Domain`, `PostingType`, `DetailType`, `Active`,
  `Taxable`, `FiscalYearStartMonth`, `ContentType`, `Currency`, `Level`,
  `SubAccount`, `Sparse`.
- `LinkedTxn`, reference ids (`value` of every reference), `DocNumber`, currency
  (`CurrencyRef`).
- Account names and fully qualified names, account numbers (`AcctNum` on Account),
  and names of Class, Department, Term, PaymentMethod, TaxCode, TaxRate, and
  references to accounts, classes, departments, terms, payment methods, currencies,
  tax codes and tax rates.
- `State` (`CountrySubDivisionCode`) and `Country`.
- Quantities and rates: `Qty`, `Rate`, `RateValue`, `TaxPercent`, `Size`.
- Report row labels (column one), so the report structure stays readable.
- Custom field labels (`Name` inside `CustomField`).

### Scaled (money)

Every value under these keys is money. It is multiplied by one global factor between
0.5 and 1.5 (derived from `RGNR8_SCRUB_KEY`) and rounded half-up to cents:

`TotalAmt`, `Balance`, `BalanceWithJobs`, `Amount` (every line, cash back and tax
line), `UnitPrice`, `PurchaseCost`, `TotalTax`, `TaxAmount`, `TaxInclusiveAmount`,
`NetAmountTaxable`, `UnappliedAmt`, `RemainingCredit`, `CurrentBalance`,
`CurrentBalanceWithSubAccounts`, `HomeBalance`, `HomeBalanceWithSubAccounts`,
`BillRate`, `PayRate`, `HourlyRate`. In report cells, every value after the first
column that is a number.

Not money, and kept: `Qty`, `Rate`, `RateValue`, `TaxPercent`.

Totals stay consistent:

- Journal entries: after scaling, if debits differ from credits by rounding, the
  largest line on the short side absorbs the difference. The run summary counts
  these.
- Transactions in the list `Invoice, SalesReceipt, CreditMemo, RefundReceipt,
  Estimate, PurchaseOrder, Bill, VendorCredit, Purchase, Deposit, BillPayment,
  Payment`: `TotalAmt` is recomputed as the sum of the scaled non-subtotal lines,
  plus `TxnTaxDetail.TotalTax`, plus `UnappliedAmt`. `Balance` follows `TotalAmt`
  when it equalled it before scaling. This happens only when the original document
  satisfied the same formula. Otherwise the document is scaled proportionally and
  counted as "not recomputed" in the run summary.
- Trial balance: the TOTAL row is set to the sum of the scaled account rows.

Limits: `UnitPrice` is rounded independently, so `Qty x UnitPrice` can differ from
`Amount` by a cent. Subtotal lines are scaled independently. Deposit `CashBack` is
scaled as a money field and not checked against the deposit total.

### The leak check

Every value replaced as personal data (at least 4 characters) is searched, whole
word and case-insensitively, in every string the output kept. A hit fails the run
with the field path. This catches the case where a customer's name appears in an
account name that was kept. Values shorter than 4 characters are not checked.

## 3. Review before committing

The owner must read the scrubbed fixtures before committing them. In particular:

- Scan for anything the scrubber could not know is personal. The run summary lists
  every key that was replaced by default. Confirm none of them is a field the tests
  need.
- Check that account names contain no person or business name.
- Check that no free-text field was kept by mistake.
- Keep `RGNR8_SCRUB_KEY` out of the repository.

Only then copy `packages/qbo/tests/fixtures/recorded/` into a commit. Never commit
`qbo-recordings/` or any `raw/` directory.

Note that the amount factor is one number for the whole recording. If anyone knows
one true amount from the company, they can recover the factor and every amount. Keep
the key private.
