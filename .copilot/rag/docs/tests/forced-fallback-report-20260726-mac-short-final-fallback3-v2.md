# Forced daemon fallback smoke

- run_id: 20260726-mac-short-final-fallback3-v2
- git_commit: 9ec0213cc48eb5ca63dfbce46623f8b63dfd7dfa
- git_dirty: True
- worktree_fingerprint: 1f28926ed6ad8cae036ae898e07a861a1c7ed1b0d4f873e2c342a5448608b784
- daemon_code_fingerprint_expected: ba9b8b13f50d9aa1b136eb57b4eade6dcee610cd2f30fc024914bedf2cc0950c
- case_spec_fingerprint: 918536eda9455b1828086a23285eb96bf14e3d5f457cf1fbdf1df55cf79a2f64
- outer timeout: 15.000 sec
- daemon soft timeout: 5.000 sec
- result: PASS

|DB|Result|Wall sec|First attempt|Fallback|Final success|New generation|
|--|--|--:|--|--|--|--|
|ac-rag|PASS|10.554|TIMEOUT|PASS|PASS|PASS|
|incident-rag|PASS|9.411|TIMEOUT|PASS|PASS|PASS|
|rfc-full-20k-rag|PASS|9.922|TIMEOUT|PASS|PASS|PASS|

## Checks

### ac-rag

- fallback_exit_zero: PASS
- stdout_json_pure: PASS
- first_attempt_failed: PASS
- daemon_timeout_recorded: PASS
- fallback_used: PASS
- single_fallback: PASS
- fallback_succeeded: PASS
- final_user_visible_success: PASS
- outer_deadline_met: PASS
- old_generation_retired: PASS
- followup_required_daemon_success: PASS
- new_generation: PASS

### incident-rag

- fallback_exit_zero: PASS
- stdout_json_pure: PASS
- first_attempt_failed: PASS
- daemon_timeout_recorded: PASS
- fallback_used: PASS
- single_fallback: PASS
- fallback_succeeded: PASS
- final_user_visible_success: PASS
- outer_deadline_met: PASS
- old_generation_retired: PASS
- followup_required_daemon_success: PASS
- new_generation: PASS

### rfc-full-20k-rag

- fallback_exit_zero: PASS
- stdout_json_pure: PASS
- first_attempt_failed: PASS
- daemon_timeout_recorded: PASS
- fallback_used: PASS
- single_fallback: PASS
- fallback_succeeded: PASS
- final_user_visible_success: PASS
- outer_deadline_met: PASS
- old_generation_retired: PASS
- followup_required_daemon_success: PASS
- new_generation: PASS
