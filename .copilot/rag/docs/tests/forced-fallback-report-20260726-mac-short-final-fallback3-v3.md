# Forced daemon fallback smoke

- run_id: 20260726-mac-short-final-fallback3-v3
- git_commit: 9ec0213cc48eb5ca63dfbce46623f8b63dfd7dfa
- git_dirty: True
- worktree_fingerprint: 8bb2fbd390b24292da4a82c814d92d4aa4169df4ce067e0e4c60fa6c0344fdb8
- daemon_code_fingerprint_expected: ba9b8b13f50d9aa1b136eb57b4eade6dcee610cd2f30fc024914bedf2cc0950c
- case_spec_fingerprint: 0571dc0aec976c26723e031ef038fb7d29aee262ffc76a97d0359bf4d3a01cbf
- outer timeout: 15.000 sec
- daemon soft timeout: 5.000 sec
- result: PASS

|DB|Result|Wall sec|Shutdown|Old PID exited|Fallback|Final success|New generation|
|--|--|--:|--|--|--|--|--|
|ac-rag|PASS|10.989|ACK|PASS|PASS|PASS|PASS|
|incident-rag|PASS|9.726|ACK|PASS|PASS|PASS|PASS|
|rfc-full-20k-rag|PASS|10.188|FORCE|PASS|PASS|PASS|PASS|

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
- retirement_process_exited: PASS
- retirement_mode: PASS
- server_process_exited_before_cleanup: PASS
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
- retirement_process_exited: PASS
- retirement_mode: PASS
- server_process_exited_before_cleanup: PASS
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
- retirement_process_exited: PASS
- retirement_mode: PASS
- server_process_exited_before_cleanup: PASS
- old_generation_retired: PASS
- followup_required_daemon_success: PASS
- new_generation: PASS
