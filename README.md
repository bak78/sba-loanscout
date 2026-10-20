# SBA LoanScout

> **Branded edition.** Capital Sense Funding's colours and logo are in the window.

Finds **small businesses** — healthcare, construction and other fields — in
the SBA's Small Business Search, with the **contact person's first and last
name, email and phone**. It checks every email, looks each business up on
Google to confirm it's real and open, reads its website for signs of growth,
and writes a ranked CSV of leads with the evidence behind every score.

```
SBA Small Business Search ──▶ email check, ──▶ Google listing ──▶ website ──▶ sba_leads.csv
(contact name, email,          free checks,     (via Serper:       (growth
 phone; free)                  rank             rating, reviews)    signals)
```

Businesses register with the SBA to sell to the government, and each chose to
show its contact person, email and phone in public. So the list already has
the name and email the other LoanScouts have to hunt for — **no email finder
(Anymailfinder) is needed**. Only the Google check costs anything: about one
Serper credit per business.

## The easy way: the window

Double-click **`SBA LoanScout GUI.bat`**. The first time, it sets itself up
(about a minute). Then:

1. **Where** — pick a state (or *All states*), and optionally the start of the
   ZIP codes (at least two digits; blank = the whole state).
2. **Industries** — tick what you want. Each heading (*Healthcare*,
   *Construction*, *Other fields*) ticks or clears its whole column.
3. **Preview (free)** — asks the SBA and ranks every matching business from
   SBA facts alone. Writes `out\sba_leads_preview.csv`. No credits used.
4. **Run** — looks up the top businesses on Google (about 1 Serper credit
   each), reads their websites, and writes `out\sba_leads.csv`. Run again to
   continue down the list; businesses already done are skipped.
5. **Open results** opens the newest spreadsheet. **Stop** ends a run early;
   finished rows are kept.

*Run* needs a Serper key in `.env` (below). *Preview* doesn't.

## What's in the spreadsheet

| Column | Meaning |
|---|---|
| `first_name`, `last_name` | The contact person the business gave the SBA |
| `email` | The contact's email, as given to the SBA (checked: real shape, domain takes mail) |
| `phone` | The SBA phone, else the Google listing's |
| `company_name` | The name the public knows it by (its DBA when it has one) |
| `industry`, `industry_group` | e.g. *Home care* in *Healthcare* — the checkbox it came from |
| `score` | 0–100 weight; see below |
| `google_rating`, `google_reviews` | From the Google listing found through Serper |
| `email_is_contacts_own` | *yes* when the email carries the contact's name (jane.doe@…) |

Then the website, legal name, address, year established, SBA certifications
(8(a), women-owned, HUBZone, veteran-owned), industry code, the top three
pieces of evidence, the Google listing it matched, and notes.

## The score (weight)

Points come from evidence, and every point is traceable:

- **SBA facts:** years in business, the email being the contact's own, an
  email on the business's own domain, SBA certifications.
- **Google:** a confirmed listing, its review count and rating.
- **The website:** hiring, expansion, new locations, signs of strain.

They're combined as **40% fit + 35% growth + 25% distress**, the same as the
other LoanScouts. The weights are in `config\sba.yaml` and `config\signals.yaml`.

## Who is left out, and why

Every business left out goes to **`out\sba_leads_left_out.csv`** with the
reason in its first column, so nothing disappears silently.

Before any lookup (free):

- **Email fails the check** — badly shaped, a placeholder, a misspelled
  domain (gmial.com), a Gmail name too short to exist, or a domain that
  doesn't take mail.
- **Department inbox** — hr@, careers@, billing@, credentialing@… won't reach the owner.
- **Chain or group** — one email domain used by 3 or more businesses: one
  company's many nursing homes or branches (credentialing@chs-corp.com on 18
  of them). Email providers and franchise brands never count.
- **Email already used** — listed in an earlier batch (see *Memory* below).
- **Same email or same contact twice** — one owner's several companies; the
  best-ranked one is kept.
- **Nonprofit** — the SBA record says so (a `.org` email alone doesn't: most
  `.org` addresses in the list belong to for-profit companies).
- **Government or school email** (.gov, .edu, .mil), and **national chains**
  (the `national_chains` list in `sba.yaml`).

After the Google lookup:

- **No matching Google listing, and no website of its own** — Google shows
  nothing for it (or only another business at that address), and it has no
  website that loads on its email's domain. Often closed, or a mailing address.

  A business that isn't on Google but whose **own website loads** (on the same
  domain as its email, or a franchise brand's page such as a Home Instead
  office) is **kept**: often a telehealth or home-based practice. Its
  `google_match` column says *not on Google; own website*, and it scores a
  little lower than one with Google reviews.
- **Disqualified by its website** — a nonprofit, a lender, a public company.
- **Chains and groups** — a website domain shared by 3 or more businesses
  in the results (franchises, listed in `franchise_domains`, stay).

## Memory

Three files in `out\` remember what's done, so no credits are spent twice and
no one is emailed twice:

- `sba_leads.seen.txt` — every business already looked up (or left out).
- `sba_leads.emails.txt` — every email ever written to the results.
- `sba_leads.csv` — the current batch.

To start a fresh batch for your boss, **delete only `sba_leads.csv`**. Keep the
two `.txt` files.

## Industries

The checkboxes come from `config\sba.yaml`. Each line names industry codes
(NAICS), or the start of them: `"6211"` means every code starting 6211. A
business counts only in the industry of its **main** code, so consultants and
suppliers that merely list a healthcare or construction code are left out.
Add, rename or move industries there — no code change needed.

## Setup (Serper key)

Copy `.env.example` to `.env` and fill in:

```
SERPER_API_KEY=your-key-from-serper.dev
CRAWLER_CONTACT_EMAIL=you@yourcompany.com
```

Never share or upload `.env`.

## Command line

```
.venv\Scripts\python -m sba_loanscout run --state MD --group Healthcare --dry-run
.venv\Scripts\python -m sba_loanscout run --state MD --industry "Home care" --limit 100
.venv\Scripts\python -m sba_loanscout run --state ALL --group Construction --limit 500
```

## Good to know

- **The SBA search has no official download.** The program asks the same data
  feed the search page uses, one industry at a time, with a pause between.
  If the SBA redesigns its site, the program may need updating.
- The SBA feed can't filter by state, so each industry comes back for the
  whole country and the state is picked out here. Each day's answers are
  saved in `data\sba\`, so a second run the same day doesn't ask again.

## Tests

```
.venv\Scripts\python -m pytest
```
