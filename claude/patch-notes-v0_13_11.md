# v0.13.11 — shorting mode: live (2026-08-22, retroactive to 2026-08-15)

One line: config/shorting.yaml mode shadow -> live. Flipped on the Spark at
shorting go-live (2026-08-15) and never committed; found by git status
during the v0.14.0 deploy. Between those dates an emergency git-checkout
rollback would have silently disarmed the short lanes. Own tag because it
is a risk-posture change, not part of the model upgrade it was found during.
