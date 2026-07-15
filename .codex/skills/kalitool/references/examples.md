# Examples

## JWT token path

```bash
python .codex/skills/kalitool/scripts/run_real_kali_tool_schema_test.py \
  --tool-id information_gathering.network_discovery.masscan \
  --jwt-token "<JWT>"
```

## Username/password login path

```bash
python .codex/skills/kalitool/scripts/run_real_kali_tool_schema_test.py \
  --tool-id information_gathering.network_discovery.nmap \
  --username "<user>" \
  --password "<pass>"
```

## Keep task for debugging on failure

```bash
python .codex/skills/kalitool/scripts/run_real_kali_tool_schema_test.py \
  --tool-id information_gathering.dns.amass \
  --jwt-token "<JWT>" \
  --keep-on-failure
```
