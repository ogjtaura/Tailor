# Run prompt

Portable. Paste this, then `fact_bank.json`, then the job ad, into any model.
No tooling required. Keep this file in the repo so it versions alongside the
schema it depends on.

---

You are drafting a CV and cover letter from a canonical fact bank. You are an
untrusted transformation step. Your job is to make verification easy, not to
produce polished prose.

## Absolute rules

1. Every factual claim must cite at least one `fact_id` from the bank. No
   citation means the claim cannot appear.
2. You may reword, compress, reorder and reframe cited facts. You may not add,
   infer, extrapolate, round, or supply causation that the fact does not state.
3. Never generate anything under `protected_fields`. Names, employers, titles,
   dates, institutions, degrees, grades and contact details are copied
   character-for-character. If a value is missing or ambiguous, stop and ask.
4. A fact with `ownership: team` or `organisation` cannot become "I". Use "our
   team" or name the collective.
5. A fact with `state: planned`, `pending_verification` or `development_result`
   cannot be written as a completed achievement. "Preparing an attempt" is not
   "holds the record". "Development best" is not "achieved".
6. Numbers, units and product names are copied exactly from the cited fact.
   28 ms is not 30 ms and not 28 s. ODrive S1 is not ODrive Pro.
7. If two facts conflict, prefer the one with the later `confirmed_at`, and
   flag the conflict. Do not silently pick one.

## Output, in this order

**1. Requirement read.** Parse the ad into a numbered list. Mark each `hard`,
`important` or `preferred`, and quote the phrase it came from.

**2. Evidence table.** One row per requirement:

| req | supporting fact_ids | strength | notes |

`strength` is `direct`, `adjacent` or `none`. Be pessimistic. `adjacent` means
the bank shows a related but not equivalent capability. `none` means you have
nothing, and you must say so rather than reaching.

**3. Gap list.** Every requirement scoring `none`, plus one line each on
whether to address it, ignore it, or reconsider the application.

**4. CV.** Plain structure only: standard headings, no tables, no columns, no
text boxes, no graphics. Order entries by relevance to this ad. Every bullet
ends with its citations in brackets, e.g. `[FACT-ROTATION-28MS]`.

**5. Cover letter.** Three paragraphs. Same citation format. Leave the
"why this organisation" paragraph as a marked placeholder with 2-3 questions
for me to answer, unless the ad itself gives you something specific and real
to work with. Do not manufacture enthusiasm.

**6. Claims table.** This is the important one. One row per factual sentence
you produced, across both documents:

| claim_id | text | fact_ids | ownership | state | what I changed from the source |

`ownership` is `individual`, `team`, `organisation` or `non_factual`.
`state` is `completed`, `current`, `development_result`, `planned` or
`pending_verification`. The last column is prose: name every departure from
the source wording, including ones you think are harmless. If you rewrote
"catalogued" as "led the cataloguing of", say so.

**7. Self-flag.** List any claim you are less than fully confident is
supported by its cited facts. Under-flagging is worse than over-flagging.

**8. Machine-readable block.** Repeat sections 2 and 6 as a single fenced JSON
object, so I can save it as `output.json` and run it through the gates. No
commentary inside the block. Exact shape:

```json
{
  "applicant_id": "<copy from the bank>",
  "supported_requirement_ids": ["REQ-1", "REQ-2"],
  "protected_fields_produced": {
    "employment.f1.job_title": "<exactly what you wrote>"
  },
  "objects": [
    {
      "object_id": "CV",
      "text": "<full CV text>",
      "claims": [
        {"claim_id": "C1", "text": "...", "fact_ids": ["FACT-..."],
         "requirement_ids": ["REQ-1"], "ownership": "individual",
         "state": "completed"}
      ]
    },
    {
      "object_id": "COVER",
      "text": "<full letter text>",
      "word_limit": 350,
      "claims": []
    }
  ]
}
```

`supported_requirement_ids` is every requirement you scored `direct` or
`adjacent` in section 2. `protected_fields_produced` uses dotted paths into
the bank's `protected_fields` tree, one entry per protected value you
reproduced anywhere in either document.

## What I will do with this

I verify every row of the claims table against the bank by hand. Claims that
fail get deleted, not rewritten by you. Do not attempt a global polish pass.
