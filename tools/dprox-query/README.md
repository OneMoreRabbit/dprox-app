# dprox-query

One question to dprox, over mTLS, printing the passages you are allowed to
see. Installed with pipx in the seat image and the OpenClaw image, at a
pinned release; never installed while an agent runs.

```
dprox-query check
dprox-query ask "what is the wage policy"
dprox-query ask --limit 3 --json "what is the wage policy"
```

It decides nothing about access. dprox reads the certificate presented in the
handshake, resolves the caller in the compiled plan, searches only that
caller's orgs and keeps only that caller's groups. This tool has no parameter
for a group, an org or another caller, and dprox refuses request fields it
does not know.

## Configuration

The estate writes it, into the seat's persistent home — never into an image.
Default path `~/.config/dprox-query/config.yml`, overridden by
`DPROX_QUERY_CONFIG` or `--config`.

```yaml
endpoint: https://dprox.internal:8443
ca_cert_path: ~/.config/dprox-query/ca.crt
client_cert_path: ~/.config/dprox-query/client.crt
client_key_path: ~/.config/dprox-query/client.key
timeout_seconds: 30     # optional
```

The first four are required and none has a default. A key the tool does not
read is refused rather than ignored, so a setting nobody is honouring cannot
sit in the file looking as though it is.

## Versions

`dprox-query` 0.3.0 works with dprox >= 0.3.0 and < 0.4.0, and refuses
anything else with a message naming both versions. The check runs before every
question, not only in `check`: a tool baked into an image months ago is the
copy nobody thinks to check.

## Exit codes

| code | meaning |
|---|---|
| 0 | answered, or the check passed |
| 1 | not answered: unreachable, refused, or an upstream fault |
| 2 | configuration missing, unreadable or incomplete |
| 3 | the dprox answering is outside the versions this tool reads |
