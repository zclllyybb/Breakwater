# Breakwater Regression Tests

These tests are intentionally outside the default `unittest` suite because they depend on live local services or model access.

## Lark Image E2E

Runs a live Breakwater flow from a mock lark-cli image message through Breakwater's slot creation, image download, Codex `localImage` input, Codex reply recording, and fake Lark reply delivery.

The test uses an isolated regression database under `.breakwater/regression-artifacts/lark-image-e2e/` and does not touch the daemon's `.breakwater/breakwater.db`.

```bash
uv run python regression_tests/lark_image_e2e_regression.py --model gpt-5.4-mini --json
```

Defaults:

- app-server: `ws://127.0.0.1:17345`
- model: `gpt-5.4-mini`
- artifacts: `.breakwater/regression-artifacts/lark-image-e2e/<run>/`

The mock Feishu/Lark message uses the real lark-cli image shape: `message_type=post` with `[Image: img_v3_...]` in `content`. The script checks the final fake Lark reply for the generated image tokens: `BREAKWATER`, `SYNC`, `OK`, `42`, and `GREEN`.
