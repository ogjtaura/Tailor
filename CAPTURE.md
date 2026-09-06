# Capture

How a manual application run becomes product data. The whole point is that
this costs you about ten minutes per application and produces the adversarial
benchmark you would otherwise have to invent from scratch.

## Layout

Split applicant-level from job-level now, while there is one of each.

```
applicants/<applicant_id>/fact_bank.json      canonical, one per person
jobs/<job_id>/ad.txt                          raw ad, as pasted
jobs/<job_id>/requirements.json               parsed, after you check it
runs/<job_id>/<date>/raw_output.md            unedited model output
runs/<job_id>/<date>/final.md                 what you actually sent
runs/<job_id>/<date>/notes.md                 model used, corrections made
tests/cases.jsonl                             the benchmark, grows every run
```

Keep `raw_output.md` unedited. The diff between raw and final is the signal.

## After each run

Work down the claims table. Every claim you reject falls into exactly one of
three buckets, and each has a different destination.

**Bucket 1: the bank was wrong or incomplete.** The model was faithful, your
data was stale. Patch `fact_bank.json`. This is not a benchmark case, it is a
data quality event. Add a `supersedes` link rather than editing in place, so
the correction history survives. This is the 28 ms / 90 degree situation.

**Bucket 2: the writer invented, inflated or drifted.** The bank was right and
the output was not. This is a benchmark case. Append to `cases.jsonl`.

**Bucket 3: it is supported but you would not say it that way.** Style, not
truth. Log nothing. Fix it in `final.md` and move on. Resist the urge to
encode taste as a verifier rule, or you will build something that rejects
correct output.

## Case format

One JSON object per line, appended. Nothing else.

```json
{
  "case_id": "C-0001",
  "run": "runs/tait-2026/2026-09-06",
  "category": "responsibility_inflation",
  "claim_text": "Led the design of the Clean-In-Place filtration strategy.",
  "fact_ids": ["FACT-FONTERRA-180"],
  "declared_ownership": "individual",
  "declared_state": "completed",
  "should_pass": false,
  "why": "Bank says investigated and catalogued. 'Led the design' is invented.",
  "caught_by": "human",
  "model": "..."
}
```

`caught_by` is the field that turns this into a metric. Set it to `human` when
you write the case. Later, when the verifier runs against the file, anything
still marked `human` is a false accept. Your false acceptance rate is simply
the share of `should_pass: false` cases the verifier misses.

Also log the ones that went **right**. When a claim is a genuine paraphrase
that you kept, write it with `should_pass: true`. Without these you will build
a verifier that rejects everything and scores perfectly on false accepts. The
target ratio is roughly two supported cases for every unsupported one.

## Categories

Start with these and add as you meet them. Consistency matters more than the
taxonomy being right, because the categories are how you find clusters.

```
technology_swap          responsibility_inflation
number_swap              team_to_individual
unit_drift               seniority_inflation
causal_invention         state_inflation
outcome_invention        protected_field_drift
supported_paraphrase     scope_inflation
```

`state_inflation` is the planned-to-completed case. `scope_inflation` is when
a real thing gets described as bigger than it was.

## What not to track

Skip outcomes. Do not log interviews, rejections or response rates. With
thirty applications the signal is far too weak to learn anything from, and
tracking it will quietly turn into a mood diary at the exact point in the
process where that is least useful. Correction data teaches you something at
n = 5. Outcome data teaches you nothing at n = 50.

## When to stop and build

Two triggers, whichever comes first.

- **Roughly 40 cases**, at which point the harness is worth writing and the
  false acceptance rate becomes a real number rather than an anecdote.
- **The same category appears three times.** That cluster is your next
  verifier gate, and you now have evidence for it rather than a hunch.

Until then, keep shipping applications. The repo is accumulating the thing
you would otherwise have had to guess at.
