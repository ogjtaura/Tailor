# Verifier-first architecture

## Data flow

```text
SOURCE DOCUMENTS / USER CONFIRMATIONS
                ↓
        CANONICAL FACT BANK
                ↓
JOB AD → REQUIREMENT BANK
                ↓
          EVIDENCE PLANNER
                ↓
              WRITER
                ↓
      DETERMINISTIC VERIFIER
           ↺ targeted repair
                ↓
       ATOMIC CLAIM VERIFIER
           ↺ targeted repair
                ↓
         COVERAGE VERIFIER
           ↺ targeted add
                ↓
          LENGTH VERIFIER
           ↺ prune/compress
                ↓
       CODE-ONLY ASSEMBLER
                ↓
             EXPORT
```

## Provenance graph

```text
SOURCE → FACT → CLAIM → OUTPUT_OBJECT
                  ↓
             REQUIREMENT
```

A factual claim cannot enter a final document unless there is a valid path from at least one source to that claim.

## Protected fields

The model must not generate these fields:

- applicant name
- contact information
- employer names
- job titles
- employment dates
- institution names
- degree titles
- graduation dates
- grades
- scholarship names
- referee details

They are copied verbatim from canonical structured data.

## Fact ownership

Facts are typed as:

- `individual`
- `team`
- `organisation`

A team-level fact may support a sentence such as "our team achieved..." but cannot support "I achieved..." without additional individual evidence.

## Fact state

Facts can be:

- `completed`
- `current`
- `development_result`
- `planned`
- `pending_verification`

A pending or planned achievement cannot be rewritten as completed.

## Repair rule

Never run a global polish pass over verified content.

When an object fails:

1. return only the failed object,
2. include explicit failure reasons,
3. include only permitted facts,
4. regenerate that object,
5. re-run all gates,
6. after two failed repairs, drop the claim or fall back to source wording.
