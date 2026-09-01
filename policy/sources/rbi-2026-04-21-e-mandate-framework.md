# Digital Payments – E-mandate Framework, 2026

**Source of record for every rule in `policy/mandate_policy.yaml`.**

| field | value |
|---|---|
| Circular no. | RBI/DPSS/2026-27/396 |
| Internal ref. | RBI/CO.DPSS.POLC.No.S56/02.14.003/2026-27 |
| Dated | 21 April 2026 |
| Issued by | Reserve Bank of India, Department of Payment and Settlement Systems |
| Retrieved from | https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13374 |
| Retrieved on | 2026-09-01 |

## Provenance, stated exactly

This text was retrieved from `rbi.org.in` by automated fetch on 2026-09-01 and converted
from HTML to markdown. **The RBI PDF was not parsed byte for byte.** Two independent
renderings were compared before this file was committed — the RBI notification page and a
verbatim reproduction on taxguru.in — and they agree on clause numbering, sub-clause
lettering, and the wording of every obligation quoted below.

That is the honest status: this is a *retrieved* primary text, not a *validated* one. It is
a strict upgrade on `docs/calibration.md` §1, which until now sourced the framework only
from three secondary law-firm summaries and said so. What it is not is a certified copy. If
a rule ever has to be defended in front of someone who matters, the PDF is the artefact to
put in front of them, and the clause numbers here are what tells them where to look.

Formatting note: headings and the repeal table are markdown; the clause text itself is
verbatim, including the typographical error in clause 6(c) ("shall provider a customer"),
which is reproduced rather than corrected. A silently corrected quote is an unverifiable
quote, and `policy/loader.py` checks every rule's `quote` against this file as a literal
substring — a tidied quote would fail that check, which is the intended behaviour. The one
substitution made deliberately is the rupee sign: the amounts in clause 8 are written
`Rs.15,000/-` and `Rs.1,00,000/-` here so that the file, the YAML quotes and the test
suite agree on a single ASCII encoding across Windows and Linux.

---

## 1. Short Title and Commencement

(a) These Directions shall be called the "Digital Payments - E-mandate Framework, 2026".

(b) These Directions shall be effective immediately.

## 2. Applicability

The provisions of these Directions shall be applicable to all Payment System Providers and Payment System Participants in respect of processing of recurring transactions, domestic or cross-border, using cards / PPI / UPI.

## 3. Definitions

(a) The terms 'authentication', 'factor of authentication', 'issuer', 'merchant' will have the same meaning as defined in Reserve Bank of India (Authentication mechanisms for digital payment transactions) Directions, 2025 dated September 25, 2025 and Master Direction on Regulation of Payment Aggregator (PA) dated September 15, 2025.

## 4. Registration and revocation of E-Mandate

(a) A customer desirous of opting for e-mandate facility shall undertake a one-time registration process. The mandate shall be registered only after successful validation of additional factor of authentication (AFA), in addition to the normal process required by the issuer.

(b) Every e-mandate registered by the issuer shall specify the validity period of the e-mandate. The issuer shall provide the customer with a facility to modify the validity period or withdraw the e-mandate at any point of time. Information about this facility shall be clearly communicated to the customer at the time of registration.

(c) The e-mandate may be for either a pre-specified fixed amount or for a variable amount subject to the overall cap fixed by the RBI. In the case of variable e-mandates, the issuer shall provide the customer with a facility to specify the maximum value of any recurring transaction.

(d) The customer shall be given a facility to choose or change a mode among available options (SMS, email, etc.) for receiving the pre-transaction notification from the issuer.

(e) Any modification in, or withdrawal of, an existing e-mandate shall require AFA validation by the issuer.

## 5. Processing of first transaction and subsequent recurring transactions

(a) The first transaction under an e-mandate shall require AFA validation. If the first transaction is processed along with registration of the e-mandate, then AFA validation may be combined.

(b) Payments under e-mandates shall not be subject to any other limits / controls set by the customer.

## 6. Pre-transaction Notification

(a) An issuer shall send a pre-transaction notification to the customer, at least 24 hours prior to the actual charge / debit.

(b) The pre-transaction notification shall, at the minimum, inform the customer about the merchant's name, transaction amount, date / time of debit, reference number of e-mandate, reason for debit, i.e., e-mandate registered by the customer.

(c) The issuer shall provider a customer with a facility to opt-out of any particular transaction or the e-mandate. Any such opt-out shall be validated by the issuer using AFA. An intimation to this effect shall be sent to the customer.

(d) Pre-transaction notification is not required for e-mandates registered to auto-replenish balances of FASTag, and National Common Mobility Card (NCMC).

## 7. Post-transaction Notification

