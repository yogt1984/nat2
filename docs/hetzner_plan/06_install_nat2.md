# 06 — Install nat2

**Effort** 30 min · **Depends on** 05 · **Status** todo

## What
Get the repo and both artefacts the units reference onto the box, with nothing
enabled yet.

## How
Generate a deploy key **on the VM first** if the repo is going private —
otherwise the clone breaks at the worst moment. Both remotes are SSH.

```
ssh-keygen -t ed25519 -C nat2-primary -f ~/.ssh/id_ed25519_gh -N ''
# register the .pub as a read-only deploy key, then:
git clone git@github.com:yogt1984/nat2.git /home/onat/nat2
curl -LsSf https://astral.sh/uv/install.sh | sh    # uv is not in the archive
cd /home/onat/nat2 && ./install.sh && uv sync --extra dev
```

The box needs **both** `~/.local/bin/nat2` (which the capture unit execs) and
`.venv/bin/python` (which gapwatch and the report unit exec). `--extra dev` or
there is no pytest. Ubuntu 24.04's system Python 3.12 satisfies the project.

## Verify
```
uv run pytest -q          # must be green before anything runs
which nat2; readlink -f .venv/bin/python
```

## Done when
The suite passes on the VM and both interpreters resolve. No unit is enabled.
