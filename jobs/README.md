# jobs/

One directory per job: `jobs/<job_id>/`.

| file                | what it holds                                              |
|---------------------|-----------------------------------------------------------|
| `ad.txt`            | the raw job ad, exactly as posted. Never edited.          |
| `requirements.json` | the ad parsed to `schemas/job_requirements.schema.json`, after you have read it back and fixed the priorities. |

Parse the ad first, in its own step. Models systematically downgrade `hard`
requirements to `preferred`, and that error propagates silently into the
coverage report.

This directory is gitignored (an ad may be copyrighted, and drafts alongside it
carry personal data); only this README is tracked.