An issuer shall send a post-transaction notification to the customer. This notification shall, at the minimum, inform the customer about the merchant's name, transaction amount, date and time of debit, reference number of transaction and e-mandate, reason for debit, i.e., e-mandate registered by the customer, and details on grievance redressal.

## 8. Transaction limits and velocity check

(a) All recurring transactions may be authorised without AFA up to Rs.15,000/- per transaction. Transactions above this amount shall be subject to AFA.

(b) Payment of insurance premiums, subscription to mutual funds, and credit card bill payments may be made without AFA up to Rs.1,00,000/- per transaction.

## 9. Dispute resolution and grievance redressal

(a) An appropriate dispute redressal system shall be put in place by the issuer to facilitate the customer to lodge grievance/s.

(b) RBI instructions on limiting liability of customers for unauthorised transactions shall be applicable to recurring transactions under e-mandates as well.

## 10. Other provisions

(a) No charges shall be levied to the customer for availing the e-mandate facility for recurring transactions.

(b) In case of cards, existing e-mandate(s) can be mapped to reissued cards.

(c) An acquirer shall ensure compliance with these directions by merchants on-boarded by them.

## 11. Repeal

With the issue of these directions, the instructions/guidelines contained in the following circulars, issued by the Reserve Bank stand repealed.

| No | Circular No. | Date | Subject |
|---|---|---|---|
| 1. | DPSS.CO.PD.No.447/02.14.003/2019-20 | August 21, 2019 | Processing of e-mandate on cards for recurring transactions |
| 2. | DPSS.CO.PD No.1324/02.23.001/2019-20 | January 10, 2020 | Processing of e-mandate in Unified Payments Interface (UPI) for recurring transactions |
| 3. | DPSS.CO.PD No.754/02.14.003/2020-21 | December 04, 2020 | Processing of e-mandates for recurring transactions |
| 4. | CO.DPSS.POLC.No.S34/02-14-003/2020 | March 31, 2021 | Framework for processing of e-mandates for recurring online transactions |
| 5. | Clarification issued to IBA on e-mandate based recurring transactions | October 08, 2021 | RBI's framework for e-mandate based recurring transactions |
| 6. | CO.DPSS.POLC.No.S-518/02.14.003/2022-23 | June 16, 2022 | Processing of e-mandates for recurring transactions |
| 7. | CO.DPSS.POLC.No.S-882/02.14.003/2023-24 | December 12, 2023 | Processing of e-mandates for recurring transactions |
| 8. | CO.DPSS.POLC.No.S528/02-14-003/2024-25 | August 22, 2024 | Processing of e-mandates for recurring transactions |

---

## What the text says that this project had assumed, and what it does not

Recorded here because the compile in T4.1 is the first time this project read the
regulation instead of reading about it.

**Settled: there is no transition period, and the text says nothing about existing
mandates.** Clause 1(b) is "effective immediately" and clause 11 repeals eight circulars
with no savings clause, no grandfathering clause, and no migration window anywhere in the
text. `docs/calibration.md` §5 listed "no transition period" as carried from the build plan
and unverified; it is now verified *as far as the text goes*. The remaining gap is different
and smaller: the framework is **silent** on whether mandates registered under the repealed
circulars must be re-registered. Clause 10(b) is the only sentence touching existing
mandates and it covers card re-issuance only. Silence is not permission and it is not a
requirement — it is an open question, and it stays in `limitations.md` as one.

**Against us: clause 2 does not cover eNACH.** Applicability is "cards / PPI / UPI".
`config/params.yaml` gives eNACH a **15%** share of the modelled rail mix, and eNACH/NACH
mandates run under NPCI's NACH procedural guidelines, not under this framework. So roughly a
seventh of the modelled book is outside the regulation whose arrival is this project's "why
now". The framework's own scope clause is why the auditor returns `needs_human` rather than
`compliant` for an eNACH mandate — it cannot pronounce on a rulebook that does not reach it.

**Against us: the pre-transaction notification is the *issuer's* obligation.** Clause 6(a)
says "An issuer shall send"; clause 3(a) takes 'issuer' from the 2025 authentication
directions, where it is the card/PPI/account issuer, not the merchant. This project is a
merchant-side allocator. It therefore does not *discharge* clause 6 — clause 10(c) is the
only line that reaches merchants, and it makes the acquirer responsible for merchant
compliance. The notice composer (T4.3) accordingly produces a notice for an issuer or
payment aggregator to send, and a merchant-side re-consent ask that piggybacks on it is a
commercial arrangement, not a regulatory entitlement.

**Not compilable: clause 8's heading.** It reads "Transaction limits and velocity check",
but neither 8(a) nor 8(b) states a velocity limit, and no other clause defines one. No rule
is compiled for velocity, because there is no obligation in the text to compile. A rule
invented to match a heading is exactly the hallucination this file exists to prevent.
