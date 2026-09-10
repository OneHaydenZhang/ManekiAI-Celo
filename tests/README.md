These suites run inside the host application (they import `auto_service.*`).
From the host repo root: `python -m pytest tests/test_celo_deposit.py tests/test_celo_agentid.py tests/test_x402.py -q`.
