# runs/

One directory per application attempt: `runs/<job_id>/<date>/`.

| file            | what it holds                                            |
|-----------------|---------------------------------------------------------|
| `raw_output.md` | the model's output, unedited. Never touch this.         |
| `output.json`   | section 8's JSON block, saved for `check.py`.           |
| `final.md`      | what you actually sent. The diff from `raw_output.md` is the signal. |
| `notes.md`      | model used, failure codes hit, corrections made.        |

This directory is gitignored (outputs carry personal data); only this README is tracked.
