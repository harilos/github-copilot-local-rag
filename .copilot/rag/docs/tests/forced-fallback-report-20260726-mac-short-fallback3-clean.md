# Forced daemon fallback smoke

- run_id: 20260726-mac-short-fallback3-clean
- outer timeout: 15.000 sec
- daemon soft timeout: 5.000 sec
- result: PASS

|DB|Result|Wall sec|First attempt|Fallback|Final success|New generation|
|--|--|--:|--|--|--|--|
|ac-rag|PASS|10.591|TIMEOUT|PASS|PASS|PASS|
|incident-rag|PASS|8.897|TIMEOUT|PASS|PASS|PASS|
|rfc-full-20k-rag|PASS|9.417|TIMEOUT|PASS|PASS|PASS|

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
